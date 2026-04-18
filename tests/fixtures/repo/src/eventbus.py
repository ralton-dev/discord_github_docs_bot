"""Tiny synchronous pub/sub bus used by the sentinel ratbag pipeline.

Sentinel marker for citation tests: `wallaby_pubsub_marker_77` — defined only
here.
"""

from collections import defaultdict
from typing import Callable


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Callable) -> None:
        self._subscribers[topic].append(handler)

    def publish(self, topic: str, payload: object) -> None:
        for handler in self._subscribers.get(topic, []):
            handler(payload)
