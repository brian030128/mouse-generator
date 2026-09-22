"""Bounded movement-to-click segmentation, independent of the desktop API."""


class SegmentCollector:
    def __init__(self, save, max_duration_ns=5_000_000_000):
        self.save = save
        self.max_duration_ns = max_duration_ns
        self.events = []
        self.start_ns = None
        self.saved = 0
        self.discarded = 0
        self.expired = False

    def feed(self, event):
        boundary = event.kind == "down" and event.button == "left"
        if self.start_ns is None:
            if event.kind == "move":
                self.start_ns = event.timestamp_ns
                self.events = [event]
                self.expired = False
            return
        if self.start_ns is not None:
            if event.timestamp_ns - self.start_ns > self.max_duration_ns:
                self.events.clear()
                self.expired = True
            if boundary:
                if self.expired:
                    self.discarded += 1
                else:
                    self.events.append(event)
                    self.save(self.events)
                    self.saved += 1
            elif not self.expired:
                self.events.append(event)
        if boundary:
            self.start_ns = None
            self.events = []
            self.expired = False

    def finish(self):
        if self.start_ns is not None:
            self.discarded += 1
        self.events.clear()
        self.start_ns = None
        self.expired = False
