"""The titles on a pen: list, delete, empty trash, and compare against the catalog."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from tiptoi_linux.catalog import GME_SUFFIX, Catalog, CatalogError, Product, gme_file_name
from tiptoi_linux.i18n import _
from tiptoi_linux.pen._base import Pen, PenError, disk_space, fsync_directory, require_mounted
from tiptoi_linux.pen.gme import read_gme_version
from tiptoi_linux.pen.install import validate_title_name


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


def _trash_dirs(pen: Pen) -> list[Path]:
    """This uid's freedesktop trash directories on the pen that are real, in-place directories."""
    resolved_mountpoint = pen.mountpoint.resolve()
    uid = os.getuid()
    # WHY: exactly the two freedesktop trash locations for this uid - own uid only, never another
    # user's trash, the .Trash parent directory itself, or anything else on the pen
    candidates = (
        (pen.mountpoint / f".Trash-{uid}", resolved_mountpoint),
        (pen.mountpoint / ".Trash" / str(uid), resolved_mountpoint / ".Trash"),
    )

    found = []
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
        found.append(candidate)
    return found


def _remove_child(child: Path) -> int:
    """Remove one trash entry without following symlinks; return the bytes of regular files freed."""
    child_stat = child.lstat()
    if stat.S_ISDIR(child_stat.st_mode):
        freed = _sum_file_sizes(child)
        shutil.rmtree(child)
        return freed
    child.unlink()
    return child_stat.st_size if stat.S_ISREG(child_stat.st_mode) else 0


def empty_trash(pen: Pen) -> int:
    freed = 0
    for trash_dir in _trash_dirs(pen):
        try:
            for child in trash_dir.iterdir():
                freed += _remove_child(child)
        except OSError as exc:
            raise PenError(
                _("cannot empty pen trash directory {path}: {error}").format(path=trash_dir, error=exc)
            ) from exc
    return freed


def delete_title(pen: Pen, file_name: str) -> int:
    require_mounted(pen)
    validate_title_name(file_name)

    target = pen.mountpoint / file_name
    if target.is_symlink() or not target.is_file():
        raise PenError(_("{name} is not a title on the pen").format(name=file_name))

    try:
        freed = target.stat().st_size
        os.unlink(target)
    except OSError as exc:
        raise PenError(_("cannot delete {name} from the pen: {error}").format(name=file_name, error=exc)) from exc

    freed += empty_trash(pen)
    fsync_directory(pen.mountpoint)
    return freed


def _products_by_file_name(catalog: Catalog) -> dict[str, Product]:
    by_name: dict[str, Product] = {}
    for product in catalog.products:
        try:
            by_name[gme_file_name(product).casefold()] = product
        except CatalogError:
            continue  # an unsafe catalog name can't be a file on the pen
    return by_name


def outdated_titles(
    pen: Pen, catalog: Catalog, *, installed: tuple[str, ...] | None = None
) -> list[OutdatedTitle]:
    by_file_name = _products_by_file_name(catalog)

    outdated: list[OutdatedTitle] = []
    for file_name in installed if installed is not None else installed_gme_files(pen):
        product = by_file_name.get(file_name.casefold())
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
