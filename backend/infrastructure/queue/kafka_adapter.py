class KafkaAdapter:
    """Reserved adapter slot; Redis Streams is the active event bus."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "Kafka is not configured. Redis Streams is the active event bus."
        )
