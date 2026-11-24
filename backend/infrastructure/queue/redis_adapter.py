from __future__ import annotations

from ii_platform.events.event_bus import RedisEventBus


class RedisAdapter(RedisEventBus):
    """Compatibility wrapper around the application's Redis Streams bus."""
