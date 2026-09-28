"""Chunked copying with throttled progress, shared by download and pen."""

from __future__ import annotations

from collections.abc import Callable
from typing import BinaryIO

CHUNK_BYTES = 65536

# WHY: with an unknown total (a resumed download whose Content-Range total is "*"), emitting on
# every chunk would queue ~4800 signals for a 300 MB transfer; throttle those to roughly one per MiB
UNKNOWN_TOTAL_STEP_BYTES = 1024 * 1024

Progress = Callable[[int, int | None], None]


def percent(done: int, total: int) -> int:
    return min(100, int(done * 100 / total))


class ProgressThrottle:
    """Throttles a (done, total) progress callback to integer-percent changes, plus a final call.

    Shared by download.py's own streaming and by pen's copy/verify passes so both report progress
    the same way: ~101 callbacks for a 300 MB transfer instead of one per chunk.
    """

    def __init__(self, progress: Progress | None, total: int | None) -> None:
        self._progress = progress
        self._total = total
        self._last_percent = -1
        self._last_done = 0

    def update(self, done: int) -> None:
        if self._progress is None:
            return
        if self._total:
            current = percent(done, self._total)
            if current != self._last_percent:
                self._progress(done, self._total)
                self._last_percent = current
        elif done - self._last_done >= UNKNOWN_TOTAL_STEP_BYTES:
            self._progress(done, self._total)
            self._last_done = done

    def finish(self, done: int) -> None:
        if self._progress is None:
            return
        if self._total:
            if self._last_percent != 100:
                self._progress(done, self._total)
        elif done != self._last_done:
            self._progress(done, self._total)


def pump(
    source: BinaryIO,
    sink: Callable[[bytes], object],
    progress: Progress | None,
    total: int | None,
    start: int = 0,
) -> int:
    """Read `source` in CHUNK_BYTES chunks, pass each to `sink`, and throttle progress via total.

    Shared by download.py's streaming write and pen's copy/verify passes - the read-chunk,
    hand-to-sink, report-progress loop otherwise existed three times. Returns the final byte count.
    """
    throttle = ProgressThrottle(progress, total)
    written = start
    while chunk := source.read(CHUNK_BYTES):
        sink(chunk)
        written += len(chunk)
        throttle.update(written)
    throttle.finish(written)
    return written
