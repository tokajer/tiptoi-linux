from __future__ import annotations

import http.client
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tiptoi_linux import common
from tiptoi_linux.catalog import Product
from tiptoi_linux.download import DownloadError, download_product, gme_cache_dir, gme_file_name


def _product(
    name: str = "Test Title",
    url: str = "https://cdn.example.com/Test%20Title.gme",
    version: str = "1",
) -> Product:
    return Product(series_id="1", version=version, url=url, name=name)


class FakeResponse:
    """Minimal stand-in for the context manager urllib's opener.open() returns."""

    def __init__(
        self,
        body: bytes,
        status: int,
        content_length: int | None = None,
        content_range: str | None = None,
        content_type: str | None = None,
    ) -> None:
        self._body = io.BytesIO(body)
        self.status = status
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        if content_range is not None:
            self.headers["Content-Range"] = content_range
        if content_type is not None:
            self.headers["Content-Type"] = content_type

    def read(self, size: int) -> bytes:
        return self._body.read(size)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class GmeFileNameTests(unittest.TestCase):
    def test_builds_dot_gme_name(self) -> None:
        self.assertEqual(gme_file_name(_product(name="Foo Bar")), "Foo Bar.gme")

    def test_rejects_traversal_name(self) -> None:
        with self.assertRaises(DownloadError):
            gme_file_name(_product(name="../../evil"))

    def test_rejects_embedded_separator_name(self) -> None:
        with self.assertRaises(DownloadError):
            gme_file_name(_product(name="a/b"))

    def test_rejects_dot_and_dotdot(self) -> None:
        for bad_name in (".", ".."):
            with self.subTest(bad_name=bad_name):
                with self.assertRaises(DownloadError):
                    gme_file_name(_product(name=bad_name))


class GmeCacheDirTests(unittest.TestCase):
    def test_honours_xdg_cache_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.dict("os.environ", {"XDG_CACHE_HOME": tmp_dir}):
                self.assertEqual(gme_cache_dir(), Path(tmp_dir) / "tiptoi-linux" / "gme")

    def test_falls_back_when_xdg_cache_home_empty(self) -> None:
        with patch.dict("os.environ", {"XDG_CACHE_HOME": ""}):
            self.assertEqual(gme_cache_dir(), Path.home() / ".cache" / "tiptoi-linux" / "gme")


class ResumeTests(unittest.TestCase):
    def test_resumes_with_range_header_and_concatenates_correctly(self) -> None:
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            version_dir = cache_dir / product.version
            version_dir.mkdir()
            part = version_dir / "Test Title.gme.part"
            prefix = b"HELLO "
            remainder = b"WORLD"
            part.write_bytes(prefix)

            fake_response = FakeResponse(
                remainder,
                status=206,
                content_length=len(remainder),
                content_range=f"bytes {len(prefix)}-{len(prefix) + len(remainder) - 1}/{len(prefix) + len(remainder)}",
            )
            captured_requests = []

            def fake_open(request, timeout=None):  # noqa: ANN001
                captured_requests.append(request)
                return fake_response

            with patch.object(common.https_opener, "open", side_effect=fake_open):
                result = download_product(product, cache_dir=cache_dir)

            self.assertEqual(len(captured_requests), 1)
            self.assertEqual(captured_requests[0].get_header("Range"), f"bytes={len(prefix)}-")
            self.assertEqual(result, version_dir / "Test Title.gme")
            self.assertEqual(result.read_bytes(), prefix + remainder)
            self.assertFalse(part.exists())


class VersionKeyedCacheTests(unittest.TestCase):
    def test_a_v1_cached_file_is_not_reused_for_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            v2_product = _product(version="2")

            (cache_dir / "1").mkdir()
            (cache_dir / "1" / "Test Title.gme").write_bytes(b"old bytes")

            body = b"new bytes"
            fake_response = FakeResponse(body, status=200, content_length=len(body))

            with patch.object(common.https_opener, "open", return_value=fake_response):
                result = download_product(v2_product, cache_dir=cache_dir)

            self.assertEqual(result, cache_dir / "2" / "Test Title.gme")
            self.assertEqual(result.read_bytes(), body)
            # WHY: the stale v1 cache entry is untouched, not pruned
            self.assertEqual((cache_dir / "1" / "Test Title.gme").read_bytes(), b"old bytes")


class NoOpRerunTests(unittest.TestCase):
    def test_existing_target_skips_network_entirely(self) -> None:
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            version_dir = cache_dir / product.version
            version_dir.mkdir()
            target = version_dir / "Test Title.gme"
            target.write_bytes(b"already here")

            with patch.object(
                common.https_opener,
                "open",
                side_effect=AssertionError("opener.open must not be called for an existing target"),
            ):
                result = download_product(product, cache_dir=cache_dir)

            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), b"already here")


class RangeIgnoredTests(unittest.TestCase):
    def test_stale_part_discarded_when_server_ignores_range(self) -> None:
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            version_dir = cache_dir / product.version
            version_dir.mkdir()
            part = version_dir / "Test Title.gme.part"
            part.write_bytes(b"STALEDATA-NOT-A-PREFIX")

            full_body = b"FRESH FULL CONTENT"
            fake_response = FakeResponse(full_body, status=200, content_length=len(full_body))

            with patch.object(common.https_opener, "open", return_value=fake_response):
                result = download_product(product, cache_dir=cache_dir)

            self.assertEqual(result.read_bytes(), full_body)


class MismatchedContentRangeTests(unittest.TestCase):
    def test_206_with_mismatched_start_restarts_instead_of_concatenating(self) -> None:
        # WHY: proven failure mode - a server answering "206 bytes 0-4/5" to "Range: bytes=5-"
        # must not be concatenated onto the existing .part (that produced "HELLOHELLO")
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            version_dir = cache_dir / product.version
            version_dir.mkdir()
            part = version_dir / "Test Title.gme.part"
            part.write_bytes(b"HELLO")

            full_body = b"HELLO"
            responses = [
                FakeResponse(full_body, status=206, content_length=len(full_body), content_range="bytes 0-4/5"),
                FakeResponse(full_body, status=200, content_length=len(full_body)),
            ]

            def fake_open(request, timeout=None):  # noqa: ANN001
                return responses.pop(0)

            with patch.object(common.https_opener, "open", side_effect=fake_open):
                result = download_product(product, cache_dir=cache_dir)

            self.assertEqual(result.read_bytes(), b"HELLO")


class MissingContentRangeTests(unittest.TestCase):
    def test_206_with_no_content_range_restarts(self) -> None:
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            version_dir = cache_dir / product.version
            version_dir.mkdir()
            part = version_dir / "Test Title.gme.part"
            part.write_bytes(b"STALE")

            full_body = b"FRESH"
            responses = [
                FakeResponse(full_body, status=206, content_length=len(full_body)),  # no Content-Range
                FakeResponse(full_body, status=200, content_length=len(full_body)),
            ]

            def fake_open(request, timeout=None):  # noqa: ANN001
                return responses.pop(0)

            with patch.object(common.https_opener, "open", side_effect=fake_open):
                result = download_product(product, cache_dir=cache_dir)

            self.assertEqual(result.read_bytes(), full_body)


class RangeNotSatisfiableTests(unittest.TestCase):
    def test_416_removes_part_and_retries_once_cleanly(self) -> None:
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            version_dir = cache_dir / product.version
            version_dir.mkdir()
            part = version_dir / "Test Title.gme.part"
            part.write_bytes(b"STALE-PAST-EOF")

            full_body = b"FRESH FULL BODY"
            fresh_response = FakeResponse(full_body, status=200, content_length=len(full_body))

            calls = []

            def fake_open(request, timeout=None):  # noqa: ANN001
                calls.append(request)
                if len(calls) == 1:
                    raise urllib.error.HTTPError(request.full_url, 416, "Range Not Satisfiable", {}, None)
                return fresh_response

            with patch.object(common.https_opener, "open", side_effect=fake_open):
                result = download_product(product, cache_dir=cache_dir)

            self.assertEqual(len(calls), 2)
            self.assertFalse(calls[1].has_header("Range"))
            self.assertEqual(result.read_bytes(), full_body)

    def test_other_http_error_is_not_retried(self) -> None:
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)

            def fake_open(request, timeout=None):  # noqa: ANN001
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

            with patch.object(common.https_opener, "open", side_effect=fake_open):
                with self.assertRaises(DownloadError):
                    download_product(product, cache_dir=cache_dir)


class RedirectDowngradeTests(unittest.TestCase):
    def test_url_error_from_opener_raises_download_error(self) -> None:
        # WHY: this is what common.HTTPSOnlyRedirectHandler raises on an https->http redirect -
        # verifies download_product wraps it into DownloadError via its existing except OSError
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)

            def fake_open(request, timeout=None):  # noqa: ANN001
                raise urllib.error.URLError("refusing to follow redirect to non-https URL: http://evil.example/x.gme")

            with patch.object(common.https_opener, "open", side_effect=fake_open):
                with self.assertRaises(DownloadError):
                    download_product(product, cache_dir=cache_dir)


class MissingContentLengthTests(unittest.TestCase):
    def test_missing_content_length_raises_and_caches_nothing(self) -> None:
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            fake_response = FakeResponse(b"whatever", status=200)  # no Content-Length

            with patch.object(common.https_opener, "open", return_value=fake_response):
                with self.assertRaises(DownloadError):
                    download_product(product, cache_dir=cache_dir)

            target = cache_dir / product.version / "Test Title.gme"
            part = cache_dir / product.version / "Test Title.gme.part"
            self.assertFalse(target.exists())
            self.assertFalse(part.exists())


class TextContentTypeTests(unittest.TestCase):
    def test_text_html_response_raises_and_caches_nothing(self) -> None:
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            body = b"<html>captive portal</html>"
            fake_response = FakeResponse(
                body, status=200, content_length=len(body), content_type="text/html; charset=utf-8"
            )

            with patch.object(common.https_opener, "open", return_value=fake_response):
                with self.assertRaises(DownloadError):
                    download_product(product, cache_dir=cache_dir)

            target = cache_dir / product.version / "Test Title.gme"
            part = cache_dir / product.version / "Test Title.gme.part"
            self.assertFalse(target.exists())
            self.assertFalse(part.exists())


class IncompleteReadTests(unittest.TestCase):
    def test_incomplete_read_raises_download_error(self) -> None:
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)

            class RaisingResponse(FakeResponse):
                def read(self, size: int) -> bytes:
                    raise http.client.IncompleteRead(b"partial")

            fake_response = RaisingResponse(b"x" * 100, status=200, content_length=100)

            with patch.object(common.https_opener, "open", return_value=fake_response):
                with self.assertRaises(DownloadError):
                    download_product(product, cache_dir=cache_dir)


class SizeMismatchTests(unittest.TestCase):
    def test_size_mismatch_raises_and_leaves_part_on_disk(self) -> None:
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            body = b"short"
            fake_response = FakeResponse(body, status=200, content_length=len(body) + 100)

            with patch.object(common.https_opener, "open", return_value=fake_response):
                with self.assertRaises(DownloadError):
                    download_product(product, cache_dir=cache_dir)

            part = cache_dir / product.version / "Test Title.gme.part"
            target = cache_dir / product.version / "Test Title.gme"
            self.assertTrue(part.exists())
            self.assertEqual(part.read_bytes(), body)
            self.assertFalse(target.exists())


class FilenameTraversalTests(unittest.TestCase):
    def test_rejects_traversal_name_and_writes_nothing(self) -> None:
        product = _product(name="../../evil")
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            with patch.object(
                common.https_opener,
                "open",
                side_effect=AssertionError("opener.open must not be called for an unsafe name"),
            ):
                with self.assertRaises(DownloadError):
                    download_product(product, cache_dir=cache_dir)
            self.assertEqual(list(cache_dir.iterdir()), [])

    def test_rejects_embedded_separator_name_and_writes_nothing(self) -> None:
        product = _product(name="a/b")
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            with patch.object(
                common.https_opener,
                "open",
                side_effect=AssertionError("opener.open must not be called for an unsafe name"),
            ):
                with self.assertRaises(DownloadError):
                    download_product(product, cache_dir=cache_dir)
            self.assertEqual(list(cache_dir.iterdir()), [])


class UnsafeVersionTests(unittest.TestCase):
    def test_rejects_traversal_version_and_writes_nothing(self) -> None:
        product = _product(version="../../evil")
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            with patch.object(
                common.https_opener,
                "open",
                side_effect=AssertionError("opener.open must not be called for an unsafe version"),
            ):
                with self.assertRaises(DownloadError):
                    download_product(product, cache_dir=cache_dir)
            self.assertEqual(list(cache_dir.iterdir()), [])


class NonHttpsUrlTests(unittest.TestCase):
    def test_rejects_non_https_url(self) -> None:
        product = _product(url="http://cdn.example.com/Test%20Title.gme")
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(DownloadError):
                download_product(product, cache_dir=Path(tmp_dir))


class UrlEncodingTests(unittest.TestCase):
    def test_space_is_encoded_and_percent_encoding_is_preserved(self) -> None:
        product = _product(name="Encoding Test", url="https://cdn.example.com/a b/%20literal.gme")
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            body = b"data"
            fake_response = FakeResponse(body, status=200, content_length=len(body))
            captured_requests = []

            def fake_open(request, timeout=None):  # noqa: ANN001
                captured_requests.append(request)
                return fake_response

            with patch.object(common.https_opener, "open", side_effect=fake_open):
                download_product(product, cache_dir=cache_dir)

            requested_url = captured_requests[0].full_url
            self.assertIn("a%20b", requested_url)
            # WHY: an already-encoded %20 must not become %2520
            self.assertIn("%20literal.gme", requested_url)
            self.assertNotIn("%2520", requested_url)


class ProgressThrottleTests(unittest.TestCase):
    def test_progress_is_called_at_most_about_101_times(self) -> None:
        product = _product()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            body = b"x" * 50_000
            fake_response = FakeResponse(body, status=200, content_length=len(body))

            calls: list[tuple[int, int | None]] = []

            # WHY: a tiny chunk size forces ~500 read() calls so an unthrottled implementation would
            # call progress ~500 times instead of ~101
            with (
                patch.object(common, "CHUNK_BYTES", 100),
                patch.object(common.https_opener, "open", return_value=fake_response),
            ):
                download_product(
                    product, cache_dir=cache_dir, progress=lambda done, total: calls.append((done, total))
                )

            self.assertLessEqual(len(calls), 101)
            self.assertGreater(len(calls), 50)
            self.assertEqual(calls[-1][0], len(body))


if __name__ == "__main__":
    unittest.main()
