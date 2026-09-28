"""Locate a tiptoi pen, install .gme files onto it, and report outdated titles."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, NamedTuple

from tiptoi_linux.catalog import GME_SUFFIX, Catalog, Product
from tiptoi_linux.common import is_safe_file_name, pump
from tiptoi_linux.download import download_product, gme_file_name
from tiptoi_linux.errors import TiptoiError
from tiptoi_linux.i18n import _

PEN_LABEL = "tiptoi"
PEN_FSTYPES = frozenset({"vfat", "msdos", "exfat", "fuseblk"})
PROTECTED_NAMES = frozenset({"system", "songs", "stories"})
GME_HEADER_OFFSET = 0x20
GME_HEADER_READ_BYTES = 0x100
PEN_BY_LABEL_PATH = Path("/dev/disk/by-label") / PEN_LABEL
PROC_MOUNTS = Path("/proc/self/mounts")

PHASE_DOWNLOAD = "download"
PHASE_COPY = "copy"
PHASE_VERIFY = "verify"
PhaseProgress = Callable[[str, int, int | None], None]


class PenError(TiptoiError):
    """Raised for pen-detection or install failures."""


@dataclass(frozen=True)
class Pen:
    mountpoint: Path
    source: str


@dataclass(frozen=True)
class OutdatedTitle:
    file_name: str
    installed_version: str | None
    catalog_version: str

    def describe(self) -> str:
        installed = self.installed_version if self.installed_version is not None else _("(unknown)")
        return _("{name}: installed {installed}, catalog {catalog}").format(
            name=self.file_name, installed=installed, catalog=self.catalog_version
        )


def find_pen(*, override: Path | None = None) -> Pen:
    if override is not None:
        if not override.is_dir():
            raise PenError(_("--pen override {path} is not a directory").format(path=override))
        resolved = override.resolve()
        for part in resolved.parts:
            if part.casefold() in PROTECTED_NAMES:
                # WHY: system/songs/stories belong to the pen's firmware - an override pointed at
                # one of them (proven: --pen <pen>/system) must never become the install root
                raise PenError(
                    _("--pen override {path} points inside the protected folder {folder!r}").format(
                        path=override, folder=part
                    )
                )
        return Pen(mountpoint=override, source="")

    try:
        result = subprocess.run(
            ["findmnt", "--json", "--list", "-o", "TARGET,SOURCE,FSTYPE,LABEL"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise PenError(_("findmnt not found; install util-linux or pass --pen <path>")) from exc
    except subprocess.TimeoutExpired as exc:
        raise PenError(_("findmnt timed out while looking for the tiptoi pen")) from exc

    if result.returncode != 0:
        raise PenError(
            _("findmnt failed (exit {code}): {error}").format(
                code=result.returncode, error=result.stderr.strip()
            )
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PenError(_("could not parse findmnt output: {error}").format(error=exc)) from exc

    raw_filesystems = payload.get("filesystems", []) if isinstance(payload, dict) else []
    filesystems = raw_filesystems if isinstance(raw_filesystems, list) else []

    candidates: dict[str, dict] = {}
    for entry in filesystems:
        if not isinstance(entry, dict):
            continue
        target = entry.get("target")
        if not isinstance(target, str):
            continue
        label = entry.get("label")
        fstype = entry.get("fstype")
        if not isinstance(label, str) or not isinstance(fstype, str):
            continue
        if label.lower() == PEN_LABEL and fstype.lower() in PEN_FSTYPES:
            source = entry.get("source") or ""
            # WHY: a bind mount of the same device surfaces as two findmnt rows but is one physical pen
            candidates.setdefault(source, entry)

    if not candidates:
        raise PenError(
            _(
                "no tiptoi pen found (looked for a {label!r}-labelled vfat/exfat volume); "
                "mount it, or pass --pen <path> to point at it directly"
            ).format(label=PEN_LABEL)
        )
    if len(candidates) > 1:
        mountpoints = ", ".join(str(entry.get("target")) for entry in candidates.values())
        raise PenError(
            _("multiple tiptoi pens found: {mountpoints}; use --pen to pick one").format(
                mountpoints=mountpoints
            )
        )

    entry = next(iter(candidates.values()))
    return Pen(mountpoint=Path(entry["target"]), source=entry.get("source") or "")


def disk_space(pen: Pen) -> tuple[int, int]:
    usage = shutil.disk_usage(pen.mountpoint)
    return usage.free, usage.total


def pen_device_present() -> bool:
    # WHY: udev creates this by-label symlink as soon as a volume labelled "tiptoi" is plugged in,
    # mounted or not - a cheap stat(), suitable for polling on the UI thread every couple seconds
    return PEN_BY_LABEL_PATH.exists()


def pen_device() -> Path | None:
    return PEN_BY_LABEL_PATH.resolve() if PEN_BY_LABEL_PATH.exists() else None


def pen_device_mounted() -> bool:
    # WHY: cheap, no subprocess (unlike find_pen's findmnt) - suitable for polling on the GUI thread
    # every couple seconds to check whether a plugged-in-but-unmounted pen has since been mounted
    device = pen_device()
    if device is None:
        return False
    try:
        text = PROC_MOUNTS.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        try:
            mounted_device = Path(fields[0]).resolve()
        except OSError:
            continue
        if mounted_device == device:
            return True
    return False


def _run_udisksctl(args: list[str]) -> None:
    try:
        result = subprocess.run(
            ["udisksctl", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise PenError(
            _("udisksctl not found; install udisks2 or (un)mount the pen in your file manager")
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PenError(_("udisksctl timed out")) from exc

    if result.returncode != 0:
        raise PenError(_("udisksctl failed: {error}").format(error=result.stderr.strip()))


def mount_pen() -> Pen:
    try:
        return find_pen()
    except PenError:
        pass

    device = pen_device()
    if device is None:
        raise PenError(_("no tiptoi pen is plugged in"))

    _run_udisksctl(["mount", "--no-user-interaction", "-b", str(device)])
    return find_pen()


def unmount_pen(pen: Pen) -> None:
    if pen.source == "":
        raise PenError(_("this pen was chosen as a folder; unmount it in your file manager"))

    # WHY: belt and braces - unmount flushes too, but a sync first means a slow flush happens
    # before, not inside, udisks' own timeout
    os.sync()
    _run_udisksctl(["unmount", "--no-user-interaction", "-b", pen.source])


def pen_still_mounted(pen: Pen) -> bool:
    if pen.source:
        return os.path.ismount(pen.mountpoint)
    return pen.mountpoint.is_dir()


def installed_gme_files(pen: Pen) -> tuple[str, ...]:
    try:
        names = os.listdir(pen.mountpoint)
    except OSError as exc:
        raise PenError(_("cannot read pen mountpoint {path}: {error}").format(path=pen.mountpoint, error=exc)) from exc

    gme_names = [
        name
        for name in names
        if name.lower().endswith(GME_SUFFIX) and (pen.mountpoint / name).is_file()
    ]
    return tuple(sorted(gme_names, key=str.lower))


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

    match = re.search(r"(\d{8})$", text)
    return match.group(1) if match else None


def _fsync_directory(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise PenError(_("cannot fsync pen directory {path}: {error}").format(path=directory, error=exc)) from exc
        # WHY: some FUSE/exfat mounts reject directory fsync outright; the file fsync already made
        # the data durable, so this is a soft failure to tolerate rather than a real one
    finally:
        os.close(dir_fd)


def _read_back_digest(
    destination: Path,
    *,
    expected_total: int,
    progress: Callable[[int, int | None], None] | None,
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


_NAME_CHECK_ANCHOR = Path("/pen-root")


def _require_mounted(pen: Pen) -> None:
    if pen.source and not os.path.ismount(pen.mountpoint):
        # WHY: a pen found via findmnt detection can be unplugged between detection and the
        # operation, leaving an ordinary (non-mount) directory at the old mountpoint - writing or
        # deleting there would land on the root filesystem instead of failing loudly. An override
        # (pen.source == "") is exempt.
        raise PenError(_("pen mountpoint {path} is no longer mounted").format(path=pen.mountpoint))


def _validate_title_name(file_name: str) -> None:
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

    # WHY: joined onto an arbitrary anchor rather than the real pen.mountpoint, since this check is
    # only about file_name itself (no path separators smuggled in) and is shared by callers before
    # they have settled on a destination
    if (_NAME_CHECK_ANCHOR / file_name).parent != _NAME_CHECK_ANCHOR:
        raise PenError(_("refusing to use file name outside the pen root: {name!r}").format(name=file_name))


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


def _copy_with_digest(
    source: Path,
    handle: BinaryIO,
    required: int,
    progress: Callable[[int, int | None], None] | None,
) -> str:
    digest = hashlib.sha256()

    def sink(chunk: bytes) -> None:
        digest.update(chunk)
        handle.write(chunk)

    with open(source, "rb") as src:
        pump(src, sink, progress, required)
    return digest.hexdigest()


def install_product(
    pen: Pen,
    source: Path,
    *,
    file_name: str,
    dry_run: bool = False,
    progress: PhaseProgress | None = None,
) -> Path:
    _require_mounted(pen)
    _validate_title_name(file_name)

    destination = pen.mountpoint / file_name

    try:
        required = source.stat().st_size
    except OSError as exc:
        raise PenError(_("cannot stat source file {path}: {error}").format(path=source, error=exc)) from exc

    _precheck_free_space(pen, required)

    if dry_run:
        return destination

    try:
        fd, tmp_path = tempfile.mkstemp(dir=pen.mountpoint, prefix=".tiptoi-", suffix=".tmp")
    except OSError as exc:
        raise PenError(
            _("cannot create temp file for {name} in {path}: {error}").format(
                name=file_name, path=pen.mountpoint, error=exc
            )
        ) from exc

    copy_progress = (lambda done, total: progress(PHASE_COPY, done, total)) if progress else None

    try:
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            source_digest = _copy_with_digest(source, handle, required, copy_progress)
            handle.flush()
            os.fsync(handle.fileno())
            written = os.fstat(handle.fileno()).st_size
            if written != required:
                raise PenError(
                    _("copy of {name} to the pen is incomplete: wrote {written} of {required} bytes").format(
                        name=file_name, written=written, required=required
                    )
                )
        os.replace(tmp_path, destination)
    except OSError as exc:
        Path(tmp_path).unlink(missing_ok=True)
        raise PenError(_("cannot install {name} onto pen: {error}").format(name=file_name, error=exc)) from exc
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise

    # WHY: on vfat, os.replace() moves the file's directory entry (size + start cluster) into a new
    # slot but only marks the *file* inode dirty, not that entry - it is written back lazily. fsyncing
    # the destination by its new path forces the rename itself out now (proven: a real install
    # reported success, then a disconnect mid-write-back left a 0-byte title on the pen).
    try:
        dest_fd = os.open(destination, os.O_RDONLY)
    except OSError as exc:
        raise PenError(
            _("cannot open {name} on the pen to fsync after install: {error}").format(name=file_name, error=exc)
        ) from exc
    try:
        os.fsync(dest_fd)
    except OSError as exc:
        raise PenError(
            _("cannot fsync {name} on the pen after install: {error}").format(name=file_name, error=exc)
        ) from exc
    finally:
        os.close(dest_fd)

    # WHY: this is a removable device a user will physically unplug - fsync the directory entry too,
    # not just the file itself, so the rename is durable before we report success
    _fsync_directory(pen.mountpoint)

    verify_progress = (lambda done, total: progress(PHASE_VERIFY, done, total)) if progress else None
    read_back_count, read_back_digest = _read_back_digest(
        destination, expected_total=required, progress=verify_progress
    )
    if read_back_count != required or read_back_digest != source_digest:
        # WHY: os.replace() already committed the new file under the old title's name, so a mismatch
        # here can't restore the previous title - it can only stop a silently-corrupt install from
        # being reported as a success. Never delete destination: the message tells the user to reinstall.
        raise PenError(
            _("verification failed for {name} on the pen: the data read back does not match").format(
                name=file_name
            )
        )

    return destination


def install_title(
    pen: Pen,
    product: Product,
    *,
    dry_run: bool = False,
    progress: PhaseProgress | None = None,
) -> Path:
    download_progress = (lambda done, total: progress(PHASE_DOWNLOAD, done, total)) if progress else None
    source = download_product(product, progress=download_progress)
    return install_product(pen, source, file_name=gme_file_name(product), dry_run=dry_run, progress=progress)


def _sum_file_sizes(directory: Path) -> int:
    # WHY: best-effort accounting for empty_trash()'s return value, not a safety check - a file that
    # vanishes or errors mid-walk (e.g. another process racing us) is simply not counted
    total = 0
    for root, _dirs, files in os.walk(directory, followlinks=False):
        for name in files:
            try:
                child_stat = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if stat.S_ISREG(child_stat.st_mode):
                total += child_stat.st_size
    return total


def empty_trash(pen: Pen) -> int:
    resolved_mountpoint = pen.mountpoint.resolve()
    uid = os.getuid()
    # WHY: exactly the two freedesktop trash locations for this uid - own uid only, never another
    # user's trash, the .Trash parent directory itself, or anything else on the pen
    candidates = (
        (pen.mountpoint / f".Trash-{uid}", resolved_mountpoint),
        (pen.mountpoint / ".Trash" / str(uid), resolved_mountpoint / ".Trash"),
    )

    freed = 0
    for candidate, expected_parent in candidates:
        try:
            candidate_stat = candidate.lstat()
        except OSError:
            continue  # absent, or a component along the way isn't a directory - skip it

        if stat.S_ISLNK(candidate_stat.st_mode) or not stat.S_ISDIR(candidate_stat.st_mode):
            continue  # never follow a symlink, and this isn't a directory to descend into

        # WHY: catches a crafted intermediate symlink (e.g. .Trash itself pointing outside the pen)
        # that lstat() on the final path component alone would not catch
        if candidate.resolve().parent != expected_parent:
            continue

        try:
            for child in candidate.iterdir():
                child_stat = child.lstat()
                if stat.S_ISLNK(child_stat.st_mode):
                    child.unlink()
                elif stat.S_ISDIR(child_stat.st_mode):
                    freed += _sum_file_sizes(child)
                    shutil.rmtree(child)
                elif stat.S_ISREG(child_stat.st_mode):
                    freed += child_stat.st_size
                    child.unlink()
                else:
                    child.unlink()
        except OSError as exc:
            raise PenError(
                _("cannot empty pen trash directory {path}: {error}").format(path=candidate, error=exc)
            ) from exc

    return freed


def delete_title(pen: Pen, file_name: str) -> int:
    _validate_title_name(file_name)
    _require_mounted(pen)

    target = pen.mountpoint / file_name
    if target.is_symlink() or not target.is_file():
        raise PenError(_("{name} is not a title on the pen").format(name=file_name))

    freed = target.stat().st_size
    try:
        os.unlink(target)
    except OSError as exc:
        raise PenError(_("cannot delete {name} from the pen: {error}").format(name=file_name, error=exc)) from exc

    freed += empty_trash(pen)
    _fsync_directory(pen.mountpoint)
    return freed


def outdated_titles(
    pen: Pen, catalog: Catalog, *, installed: tuple[str, ...] | None = None
) -> list[OutdatedTitle]:
    by_gme_name = {f"{p.name}{GME_SUFFIX}".casefold(): p for p in catalog.products}

    outdated: list[OutdatedTitle] = []
    for file_name in installed if installed is not None else installed_gme_files(pen):
        product = by_gme_name.get(file_name.casefold())
        if product is None:
            continue
        installed_version = read_gme_version(pen.mountpoint / file_name)
        if installed_version != product.version:
            outdated.append(
                OutdatedTitle(
                    file_name=file_name,
                    installed_version=installed_version,
                    catalog_version=product.version,
                )
            )

    return sorted(outdated, key=lambda title: title.file_name.lower())


class PenSummary(NamedTuple):
    pen: Pen
    free: int
    total: int
    installed: tuple[str, ...]
    outdated: list[OutdatedTitle]


def pen_summary(pen: Pen, catalog: Catalog | None) -> PenSummary:
    installed = installed_gme_files(pen)
    outdated = outdated_titles(pen, catalog, installed=installed) if catalog is not None else []
    free, total = disk_space(pen)
    return PenSummary(pen=pen, free=free, total=total, installed=installed, outdated=outdated)
