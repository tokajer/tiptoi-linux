"""Find, mount and unmount the tiptoi pen."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tiptoi_linux.i18n import _
from tiptoi_linux.pen._base import PEN_LABEL, PROTECTED_NAMES, Pen, PenError

PEN_FSTYPES = frozenset({"vfat", "msdos", "exfat", "fuseblk"})
PEN_BY_LABEL_PATH = Path("/dev/disk/by-label") / PEN_LABEL
PROC_MOUNTS = Path("/proc/self/mounts")


def _folder_pen(override: Path) -> Pen:
    if not override.is_dir():
        raise PenError(_("--pen override {path} is not a directory").format(path=override))
    for part in override.resolve().parts:
        if part.casefold() in PROTECTED_NAMES:
            # WHY: system/songs/stories belong to the pen's firmware - an override pointed at
            # one of them (proven: --pen <pen>/system) must never become the install root
            raise PenError(
                _("--pen override {path} points inside the protected folder {folder!r}").format(
                    path=override, folder=part
                )
            )
    return Pen(mountpoint=override, source=None)


def _run_findmnt() -> str:
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
    return result.stdout


def parse_findmnt(stdout: str) -> list[Pen]:
    """Every distinct tiptoi-labelled vfat/exfat mount in `findmnt --json` output."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PenError(_("could not parse findmnt output: {error}").format(error=exc)) from exc

    raw_filesystems = payload.get("filesystems", []) if isinstance(payload, dict) else []
    filesystems = raw_filesystems if isinstance(raw_filesystems, list) else []

    pens: dict[str, Pen] = {}
    for entry in filesystems:
        if not isinstance(entry, dict):
            continue
        target = entry.get("target")
        source = entry.get("source")
        label = entry.get("label")
        fstype = entry.get("fstype")
        # WHY: a row without a source device can't be unmounted or mount-checked, so it must never
        # be mistaken for a pen - a sourceless Pen would mean "user-picked folder" and skip those checks
        if not all(isinstance(value, str) and value for value in (target, source, label, fstype)):
            continue
        if label.lower() == PEN_LABEL and fstype.lower() in PEN_FSTYPES:
            # WHY: a bind mount of the same device surfaces as two findmnt rows but is one physical pen
            pens.setdefault(source, Pen(mountpoint=Path(target), source=source))
    return list(pens.values())


def find_pen(*, override: Path | None = None) -> Pen:
    if override is not None:
        return _folder_pen(override)

    pens = parse_findmnt(_run_findmnt())
    if not pens:
        raise PenError(
            _(
                "no tiptoi pen found (looked for a {label!r}-labelled vfat/exfat volume); "
                "mount it, or pass --pen <path> to point at it directly"
            ).format(label=PEN_LABEL)
        )
    if len(pens) > 1:
        mountpoints = ", ".join(str(pen.mountpoint) for pen in pens)
        raise PenError(
            _("multiple tiptoi pens found: {mountpoints}; use --pen to pick one").format(
                mountpoints=mountpoints
            )
        )
    return pens[0]


def device_path() -> Path | None:
    return PEN_BY_LABEL_PATH.resolve() if PEN_BY_LABEL_PATH.exists() else None


def device_plugged_in() -> bool:
    # WHY: udev creates this by-label symlink as soon as a volume labelled "tiptoi" is plugged in,
    # mounted or not - a cheap stat(), suitable for polling on the UI thread every couple seconds
    return PEN_BY_LABEL_PATH.exists()


def device_mounted() -> bool:
    # WHY: cheap, no subprocess (unlike find_pen's findmnt) - suitable for polling on the GUI thread
    # every couple seconds to check whether a plugged-in-but-unmounted pen has since been mounted
    device = device_path()
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


def pen_mount_alive(pen: Pen) -> bool:
    """Whether a previously found pen is still there: still a mount, or for a folder, still a directory."""
    if pen.is_folder:
        return pen.mountpoint.is_dir()
    return os.path.ismount(pen.mountpoint)


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

    device = device_path()
    if device is None:
        raise PenError(_("no tiptoi pen is plugged in"))

    _run_udisksctl(["mount", "--no-user-interaction", "-b", str(device)])
    return find_pen()


def unmount_pen(pen: Pen) -> None:
    if pen.source is None:
        raise PenError(_("this pen was chosen as a folder; unmount it in your file manager"))

    # WHY: belt and braces - unmount flushes too, but a sync first means a slow flush happens
    # before, not inside, udisks' own timeout
    os.sync()
    _run_udisksctl(["unmount", "--no-user-interaction", "-b", pen.source])
