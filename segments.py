"""Bounded movement-to-click segmentation, independent of the desktop API."""

from collections import deque

MAX_DURATION_NS = 8_000_000_000
TAIL_DURATION_NS = 1_500_000_000


class SegmentCollector:
    def __init__(self, save, max_duration_ns=MAX_DURATION_NS,
                 tail_duration_ns=TAIL_DURATION_NS):
        self.save = save
        self.max_duration_ns = max_duration_ns
        self.tail_duration_ns = tail_duration_ns
        self.events = deque()
        self.start_ns = None
        self.saved = 0
        self.discarded = 0

    def feed(self, event):
        boundary = event.kind == "down" and event.button == "left"
        if self.start_ns is None:
            if event.kind == "move":
                self.start_ns = event.timestamp_ns
                self.events.append(event)
            return
        self.events.append(event)
        if event.timestamp_ns - self.start_ns > self.max_duration_ns:
            cutoff = event.timestamp_ns - self.tail_duration_ns
            while self.events and self.events[0].timestamp_ns < cutoff:
                self.events.popleft()
        if boundary:
            # Cropping can expose a button event; a trajectory starts at movement.
            while self.events and self.events[0].kind != "move":
                self.events.popleft()
            if self.events:
                self.save(list(self.events))
                self.saved += 1
            else:
                self.discarded += 1
            self.start_ns = None
            self.events.clear()

    def finish(self):
        if self.start_ns is not None:
            self.discarded += 1
        self.events.clear()
        self.start_ns = None
