"""Command-line interface for the tiptoi catalog."""

from __future__ import annotations

import argparse
import functools
import sys
from collections.abc import Sequence
from pathlib import Path

from tiptoi_linux.catalog import Catalog, Product, cache_path, load_catalog
from tiptoi_linux.download import download_product
from tiptoi_linux.errors import TiptoiError
from tiptoi_linux.i18n import _, ngettext
from tiptoi_linux.pen import (
    Phase,
    PhaseProgress,
    delete_title,
    find_pen,
    install_title,
    installed_gme_files,
    mount_pen,
    outdated_titles,
    pen_summary,
    unmount_pen,
)
from tiptoi_linux.streams import Progress, percent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tiptoi", description=_("Manage the tiptoi pen product catalog."))
    subparsers = parser.add_subparsers(dest="command", required=True)

    update_parser = subparsers.add_parser("update", help=_("Force-refresh the cached catalog."))
    update_parser.set_defaults(func=_cmd_update)

    list_parser = subparsers.add_parser("list", help=_("List all products in the catalog."))
    list_parser.set_defaults(func=_cmd_list)

    search_parser = subparsers.add_parser("search", help=_("Search products by name."))
    search_parser.add_argument("term", help=_("Substring to search for (case-insensitive)."))
    search_parser.set_defaults(func=_cmd_search)

    download_parser = subparsers.add_parser(
        "download", help=_("Download a product's .gme file into the cache.")
    )
    download_parser.add_argument("name", help=_("Exact product name, as shown by 'tiptoi list'."))
    download_parser.set_defaults(func=_cmd_download)

    pen_parser = subparsers.add_parser("pen", help=_("Inspect or install onto a tiptoi pen."))
    pen_parser.add_argument(
        "--pen",
        dest="pen_path",
        type=Path,
        default=None,
        help=_("Path to the pen's mountpoint, bypassing auto-detection."),
    )
    pen_subparsers = pen_parser.add_subparsers(dest="pen_command", required=True)

    pen_status_parser = pen_subparsers.add_parser(
        "status", help=_("Show the pen's mountpoint, free space, and titles.")
    )
    pen_status_parser.set_defaults(func=_cmd_pen_status)

    pen_mount_parser = pen_subparsers.add_parser("mount", help=_("Mount the pen."))
    pen_mount_parser.set_defaults(func=_cmd_pen_mount)

    pen_unmount_parser = pen_subparsers.add_parser(
        "unmount", help=_("Unmount the pen so it can be unplugged.")
    )
    pen_unmount_parser.set_defaults(func=_cmd_pen_unmount)

    pen_install_parser = pen_subparsers.add_parser(
        "install", help=_("Download (if needed) and install a title.")
    )
    pen_install_parser.add_argument("name", help=_("Exact product name, as shown by 'tiptoi list'."))
    pen_install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=_("Validate and report what would be installed without writing to the pen."),
    )
    pen_install_parser.set_defaults(func=_cmd_pen_install)

    pen_outdated_parser = pen_subparsers.add_parser(
        "outdated", help=_("List installed titles with a newer catalog version.")
    )
    pen_outdated_parser.set_defaults(func=_cmd_pen_outdated)

    pen_delete_parser = pen_subparsers.add_parser(
        "delete", help=_("Delete titles from the pen and empty its trash.")
    )
    pen_delete_parser.add_argument(
        "names", nargs="+", help=_("On-pen file name(s), as shown by 'tiptoi pen status' (e.g. WN.gme).")
    )
    pen_delete_parser.add_argument(
        "--yes", action="store_true", help=_("Delete without prompting for confirmation.")
    )
    pen_delete_parser.set_defaults(func=_cmd_pen_delete)

    return parser


def _print_products(products: Sequence[Product]) -> None:
    if not products:
        return
    width = max(len(product.name) for product in products)
    for product in products:
        print(f"{product.name:<{width}}  {product.version}")


def _print_error(message: str) -> None:
    print(_("error: {message}").format(message=message), file=sys.stderr)


def _load_catalog() -> Catalog:
    loaded = load_catalog()
    if loaded.stale_reason is not None:
        print(
            _("warning: {error} - using stale cached catalog at {path}").format(
                error=loaded.stale_reason, path=cache_path()
            ),
            file=sys.stderr,
        )
    return loaded.catalog


def _cmd_update(args: argparse.Namespace) -> int:
    catalog = load_catalog(force=True, allow_stale=False).catalog
    n = len(catalog.products)
    print(
        ngettext("Catalog updated: {n} product", "Catalog updated: {n} products", n).format(n=n)
    )
    print(_("Cache: {path}").format(path=cache_path()))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    if not catalog.products:
        _print_error(_("catalog is empty - run 'tiptoi update'"))
        return 1
    _print_products(catalog.products)
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    matches = catalog.search(args.term)
    if not matches:
        print(_("No products matching {term!r}").format(term=args.term), file=sys.stderr)
        return 1
    _print_products(matches)
    return 0


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"


PHASE_LABELS = {
    Phase.DOWNLOAD: _("Downloading"),
    Phase.COPY: _("Copying to pen"),
    Phase.VERIFY: _("Verifying on pen"),
}


def _progress_text(done: int, total: int | None) -> str:
    if total:
        return f"{percent(done, total)}%"
    return _("{n} bytes").format(n=done)


def _make_phase_progress() -> PhaseProgress | None:
    if not sys.stderr.isatty():
        # WHY: piped/redirected output (logs, CI) must stay clean, with no \r percentage spam
        return None

    state: dict[str, Phase | None] = {"phase": None}

    def _progress(phase: Phase, done: int, total: int | None) -> None:
        if state["phase"] is not None and state["phase"] != phase:
            # WHY: keep each phase's throttled \r updates on their own line instead of overwriting
            # the previous phase's last line
            print(file=sys.stderr)
        state["phase"] = phase
        print(f"\r{PHASE_LABELS[phase]} {_progress_text(done, total)}", end="", file=sys.stderr, flush=True)

    return _progress


def _make_download_progress() -> Progress | None:
    phase_progress = _make_phase_progress()
    return None if phase_progress is None else functools.partial(phase_progress, Phase.DOWNLOAD)


def _require_product(catalog: Catalog, name: str) -> Product:
    product = catalog.by_name(name)
    if product is None:
        raise TiptoiError(_("no product named {name!r}").format(name=name))
    return product


def _cmd_download(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    product = _require_product(catalog, args.name)

    progress = _make_download_progress()
    try:
        path = download_product(product, progress=progress)
    finally:
        if progress is not None:
            print(file=sys.stderr)
    print(path)
    return 0


def _cmd_pen_status(args: argparse.Namespace) -> int:
    summary = pen_summary(find_pen(override=args.pen_path), None)
    print(_("Mountpoint: {path}").format(path=summary.pen.mountpoint))
    print(
        _("Free space: {free} of {total}").format(
            free=_human_size(summary.free), total=_human_size(summary.total)
        )
    )
    if not summary.installed:
        print(_("Installed titles: none"))
        return 0
    print(_("Installed titles ({n}):").format(n=len(summary.installed)))
    for name in summary.installed:
        print(f"  {name}")
    return 0


def _cmd_pen_mount(args: argparse.Namespace) -> int:
    pen = mount_pen()
    print(_("Mounted at {path}").format(path=pen.mountpoint))
    return 0


def _cmd_pen_unmount(args: argparse.Namespace) -> int:
    pen = find_pen(override=args.pen_path)
    unmount_pen(pen)
    print(_("Unmounted — the pen can be unplugged now"))
    return 0


def _cmd_pen_install(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    product = _require_product(catalog, args.name)

    pen = find_pen(override=args.pen_path)

    progress = _make_phase_progress()
    try:
        destination = install_title(pen, product, dry_run=args.dry_run, progress=progress)
    finally:
        if progress is not None:
            print(file=sys.stderr)

    if args.dry_run:
        print(_("Would install {name} to {path}").format(name=destination.name, path=destination))
    else:
        size = destination.stat().st_size
        print(
            _("Installed {name} to {path} ({size}, verified on the pen)").format(
                name=destination.name, path=destination, size=_human_size(size)
            )
        )
        print(_("Run 'tiptoi pen unmount' before unplugging the pen."))
    return 0


def _cmd_pen_delete(args: argparse.Namespace) -> int:
    pen = find_pen(override=args.pen_path)
    installed_by_lower = {name.lower(): name for name in installed_gme_files(pen)}

    unknown: list[str] = []
    resolved: list[str] = []
    seen: set[str] = set()
    for requested in args.names:
        actual = installed_by_lower.get(requested.lower())
        if actual is None:
            unknown.append(requested)
        elif actual not in seen:
            seen.add(actual)
            resolved.append(actual)

    if unknown:
        _print_error(_("not installed on the pen: {names}").format(names=", ".join(unknown)))
        return 1

    if not args.yes:
        if not sys.stdin.isatty():
            _print_error(_("refusing to delete without confirmation; pass --yes"))
            return 1
        print(_("Titles to delete ({n}):").format(n=len(resolved)))
        for name in resolved:
            print(f"  {name}")
        # WHY: the accepted answers ("y"/"yes") are parsed by the code below and must not be
        # translated - only the surrounding sentence is
        n = len(resolved)
        prompt = ngettext(
            "Delete {n} title from the pen and empty its trash? [y/N] ",
            "Delete {n} titles from the pen and empty its trash? [y/N] ",
            n,
        ).format(n=n)
        answer = input(prompt)
        if answer.strip().lower() not in ("y", "yes"):
            return 1

    freed = 0
    for name in resolved:
        freed += delete_title(pen, name)
        print(_("Deleted {name}").format(name=name))
    print(_("Freed {size}").format(size=_human_size(freed)))
    return 0


def _cmd_pen_outdated(args: argparse.Namespace) -> int:
    pen = find_pen(override=args.pen_path)
    catalog = _load_catalog()
    titles = outdated_titles(pen, catalog)
    if not titles:
        print(_("All installed titles are up to date"))
        return 0
    for title in titles:
        print(title.describe())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except (TiptoiError, OSError) as exc:
        # WHY: OSError as a backstop - an unwrapped filesystem failure (e.g. the pen unplugged
        # mid-operation) should read as an error message, not a traceback
        _print_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
