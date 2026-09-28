from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tiptoi_linux import common, pen
from tiptoi_linux.catalog import Catalog, Product, parse_catalog
from tiptoi_linux.pen import Pen, PenError, PenSummary

_HEADER_PREFIX = b"1CHOMPTECH DATA FORMAT CopyRight 2009 Ver2.10.0901"


def _synthetic_gme(date: str = "") -> bytes:
    # WHY: mirrors the real .gme layout - ASCII header at 0x20, NUL-terminated, date as the
    # trailing 8 characters before the NUL
    return b"\0" * 0x20 + _HEADER_PREFIX + date.encode() + b"\0" + b"\0" * 64


FIXTURE_CSV = (
    "CSV file version,Firmware version,Firmware checksum,Firmware download address\n"
    "26091403,6GE027,1872396468,"
    "https://cdn.ravensburger.de/db/Firmware-Files/de/27/REV12/Update6E.upd\n"
    "Items._id,Items._version,Items._url,Items._fileName\n"
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
)


def _completed_process(payload: object, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["findmnt", "--json", "--list", "-o", "TARGET,SOURCE,FSTYPE,LABEL"],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr=stderr,
    )


class FindPenOverrideTests(unittest.TestCase):
    def test_override_used_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = pen.find_pen(override=Path(tmp_dir))
            self.assertEqual(result.mountpoint, Path(tmp_dir))

    def test_override_missing_directory_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing = Path(tmp_dir) / "does-not-exist"
            with self.assertRaises(PenError):
                pen.find_pen(override=missing)

    def test_override_into_protected_folder_raises_and_folder_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pen_root = Path(tmp_dir)
            system_dir = pen_root / "system"
            system_dir.mkdir()
            (system_dir / "keep.bin").write_bytes(b"firmware")

            with self.assertRaises(PenError):
                pen.find_pen(override=system_dir)

            self.assertTrue(system_dir.is_dir())
            self.assertEqual((system_dir / "keep.bin").read_bytes(), b"firmware")

    def test_override_into_protected_folder_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pen_root = Path(tmp_dir)
            songs_dir = pen_root / "SONGS"
            songs_dir.mkdir()
            with self.assertRaises(PenError):
                pen.find_pen(override=songs_dir)


class FindPenDetectionTests(unittest.TestCase):
    def test_selects_labelled_vfat_entry(self) -> None:
        payload = {
            "filesystems": [
                {"target": "/", "source": "/dev/sda1", "fstype": "ext4", "label": None},
                {"target": "/media/tiptoi", "source": "/dev/sdb1", "fstype": "vfat", "label": "TIPTOI"},
            ]
        }
        with patch.object(pen.subprocess, "run", return_value=_completed_process(payload)):
            result = pen.find_pen()
        self.assertEqual(result.mountpoint, Path("/media/tiptoi"))
        self.assertEqual(result.source, "/dev/sdb1")

    def test_handles_null_label_and_fstype_without_raising(self) -> None:
        payload = {
            "filesystems": [
                {"target": "/mnt/x", "source": "/dev/sdc1", "fstype": None, "label": None},
                {"target": "/media/tiptoi", "source": "/dev/sdb1", "fstype": "vfat", "label": "tiptoi"},
            ]
        }
        with patch.object(pen.subprocess, "run", return_value=_completed_process(payload)):
            result = pen.find_pen()
        self.assertEqual(result.mountpoint, Path("/media/tiptoi"))

    def test_zero_matches_raises(self) -> None:
        payload = {"filesystems": [{"target": "/", "source": "/dev/sda1", "fstype": "ext4", "label": None}]}
        with patch.object(pen.subprocess, "run", return_value=_completed_process(payload)):
            with self.assertRaises(PenError):
                pen.find_pen()

    def test_multiple_matches_raises(self) -> None:
        payload = {
            "filesystems": [
                {"target": "/media/a", "source": "/dev/sdb1", "fstype": "vfat", "label": "tiptoi"},
                {"target": "/media/b", "source": "/dev/sdc1", "fstype": "vfat", "label": "tiptoi"},
            ]
        }
        with patch.object(pen.subprocess, "run", return_value=_completed_process(payload)):
            with self.assertRaises(PenError):
                pen.find_pen()

    def test_missing_findmnt_binary_raises(self) -> None:
        with patch.object(pen.subprocess, "run", side_effect=FileNotFoundError()):
            with self.assertRaises(PenError):
                pen.find_pen()

    def test_nonzero_returncode_raises(self) -> None:
        with patch.object(pen.subprocess, "run", return_value=_completed_process({}, returncode=1, stderr="boom")):
            with self.assertRaises(PenError):
                pen.find_pen()

    def test_unparseable_json_raises(self) -> None:
        bad_result = subprocess.CompletedProcess(args=["findmnt"], returncode=0, stdout="not json{{{", stderr="")
        with patch.object(pen.subprocess, "run", return_value=bad_result):
            with self.assertRaises(PenError):
                pen.find_pen()

    def test_entry_missing_target_is_skipped_not_a_crash(self) -> None:
        payload = {
            "filesystems": [
                {"source": "/dev/sdb1", "fstype": "vfat", "label": "tiptoi"},  # no "target"
            ]
        }
        with patch.object(pen.subprocess, "run", return_value=_completed_process(payload)):
            with self.assertRaises(PenError):
                pen.find_pen()

    def test_dict_valued_filesystems_is_treated_as_empty(self) -> None:
        payload = {"filesystems": {"target": "/media/tiptoi", "source": "/dev/sdb1", "fstype": "vfat", "label": "tiptoi"}}
        with patch.object(pen.subprocess, "run", return_value=_completed_process(payload)):
            with self.assertRaises(PenError):
                pen.find_pen()

    def test_duplicated_bind_mount_same_source_does_not_raise_multiple(self) -> None:
        payload = {
            "filesystems": [
                {"target": "/media/tiptoi", "source": "/dev/sdb1", "fstype": "vfat", "label": "tiptoi"},
                {"target": "/mnt/bind/tiptoi", "source": "/dev/sdb1", "fstype": "vfat", "label": "tiptoi"},
            ]
        }
        with patch.object(pen.subprocess, "run", return_value=_completed_process(payload)):
            result = pen.find_pen()
        self.assertEqual(result.source, "/dev/sdb1")

    def test_fuseblk_fstype_is_accepted(self) -> None:
        payload = {
            "filesystems": [
                {"target": "/media/tiptoi", "source": "/dev/sdb1", "fstype": "fuseblk", "label": "tiptoi"},
            ]
        }
        with patch.object(pen.subprocess, "run", return_value=_completed_process(payload)):
            result = pen.find_pen()
        self.assertEqual(result.mountpoint, Path("/media/tiptoi"))


class InstallProtectedNamesTests(unittest.TestCase):
    def test_protected_names_rejected_and_system_dir_survives(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            (pen_root / "system").mkdir()
            fake_pen = Pen(mountpoint=pen_root, source="")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"data")

            # WHY: bare "system"/"songs"/"stories" are rejected by the .gme-suffix check, not the
            # protected-name branch - these actually exercise PROTECTED_NAMES via the stem check
            for bad_name in ("system.gme", "SONGS.gme", "Stories.GME", "../escape.gme"):
                with self.subTest(bad_name=bad_name):
                    with self.assertRaises(PenError):
                        pen.install_product(fake_pen, source, file_name=bad_name)

            self.assertTrue((pen_root / "system").is_dir())


class DetectedPenMountCheckTests(unittest.TestCase):
    def test_unmounted_detected_pen_refuses_install(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="/dev/sdx1")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"data")

            with patch.object(pen.os.path, "ismount", return_value=False):
                with self.assertRaises(PenError):
                    pen.install_product(fake_pen, source, file_name="title.gme")

    def test_override_pen_skips_mount_check(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"data")

            with patch.object(pen.os.path, "ismount", return_value=False):
                destination = pen.install_product(fake_pen, source, file_name="title.gme")

            self.assertEqual(destination.read_bytes(), b"data")


class FreeSpaceTests(unittest.TestCase):
    def test_insufficient_space_raises_and_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"x" * 1000)

            real_usage = shutil.disk_usage(pen_dir)
            tiny_free_usage = real_usage._replace(free=10)
            with patch.object(pen.shutil, "disk_usage", return_value=tiny_free_usage):
                with self.assertRaises(PenError):
                    pen.install_product(fake_pen, source, file_name="title.gme")

            self.assertEqual(list(pen_root.iterdir()), [])


class DiskSpaceTests(unittest.TestCase):
    def test_returns_free_and_total_from_disk_usage(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            fake_usage = shutil.disk_usage(pen_dir)._replace(free=111, total=222)
            with patch.object(pen.shutil, "disk_usage", return_value=fake_usage) as mock_usage:
                result = pen.disk_space(fake_pen)

            mock_usage.assert_called_once_with(pen_root)
            self.assertEqual(result, (111, 222))


class PenStillMountedTests(unittest.TestCase):
    def test_detected_pen_defers_to_ismount(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            fake_pen = Pen(mountpoint=Path(pen_dir), source="/dev/sdb1")

            with patch.object(pen.os.path, "ismount", return_value=True) as mock_ismount:
                self.assertTrue(pen.pen_still_mounted(fake_pen))
            mock_ismount.assert_called_once_with(fake_pen.mountpoint)

            with patch.object(pen.os.path, "ismount", return_value=False):
                self.assertFalse(pen.pen_still_mounted(fake_pen))

    def test_override_pen_checks_directory_existence_instead(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            with patch.object(pen.os.path, "ismount", return_value=False):
                # WHY: an override pen has no "source" device to check via ismount() - proves the
                # override path never consults it, only pen.mountpoint.is_dir()
                self.assertTrue(pen.pen_still_mounted(fake_pen))

            missing_pen = Pen(mountpoint=pen_root / "does-not-exist", source="")
            self.assertFalse(pen.pen_still_mounted(missing_pen))


class PenDevicePresentTests(unittest.TestCase):
    def test_true_when_by_label_symlink_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_path = Path(tmp_dir) / "tiptoi"
            fake_path.touch()
            with patch.object(pen, "PEN_BY_LABEL_PATH", fake_path):
                self.assertTrue(pen.pen_device_present())

    def test_false_when_by_label_symlink_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_path = Path(tmp_dir) / "tiptoi"
            with patch.object(pen, "PEN_BY_LABEL_PATH", fake_path):
                self.assertFalse(pen.pen_device_present())


class DryRunTests(unittest.TestCase):
    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"data")

            before = sorted(pen_root.iterdir())
            destination = pen.install_product(
                fake_pen, source, file_name="title.gme", dry_run=True
            )
            after = sorted(pen_root.iterdir())

            self.assertEqual(before, after)
            self.assertEqual(before, [])
            self.assertEqual(destination, pen_root / "title.gme")


class InstallSucceedsTests(unittest.TestCase):
    def test_install_copies_file_and_leaves_only_the_title_on_the_pen(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"payload-bytes")

            destination = pen.install_product(fake_pen, source, file_name="title.gme")

            self.assertEqual(destination, pen_root / "title.gme")
            self.assertEqual(destination.read_bytes(), b"payload-bytes")
            self.assertEqual([p.name for p in pen_root.iterdir()], ["title.gme"])

    def test_directory_fsync_einval_is_tolerated_and_title_is_installed(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"payload-bytes")

            real_fsync = os.fsync

            def flaky_fsync(fd: int) -> None:
                # WHY: distinguish the directory fd from regular file fds by st_mode, not call
                # order, so this is robust regardless of how install_product sequences its fsyncs
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError(errno.EINVAL, "fsync not supported on directory")
                real_fsync(fd)

            with patch.object(pen.os, "fsync", side_effect=flaky_fsync):
                destination = pen.install_product(fake_pen, source, file_name="title.gme")

            self.assertEqual(destination.read_bytes(), b"payload-bytes")

    def test_directory_fsync_other_error_raises_pen_error(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"payload-bytes")

            real_fsync = os.fsync

            def flaky_fsync(fd: int) -> None:
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError(errno.EACCES, "permission denied")
                real_fsync(fd)

            with patch.object(pen.os, "fsync", side_effect=flaky_fsync):
                with self.assertRaises(PenError):
                    pen.install_product(fake_pen, source, file_name="title.gme")


class ReadGmeVersionTests(unittest.TestCase):
    def test_returns_date_from_synthetic_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "title.gme"
            path.write_bytes(_synthetic_gme("20260115"))
            self.assertEqual(pen.read_gme_version(path), "20260115")

    def test_short_file_yields_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "title.gme"
            path.write_bytes(b"\0" * 10)
            self.assertIsNone(pen.read_gme_version(path))

    def test_header_without_trailing_date_yields_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "title.gme"
            path.write_bytes(b"\0" * 0x20 + b"no date here" + b"\0" + b"\0" * 64)
            self.assertIsNone(pen.read_gme_version(path))

    def test_non_ascii_header_yields_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "title.gme"
            path.write_bytes(b"\0" * 0x20 + b"\xff\xfe not ascii 20260115" + b"\0")
            self.assertIsNone(pen.read_gme_version(path))


class OutdatedTitlesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = parse_catalog(FIXTURE_CSV)

    def test_up_to_date_header_is_not_listed(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            (pen_root / "Die Eiskoenigin.gme").write_bytes(_synthetic_gme("20150101"))
            fake_pen = Pen(mountpoint=pen_root, source="")

            result = pen.outdated_titles(fake_pen, self.catalog)
            self.assertEqual(result, [])

    def test_older_header_date_is_listed_with_installed_version(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            (pen_root / "Die Eiskoenigin.gme").write_bytes(_synthetic_gme("20111024"))
            fake_pen = Pen(mountpoint=pen_root, source="")

            result = pen.outdated_titles(fake_pen, self.catalog)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].file_name, "Die Eiskoenigin.gme")
            self.assertEqual(result[0].installed_version, "20111024")
            self.assertEqual(result[0].catalog_version, "20150101")

    def test_undated_header_is_listed_with_installed_version_none(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            (pen_root / "Die Eiskoenigin.gme").write_bytes(_synthetic_gme(""))
            fake_pen = Pen(mountpoint=pen_root, source="")

            result = pen.outdated_titles(fake_pen, self.catalog)
            self.assertEqual(len(result), 1)
            self.assertIsNone(result[0].installed_version)
            self.assertIn("(unknown)", result[0].describe())

    def test_file_with_no_catalog_match_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            (pen_root / "UnknownTitle.gme").write_bytes(_synthetic_gme("20260115"))
            fake_pen = Pen(mountpoint=pen_root, source="")

            result = pen.outdated_titles(fake_pen, self.catalog)
            self.assertEqual(result, [])

    def test_case_insensitive_file_name_matches_catalog_name(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            (pen_root / "CASE.GME").write_bytes(_synthetic_gme("20200101"))
            fake_pen = Pen(mountpoint=pen_root, source="")

            catalog = Catalog(
                csv_version="",
                firmware=None,
                products=(
                    Product(series_id="1", version="20200101", url="https://cdn.example.com/CASE.gme", name="CASE"),
                ),
            )

            result = pen.outdated_titles(fake_pen, catalog)
            self.assertEqual(result, [])

    def test_sorts_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            for name in ("zeta.gme", "Alpha.gme", "beta.gme"):
                (pen_root / name).write_bytes(_synthetic_gme("20111024"))
            fake_pen = Pen(mountpoint=pen_root, source="")

            catalog = Catalog(
                csv_version="",
                firmware=None,
                products=(
                    Product(series_id="1", version="9", url="https://cdn.example.com/zeta.gme", name="zeta"),
                    Product(series_id="2", version="9", url="https://cdn.example.com/Alpha.gme", name="Alpha"),
                    Product(series_id="3", version="9", url="https://cdn.example.com/beta.gme", name="beta"),
                ),
            )

            result = pen.outdated_titles(fake_pen, catalog)
            self.assertEqual([t.file_name for t in result], ["Alpha.gme", "beta.gme", "zeta.gme"])


class OutdatedTitleDescribeTests(unittest.TestCase):
    def test_describe_with_installed_version(self) -> None:
        title = pen.OutdatedTitle(file_name="A.gme", installed_version="1", catalog_version="2")
        self.assertEqual(title.describe(), "A.gme: installed 1, catalog 2")

    def test_describe_with_no_installed_version(self) -> None:
        title = pen.OutdatedTitle(file_name="A.gme", installed_version=None, catalog_version="2")
        self.assertEqual(title.describe(), "A.gme: installed (unknown), catalog 2")


class PenSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = parse_catalog(FIXTURE_CSV)

    def test_with_catalog_includes_outdated_titles(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            (pen_root / "Die Eiskoenigin.gme").write_bytes(_synthetic_gme("20111024"))
            fake_pen = Pen(mountpoint=pen_root, source="")

            summary = pen.pen_summary(fake_pen, self.catalog)

            self.assertIsInstance(summary, PenSummary)
            self.assertEqual(summary.pen, fake_pen)
            self.assertEqual(summary.installed, ("Die Eiskoenigin.gme",))
            self.assertEqual(len(summary.outdated), 1)
            self.assertEqual(summary.outdated[0].file_name, "Die Eiskoenigin.gme")
            expected_free, expected_total = pen.disk_space(fake_pen)
            self.assertEqual((summary.free, summary.total), (expected_free, expected_total))

    def test_without_catalog_yields_no_outdated_titles(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            (pen_root / "Die Eiskoenigin.gme").write_bytes(_synthetic_gme("20111024"))
            fake_pen = Pen(mountpoint=pen_root, source="")

            summary = pen.pen_summary(fake_pen, None)

            self.assertEqual(summary.outdated, [])
            self.assertEqual(summary.installed, ("Die Eiskoenigin.gme",))


class InstallTitleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.product = Product(
            series_id="150",
            version="20190529",
            url="https://cdn.ravensburger.de/db/applications/CREATE_Kreative_Bildergeschichten.gme",
            name="CREATE_Kreative_Bildergeschichten",
        )

    def test_downloads_and_installs_under_gme_file_name(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            downloaded = Path(src_dir) / "downloaded.gme"
            downloaded.write_bytes(b"payload-bytes")

            with patch.object(pen, "download_product", return_value=downloaded) as mock_download:
                destination = pen.install_title(fake_pen, self.product)

            mock_download.assert_called_once()
            expected_name = f"{self.product.name}.gme"
            self.assertEqual(destination, pen_root / expected_name)
            self.assertEqual(destination.read_bytes(), b"payload-bytes")

    def test_dry_run_writes_nothing_to_the_pen(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            downloaded = Path(src_dir) / "downloaded.gme"
            downloaded.write_bytes(b"payload-bytes")

            with patch.object(pen, "download_product", return_value=downloaded):
                destination = pen.install_title(fake_pen, self.product, dry_run=True)

            self.assertEqual(list(pen_root.iterdir()), [])
            self.assertEqual(destination, pen_root / f"{self.product.name}.gme")

    def test_install_title_maps_download_progress_to_phase_download(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            downloaded = Path(src_dir) / "downloaded.gme"
            downloaded.write_bytes(b"data")

            calls: list[tuple[str, int, int | None]] = []

            def record_progress(phase: str, done: int, total: int | None) -> None:
                calls.append((phase, done, total))

            def fake_download(product, *, cache_dir=None, progress=None):
                if progress is not None:
                    progress(1, 2)
                return downloaded

            with patch.object(pen, "download_product", side_effect=fake_download):
                pen.install_title(fake_pen, self.product, progress=record_progress)

            download_calls = [call for call in calls if call[0] == pen.PHASE_DOWNLOAD]
            self.assertEqual(download_calls, [(pen.PHASE_DOWNLOAD, 1, 2)])


class ByteVerificationTests(unittest.TestCase):
    def test_short_write_raises_and_leaves_old_title_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            (pen_root / "title.gme").write_bytes(b"old-title-bytes")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"new-payload-bytes")

            real_fstat = os.fstat

            class _FakeStat:
                def __init__(self, st_size: int) -> None:
                    self.st_size = st_size

            def lying_fstat(fd: int):
                # WHY: reports one byte fewer than was actually written, simulating a short write onto
                # the pen without needing to intercept the underlying OS write call itself
                return _FakeStat(real_fstat(fd).st_size - 1)

            with patch.object(pen.os, "fstat", side_effect=lying_fstat):
                with self.assertRaises(PenError):
                    pen.install_product(fake_pen, source, file_name="title.gme")

            self.assertEqual([p.name for p in pen_root.iterdir()], ["title.gme"])
            self.assertEqual((pen_root / "title.gme").read_bytes(), b"old-title-bytes")


class DestinationFsyncTests(unittest.TestCase):
    def test_fsyncs_destination_by_its_new_path_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"payload-bytes")

            real_fsync = os.fsync
            synced_paths: list[str] = []

            def recording_fsync(fd: int) -> None:
                try:
                    synced_paths.append(os.readlink(f"/proc/self/fd/{fd}"))
                except OSError:
                    pass
                real_fsync(fd)

            with patch.object(pen.os, "fsync", side_effect=recording_fsync):
                pen.install_product(fake_pen, source, file_name="title.gme")

            tmp_index = next(i for i, p in enumerate(synced_paths) if ".tiptoi-" in p)
            resolved_destination = str((pen_root / "title.gme").resolve())
            destination_index = next(i for i, p in enumerate(synced_paths) if p == resolved_destination)
            self.assertGreater(destination_index, tmp_index)


class ReadBackVerificationTests(unittest.TestCase):
    def test_successful_install_reads_back_matching_content(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"payload-bytes")

            destination = pen.install_product(fake_pen, source, file_name="title.gme")

            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_mismatched_read_back_raises_and_does_not_delete_destination(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"payload-bytes")

            with patch.object(pen, "_read_back_digest", return_value=(0, "mismatched-digest")):
                with self.assertRaises(PenError) as ctx:
                    pen.install_product(fake_pen, source, file_name="title.gme")

            self.assertIn("verification failed", str(ctx.exception))
            self.assertTrue((pen_root / "title.gme").exists())
            self.assertEqual((pen_root / "title.gme").read_bytes(), b"payload-bytes")

    def test_posix_fadvise_oserror_still_completes_install(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"payload-bytes")

            with patch.object(pen.os, "posix_fadvise", side_effect=OSError("not supported")):
                destination = pen.install_product(fake_pen, source, file_name="title.gme")

            self.assertEqual(destination.read_bytes(), b"payload-bytes")


class InstallProgressTests(unittest.TestCase):
    def test_copy_then_verify_progress_each_end_at_total(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as src_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            source = Path(src_dir) / "title.gme"
            source.write_bytes(b"x" * 5000)

            calls: list[tuple[str, int, int | None]] = []

            def record(phase: str, done: int, total: int | None) -> None:
                calls.append((phase, done, total))

            # WHY: a tiny chunk size forces many read() calls so an unthrottled implementation would
            # call progress far more than ~101 times per phase
            with patch.object(common, "CHUNK_BYTES", 100):
                pen.install_product(fake_pen, source, file_name="title.gme", progress=record)

            phases = [call[0] for call in calls]
            self.assertEqual(set(phases), {pen.PHASE_COPY, pen.PHASE_VERIFY})

            last_copy_index = max(i for i, phase in enumerate(phases) if phase == pen.PHASE_COPY)
            first_verify_index = min(i for i, phase in enumerate(phases) if phase == pen.PHASE_VERIFY)
            self.assertLess(last_copy_index, first_verify_index)

            copy_calls = [call for call in calls if call[0] == pen.PHASE_COPY]
            verify_calls = [call for call in calls if call[0] == pen.PHASE_VERIFY]
            self.assertEqual(copy_calls[-1][1], copy_calls[-1][2])
            self.assertEqual(verify_calls[-1][1], verify_calls[-1][2])
            self.assertLessEqual(len(copy_calls), 102)
            self.assertLessEqual(len(verify_calls), 102)


class DeleteTitleTests(unittest.TestCase):
    def _make_pen_with_protected_dirs(self, pen_root: Path) -> None:
        for name in ("system", "songs", "stories", "update", "System Volume Information"):
            (pen_root / name).mkdir()
        (pen_root / "notes.txt").write_bytes(b"not a title")

    def test_deletes_file_and_returns_its_size_leaving_the_rest_of_the_pen_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            self._make_pen_with_protected_dirs(pen_root)
            payload = b"payload-bytes"
            (pen_root / "title.gme").write_bytes(payload)
            fake_pen = Pen(mountpoint=pen_root, source="")

            freed = pen.delete_title(fake_pen, "title.gme")

            self.assertEqual(freed, len(payload))
            self.assertFalse((pen_root / "title.gme").exists())
            for name in ("system", "songs", "stories", "update", "System Volume Information"):
                self.assertTrue((pen_root / name).is_dir())
            self.assertEqual((pen_root / "notes.txt").read_bytes(), b"not a title")

    def test_refuses_protected_names(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")
            for bad_name in ("system.gme", "SONGS.gme", "Stories.GME"):
                with self.subTest(bad_name=bad_name):
                    with self.assertRaises(PenError):
                        pen.delete_title(fake_pen, bad_name)

    def test_refuses_path_traversal_names(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")
            for bad_name in ("../x.gme", "a/b.gme"):
                with self.subTest(bad_name=bad_name):
                    with self.assertRaises(PenError):
                        pen.delete_title(fake_pen, bad_name)

    def test_refuses_non_gme_name_and_leaves_it_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            (pen_root / "notes.txt").write_bytes(b"keep-me")
            fake_pen = Pen(mountpoint=pen_root, source="")

            with self.assertRaises(PenError):
                pen.delete_title(fake_pen, "notes.txt")

            self.assertEqual((pen_root / "notes.txt").read_bytes(), b"keep-me")

    def test_refuses_directory_named_like_a_title(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            (pen_root / "x.gme").mkdir()
            fake_pen = Pen(mountpoint=pen_root, source="")

            with self.assertRaises(PenError):
                pen.delete_title(fake_pen, "x.gme")

            self.assertTrue((pen_root / "x.gme").is_dir())

    def test_refuses_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")
            with self.assertRaises(PenError):
                pen.delete_title(fake_pen, "missing.gme")

    def test_refuses_detected_pen_whose_mountpoint_is_not_a_mount(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            (pen_root / "title.gme").write_bytes(b"data")
            fake_pen = Pen(mountpoint=pen_root, source="/dev/sdx1")

            with patch.object(pen.os.path, "ismount", return_value=False):
                with self.assertRaises(PenError):
                    pen.delete_title(fake_pen, "title.gme")

            self.assertTrue((pen_root / "title.gme").exists())


class MountPenTests(unittest.TestCase):
    def test_already_mounted_pen_is_returned_without_calling_udisksctl(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdc1")
        with patch.object(pen, "find_pen", return_value=fake_pen):
            with patch.object(pen.subprocess, "run") as mock_run:
                result = pen.mount_pen()
        mock_run.assert_not_called()
        self.assertEqual(result, fake_pen)

    def test_not_mounted_calls_udisksctl_mount_then_returns_find_pen_result(self) -> None:
        fake_device = Path("/dev/sdc")
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdc1")

        with patch.object(pen, "find_pen", side_effect=[PenError("no tiptoi pen found"), fake_pen]):
            with patch.object(pen, "pen_device", return_value=fake_device):
                with patch.object(
                    pen.subprocess, "run", return_value=subprocess.CompletedProcess(args=[], returncode=0)
                ) as mock_run:
                    result = pen.mount_pen()

        mock_run.assert_called_once_with(
            ["udisksctl", "mount", "--no-user-interaction", "-b", str(fake_device)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result, fake_pen)

    def test_no_device_raises_and_does_not_call_udisksctl(self) -> None:
        with patch.object(pen, "find_pen", side_effect=PenError("no tiptoi pen found")):
            with patch.object(pen, "pen_device", return_value=None):
                with patch.object(pen.subprocess, "run") as mock_run:
                    with self.assertRaises(PenError):
                        pen.mount_pen()
        mock_run.assert_not_called()


class UnmountPenTests(unittest.TestCase):
    def test_calls_sync_then_udisksctl_unmount(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdc1")
        calls: list[str] = []

        def record_sync() -> None:
            calls.append("sync")

        with patch.object(pen.os, "sync", side_effect=record_sync) as mock_sync:
            with patch.object(
                pen.subprocess, "run", return_value=subprocess.CompletedProcess(args=[], returncode=0)
            ) as mock_run:
                pen.unmount_pen(fake_pen)

        mock_sync.assert_called_once()
        mock_run.assert_called_once_with(
            ["udisksctl", "unmount", "--no-user-interaction", "-b", "/dev/sdc1"],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_override_pen_raises_and_does_not_call_subprocess(self) -> None:
        fake_pen = Pen(mountpoint=Path("/some/folder"), source="")
        with patch.object(pen.subprocess, "run") as mock_run:
            with self.assertRaises(PenError):
                pen.unmount_pen(fake_pen)
        mock_run.assert_not_called()

    def test_nonzero_exit_surfaces_stderr(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdc1")
        with patch.object(pen.os, "sync"):
            with patch.object(
                pen.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=1, stderr="Error unmounting /dev/sdc1: target is busy.\n"
                ),
            ):
                with self.assertRaises(PenError) as ctx:
                    pen.unmount_pen(fake_pen)
        self.assertIn("target is busy", str(ctx.exception))

    def test_missing_udisksctl_binary_raises(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdc1")
        with patch.object(pen.os, "sync"):
            with patch.object(pen.subprocess, "run", side_effect=FileNotFoundError()):
                with self.assertRaises(PenError):
                    pen.unmount_pen(fake_pen)

    def test_timeout_raises(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdc1")
        with patch.object(pen.os, "sync"):
            with patch.object(
                pen.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="udisksctl", timeout=60)
            ):
                with self.assertRaises(PenError):
                    pen.unmount_pen(fake_pen)


class PenDeviceTests(unittest.TestCase):
    def test_returns_resolved_path_when_symlink_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            real_device = Path(tmp_dir) / "sdc"
            real_device.touch()
            fake_link = Path(tmp_dir) / "tiptoi"
            fake_link.symlink_to(real_device)
            with patch.object(pen, "PEN_BY_LABEL_PATH", fake_link):
                self.assertEqual(pen.pen_device(), real_device.resolve())

    def test_returns_none_when_symlink_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_link = Path(tmp_dir) / "tiptoi"
            with patch.object(pen, "PEN_BY_LABEL_PATH", fake_link):
                self.assertIsNone(pen.pen_device())


class PenDeviceMountedTests(unittest.TestCase):
    def test_false_when_no_device(self) -> None:
        with patch.object(pen, "pen_device", return_value=None):
            self.assertFalse(pen.pen_device_mounted())

    def test_true_when_resolved_device_is_a_mounts_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            device = Path(tmp_dir) / "sdb1"
            device.touch()
            mounts_file = Path(tmp_dir) / "mounts"
            mounts_file.write_text(f"{device} /media/tiptoi vfat rw,relatime 0 0\n")

            with patch.object(pen, "pen_device", return_value=device):
                with patch.object(pen, "PROC_MOUNTS", mounts_file):
                    self.assertTrue(pen.pen_device_mounted())

    def test_false_when_device_not_among_mounts_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            device = Path(tmp_dir) / "sdb1"
            device.touch()
            other = Path(tmp_dir) / "sdc1"
            other.touch()
            mounts_file = Path(tmp_dir) / "mounts"
            mounts_file.write_text(f"{other} /media/other vfat rw 0 0\n")

            with patch.object(pen, "pen_device", return_value=device):
                with patch.object(pen, "PROC_MOUNTS", mounts_file):
                    self.assertFalse(pen.pen_device_mounted())

    def test_false_when_mounts_file_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            device = Path(tmp_dir) / "sdb1"
            device.touch()
            missing_mounts = Path(tmp_dir) / "does-not-exist"

            with patch.object(pen, "pen_device", return_value=device):
                with patch.object(pen, "PROC_MOUNTS", missing_mounts):
                    self.assertFalse(pen.pen_device_mounted())


class EmptyTrashTests(unittest.TestCase):
    def test_empties_both_trash_forms_and_returns_bytes_freed(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")
            uid = os.getuid()

            trash1_files = pen_root / f".Trash-{uid}" / "files"
            trash1_files.mkdir(parents=True)
            (trash1_files / "a.gme").write_bytes(b"12345")
            trash1_info = pen_root / f".Trash-{uid}" / "info"
            trash1_info.mkdir()
            trashinfo = b"[Trash Info]"
            (trash1_info / "a.gme.trashinfo").write_bytes(trashinfo)

            trash2_files = pen_root / ".Trash" / str(uid) / "files"
            trash2_files.mkdir(parents=True)
            (trash2_files / "b").write_bytes(b"1234567")

            freed = pen.empty_trash(fake_pen)

            self.assertEqual(freed, 5 + len(trashinfo) + 7)
            self.assertTrue((pen_root / f".Trash-{uid}").is_dir())
            self.assertEqual(list((pen_root / f".Trash-{uid}").iterdir()), [])
            self.assertTrue((pen_root / ".Trash" / str(uid)).is_dir())
            self.assertEqual(list((pen_root / ".Trash" / str(uid)).iterdir()), [])

    def test_other_uid_trash_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")
            other_uid = os.getuid() + 12345

            other_dir = pen_root / f".Trash-{other_uid}" / "files"
            other_dir.mkdir(parents=True)
            (other_dir / "keep.gme").write_bytes(b"keep-me")

            freed = pen.empty_trash(fake_pen)

            self.assertEqual(freed, 0)
            self.assertEqual((other_dir / "keep.gme").read_bytes(), b"keep-me")

    def test_symlinked_trash_dir_is_skipped_and_outside_target_survives(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir, tempfile.TemporaryDirectory() as outside_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")
            uid = os.getuid()

            outside_root = Path(outside_dir)
            (outside_root / "secret.txt").write_bytes(b"do-not-touch")

            (pen_root / f".Trash-{uid}").symlink_to(outside_root, target_is_directory=True)

            freed = pen.empty_trash(fake_pen)

            self.assertEqual(freed, 0)
            self.assertEqual((outside_root / "secret.txt").read_bytes(), b"do-not-touch")

    def test_no_trash_directories_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as pen_dir:
            pen_root = Path(pen_dir)
            fake_pen = Pen(mountpoint=pen_root, source="")

            self.assertEqual(pen.empty_trash(fake_pen), 0)


if __name__ == "__main__":
    unittest.main()
