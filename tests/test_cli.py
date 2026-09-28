from __future__ import annotations

import contextlib
import errno
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from tiptoi_linux import cli
from tiptoi_linux.catalog import Catalog, LoadedCatalog, Product
from tiptoi_linux.pen import Pen, PenError, PenSummary, Phase

PEN = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
CATALOG = Catalog(products=(Product(version="20200101", url="https://x/WN.gme", name="WN"),))


def _run(argv: list[str], *, stdin_tty: bool = True, answer: str = "") -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with (
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
        patch.object(cli.sys.stdin, "isatty", return_value=stdin_tty),
        patch("builtins.input", return_value=answer),
    ):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class PenDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.deleted: list[str] = []

        def fake_delete(pen: Pen, name: str) -> int:
            self.deleted.append(name)
            return 1024

        patches = [
            patch.object(cli, "find_pen", return_value=PEN),
            patch.object(cli, "installed_gme_files", return_value=("WN.gme", "Tiere.gme")),
            patch.object(cli, "delete_title", side_effect=fake_delete),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_resolves_case_insensitively_and_deduplicates(self) -> None:
        code, out, _err = _run(["pen", "delete", "wn.gme", "WN.GME", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(self.deleted, ["WN.gme"])
        self.assertIn("Freed 1.0 KiB", out)

    def test_unknown_title_deletes_nothing(self) -> None:
        code, _out, err = _run(["pen", "delete", "WN.gme", "Nope.gme", "--yes"])
        self.assertEqual(code, 1)
        self.assertEqual(self.deleted, [])
        self.assertIn("Nope.gme", err)

    def test_refuses_without_yes_when_stdin_is_not_a_tty(self) -> None:
        code, _out, err = _run(["pen", "delete", "WN.gme"], stdin_tty=False)
        self.assertEqual(code, 1)
        self.assertEqual(self.deleted, [])
        self.assertIn("--yes", err)

    def test_prompt_declined(self) -> None:
        code, _out, _err = _run(["pen", "delete", "WN.gme"], answer="n")
        self.assertEqual(code, 1)
        self.assertEqual(self.deleted, [])

    def test_prompt_accepted(self) -> None:
        code, _out, _err = _run(["pen", "delete", "WN.gme"], answer="Yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.deleted, ["WN.gme"])


class ErrorReportingTests(unittest.TestCase):
    def test_tiptoi_error_is_reported_without_traceback(self) -> None:
        with patch.object(cli, "find_pen", side_effect=PenError("no tiptoi pen found")):
            code, _out, err = _run(["pen", "status"])
        self.assertEqual(code, 1)
        self.assertEqual(err, "error: no tiptoi pen found\n")

    def test_stray_oserror_is_reported_without_traceback(self) -> None:
        with patch.object(cli, "find_pen", side_effect=OSError(errno.EIO, "Input/output error")):
            code, _out, err = _run(["pen", "status"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "))
        self.assertNotIn("Traceback", err)


class PenStatusTests(unittest.TestCase):
    def test_prints_summary(self) -> None:
        summary = PenSummary(pen=PEN, free=2048, total=4096, installed=("WN.gme",), outdated=[])
        with (
            patch.object(cli, "find_pen", return_value=PEN),
            patch.object(cli, "pen_summary", return_value=summary),
        ):
            code, out, _err = _run(["pen", "status"])
        self.assertEqual(code, 0)
        self.assertIn("Mountpoint: /media/tiptoi", out)
        self.assertIn("Free space: 2.0 KiB of 4.0 KiB", out)
        self.assertIn("  WN.gme", out)


class StaleCatalogTests(unittest.TestCase):
    def test_stale_catalog_prints_warning_and_still_lists(self) -> None:
        with patch.object(cli, "load_catalog", return_value=LoadedCatalog(CATALOG, stale_reason="offline")):
            code, out, err = _run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("WN", out)
        self.assertIn("warning: offline", err)

    def test_fresh_catalog_prints_no_warning(self) -> None:
        with patch.object(cli, "load_catalog", return_value=LoadedCatalog(CATALOG)):
            _code, _out, err = _run(["list"])
        self.assertEqual(err, "")


class HumanSizeTests(unittest.TestCase):
    def test_cases(self) -> None:
        cases = {
            0: "0 B",
            1023: "1023 B",
            1024: "1.0 KiB",
            1536: "1.5 KiB",
            1024**2: "1.0 MiB",
            1024**3: "1.0 GiB",
            1024**4: "1.0 TiB",
        }
        for num_bytes, expected in cases.items():
            with self.subTest(num_bytes=num_bytes):
                self.assertEqual(cli._human_size(num_bytes), expected)


class PhaseLabelTests(unittest.TestCase):
    def test_every_phase_has_a_label(self) -> None:
        self.assertEqual(set(cli.PHASE_LABELS), set(Phase))


if __name__ == "__main__":
    unittest.main()
