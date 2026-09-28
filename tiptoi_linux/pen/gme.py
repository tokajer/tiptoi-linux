"""Read the version date out of a .gme file's header."""

from __future__ import annotations

import re
from pathlib import Path

from tiptoi_linux.i18n import _
from tiptoi_linux.pen._base import PenError

GME_HEADER_OFFSET = 0x20
GME_HEADER_READ_BYTES = 0x100

# the yyyymmdd date that ends the header text, e.g. "...Wieso Weshalb Warum 20231015" -> "20231015"
_TRAILING_DATE_RE = re.compile(r"(\d{8})$")


def read_gme_version(path: Path) -> str | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read(GME_HEADER_READ_BYTES)
    except OSError as exc:
        raise PenError(_("cannot read {path}: {error}").format(path=path, error=exc)) from exc

    # WHY: verified against 32/32 catalog titles on a real pen on 2026-09-28 - every .gme's header
    # starts at this offset and its NUL-terminated ASCII text ends in the installed yyyymmdd date
    text_bytes = data[GME_HEADER_OFFSET:].split(b"\0", 1)[0]
    try:
        text = text_bytes.decode("ascii")
    except UnicodeDecodeError:
        return None

    match = _TRAILING_DATE_RE.search(text)
    return match.group(1) if match else None
