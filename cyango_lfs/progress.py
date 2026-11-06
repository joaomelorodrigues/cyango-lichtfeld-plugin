"""Thread-safe upload progress."""

from __future__ import annotations

import threading
import time

from .models import ProgressSnapshot


class ProgressTracker:
    def __init__(self, total_bytes: int, committed: dict[int, int] | None = None) -> None:
        self.total_bytes = max(int(total_bytes), 0)
        self._committed = dict(committed or {})
        self._in_flight: dict[int, int] = {}
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._started_bytes = sum(self._committed.values())

    def add_bytes(self, part_number: int, count: int) -> ProgressSnapshot:
        with self._lock:
            self._in_flight[part_number] = self._in_flight.get(part_number, 0) + max(int(count), 0)
            return self._snapshot()

    def reset(self, part_number: int) -> ProgressSnapshot:
        with self._lock:
            self._in_flight.pop(part_number, None)
            return self._snapshot()

    def commit(self, part_number: int, size: int) -> ProgressSnapshot:
        with self._lock:
            self._in_flight.pop(part_number, None)
            self._committed[part_number] = max(int(size), 0)
            return self._snapshot()

    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> ProgressSnapshot:
        uploaded = min(sum(self._committed.values()) + sum(self._in_flight.values()), self.total_bytes)
        elapsed = max(time.monotonic() - self._started_at, 1e-6)
        rate = max(uploaded - self._started_bytes, 0) / elapsed
        eta = (self.total_bytes - uploaded) / rate if rate > 0 else None
        fraction = uploaded / self.total_bytes if self.total_bytes else 0.0
        return ProgressSnapshot(uploaded, self.total_bytes, max(0.0, min(fraction, 1.0)), rate, eta)


def format_bytes(count: int | float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(max(count, 0))
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    return f"{value:.0f} {units[unit]}" if unit == 0 else f"{value:.1f} {units[unit]}"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    total = max(int(seconds + 0.999), 0)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
