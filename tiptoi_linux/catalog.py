"""Fetch, parse, and cache the Ravensburger tiptoi product catalog."""

from __future__ import annotations

import csv
import io
import os
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from tiptoi_linux.common import https_opener
from tiptoi_linux.errors import TiptoiError
from tiptoi_linux.i18n import _

CATALOG_URL = "https://cdn.ravensburger.de/db/tiptoi.csv"
DEFAULT_MAX_AGE_SECONDS = 86400
MAX_CATALOG_BYTES = 4 * 1024 * 1024
GME_SUFFIX = ".gme"


class CatalogError(TiptoiError):
    """Raised for catalog network or parse failures."""


@dataclass(frozen=True)
class Product:
    series_id: str
    version: str
    url: str
    name: str


@dataclass(frozen=True)
class Firmware:
    version: str
    checksum: str
    url: str


@dataclass(frozen=True)
class Catalog:
    csv_version: str
    firmware: Firmware | None
    products: tuple[Product, ...]

    def by_name(self, name: str) -> Product | None:
        # SHORTCUT: linear scan over 321 products, index by name if the catalog grows past a few thousand
        for product in self.products:
            if product.name == name:
                return product
        return None

    def search(self, term: str) -> list[Product]:
        needle = term.lower()
        return [product for product in self.products if needle in product.name.lower()]


def _fallback_name(url: str) -> str:
    basename = urllib.parse.unquote(url.rsplit("/", 1)[-1])
    if basename.lower().endswith(GME_SUFFIX):
        return basename[: -len(GME_SUFFIX)]
    return basename


def parse_catalog(text: str) -> Catalog:
    rows = [row for row in csv.reader(io.StringIO(text)) if row]

    csv_version = ""
    firmware: Firmware | None = None
    if len(rows) >= 2:
        firmware_row = rows[1]
        if len(firmware_row) == 4 and firmware_row[3].startswith("http"):
            csv_version = firmware_row[0]
            firmware = Firmware(version=firmware_row[1], checksum=firmware_row[2], url=firmware_row[3])

    item_rows = rows
    for idx, row in enumerate(rows):
        if row[0].strip() == "Items._id":
            item_rows = rows[idx + 1 :]
            break
    # WHY: if the header row is missing/renamed, fall back to scanning everything - the
    # 4-field/non-empty-url/.gme filter below is selective enough to reject header/firmware rows

    products: list[Product] = []
    for row in item_rows:
        if len(row) != 4:
            continue
        series_id, version, url, file_name = row
        if not url:
            continue
        if not url.lower().endswith(GME_SUFFIX):
            # excludes non-product rows (e.g. the OTA firmware .bin row)
            continue
        name = file_name if file_name else _fallback_name(url)
        products.append(Product(series_id=series_id, version=version, url=url, name=name))

    return Catalog(csv_version=csv_version, firmware=firmware, products=tuple(products))


def cache_path() -> Path:
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache) if xdg_cache else Path.home() / ".cache"
    return base / "tiptoi-linux" / "tiptoi.csv"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".tiptoi-", suffix=".tmp")
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="latin-1")
        except BaseException:
            os.close(fd)  # WHY: fdopen failed before wrapping fd in a file object, so nothing else owns it
            raise
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())  # WHY: without this, rename metadata can hit disk before the data does
        os.replace(tmp_path, path)  # WHY: atomic rename so an interrupted write never leaves a truncated cache
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def download_catalog(url: str = CATALOG_URL, *, cache: Path | None = None) -> Catalog:
    scheme = urllib.parse.urlparse(url).scheme
    if scheme != "https":
        # WHY: the catalog is a trust boundary for later .gme downloads; never follow a downgrade to http
        raise CatalogError(_("refusing to download catalog over non-https URL: {url}").format(url=url))

    target = cache_path() if cache is None else cache

    try:
        with https_opener.open(url, timeout=30) as response:
            raw = response.read(MAX_CATALOG_BYTES + 1)
    except OSError as exc:  # covers URLError, HTTPError, and timeouts
        raise CatalogError(_("failed to download catalog from {url}: {error}").format(url=url, error=exc)) from exc

    if len(raw) > MAX_CATALOG_BYTES:
        raise CatalogError(
            _("catalog response from {url} exceeds {max_bytes} bytes").format(url=url, max_bytes=MAX_CATALOG_BYTES)
        )

    # WHY: the server reports ISO-8859 and the content is ASCII today; latin-1 decodes
    # any byte sequence without raising, unlike utf-8 which could fail on future input
    text = raw.decode("latin-1")
    catalog = parse_catalog(text)
    if not catalog.products:
        # WHY: parse-then-validate-then-write so an HTML error page or empty 200 never clobbers a good cache
        raise CatalogError(
            _("catalog downloaded from {url} has zero products; refusing to overwrite cache").format(url=url)
        )

    try:
        _atomic_write(target, text)
    except OSError as exc:
        raise CatalogError(_("cannot write catalog cache at {path}: {error}").format(path=target, error=exc)) from exc

    return catalog


def load_catalog(
    *,
    force: bool = False,
    max_age: int = DEFAULT_MAX_AGE_SECONDS,
    url: str = CATALOG_URL,
    cache: Path | None = None,
    allow_stale: bool = True,
) -> Catalog:
    path = cache_path() if cache is None else cache

    if not force:
        try:
            age = time.time() - path.stat().st_mtime
        except FileNotFoundError:
            age = None
        except OSError as exc:
            raise CatalogError(_("cannot read catalog cache at {path}: {error}").format(path=path, error=exc)) from exc
        # WHY: also reject a negative age so clock skew or a restored backup can't pin a stale cache forever
        if age is not None and 0 <= age < max_age:
            try:
                text = path.read_text(encoding="latin-1")
            except OSError as exc:
                raise CatalogError(
                    _("cannot read catalog cache at {path}: {error}").format(path=path, error=exc)
                ) from exc
            return parse_catalog(text)

    try:
        return download_catalog(url, cache=path)
    except CatalogError as exc:
        if not allow_stale:
            raise
        try:
            text = path.read_text(encoding="latin-1")
        except FileNotFoundError:
            raise CatalogError(
                _("{error}; no cached catalog available at {path}").format(error=exc, path=path)
            ) from exc
        except OSError as read_exc:
            raise CatalogError(
                _("cannot read catalog cache at {path}: {error}").format(path=path, error=read_exc)
            ) from read_exc
        print(_("warning: {error} - using stale cached catalog at {path}").format(error=exc, path=path), file=sys.stderr)
        return parse_catalog(text)
