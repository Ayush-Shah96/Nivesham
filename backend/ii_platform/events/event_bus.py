from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any


from infrastructure.config.settings import get_settings
from ii_platform.events.schemas import Event, EventType

logger = logging.getLogger(__name__)

EventHandler = Callable[[Event], Awaitable[None]]


class RedisEventBus:
    """Redis Streams event bus for the six application pipelines.

    Every event type has its own stream. Each application gets a Redis
    consumer group, so the same event can be consumed independently by
    different application pipelines. Successful processing is acknowledged
    with XACK; failed messages remain pending for retry/recovery.
    """

    def __init__(
        self,
        url: str | None = None,
        stream_prefix: str | None = None,
    ) -> None:
        settings = get_settings()
        self.url = url or settings.redis_url
        self.stream_prefix = stream_prefix or settings.redis_stream_prefix
        self.block_ms = settings.redis_consumer_block_ms
        self.batch_size = settings.redis_message_batch_size
        self.claim_idle_ms = settings.redis_claim_idle_ms
        self.connect_timeout = settings.redis_connect_timeout_seconds
        self.socket_timeout = settings.redis_socket_timeout_seconds

        self.redis = None
        self._tasks: list[asyncio.Task] = []
        self._closed = False

    @staticmethod
    def group_name(application: str) -> str:
        return f"investment:{application}:consumer"

    def stream_for(self, event_type: EventType | str) -> str:
        value = event_type.value if isinstance(event_type, EventType) else str(event_type)
        return f"{self.stream_prefix}:events:{value.lower()}"

    async def connect(self) -> None:
        if self.redis is not None and not self._closed:
            try:
                await self.redis.ping()
                return
            except Exception:
                # Never call close() here: connect() can be invoked from a
                # running consumer task, and close() cancels all consumer tasks
                # including the current one.
                await self._reset_client()

        self._closed = False
        try:
            from redis.asyncio import Redis
        except ImportError as exc:
            raise RuntimeError("redis package is required for the Redis Streams event bus") from exc

        # XREADGROUP blocks for block_ms. The socket timeout must be comfortably
        # larger than that value or redis-py will report false transport failures.
        effective_socket_timeout = max(
            float(self.socket_timeout),
            (self.block_ms / 1000.0) + 15.0,
        )
        client = Redis.from_url(
            self.url,
            decode_responses=True,
            socket_connect_timeout=self.connect_timeout,
            socket_timeout=effective_socket_timeout,
            health_check_interval=15,
            socket_keepalive=True,
            retry_on_timeout=True,
        )

        try:
            await client.ping()
        except Exception as exc:
            await client.aclose()
            raise RuntimeError(
                "Redis is enabled but unavailable. "
                f"Unable to connect to {self.url}. "
                "Start Redis on 127.0.0.1:6379 or configure REDIS_URL."
            ) from exc

        self.redis = client
        logger.info("Connected to Redis event bus: %s", self.url)

    async def _reset_client(self) -> None:
        """Close only the Redis client after a transport failure.

        This intentionally does not cancel consumer tasks; callers use it from
        inside a consumer loop and the next iteration reconnects cleanly.
        """
        client = self.redis
        self.redis = None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                logger.debug("Redis client close after transport failure failed", exc_info=True)

    async def close(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self.redis is not None:
            await self.redis.aclose()
        self.redis = None

    async def publish(self, event: Event) -> str:
        await self.connect()
        assert self.redis is not None

        stream = self.stream_for(event.event_type)
        values = {
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "payload": event.model_dump_json(),
        }
        message_id = await self.redis.xadd(
            stream,
            values,
            maxlen=100_000,
            approximate=True,
        )
        return str(message_id)

    async def _ensure_group(self, stream: str, group: str) -> None:
        assert self.redis is not None
        try:
            await self.redis.xgroup_create(
                name=stream,
                groupname=group,
                id="0-0",
                mkstream=True,
            )
        except Exception as exc:
            # redis-py exposes ResponseError; keep import lazy so structural startup can run without the optional client installed.
            from redis.exceptions import ResponseError
            if "BUSYGROUP" not in str(exc):
                raise

    async def _process_message(
        self,
        stream: str,
        group: str,
        message_id: str,
        values: dict[str, Any],
        handler: EventHandler,
    ) -> None:
        assert self.redis is not None
        try:
            raw_event = values.get("payload")
            if not raw_event:
                raise ValueError(f"Redis event {message_id} has no payload")

            event = Event.model_validate_json(raw_event)
            await handler(event)
            await self.redis.xack(stream, group, message_id)
        except Exception:
            logger.exception(
                "Redis event handler failed: stream=%s group=%s message_id=%s",
                stream,
                group,
                message_id,
            )
            # Intentionally do not XACK. The message remains in the pending
            # entries list and can be reclaimed by another worker.

    async def _claim_stale(self, stream: str, group: str, consumer: str) -> None:
        assert self.redis is not None
        try:
            pending = await self.redis.xpending_range(
                stream,
                group,
                min="-",
                max="+",
                count=self.batch_size,
            )
            stale_ids = [
                item["message_id"]
                for item in pending
                if int(item.get("time_since_delivered", 0)) >= self.claim_idle_ms
                and item.get("consumer_name") != consumer
            ]
            if not stale_ids:
                return

            await self.redis.xclaim(
                stream,
                group,
                consumer,
                min_idle_time=self.claim_idle_ms,
                message_ids=stale_ids,
            )
            logger.info(
                "Redis reclaimed %s stale event(s): stream=%s group=%s",
                len(stale_ids),
                stream,
                group,
            )
        except Exception:
            logger.debug(
                "Redis stale-message recovery failed: stream=%s group=%s",
                stream,
                group,
                exc_info=True,
            )

    async def subscribe(
        self,
        event_type: EventType,
        application: str,
        handler: EventHandler,
    ) -> asyncio.Task:
        await self.connect()
        assert self.redis is not None

        stream = self.stream_for(event_type)
        group = self.group_name(application)
        consumer = f"{application}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

        await self._ensure_group(stream, group)

        async def consume() -> None:
            while not self._closed:
                try:
                    await self.connect()
                    assert self.redis is not None
                    await self._claim_stale(stream, group, consumer)
                    responses = await self.redis.xreadgroup(
                        groupname=group,
                        consumername=consumer,
                        streams={stream: ">"},
                        count=self.batch_size,
                        block=self.block_ms,
                    )
                    if not responses:
                        continue

                    for _, messages in responses:
                        for message_id, values in messages:
                            await self._process_message(
                                stream,
                                group,
                                message_id,
                                values,
                                handler,
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError

                    if isinstance(exc, (RedisTimeoutError, RedisConnectionError, asyncio.TimeoutError, TimeoutError, OSError)):
                        logger.warning(
                            "Redis transport interruption; reconnecting: stream=%s group=%s error=%s",
                            stream,
                            group,
                            exc,
                        )
                        await self._reset_client()
                        await asyncio.sleep(1.0)
                        continue

                    logger.exception(
                        "Redis consumer loop failed: stream=%s group=%s",
                        stream,
                        group,
                    )
                    await asyncio.sleep(2)

        task = asyncio.create_task(
            consume(),
            name=f"redis-consumer:{application}:{event_type.value}",
        )
        self._tasks.append(task)
        logger.info(
            "Redis consumer started: stream=%s group=%s consumer=%s",
            stream,
            group,
            consumer,
        )
        return task


EventBus = RedisEventBus
