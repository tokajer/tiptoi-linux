from __future__ import annotations

import gc
import os
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEvent, QObject, Qt

    _PYSIDE6_AVAILABLE = True
except ImportError:
    _PYSIDE6_AVAILABLE = False

from tiptoi_linux.catalog import CatalogError, parse_catalog
from tiptoi_linux.download import gme_file_name
from tiptoi_linux.gui import Column
from tiptoi_linux.pen import OutdatedTitle, Pen, PenError, PenSummary

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

EMPTY_CSV = "CSV file version,Firmware version,Firmware checksum,Firmware download address\n"


def _pump_until_idle(app, job, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while job.is_running and time.monotonic() < deadline:
        app.processEvents()
    for _ in range(20):
        app.processEvents()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

        from tiptoi_linux.gui import ProductTableModel

        cls.ProductTableModel = ProductTableModel

    def setUp(self) -> None:
        self.catalog = parse_catalog(FIXTURE_CSV)
        self.model = self.ProductTableModel(self.catalog.products)

    def test_row_and_column_count(self) -> None:
        self.assertEqual(self.model.rowCount(), 5)
        self.assertEqual(self.model.columnCount(), 3)

    def test_header_data(self) -> None:
        self.assertEqual(self.model.headerData(0, Qt.Orientation.Horizontal), "On pen")
        self.assertEqual(self.model.headerData(1, Qt.Orientation.Horizontal), "Name")
        self.assertEqual(self.model.headerData(2, Qt.Orientation.Horizontal), "Version")

    def test_header_data_out_of_range_section(self) -> None:
        self.assertIsNone(self.model.headerData(3, Qt.Orientation.Horizontal))
        self.assertIsNone(self.model.headerData(-1, Qt.Orientation.Horizontal))

    def test_display_role_data_for_known_row(self) -> None:
        product = self.catalog.by_name("CREATE_Kreative_Bildergeschichten")
        row = self.catalog.products.index(product)
        on_pen_index = self.model.index(row, Column.ON_PEN)
        name_index = self.model.index(row, Column.NAME)
        version_index = self.model.index(row, Column.VERSION)
        self.assertEqual(
            self.model.data(name_index, Qt.ItemDataRole.DisplayRole),
            "CREATE Kreative Bildergeschichten",
        )
        self.assertEqual(self.model.data(name_index, Qt.ItemDataRole.ToolTipRole), product.name)
        self.assertEqual(self.model.data(version_index, Qt.ItemDataRole.DisplayRole), product.version)
        self.assertEqual(self.model.data(on_pen_index, Qt.ItemDataRole.DisplayRole), "")
        self.assertIsNone(self.model.data(on_pen_index, Qt.ItemDataRole.ToolTipRole))

    def test_set_installed_marks_matching_rows_case_insensitively(self) -> None:
        product = self.catalog.products[0]
        file_name = gme_file_name(product)

        changed: list[int] = []
        self.model.dataChanged.connect(lambda *_args: changed.append(1))

        self.model.set_installed([file_name.upper()])

        self.assertEqual(len(changed), 1)
        row = self.catalog.products.index(product)
        on_pen_index = self.model.index(row, Column.ON_PEN)
        self.assertEqual(self.model.data(on_pen_index, Qt.ItemDataRole.DisplayRole), "✓")
        self.assertEqual(self.model.data(on_pen_index, Qt.ItemDataRole.ToolTipRole), "On the pen")
        self.assertEqual(
            self.model.data(on_pen_index, Qt.ItemDataRole.AccessibleTextRole), "On the pen"
        )

        other_row = (row + 1) % len(self.catalog.products)
        other_index = self.model.index(other_row, Column.ON_PEN)
        self.assertEqual(self.model.data(other_index, Qt.ItemDataRole.DisplayRole), "")

    def test_set_installed_with_identical_set_is_a_no_op(self) -> None:
        product = self.catalog.products[0]
        file_name = gme_file_name(product)
        self.model.set_installed([file_name])

        changed: list[int] = []
        self.model.dataChanged.connect(lambda *_args: changed.append(1))
        self.model.set_installed([file_name.casefold()])

        self.assertEqual(changed, [])

    def test_product_at_valid_and_out_of_range(self) -> None:
        self.assertEqual(self.model.product_at(0), self.catalog.products[0])
        self.assertIsNone(self.model.product_at(-1))
        self.assertIsNone(self.model.product_at(len(self.catalog.products)))

    def test_empty_catalog_yields_zero_rows(self) -> None:
        empty_catalog = parse_catalog(EMPTY_CSV)
        model = self.ProductTableModel(empty_catalog.products)
        self.assertEqual(model.rowCount(), 0)
        self.assertIsNone(model.product_at(0))

    def test_set_products_updates_row_count(self) -> None:
        self.assertEqual(self.model.rowCount(), 5)
        self.model.set_products(())
        self.assertEqual(self.model.rowCount(), 0)
        self.model.set_products(self.catalog.products)
        self.assertEqual(self.model.rowCount(), 5)

    def test_set_products_with_identical_catalog_is_a_no_op(self) -> None:
        reset_count = 0

        def _on_reset() -> None:
            nonlocal reset_count
            reset_count += 1

        self.model.modelReset.connect(_on_reset)
        same_products = tuple(self.catalog.products)
        self.model.set_products(same_products)
        self.assertEqual(reset_count, 0)
        self.assertEqual(self.model.rowCount(), 5)


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed")
class SearchFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

        from tiptoi_linux.gui import MainWindow

        cls.MainWindow = MainWindow

    def setUp(self) -> None:
        self.catalog = parse_catalog(FIXTURE_CSV)
        self.window = self.MainWindow()
        self.window._model.set_products(self.catalog.products)

    def tearDown(self) -> None:
        self.window.close()

    def _visible_names(self) -> set[str]:
        proxy = self.window._proxy
        names = set()
        for row in range(proxy.rowCount()):
            index = proxy.index(row, Column.NAME)
            names.add(proxy.data(index, Qt.ItemDataRole.DisplayRole))
        return names

    def test_filter_matches_catalog_search(self) -> None:
        for term in ("WiSsEn", "eisk"):
            with self.subTest(term=term):
                self.window._search_box.setText(term)
                expected = {p.name.replace("_", " ") for p in self.catalog.search(term)}
                self.assertEqual(self._visible_names(), expected)
                self.assertTrue(expected)

    def test_empty_filter_shows_all_products(self) -> None:
        self.window._search_box.setText("")
        self.assertEqual(
            self._visible_names(), {p.name.replace("_", " ") for p in self.catalog.products}
        )


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed")
class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

        from tiptoi_linux.gui import MainWindow

        cls.MainWindow = MainWindow

    def setUp(self) -> None:
        self.window = self.MainWindow()
        self.window._poll_timer.stop()

    def tearDown(self) -> None:
        self.window.close()

    def test_header_chip_and_buttons_exist(self) -> None:
        self.assertEqual(self.window._connection_label.accessibleName(), "Pen connection")
        self.assertEqual(self.window._connection_label.text(), "○ Not connected")
        self.assertIn("Mount", self.window._mount_button.text())
        self.assertIn("Unmount", self.window._unmount_button.text())

    def test_splitter_has_two_panes(self) -> None:
        self.assertEqual(self.window._splitter.count(), 2)

    def test_count_labels_start_at_zero(self) -> None:
        self.assertEqual(self.window._installed_label.text(), "On the &pen (0)")
        self.assertEqual(self.window._outdated_label.text(), "&Outdated (0)")


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed")
class ThreadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

        import tiptoi_linux.gui as gui_module

        cls.gui_module = gui_module

    def setUp(self) -> None:
        self.catalog = parse_catalog(FIXTURE_CSV)
        self._original_load_catalog = self.gui_module.load_catalog
        self.window = self.gui_module.MainWindow()

    def tearDown(self) -> None:
        self.gui_module.load_catalog = self._original_load_catalog
        _pump_until_idle(self.app, self.window._catalog_job)
        self.window.close()
        for _ in range(20):
            self.app.processEvents()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_successful_load_populates_model_and_status_bar(self) -> None:
        self.gui_module.load_catalog = lambda **kwargs: self.catalog
        self.window._start_load(force=True)
        _pump_until_idle(self.app, self.window._catalog_job)

        self.assertFalse(self.window._catalog_job.is_running)
        self.assertEqual(self.window._model.rowCount(), len(self.catalog.products))
        self.assertIn(str(len(self.catalog.products)), self.window.statusBar().currentMessage())

    def test_failed_load_shows_error_and_reenables_button(self) -> None:
        def _raise(**kwargs):
            raise CatalogError("boom")

        self.gui_module.load_catalog = _raise

        with patch.object(self.gui_module.QMessageBox, "warning"):
            self.window._start_load(force=True)
            _pump_until_idle(self.app, self.window._catalog_job)

        self.assertFalse(self.window._catalog_job.is_running)
        self.assertIn("boom", self.window.statusBar().currentMessage())
        self.assertTrue(self.window._refresh_button.isEnabled())

    def test_no_thread_leak_after_repeated_loads(self) -> None:
        self.gui_module.load_catalog = lambda **kwargs: self.catalog

        thread_refs = []
        for _ in range(3):
            self.window._start_load(force=True)
            thread_refs.append(weakref.ref(self.window._catalog_job._thread))
            _pump_until_idle(self.app, self.window._catalog_job)

        for _ in range(20):
            self.app.processEvents()

        gc.collect()
        for ref in thread_refs:
            self.assertIsNone(ref())

    def test_close_event_hides_and_defers_when_catalog_job_running(self) -> None:
        self.window._catalog_job._thread = object()
        try:
            with (
                patch.object(self.window._catalog_job, "stop"),
                patch.object(self.window, "hide") as mock_hide,
            ):
                event = QEvent(QEvent.Type.Close)
                self.window.closeEvent(event)
        finally:
            self.window._catalog_job._thread = None

        self.assertFalse(event.isAccepted())
        mock_hide.assert_called_once()
        self.assertTrue(self.window._close_when_idle)

        with patch.object(self.window, "close") as mock_close:
            self.window._catalog_job.finished.emit()

        mock_close.assert_called_once()
        self.assertFalse(self.window._close_when_idle)

    def test_close_event_quits_app_once_deferred_catalog_job_finishes(self) -> None:
        # Regression: once the deferred catalog job finishes and _on_catalog_job_finished() calls
        # self.close(), the window was already hidden by the earlier closeEvent() - Qt's
        # quitOnLastWindowClosed never fires for a window that was hidden rather than closed, so
        # the event loop would run forever with nothing visible unless something quits explicitly.
        def slow_load(**kwargs):
            time.sleep(0.3)
            return self.catalog

        self.gui_module.load_catalog = slow_load
        self.window._start_load(force=True)
        self.assertTrue(self.window._catalog_job.is_running)

        with patch.object(self.window._catalog_job, "stop"):
            event = QEvent(QEvent.Type.Close)
            self.window.closeEvent(event)

        self.assertFalse(event.isAccepted())
        self.assertTrue(self.window.isHidden())
        self.assertTrue(self.window._catalog_job.is_running)
        self.assertTrue(self.window._close_when_idle)

        original_close = self.window.close
        close_results: list[bool] = []

        def recording_close():
            close_results.append(original_close())

        with (
            patch.object(self.window, "close", side_effect=recording_close),
            patch.object(self.gui_module.QApplication, "quit") as mock_quit,
        ):
            _pump_until_idle(self.app, self.window._catalog_job)

        mock_quit.assert_called_once()
        self.assertEqual(close_results, [True])
        self.assertFalse(self.window._close_when_idle)


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed")
class BackgroundJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

        import tiptoi_linux.gui as gui_module

        cls.gui_module = gui_module

    def setUp(self) -> None:
        self.owner = QObject()
        self.job = self.gui_module.BackgroundJob(self.owner)

    def tearDown(self) -> None:
        _pump_until_idle(self.app, self.job)

    def test_second_start_while_running_is_refused(self) -> None:
        # WHY: start() sets self._thread synchronously before returning, so a second call made
        # right after (with no event-loop turn in between) always sees a job already in flight -
        # no need to actually block the first task to make this deterministic
        started_first = self.job.start(lambda progress: "first", lambda result: None, lambda message: None)
        started_second = self.job.start(lambda progress: "second", lambda result: None, lambda message: None)

        self.assertTrue(started_first)
        self.assertFalse(started_second)

        _pump_until_idle(self.app, self.job)

    def test_finished_emitted_exactly_once_on_success(self) -> None:
        calls: list[None] = []
        self.job.finished.connect(lambda: calls.append(None))

        self.job.start(lambda progress: "ok", lambda result: None, lambda message: None)
        _pump_until_idle(self.app, self.job)

        self.assertEqual(len(calls), 1)

    def test_finished_emitted_exactly_once_on_failure(self) -> None:
        calls: list[None] = []
        self.job.finished.connect(lambda: calls.append(None))

        def failing_task(progress):
            raise PenError("boom")

        self.job.start(failing_task, lambda result: None, lambda message: None)
        _pump_until_idle(self.app, self.job)

        self.assertEqual(len(calls), 1)

    def test_on_success_and_on_failure_callbacks_receive_expected_values(self) -> None:
        results: list[object] = []
        failures: list[str] = []

        self.job.start(lambda progress: 42, results.append, failures.append)
        _pump_until_idle(self.app, self.job)

        self.assertEqual(results, [42])
        self.assertEqual(failures, [])

        def failing_task(progress):
            raise PenError("boom")

        self.job.start(failing_task, results.append, failures.append)
        _pump_until_idle(self.app, self.job)

        self.assertEqual(results, [42])
        self.assertEqual(failures, ["boom"])


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed")
class PenPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

        import tiptoi_linux.gui as gui_module

        cls.gui_module = gui_module

    def setUp(self) -> None:
        self.catalog = parse_catalog(FIXTURE_CSV)
        self._original_find_pen = self.gui_module.find_pen
        self._original_pen_summary = self.gui_module.pen_summary
        self._original_install_title = self.gui_module.install_title
        self._original_delete_title = self.gui_module.delete_title
        self._original_pen_device_present = self.gui_module.pen_device_present
        self._original_pen_still_mounted = self.gui_module.pen_still_mounted
        self._original_mount_pen = self.gui_module.mount_pen
        self._original_unmount_pen = self.gui_module.unmount_pen

        # WHY: default to "no device, pen stays mounted" so _update_connection_label() and
        # _poll_pen() never touch the real /dev/disk/by-label/tiptoi symlink unless a test
        # explicitly overrides one of these
        self.gui_module.pen_device_present = lambda: False
        self.gui_module.pen_still_mounted = lambda pen: True

        self.window = self.gui_module.MainWindow()
        self.window._poll_timer.stop()
        self.window._model.set_products(self.catalog.products)
        self.window._catalog = self.catalog

    def tearDown(self) -> None:
        self.gui_module.find_pen = self._original_find_pen
        self.gui_module.pen_summary = self._original_pen_summary
        self.gui_module.install_title = self._original_install_title
        self.gui_module.delete_title = self._original_delete_title
        self.gui_module.pen_device_present = self._original_pen_device_present
        self.gui_module.pen_still_mounted = self._original_pen_still_mounted
        self.gui_module.mount_pen = self._original_mount_pen
        self.gui_module.unmount_pen = self._original_unmount_pen
        _pump_until_idle(self.app, self.window._pen_job)
        self.window.close()
        for _ in range(20):
            self.app.processEvents()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_successful_detect_updates_installed_list_space_bar_and_connection_label(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        outdated = [OutdatedTitle(file_name="A.gme", installed_version="1", catalog_version="2")]
        summary = PenSummary(
            pen=fake_pen,
            free=1024 * 1024,
            total=4 * 1024 * 1024,
            installed=("A.gme", "B.gme"),
            outdated=outdated,
        )

        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: summary

        self.window._detect()
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertFalse(self.window._pen_job.is_running)
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.CONNECTED)
        self.assertEqual(
            [self.window._installed_list.item(i).text() for i in range(self.window._installed_list.count())],
            ["A.gme", "B.gme"],
        )
        self.assertEqual(self.window._space_bar.value(), 75)
        self.assertIn("free", self.window._space_bar.format())
        self.assertTrue(self.window._connection_label.text().startswith("● Connected"))
        self.assertIn("/media/tiptoi", self.window._connection_label.text())
        self.assertEqual(self.window._outdated_list.count(), 1)
        self.assertEqual(self.window._outdated_list.item(0).text(), "A.gme: installed 1, catalog 2")

    def test_successful_detect_shows_status_bar_success_message(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        summary = PenSummary(pen=fake_pen, free=0, total=0, installed=(), outdated=[])

        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: summary

        self.window._detect()
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertIn("/media/tiptoi", self.window.statusBar().currentMessage())

    def test_pen_error_from_detection_shows_message_and_reenables_button(self) -> None:
        def _raise(**kwargs):
            raise PenError("boom")

        self.gui_module.find_pen = _raise

        with patch.object(self.gui_module.QMessageBox, "warning"):
            self.window._detect()
            _pump_until_idle(self.app, self.window._pen_job)

        self.assertFalse(self.window._pen_job.is_running)
        self.assertIn("boom", self.window.statusBar().currentMessage())
        self.assertTrue(self.window._detect_button.isEnabled())

    def test_failed_detect_after_successful_detect_clears_pen_and_disables_install(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        summary = PenSummary(pen=fake_pen, free=0, total=0, installed=("A.gme",), outdated=[])

        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: summary

        self.window._detect()
        _pump_until_idle(self.app, self.window._pen_job)
        self.assertIsNotNone(self.window._pen)

        def _raise(**kwargs):
            raise PenError("unplugged")

        self.gui_module.find_pen = _raise

        with patch.object(self.gui_module.QMessageBox, "warning"):
            self.window._detect()
            _pump_until_idle(self.app, self.window._pen_job)

        self.assertIsNone(self.window._pen)
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.DISCONNECTED)
        self.assertEqual(self.window._connection_label.text(), "○ Not connected")
        self.assertEqual(self.window._installed_list.count(), 0)
        self.assertEqual(self.window._outdated_list.count(), 0)
        self.assertFalse(self.window._install_button.isEnabled())

    def test_count_labels_update_after_refresh_and_reset_after_clear(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        outdated = [OutdatedTitle(file_name="A.gme", installed_version="1", catalog_version="2")]
        summary = PenSummary(pen=fake_pen, free=0, total=0, installed=("A.gme", "B.gme"), outdated=outdated)

        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: summary

        self.window._detect()
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertEqual(self.window._installed_label.text(), "On the &pen (2)")
        self.assertEqual(self.window._outdated_label.text(), "&Outdated (1)")

        def _raise(**kwargs):
            raise PenError("unplugged")

        self.gui_module.find_pen = _raise

        with patch.object(self.gui_module.QMessageBox, "warning"):
            self.window._detect()
            _pump_until_idle(self.app, self.window._pen_job)

        self.assertEqual(self.window._installed_label.text(), "On the &pen (0)")
        self.assertEqual(self.window._outdated_label.text(), "&Outdated (0)")

    def test_install_failure_keeps_pen(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        summary = PenSummary(pen=fake_pen, free=0, total=0, installed=(), outdated=[])

        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: summary

        self.window._detect()
        _pump_until_idle(self.app, self.window._pen_job)
        self.assertIsNotNone(self.window._pen)

        self.window._table.selectRow(0)

        def _raise(pen, product, *, dry_run=False, progress=None):
            raise PenError("install boom")

        self.gui_module.install_title = _raise

        with patch.object(self.gui_module.QMessageBox, "warning"):
            self.window._start_install_selected()
            _pump_until_idle(self.app, self.window._pen_job)

        self.assertIsNotNone(self.window._pen)
        self.assertIn("install boom", self.window.statusBar().currentMessage())

    def test_install_success_shows_message_box_and_verified_status(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        summary = PenSummary(pen=fake_pen, free=0, total=0, installed=(), outdated=[])

        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: summary

        self.window._detect()
        _pump_until_idle(self.app, self.window._pen_job)
        self.assertIsNotNone(self.window._pen)

        self.window._table.selectRow(0)

        def fake_install_title(pen, product, *, dry_run=False, progress=None):
            return Path("/media/tiptoi/title.gme")

        self.gui_module.install_title = fake_install_title

        with patch.object(self.gui_module.QMessageBox, "information") as mock_information:
            self.window._start_install_selected()
            _pump_until_idle(self.app, self.window._pen_job)

        mock_information.assert_called_once()
        self.assertIn("verified", self.window.statusBar().currentMessage())

    def test_double_click_on_enabled_row_triggers_install(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        summary = PenSummary(pen=fake_pen, free=0, total=0, installed=(), outdated=[])

        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: summary

        self.window._detect()
        _pump_until_idle(self.app, self.window._pen_job)
        self.assertIsNotNone(self.window._pen)

        self.window._table.selectRow(0)
        self.assertTrue(self.window._install_button.isEnabled())
        expected_product = self.window._selected_product()

        recorded: list[object] = []

        def fake_install_title(pen, product, *, dry_run=False, progress=None):
            recorded.append(product)
            return Path("/media/tiptoi/title.gme")

        self.gui_module.install_title = fake_install_title

        index = self.window._proxy.index(0, self.gui_module.Column.NAME)

        with patch.object(self.gui_module.QMessageBox, "information") as mock_information:
            self.window._on_table_double_clicked(index)
            _pump_until_idle(self.app, self.window._pen_job)

        mock_information.assert_called_once()
        self.assertEqual(recorded, [expected_product])

    def test_double_click_does_nothing_while_install_button_disabled(self) -> None:
        recorded = []
        self.gui_module.install_title = lambda pen, product, *, dry_run=False, progress=None: recorded.append(
            product
        )

        self.window._table.selectRow(0)
        self.assertFalse(self.window._install_button.isEnabled())

        index = self.window._proxy.index(0, self.gui_module.Column.NAME)
        self.window._on_table_double_clicked(index)
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertEqual(recorded, [])
        self.assertFalse(self.window._pen_job.is_running)

    def test_on_task_progress_shows_copy_phase_in_bar_format_and_value(self) -> None:
        self.window._on_task_progress(self.gui_module.PHASE_COPY, 50, 100)

        self.assertIn("Copying", self.window._progress_bar.format())
        self.assertEqual(self.window._progress_bar.value(), 50)

    def test_install_button_disabled_without_pen_or_selection(self) -> None:
        self.assertFalse(self.window._install_button.isEnabled())

        self.window._table.selectRow(0)
        self.assertFalse(self.window._install_button.isEnabled())

        self.window._pen = Pen(mountpoint=Path("/media/tiptoi"), source="")
        self.window._table.clearSelection()
        self.assertFalse(self.window._install_button.isEnabled())

        self.window._table.selectRow(0)
        self.assertTrue(self.window._install_button.isEnabled())

    def test_install_stays_disabled_while_task_in_flight_even_on_selection_change(self) -> None:
        self.window._pen = Pen(mountpoint=Path("/media/tiptoi"), source="")
        self.window._table.selectRow(0)
        self.assertTrue(self.window._install_button.isEnabled())

        # WHY: simulate a task in flight without actually running one, to isolate the button-state
        # rule (G-3) from thread lifecycle timing
        self.window._pen_job._thread = object()
        try:
            self.window._update_install_button_enabled()
            self.assertFalse(self.window._install_button.isEnabled())

            self.window._table.clearSelection()
            self.window._table.selectRow(1)
            self.assertFalse(self.window._install_button.isEnabled())
        finally:
            self.window._pen_job._thread = None

    def test_delete_button_disabled_without_pen_or_selection(self) -> None:
        self.window._installed_list.addItem("A.gme")

        self.assertFalse(self.window._delete_button.isEnabled())

        self.window._installed_list.item(0).setSelected(True)
        self.assertFalse(self.window._delete_button.isEnabled())

        self.window._pen = Pen(mountpoint=Path("/media/tiptoi"), source="")
        self.window._installed_list.clearSelection()
        self.assertFalse(self.window._delete_button.isEnabled())

        self.window._installed_list.item(0).setSelected(True)
        self.assertTrue(self.window._delete_button.isEnabled())

    def test_delete_button_disabled_while_task_in_flight(self) -> None:
        self.window._pen = Pen(mountpoint=Path("/media/tiptoi"), source="")
        self.window._installed_list.addItem("A.gme")
        self.window._installed_list.item(0).setSelected(True)
        self.assertTrue(self.window._delete_button.isEnabled())

        # WHY: simulate a task in flight without actually running one, mirroring the install
        # button's equivalent test above
        self.window._pen_job._thread = object()
        try:
            self.window._update_delete_button_enabled()
            self.assertFalse(self.window._delete_button.isEnabled())
        finally:
            self.window._pen_job._thread = None

    def test_all_six_pen_buttons_disabled_while_pen_job_running(self) -> None:
        self.window._pen_job._thread = object()
        try:
            # mount's own precondition (device present, no pen) would otherwise enable it
            self.gui_module.pen_device_present = lambda: True
            self.window._pen = None
            self.window._update_action_buttons_enabled()
            self.assertFalse(self.window._detect_button.isEnabled())
            self.assertFalse(self.window._choose_button.isEnabled())
            self.assertFalse(self.window._mount_button.isEnabled())

            # unmount/install/delete's own preconditions (a detected pen, a selection) would
            # otherwise enable them
            self.window._pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
            self.window._table.selectRow(0)
            self.window._installed_list.addItem("A.gme")
            self.window._installed_list.item(0).setSelected(True)
            self.window._update_action_buttons_enabled()
            self.assertFalse(self.window._unmount_button.isEnabled())
            self.assertFalse(self.window._install_button.isEnabled())
            self.assertFalse(self.window._delete_button.isEnabled())
        finally:
            self.window._pen_job._thread = None
            self.window._pen = None

    def test_delete_confirmed_calls_delete_title_per_selected_title_and_refreshes(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="")
        self.window._pen = fake_pen
        self.window._installed_list.addItem("A.gme")
        self.window._installed_list.addItem("B.gme")
        # WHY: mirrors what a real prior detect leaves behind, so _update_installed_list() (which
        # no-ops when the new tuple equals the tracked one) actually clears the list below
        self.window._installed_names = ("A.gme", "B.gme")
        self.window._installed_list.item(0).setSelected(True)
        self.window._installed_list.item(1).setSelected(True)

        self.gui_module.pen_summary = lambda pen, catalog: PenSummary(
            pen=fake_pen, free=0, total=0, installed=(), outdated=[]
        )

        deleted: list[tuple[object, str]] = []

        def fake_delete_title(pen, file_name):
            deleted.append((pen, file_name))
            return 100

        self.gui_module.delete_title = fake_delete_title

        with patch.object(
            self.gui_module.QMessageBox,
            "question",
            return_value=self.gui_module.QMessageBox.StandardButton.Yes,
        ) as mock_question:
            self.window._start_delete_selected()
            _pump_until_idle(self.app, self.window._pen_job)

        mock_question.assert_called_once()
        self.assertEqual(len(deleted), 2)
        self.assertEqual({pen for pen, _name in deleted}, {fake_pen})
        self.assertEqual({name for _pen, name in deleted}, {"A.gme", "B.gme"})
        self.assertEqual(self.window._installed_list.count(), 0)
        self.assertIn("Deleted 2 titles", self.window.statusBar().currentMessage())

    def test_delete_declined_calls_delete_title_for_nothing(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="")
        self.window._pen = fake_pen
        self.window._installed_list.addItem("A.gme")
        self.window._installed_list.item(0).setSelected(True)

        called = False

        def fake_delete_title(pen, file_name):
            nonlocal called
            called = True
            return 0

        self.gui_module.delete_title = fake_delete_title

        with patch.object(
            self.gui_module.QMessageBox,
            "question",
            return_value=self.gui_module.QMessageBox.StandardButton.No,
        ):
            self.window._start_delete_selected()

        self.assertFalse(called)
        self.assertFalse(self.window._pen_job.is_running)
        self.assertEqual(self.window._installed_list.count(), 1)

    def test_close_event_ignored_while_task_thread_set(self) -> None:
        self.window._pen_job._thread = object()
        try:
            with patch.object(self.gui_module.QMessageBox, "information") as mock_information:
                event = QEvent(QEvent.Type.Close)
                self.window.closeEvent(event)
                self.assertFalse(event.isAccepted())
                mock_information.assert_called_once()
        finally:
            self.window._pen_job._thread = None

    def test_install_uses_correct_product_when_table_sorted_reverse(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="")
        self.window._pen = fake_pen

        self.window._table.sortByColumn(self.gui_module.Column.NAME, Qt.SortOrder.DescendingOrder)

        expected_source_index = self.window._proxy.mapToSource(self.window._proxy.index(0, 0))
        expected_product = self.window._model.product_at(expected_source_index.row())
        # sanity: proves the view row and the underlying model row actually differ here
        self.assertNotEqual(expected_source_index.row(), 0)

        self.window._table.selectRow(0)

        recorded: list[tuple[object, object]] = []

        def fake_install_title(pen, product, *, dry_run=False, progress=None):
            recorded.append((pen, product))
            return Path("/media/tiptoi") / f"{product.name}.gme"

        self.gui_module.install_title = fake_install_title
        self.gui_module.pen_summary = lambda pen, catalog: PenSummary(
            pen=pen, free=2048, total=4096, installed=(f"{expected_product.name}.gme",), outdated=[]
        )

        with patch.object(self.gui_module.QMessageBox, "information"):
            self.window._start_install_selected()
            _pump_until_idle(self.app, self.window._pen_job)

        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][0], fake_pen)
        self.assertEqual(recorded[0][1], expected_product)
        self.assertEqual(self.window._installed_list.count(), 1)
        self.assertEqual(self.window._installed_list.item(0).text(), f"{expected_product.name}.gme")

    def test_task_callbacks_run_on_gui_thread_even_when_lambdas(self) -> None:
        # Regression: a lambda on_success ran in the worker thread and segfaulted in libQt6Gui.
        from PySide6.QtCore import QThread

        on_gui_thread: dict[str, bool] = {}

        def record(name: str) -> None:
            on_gui_thread[name] = QThread.currentThread() is self.app.thread()

        self.window._run_task(lambda progress: "ok", lambda result: record("success"), "busy")
        _pump_until_idle(self.app, self.window._pen_job)

        def fail(progress):
            raise PenError("boom")

        self.window._run_task(fail, lambda result: None, "busy", on_failure=lambda message: record("failure"))
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertEqual(on_gui_thread, {"success": True, "failure": True})

    def test_second_run_task_call_while_in_flight_is_ignored(self) -> None:
        call_count = 0

        def counting_find_pen(**kwargs):
            nonlocal call_count
            call_count += 1
            return Pen(mountpoint=Path("/media/tiptoi"), source="")

        self.gui_module.find_pen = counting_find_pen
        self.gui_module.pen_summary = lambda pen, catalog: PenSummary(
            pen=pen, free=0, total=0, installed=(), outdated=[]
        )

        self.window._detect()
        self.window._detect()
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertEqual(call_count, 1)

    def test_no_thread_leak_after_repeated_tasks(self) -> None:
        self.gui_module.find_pen = lambda **kwargs: Pen(mountpoint=Path("/media/tiptoi"), source="")
        self.gui_module.pen_summary = lambda pen, catalog: PenSummary(
            pen=pen, free=0, total=0, installed=(), outdated=[]
        )

        thread_refs = []
        for _ in range(3):
            self.window._detect()
            thread_refs.append(weakref.ref(self.window._pen_job._thread))
            _pump_until_idle(self.app, self.window._pen_job)

        for _ in range(20):
            self.app.processEvents()

        gc.collect()
        for ref in thread_refs:
            self.assertIsNone(ref())

    def test_poll_pen_clears_pen_when_no_longer_mounted(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")

        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: PenSummary(
            pen=pen, free=1024, total=4096, installed=("A.gme",), outdated=[]
        )

        self.window._detect()
        _pump_until_idle(self.app, self.window._pen_job)
        self.assertIsNotNone(self.window._pen)
        self.assertEqual(self.window._installed_list.count(), 1)

        self.gui_module.pen_still_mounted = lambda pen: False

        with patch.object(self.gui_module.QMessageBox, "warning") as mock_warning:
            self.window._poll_pen()

        self.assertIsNone(self.window._pen)
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.DISCONNECTED)
        self.assertEqual(self.window._installed_list.count(), 0)
        self.assertEqual(self.window._connection_label.text(), "○ Not connected")
        self.assertEqual(self.window.statusBar().currentMessage(), "Pen disconnected")
        mock_warning.assert_not_called()

    def test_poll_pen_starts_quiet_detection_when_device_present(self) -> None:
        def _raise(**kwargs):
            raise PenError("boom")

        self.gui_module.find_pen = _raise
        self.gui_module.pen_device_present = lambda: True

        with patch.object(self.gui_module.QMessageBox, "warning") as mock_warning:
            self.window._poll_pen()
            _pump_until_idle(self.app, self.window._pen_job)

        mock_warning.assert_not_called()
        self.assertIsNone(self.window._pen)
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.PLUGGED_IN_UNMOUNTED)
        self.assertEqual(
            self.window._connection_label.text(),
            "○ Pen plugged in but not mounted — open it in your file manager",
        )

    def test_poll_pen_does_nothing_while_task_thread_set(self) -> None:
        call_count = 0

        def counting_find_pen(**kwargs):
            nonlocal call_count
            call_count += 1
            return Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")

        self.gui_module.find_pen = counting_find_pen
        self.gui_module.pen_device_present = lambda: True

        self.window._pen_job._thread = object()
        try:
            self.window._poll_pen()
        finally:
            self.window._pen_job._thread = None

        self.assertEqual(call_count, 0)

    def test_poll_pen_does_not_clear_override_pen_when_device_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_pen = Pen(mountpoint=Path(tmp_dir), source="")
            self.window._pen = fake_pen
            self.window._connection = self.gui_module.ConnectionState.CONNECTED
            self.gui_module.pen_device_present = lambda: False
            # WHY: override pens have no by-label device to key off - pen_still_mounted() falls back
            # to Path.is_dir(), which the real implementation (not the setUp default) exercises here
            self.gui_module.pen_still_mounted = self._original_pen_still_mounted

            self.window._poll_pen()

            self.assertIs(self.window._pen, fake_pen)

    def test_poll_device_absent_and_not_connected_sets_disconnected(self) -> None:
        self.gui_module.pen_device_present = lambda: False
        self.window._connection = self.gui_module.ConnectionState.PLUGGED_IN_UNMOUNTED

        self.window._poll_pen()

        self.assertIs(self.window._connection, self.gui_module.ConnectionState.DISCONNECTED)
        self.assertEqual(self.window._connection_label.text(), "○ Not connected")

    def test_manual_detect_failure_sets_plugged_in_unmounted_when_device_present(self) -> None:
        def _raise(**kwargs):
            raise PenError("boom")

        self.gui_module.find_pen = _raise
        self.gui_module.pen_device_present = lambda: True

        with patch.object(self.gui_module.QMessageBox, "warning") as mock_warning:
            self.window._detect()
            _pump_until_idle(self.app, self.window._pen_job)

        mock_warning.assert_called_once()
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.PLUGGED_IN_UNMOUNTED)

    def test_manual_detect_failure_sets_disconnected_when_device_absent(self) -> None:
        def _raise(**kwargs):
            raise PenError("boom")

        self.gui_module.find_pen = _raise
        self.gui_module.pen_device_present = lambda: False

        with patch.object(self.gui_module.QMessageBox, "warning") as mock_warning:
            self.window._detect()
            _pump_until_idle(self.app, self.window._pen_job)

        mock_warning.assert_called_once()
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.DISCONNECTED)

    def test_quiet_detect_failure_sets_state_without_message_box(self) -> None:
        def _raise(**kwargs):
            raise PenError("boom")

        self.gui_module.find_pen = _raise
        self.gui_module.pen_device_present = lambda: True

        with patch.object(self.gui_module.QMessageBox, "warning") as mock_warning:
            self.window._detect(quiet=True)
            _pump_until_idle(self.app, self.window._pen_job)

        mock_warning.assert_not_called()
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.PLUGGED_IN_UNMOUNTED)
        self.assertIn("boom", self.window.statusBar().currentMessage())

    def test_mount_button_enabled_only_with_device_present_no_pen_no_task(self) -> None:
        self.gui_module.pen_device_present = lambda: False
        self.window._pen = None
        self.window._update_action_buttons_enabled()
        self.assertFalse(self.window._mount_button.isEnabled())

        self.gui_module.pen_device_present = lambda: True
        self.window._update_action_buttons_enabled()
        self.assertTrue(self.window._mount_button.isEnabled())

        self.window._pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        self.window._update_action_buttons_enabled()
        self.assertFalse(self.window._mount_button.isEnabled())

        self.window._pen = None
        self.window._pen_job._thread = object()
        try:
            self.window._update_action_buttons_enabled()
            self.assertFalse(self.window._mount_button.isEnabled())
        finally:
            self.window._pen_job._thread = None

    def test_unmount_button_enabled_only_with_detected_pen_no_task(self) -> None:
        self.window._pen = None
        self.window._update_action_buttons_enabled()
        self.assertFalse(self.window._unmount_button.isEnabled())

        self.window._pen = Pen(mountpoint=Path("/some/folder"), source="")
        self.window._update_action_buttons_enabled()
        self.assertFalse(self.window._unmount_button.isEnabled())

        self.window._pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        self.window._update_action_buttons_enabled()
        self.assertTrue(self.window._unmount_button.isEnabled())

        self.window._pen_job._thread = object()
        try:
            self.window._update_action_buttons_enabled()
            self.assertFalse(self.window._unmount_button.isEnabled())
        finally:
            self.window._pen_job._thread = None

    def test_successful_mount_populates_pen_panel(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdc1")

        self.gui_module.mount_pen = lambda: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: PenSummary(
            pen=pen, free=1024, total=4096, installed=("A.gme",), outdated=[]
        )

        self.window._start_mount()
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertEqual(self.window._pen, fake_pen)
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.CONNECTED)
        self.assertEqual(
            [self.window._installed_list.item(i).text() for i in range(self.window._installed_list.count())],
            ["A.gme"],
        )
        self.assertIn("Pen mounted", self.window.statusBar().currentMessage())

    def test_successful_unmount_clears_pen_and_blocks_poll_until_replug(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        self.window._pen = fake_pen
        self.window._connection = self.gui_module.ConnectionState.CONNECTED
        self.window._installed_list.addItem("A.gme")
        self.window._installed_names = ("A.gme",)

        self.gui_module.unmount_pen = lambda pen: None

        self.window._start_unmount()
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertIsNone(self.window._pen)
        self.assertEqual(self.window._installed_list.count(), 0)
        self.assertIn("safe to unplug", self.window._connection_label.text())
        self.assertIn("safe to unplug", self.window.statusBar().currentMessage())
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.SAFELY_UNMOUNTED)

        # WHY: the device is still plugged in (present) but was deliberately unmounted - a quiet
        # poll must not remount it
        self.gui_module.pen_device_present = lambda: True
        with patch.object(self.gui_module, "find_pen") as mock_find_pen:
            self.window._poll_pen()
        mock_find_pen.assert_not_called()
        self.assertIsNone(self.window._pen)

        # unplugging (device disappears) clears the guard
        self.gui_module.pen_device_present = lambda: False
        self.window._poll_pen()
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.DISCONNECTED)

        # replugging (device reappears) lets auto-detect run again
        self.gui_module.pen_device_present = lambda: True
        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: PenSummary(
            pen=pen, free=0, total=0, installed=(), outdated=[]
        )
        self.window._poll_pen()
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertEqual(self.window._pen, fake_pen)
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.CONNECTED)

    def test_unmount_failure_shows_message_box_and_keeps_pen(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        self.window._pen = fake_pen
        self.window._connection = self.gui_module.ConnectionState.CONNECTED

        def _raise(pen):
            raise PenError("target is busy")

        self.gui_module.unmount_pen = _raise

        with patch.object(self.gui_module.QMessageBox, "warning") as mock_warning:
            self.window._start_unmount()
            _pump_until_idle(self.app, self.window._pen_job)

        mock_warning.assert_called_once()
        self.assertIsNotNone(self.window._pen)
        self.assertIn("target is busy", self.window.statusBar().currentMessage())

    def test_on_loaded_refreshes_pen_summary_reflecting_new_catalog(self) -> None:
        # Regression: a pen detected before the initial catalog load finished was summarised
        # against a None catalog, so its outdated list stayed empty forever.
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")

        def fake_pen_summary(pen, catalog):
            outdated = (
                []
                if catalog is None
                else [OutdatedTitle(file_name="A.gme", installed_version="1", catalog_version="2")]
            )
            return PenSummary(pen=pen, free=0, total=0, installed=(), outdated=outdated)

        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = fake_pen_summary

        self.window._catalog = None  # WHY: simulates detect winning the race against the catalog load
        self.window._detect()
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertIs(self.window._connection, self.gui_module.ConnectionState.CONNECTED)
        self.assertEqual(self.window._outdated_list.count(), 0)

        self.window._on_loaded((self.catalog, False))
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertEqual(self.window._outdated_list.count(), 1)

    def test_on_loaded_defers_pen_summary_refresh_while_pen_job_running(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: PenSummary(
            pen=pen, free=0, total=0, installed=(), outdated=[]
        )

        self.window._detect()
        _pump_until_idle(self.app, self.window._pen_job)
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.CONNECTED)

        self.window._pen_job._thread = object()
        try:
            self.window._on_loaded((self.catalog, False))
            self.assertTrue(self.window._summary_refresh_pending)
        finally:
            self.window._pen_job._thread = None

        self.window._pen_job.finished.emit()
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertFalse(self.window._summary_refresh_pending)
        self.assertFalse(self.window._pen_job.is_running)

    def test_on_loaded_during_in_flight_detect_refreshes_summary_once_pen_confirmed(self) -> None:
        # Regression: the startup quiet detect usually finishes loading *while* the initial catalog
        # load is still running, so self._connection isn't CONNECTED yet when _on_loaded() arrives.
        # The old code only flagged a refresh when already CONNECTED, so the detect's pen_summary()
        # call - captured against catalog=None - was never redone once the real catalog showed up.
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        release = threading.Event()
        seen_catalogs: list[object] = []

        def blocking_find_pen(**kwargs):
            release.wait(5)
            return fake_pen

        def recording_pen_summary(pen, catalog):
            seen_catalogs.append(catalog)
            return PenSummary(pen=pen, free=0, total=0, installed=(), outdated=[])

        self.gui_module.find_pen = blocking_find_pen
        self.gui_module.pen_summary = recording_pen_summary

        self.window._catalog = None  # WHY: mirrors startup, where the detect starts before the load
        self.window._detect()
        self.assertTrue(self.window._pen_job.is_running)
        self.assertIsNot(self.window._connection, self.gui_module.ConnectionState.CONNECTED)

        self.window._on_loaded((self.catalog, False))
        self.assertTrue(self.window._summary_refresh_pending)

        release.set()
        _pump_until_idle(self.app, self.window._pen_job)

        self.assertIs(self.window._connection, self.gui_module.ConnectionState.CONNECTED)
        self.assertIs(seen_catalogs[-1], self.catalog)

    def test_mount_failure_after_unmount_clears_safely_unmounted_state(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        self.window._pen = fake_pen
        self.window._connection = self.gui_module.ConnectionState.SAFELY_UNMOUNTED

        def _raise():
            raise PenError("mount failed")

        self.gui_module.mount_pen = _raise
        self.gui_module.pen_device_present = lambda: True

        with patch.object(self.gui_module.QMessageBox, "warning") as mock_warning:
            self.window._start_mount()
            _pump_until_idle(self.app, self.window._pen_job)

        mock_warning.assert_called_once()
        self.assertIsNone(self.window._pen)
        self.assertIs(self.window._connection, self.gui_module.ConnectionState.PLUGGED_IN_UNMOUNTED)

        # a subsequent poll with the device mounted resumes detection instead of staying stuck
        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: PenSummary(
            pen=pen, free=0, total=0, installed=(), outdated=[]
        )
        with patch.object(self.gui_module, "pen_device_mounted", return_value=True):
            self.window._poll_pen()
            _pump_until_idle(self.app, self.window._pen_job)

        self.assertIs(self.window._connection, self.gui_module.ConnectionState.CONNECTED)

    def test_poll_pen_plugged_in_unmounted_skips_detect_when_not_mounted(self) -> None:
        self.window._connection = self.gui_module.ConnectionState.PLUGGED_IN_UNMOUNTED
        self.gui_module.pen_device_present = lambda: True

        with patch.object(self.gui_module, "pen_device_mounted", return_value=False):
            with patch.object(self.gui_module, "find_pen") as mock_find_pen:
                self.window._poll_pen()

        mock_find_pen.assert_not_called()

    def test_poll_pen_plugged_in_unmounted_detects_when_mounted(self) -> None:
        fake_pen = Pen(mountpoint=Path("/media/tiptoi"), source="/dev/sdb1")
        self.window._connection = self.gui_module.ConnectionState.PLUGGED_IN_UNMOUNTED
        self.gui_module.pen_device_present = lambda: True
        self.gui_module.find_pen = lambda **kwargs: fake_pen
        self.gui_module.pen_summary = lambda pen, catalog: PenSummary(
            pen=pen, free=0, total=0, installed=(), outdated=[]
        )

        with patch.object(self.gui_module, "pen_device_mounted", return_value=True):
            self.window._poll_pen()
            _pump_until_idle(self.app, self.window._pen_job)

        self.assertIs(self.window._connection, self.gui_module.ConnectionState.CONNECTED)


if __name__ == "__main__":
    unittest.main()
