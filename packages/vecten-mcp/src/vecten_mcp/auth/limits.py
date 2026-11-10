import time
from collections import OrderedDict
from threading import Lock


class RateLimit(ValueError):
    pass


class RateLimiter:
    def __init__(self, limit=5, window=300, *, clock=time.time, capacity=10000):
        self.limit, self.window, self.clock, self.capacity = limit, window, clock, capacity
        self.entries = OrderedDict()
        self.lock = Lock()

    def hit(self, key):
        with self.lock:
            now = self.clock()
            while self.entries and next(iter(self.entries.values()))[0] + self.window <= now:
                self.entries.popitem(last=False)
            started, count = self.entries.get(key, (now, 0))
            if started + self.window <= now:
                started, count = now, 0
            if count >= self.limit or (
                key not in self.entries and len(self.entries) >= self.capacity
            ):
                raise RateLimit("rate limit exceeded; retry later")
            self.entries[key] = (started, count + 1)

    def clear(self, key):
        with self.lock:
            self.entries.pop(key, None)
