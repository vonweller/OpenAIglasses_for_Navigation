"""Sample-clock pacing for bursty PCM producers."""

import time
from typing import Callable


class PlaybackClock:
    def __init__(self, bytes_per_second: int = 16000, ahead_seconds: float = 0.16,
                 clock: Callable[[], float] = time.monotonic):
        self.bytes_per_second = bytes_per_second
        self.ahead_seconds = max(0.0, ahead_seconds)
        self.clock = clock
        self.end_at = 0.0

    def reset(self) -> None:
        self.end_at = 0.0

    def reserve(self, byte_count: int) -> float:
        """Return the earliest dispatch time, retaining at most a short preroll."""
        now = self.clock()
        start = max(now, self.end_at)
        self.end_at = start + byte_count / self.bytes_per_second
        return max(now, start - self.ahead_seconds)
