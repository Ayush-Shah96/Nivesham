from __future__ import annotations

from ii_platform.events.event_bus import RedisEventBus


class QueueManager:
    def __init__(self, event_bus: RedisEventBus):
        self.event_bus = event_bus
