"""Durably copy a .gme file onto the pen and verify it by reading it back from the device."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from tiptoi_linux.catalog import GME_SUFFIX, Product, gme_file_name, is_safe_file_name
from tiptoi_linux.download import download_product
from tiptoi_linux.i18n import _
from tiptoi_linux.pen._base import PROTECTED_NAMES, Pen, PenError, disk_space, fsync_directory, require_mounted
from tiptoi_linux.streams import Progress, pump


class Phase(StrEnum):
    DOWNLOAD = "download"
    COPY = "copy"
    VERIFY = "verify"


PhaseProgress = Callable[[Phase, int, int | None], None]


def _phased(progress: PhaseProgress | None, phase: Phase) -> Progress | None:
    if progress is None:
        return None
    return lambda done, total: progress(phase, done, total)


def validate_title_name(file_name: str) -> None:
    if not is_safe_file_name(file_name):
        raise PenError(_("refusing unsafe file name: {name!r}").format(name=file_name))
    if not file_name.lower().endswith(GME_SUFFIX):
        raise PenError(
            _("refusing to use non-{suffix} file name: {name!r}").format(suffix=GME_SUFFIX, name=file_name)
        )

    stem = file_name[: -len(GME_SUFFIX)]
    if stem.lower() in PROTECTED_NAMES:
        # WHY: system/songs/stories belong to the pen's firmware and must never be overwritten or
        # deleted by a title operation
        raise PenError(_("refusing to use protected file name: {name!r}").format(name=file_name))


def _source_size(source: Path) -> int:
    try:
        return source.stat().st_size
    except OSError as exc:
        raise PenError(_("cannot stat source file {path}: {error}").format(path=source, error=exc)) from exc


def _precheck_free_space(pen: Pen, required: int) -> None:
    try:
        cluster = os.statvfs(pen.mountpoint).f_frsize
    except OSError as exc:
        raise PenError(
            _("cannot read free space on pen at {path}: {error}").format(path=pen.mountpoint, error=exc)
        ) from exc

    # WHY: a temp file is written alongside before the rename, so peak usage is one full copy even on
    # overwrite; round up to whole clusters since the filesystem allocates in cluster-sized units
    if cluster > 0:
        clusters_needed = -(-required // cluster)
        required_rounded = clusters_needed * cluster
    else:
        # SHORTCUT: an f_frsize of 0 would only happen on a broken/virtual filesystem; fall back to
        # the unrounded size rather than risk a spurious "enough space" pass - revisit if ever hit
        required_rounded = required

    available, _total = disk_space(pen)
    if available < required_rounded:
        raise PenError(
            _("not enough free space on pen: need {needed} bytes, have {available} bytes").format(
                needed=required_rounded, available=available
            )
        )


def _copy_with_digest(source: Path, handle: BinaryIO, required: int, progress: Progress | None) -> str:
    digest = hashlib.sha256()

    def sink(chunk: bytes) -> None:
        digest.update(chunk)
        handle.write(chunk)

    with open(source, "rb") as src:
        pump(src, sink, progress, required)
    return digest.hexdigest()


def _write_temp_copy(source: Path, destination: Path, required: int, progress: Progress | None) -> str:
    """Copy source to a temp file beside destination, fsync it, rename it into place; return its digest."""
    name = destination.name
    try:
        fd, tmp_path = tempfile.mkstemp(dir=destination.parent, prefix=".tiptoi-", suffix=".tmp")
    except OSError as exc:
        raise PenError(
            _("cannot create temp file for {name} in {path}: {error}").format(
                name=name, path=destination.parent, error=exc
            )
        ) from exc

    try:
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            source_digest = _copy_with_digest(source, handle, required, progress)
            handle.flush()
            os.fsync(handle.fileno())
            written = os.fstat(handle.fileno()).st_size
            if written != required:
                raise PenError(
                    _("copy of {name} to the pen is incomplete: wrote {written} of {required} bytes").format(
                        name=name, written=written, required=required
                    )
                )
        os.replace(tmp_path, destination)
    except OSError as exc:
        Path(tmp_path).unlink(missing_ok=True)
        raise PenError(_("cannot install {name} onto pen: {error}").format(name=name, error=exc)) from exc
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    return source_digest


def _fsync_renamed_file(destination: Path) -> None:
    # WHY: on vfat, os.replace() moves the file's directory entry (size + start cluster) into a new
    # slot but only marks the *file* inode dirty, not that entry - it is written back lazily. fsyncing
    # the destination by its new path forces the rename itself out now (proven: a real install
    # reported success, then a disconnect mid-write-back left a 0-byte title on the pen).
    name = destination.name
    try:
        dest_fd = os.open(destination, os.O_RDONLY)
    except OSError as exc:
        raise PenError(
            _("cannot open {name} on the pen to fsync after install: {error}").format(name=name, error=exc)
        ) from exc
    try:
        os.fsync(dest_fd)
    except OSError as exc:
        raise PenError(
            _("cannot fsync {name} on the pen after install: {error}").format(name=name, error=exc)
        ) from exc
    finally:
        os.close(dest_fd)


def _read_back_digest(
    destination: Path,
    *,
    expected_total: int,
    progress: Progress | None,
) -> tuple[int, str]:
    # WHY: reads the destination from the device rather than the page cache (see the fadvise call
    # below) so a "verified" result can't be a page-cache echo of data that never made it to the pen
    fd = os.open(destination, os.O_RDONLY)
    try:
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            # SHORTCUT: fadvise is best-effort; if it fails, the read-back below may still be served
            # from the page cache rather than the device, but that's still worth doing
            pass
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb") as handle:
            fd = -1  # WHY: fdopen() now owns the fd; closing handle below closes it, not the finally
            written = pump(handle, digest.update, progress, expected_total)
        return written, digest.hexdigest()
    finally:
        if fd != -1:
            os.close(fd)


def _verify(destination: Path, required: int, source_digest: str, progress: Progress | None) -> None:
    name = destination.name
    try:
        read_back_count, read_back_digest = _read_back_digest(
            destination, expected_total=required, progress=progress
        )
    except OSError as exc:
        # WHY: the likeliest cause is the pen being unplugged mid-verify - report it like any other
        # pen failure instead of letting a raw OSError reach the CLI as a traceback
        raise PenError(
            _("cannot verify {name} on the pen: {error}").format(name=name, error=exc)
        ) from exc
    if read_back_count != required or read_back_digest != source_digest:
        # WHY: os.replace() already committed the new file under the old title's name, so a mismatch
        # here can't restore the previous title - it can only stop a silently-corrupt install from
        # being reported as a success. Never delete destination: the message tells the user to reinstall.
        raise PenError(
            _("verification failed for {name} on the pen: the data read back does not match").format(name=name)
        )


def install_file(
    pen: Pen,
    source: Path,
    *,
    file_name: str,
    dry_run: bool = False,
    progress: PhaseProgress | None = None,
) -> Path:
    require_mounted(pen)
    validate_title_name(file_name)

    destination = pen.mountpoint / file_name
    required = _source_size(source)
    _precheck_free_space(pen, required)
    if dry_run:
        return destination

    source_digest = _write_temp_copy(source, destination, required, _phased(progress, Phase.COPY))
    _fsync_renamed_file(destination)
    # WHY: this is a removable device a user will physically unplug - fsync the directory entry too,
    # not just the file itself, so the rename is durable before we report success
    fsync_directory(pen.mountpoint)
    _verify(destination, required, source_digest, _phased(progress, Phase.VERIFY))
    return destination


def install_title(
    pen: Pen,
    product: Product,
    *,
    dry_run: bool = False,
    progress: PhaseProgress | None = None,
) -> Path:
    source = download_product(product, progress=_phased(progress, Phase.DOWNLOAD))
    return install_file(pen, source, file_name=gme_file_name(product), dry_run=dry_run, progress=progress)
