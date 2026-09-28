from __future__ import annotations

import io
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tiptoi_linux import catalog, common
from tiptoi_linux.catalog import (
    CatalogError,
    cache_path,
    download_catalog,
    parse_catalog,
)
from tiptoi_linux.common import HTTPSOnlyRedirectHandler

FIXTURE_CSV = (
    "CSV file version,Firmware version,Firmware checksum,Firmware download address\n"
    "26091403,6GE027,1872396468,"
    "https://cdn.ravensburger.de/db/Firmware-Files/de/27/REV12/Update6E.upd\n"
    "Items._id,Items._version,Items._url,Items._fileName\n"
    "0,v201022.1,"
    "https://cdn.ravensburger.de/db/Firmware-Files/de/v201022.1/REV-WLAN-MODUL/OTAUpdate.bin,\n"
    "150,20190529,"
    "https://cdn.ravensburger.de/db/applications/CREATE_Kreative_Bildergeschichten.gme,"
    "CREATE_Kreative_Bildergeschichten\n"
    "68,20190929,"
    "https://cdn.ravensburger.de/db/applications/WissenQuizzenFCBayernMuenchen.gme,"
    "WissenQuizzenFCBayernMuenchen\n"
    "68,20190930,"
    "https://cdn.ravensburger.de/db/applications/WissenQuizzenTiere.gme,"
    "WissenQuizzenTiere\n"
    "60,20141021,"
    "https://cdn.ravensburger.de/db/applications/Adventskalender%20Mandelmann.gme,"
    "Adventskalender Mandelmann\n"
    "70,20150101,"
    "https://cdn.ravensburger.de/db/applications/DieEiskoenigin.gme,"
    "Die Eiskoenigin\n"
    "80,20150202,"
    "https://cdn.ravensburger.de/db/applications/Space%20Adventure.gme,\n"
    "1,2,3\n"
    '"",,,Ravensburger\n'
)


class ParseCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = parse_catalog(FIXTURE_CSV)

    def test_expected_product_count(self) -> None:
        self.assertEqual(len(self.catalog.products), 6)

    def test_sentinel_row_dropped(self) -> None:
        names = [product.name for product in self.catalog.products]
        self.assertNotIn("Ravensburger", names)

    def test_ota_bin_row_excluded(self) -> None:
        series_ids = [product.series_id for product in self.catalog.products]
        self.assertNotIn("0", series_ids)
        for product in self.catalog.products:
            self.assertTrue(product.url.lower().endswith(".gme"))

    def test_malformed_row_skipped_without_raising(self) -> None:
        # parse_catalog(FIXTURE_CSV) in setUp already succeeded despite the "1,2,3" row;
        # confirm none of its fields leaked into a product.
        for product in self.catalog.products:
            self.assertNotEqual(product.series_id, "1")

    def test_shared_series_id_both_kept(self) -> None:
        matches = [p for p in self.catalog.products if p.series_id == "68"]
        names = {p.name for p in matches}
        self.assertEqual(
            names,
            {"WissenQuizzenFCBayernMuenchen", "WissenQuizzenTiere"},
        )

    def test_percent_encoded_filename_kept_as_is(self) -> None:
        product = self.catalog.by_name("Adventskalender Mandelmann")
        self.assertIsNotNone(product)
        self.assertIn("%20", product.url)

    def test_fallback_name_from_url_when_filename_empty(self) -> None:
        product = self.catalog.by_name("Space Adventure")
        self.assertIsNotNone(product)
        self.assertTrue(product.url.endswith("Space%20Adventure.gme"))

    def test_firmware_parsed_off_line_two(self) -> None:
        self.assertIsNotNone(self.catalog.firmware)
        self.assertEqual(self.catalog.firmware.version, "6GE027")
        self.assertEqual(self.catalog.firmware.checksum, "1872396468")
        self.assertEqual(
            self.catalog.firmware.url,
            "https://cdn.ravensburger.de/db/Firmware-Files/de/27/REV12/Update6E.upd",
        )
        self.assertEqual(self.catalog.csv_version, "26091403")

    def test_search_case_insensitive(self) -> None:
        matches = self.catalog.search("eisk")
        self.assertEqual([p.name for p in matches], ["Die Eiskoenigin"])
        matches_upper = self.catalog.search("EISK")
        self.assertEqual([p.name for p in matches_upper], ["Die Eiskoenigin"])

    def test_missing_firmware_section_yields_none(self) -> None:
        short_csv = (
            "CSV file version,Firmware version,Firmware checksum,Firmware download address\n"
            "23,fw\n"
            "Items._id,Items._version,Items._url,Items._fileName\n"
            "1,1,https://cdn.ravensburger.de/db/applications/Foo.gme,Foo\n"
        )
        catalog_obj = parse_catalog(short_csv)
        self.assertIsNone(catalog_obj.firmware)
        self.assertEqual(catalog_obj.csv_version, "")
        self.assertEqual(len(catalog_obj.products), 1)

    def test_items_header_found_after_blank_line(self) -> None:
        # Regression test: a blank line inserted before the items header must not shift
        # a hardcoded row index and misparse the section.
        csv_with_blank = (
            "CSV file version,Firmware version,Firmware checksum,Firmware download address\n"
            "26091403,6GE027,1872396468,"
            "https://cdn.ravensburger.de/db/Firmware-Files/de/27/REV12/Update6E.upd\n"
            "\n"
            "Items._id,Items._version,Items._url,Items._fileName\n"
            "150,20190529,"
            "https://cdn.ravensburger.de/db/applications/CREATE_Kreative_Bildergeschichten.gme,"
            "CREATE_Kreative_Bildergeschichten\n"
        )
        catalog_obj = parse_catalog(csv_with_blank)
        self.assertEqual(len(catalog_obj.products), 1)
        self.assertEqual(catalog_obj.products[0].name, "CREATE_Kreative_Bildergeschichten")
        self.assertEqual(catalog_obj.csv_version, "26091403")


class CachePathTests(unittest.TestCase):
    def test_honours_xdg_cache_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.dict("os.environ", {"XDG_CACHE_HOME": tmp_dir}):
                self.assertEqual(
                    cache_path(),
                    Path(tmp_dir) / "tiptoi-linux" / "tiptoi.csv",
                )

    def test_falls_back_when_xdg_cache_home_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("XDG_CACHE_HOME", None)
            expected = Path.home() / ".cache" / "tiptoi-linux" / "tiptoi.csv"
            self.assertEqual(cache_path(), expected)

    def test_falls_back_when_xdg_cache_home_empty(self) -> None:
        with patch.dict("os.environ", {"XDG_CACHE_HOME": ""}):
            expected = Path.home() / ".cache" / "tiptoi-linux" / "tiptoi.csv"
            self.assertEqual(cache_path(), expected)


class DownloadCatalogTests(unittest.TestCase):
    def test_rejects_non_https_url(self) -> None:
        with self.assertRaises(CatalogError):
            download_catalog("http://cdn.ravensburger.de/db/tiptoi.csv")

    def test_opener_url_error_raises_catalog_error(self) -> None:
        # WHY: this is what common.HTTPSOnlyRedirectHandler raises on an https->http redirect -
        # verifies download_catalog wraps it into CatalogError via its existing except OSError
        # (URLError is an OSError subclass)
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache = Path(tmp_dir) / "tiptoi.csv"

            def fake_open(url, timeout=None):  # noqa: ANN001
                raise urllib.error.URLError(
                    "refusing to follow redirect to non-https URL: http://evil.example/tiptoi.csv"
                )

            with patch.object(common.https_opener, "open", side_effect=fake_open):
                with self.assertRaises(CatalogError):
                    download_catalog(cache=cache)


class RedirectHandlerTests(unittest.TestCase):
    def test_rejects_non_https_redirect_target(self) -> None:
        handler = HTTPSOnlyRedirectHandler()
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(None, None, 302, "Found", {}, "http://evil.example/tiptoi.csv")


class DownloadCatalogBehaviourTests(unittest.TestCase):
    def test_oversized_response_raises(self) -> None:
        oversized = b"a" * (catalog.MAX_CATALOG_BYTES + 1)
        fake_response = io.BytesIO(oversized)
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache = Path(tmp_dir) / "tiptoi.csv"
            with patch.object(common.https_opener, "open") as mock_open:
                mock_open.return_value.__enter__.return_value = fake_response
                with self.assertRaises(CatalogError):
                    download_catalog(cache=cache)

    def test_zero_product_response_leaves_existing_cache_untouched(self) -> None:
        empty_csv = "CSV file version,Firmware version,Firmware checksum,Firmware download address\n"
        fake_response = io.BytesIO(empty_csv.encode("latin-1"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache = Path(tmp_dir) / "tiptoi.csv"
            cache.write_text(FIXTURE_CSV, encoding="latin-1")

            with patch.object(common.https_opener, "open") as mock_open:
                mock_open.return_value.__enter__.return_value = fake_response
                with self.assertRaises(CatalogError):
                    download_catalog(cache=cache)

            self.assertEqual(cache.read_text(encoding="latin-1"), FIXTURE_CSV)

    def test_writes_to_injected_cache_and_never_touches_real_cache_path(self) -> None:
        fake_response = io.BytesIO(FIXTURE_CSV.encode("latin-1"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            injected_cache = Path(tmp_dir) / "tiptoi.csv"
            with patch.object(
                catalog,
                "cache_path",
                side_effect=AssertionError("cache_path() must not be called when cache is injected"),
            ):
                with patch.object(common.https_opener, "open") as mock_open:
                    mock_open.return_value.__enter__.return_value = fake_response
                    result = download_catalog(cache=injected_cache)

            self.assertEqual(len(result.products), 6)
            self.assertEqual(injected_cache.read_text(encoding="latin-1"), FIXTURE_CSV)


class LoadCatalogFallbackTests(unittest.TestCase):
    def test_allow_stale_false_reraises_on_download_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache = Path(tmp_dir) / "tiptoi.csv"
            cache.write_text(FIXTURE_CSV, encoding="latin-1")
            with patch.object(catalog, "download_catalog", side_effect=CatalogError("boom")):
                with self.assertRaises(CatalogError):
                    catalog.load_catalog(cache=cache, allow_stale=False, force=True)

    def test_allow_stale_true_falls_back_to_stale_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache = Path(tmp_dir) / "tiptoi.csv"
            cache.write_text(FIXTURE_CSV, encoding="latin-1")
            with patch.object(catalog, "download_catalog", side_effect=CatalogError("boom")):
                result = catalog.load_catalog(cache=cache, allow_stale=True, force=True)
            self.assertEqual(len(result.products), 6)


if __name__ == "__main__":
    unittest.main()
