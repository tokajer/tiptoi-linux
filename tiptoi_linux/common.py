"""Primitives shared by catalog, download and pen."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from tiptoi_linux.i18n import _

CHUNK_BYTES = 65536

# WHY: with an unknown total (a resumed download whose Content-Range total is "*"), emitting on
# every chunk would queue ~4800 signals for a 300 MB transfer; throttle those to roughly one per MiB
UNKNOWN_TOTAL_STEP_BYTES = 1024 * 1024


def is_safe_file_name(name: str) -> bool:
    if name in ("", ".", ".."):
        return False
    if "/" in name or "\\" in name or "\0" in name:
        return False
    return name == Path(name).name


class ProgressThrottle:
    """Throttles a (done, total) progress callback to integer-percent changes, plus a final call.

    Shared by download.py's own streaming and by pen.py's copy/verify passes so both report progress
    the same way: ~101 callbacks for a 300 MB transfer instead of one per chunk.
    """

    def __init__(self, progress: Callable[[int, int | None], None] | None, total: int | None) -> None:
        self._progress = progress
        self._total = total
        self._last_percent = -1
        self._last_done = 0

    def update(self, done: int) -> None:
        if self._progress is None:
            return
        if self._total:
            percent = min(100, int(done * 100 / self._total))
            if percent != self._last_percent:
                self._progress(done, self._total)
                self._last_percent = percent
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


class HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urllib.parse.urlparse(newurl).scheme != "https":
            # WHY: urlopen follows 301/302 to plain http by default, silently defeating the scheme check below
            raise urllib.error.URLError(
                _("refusing to follow redirect to non-https URL: {url}").format(url=newurl)
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


https_opener = urllib.request.build_opener(HTTPSOnlyRedirectHandler)


def pump(
    source: BinaryIO,
    sink: Callable[[bytes], object],
    progress: Callable[[int, int | None], None] | None,
    total: int | None,
    start: int = 0,
) -> int:
    """Read `source` in CHUNK_BYTES chunks, pass each to `sink`, and throttle progress via total.

    Shared by download.py's streaming write and pen.py's copy/verify passes - the read-chunk,
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
