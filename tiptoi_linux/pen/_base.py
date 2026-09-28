"""The Pen value, its error type, and filesystem guards shared by every pen operation."""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from tiptoi_linux.errors import TiptoiError
from tiptoi_linux.i18n import _

PEN_LABEL = "tiptoi"
PROTECTED_NAMES = frozenset({"system", "songs", "stories"})


class PenError(TiptoiError):
    """Raised for pen-detection or install failures."""


@dataclass(frozen=True)
class Pen:
    mountpoint: Path
    # WHY: the block device a detected pen is mounted from; None only for a folder the user picked
    # with --pen / Choose folder, which can't be (un)mounted or checked with ismount()
    source: str | None

    @property
    def is_folder(self) -> bool:
        return self.source is None


def disk_space(pen: Pen) -> tuple[int, int]:
    try:
        usage = shutil.disk_usage(pen.mountpoint)
    except OSError as exc:
        raise PenError(
            _("cannot read free space on pen at {path}: {error}").format(path=pen.mountpoint, error=exc)
        ) from exc
    return usage.free, usage.total


def require_mounted(pen: Pen) -> None:
    if not pen.is_folder and not os.path.ismount(pen.mountpoint):
        # WHY: a pen found via findmnt detection can be unplugged between detection and the
        # operation, leaving an ordinary (non-mount) directory at the old mountpoint - writing or
        # deleting there would land on the root filesystem instead of failing loudly. A folder the
        # user picked is exempt.
        raise PenError(_("pen mountpoint {path} is no longer mounted").format(path=pen.mountpoint))


def fsync_directory(directory: Path) -> None:
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise PenError(_("cannot fsync pen directory {path}: {error}").format(path=directory, error=exc)) from exc
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise PenError(_("cannot fsync pen directory {path}: {error}").format(path=directory, error=exc)) from exc
        # WHY: some FUSE/exfat mounts reject directory fsync outright; the file fsync already made
        # the data durable, so this is a soft failure to tolerate rather than a real one
    finally:
        os.close(dir_fd)
