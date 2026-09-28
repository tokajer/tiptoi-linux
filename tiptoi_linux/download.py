"""Download .gme product files into the local cache, with resume support."""

from __future__ import annotations

import http.client
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tiptoi_linux.catalog import Product, cache_path, gme_file_name, is_safe_file_name
from tiptoi_linux.errors import TiptoiError
from tiptoi_linux.i18n import _
from tiptoi_linux.net import https_opener
from tiptoi_linux.streams import Progress, pump

_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-\d+/(\d+|\*)$")


class DownloadError(TiptoiError):
    """Raised for .gme download failures."""


class _RestartNeeded(Exception):
    """Internal signal: the resumed response can't be trusted; retry once from zero."""


def gme_cache_dir() -> Path:
    return cache_path().parent / "gme"


def _encode_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    # WHY: safe="/%" leaves an already-encoded URL (every live one today) byte-for-byte unchanged,
    # while still encoding a literal space or other unsafe character in the path
    path = urllib.parse.quote(parts.path, safe="/%")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _parse_content_range(value: str | None) -> tuple[int, int | None] | None:
    if value is None:
        return None
    match = _CONTENT_RANGE_RE.match(value.strip())
    if match is None:
        return None
    start = int(match.group(1))
    total_str = match.group(2)
    return start, (None if total_str == "*" else int(total_str))


def _stream(
    response: object,
    part: Path,
    mode: str,
    offset: int,
    total: int | None,
    progress: Progress | None,
) -> None:
    with open(part, mode) as handle:
        pump(response, handle.write, progress, total, start=offset)
        handle.flush()
        os.fsync(handle.fileno())


def _attempt(
    url: str,
    part: Path,
    offset: int,
    progress: Progress | None,
    opener: urllib.request.OpenerDirector,
) -> int | None:
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    request = urllib.request.Request(url, headers=headers)

    with opener.open(request, timeout=30) as response:
        if offset:
            if response.status != 206:
                # WHY: server ignored our Range header; the body is the whole file, not a continuation
                raise _RestartNeeded()
            parsed = _parse_content_range(response.headers.get("Content-Range"))
            if parsed is None or parsed[0] != offset:
                # WHY: Content-Range proved this response isn't a continuation at our offset -
                # concatenating it onto the existing .part would corrupt the file (proven: HELLOHELLO)
                raise _RestartNeeded()
            total = parsed[1]
            mode = "ab"
        else:
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type.startswith("text/"):
                # WHY: an HTML error/captive-portal page comes back as HTTP 200 with a text/* type
                raise DownloadError(
                    _("refusing non-.gme response ({content_type}) for {url}").format(
                        content_type=content_type, url=url
                    )
                )
            length_header = response.headers.get("Content-Length")
            if length_header is None:
                # WHY: an unlengthed body can be silently truncated; never cache what we can't verify
                raise DownloadError(
                    _("refusing unverifiable response with no Content-Length for {url}").format(url=url)
                )
            total = int(length_header)
            mode = "wb"

        _stream(response, part, mode, offset, total, progress)

    return total


def _restart_from_zero(
    url: str,
    part: Path,
    product_url: str,
    progress: Progress | None,
    opener: urllib.request.OpenerDirector,
) -> int | None:
    part.unlink(missing_ok=True)
    try:
        return _attempt(url, part, 0, progress, opener)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise DownloadError(_("failed to download {url}: {error}").format(url=product_url, error=exc)) from exc


def _cache_target(product: Product, cache_dir: Path | None) -> Path:
    """Validate the product's catalog fields and return its cache path, creating the directory."""
    scheme = urllib.parse.urlparse(product.url).scheme
    if scheme != "https":
        raise DownloadError(
            _("refusing to download product over non-https URL: {url}").format(url=product.url)
        )

    if not is_safe_file_name(product.version):
        # WHY: the version comes straight from the catalog CSV, same trust boundary as the name -
        # the cache is keyed on it (one directory per version), so it must be just as safe as a
        # path component
        raise DownloadError(_("refusing unsafe product version: {version!r}").format(version=product.version))

    file_name = gme_file_name(product)  # WHY: validate before touching the filesystem at all

    base = gme_cache_dir() if cache_dir is None else cache_dir
    directory = base / product.version
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadError(
            _("cannot create cache directory {path}: {error}").format(path=directory, error=exc)
        ) from exc
    return directory / file_name


def _fetch_part(
    product_url: str,
    part: Path,
    progress: Progress | None,
    opener: urllib.request.OpenerDirector,
) -> int | None:
    """Download into part, resuming if it already exists; return the expected total size if known."""
    url = _encode_url(product_url)
    try:
        offset = part.stat().st_size if part.exists() else 0
        return _attempt(url, part, offset, progress, opener)
    except _RestartNeeded:
        return _restart_from_zero(url, part, product_url, progress, opener)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and offset:
            # WHY: a Range past EOF (e.g. the remote file was replaced) can't be resumed; one clean retry
            return _restart_from_zero(url, part, product_url, progress, opener)
        raise DownloadError(_("failed to download {url}: {error}").format(url=product_url, error=exc)) from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # WHY: covers IncompleteRead, InvalidURL, a malformed Content-Length, UnicodeEncodeError
        raise DownloadError(_("failed to download {url}: {error}").format(url=product_url, error=exc)) from exc


def _finalize(part: Path, target: Path, total: int | None, product_url: str) -> None:
    try:
        final_size = part.stat().st_size
    except OSError as exc:
        raise DownloadError(_("failed to download {url}: {error}").format(url=product_url, error=exc)) from exc
    if total is not None and final_size != total:
        # WHY: leave .part on disk so the next run resumes instead of restarting from zero
        raise DownloadError(
            _("downloaded size {actual} for {url} does not match expected size {expected}").format(
                actual=final_size, url=product_url, expected=total
            )
        )

    try:
        os.replace(part, target)
    except OSError as exc:
        raise DownloadError(
            _("cannot finalize download for {url} at {path}: {error}").format(
                url=product_url, path=target, error=exc
            )
        ) from exc


def download_product(
    product: Product,
    *,
    cache_dir: Path | None = None,
    progress: Progress | None = None,
    opener: urllib.request.OpenerDirector = https_opener,
) -> Path:
    target = _cache_target(product, cache_dir)
    if target.exists():
        # WHY: this is what makes a re-run a no-op - no network request at all
        return target

    part = target.with_suffix(".gme.part")
    total = _fetch_part(product.url, part, progress, opener)
    _finalize(part, target, total, product.url)
    return target
