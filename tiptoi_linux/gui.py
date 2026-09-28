"""Desktop GUI for browsing the tiptoi product catalog."""

from __future__ import annotations

import functools
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from enum import Enum, IntEnum, auto
from pathlib import Path
from typing import TypeVar

from tiptoi_linux.catalog import (
    DEFAULT_MAX_AGE_SECONDS,
    Catalog,
    Product,
    cache_path,
    load_catalog,
)
from tiptoi_linux.download import DownloadError, gme_file_name
from tiptoi_linux.i18n import _, ngettext
from tiptoi_linux.pen import (
    PHASE_COPY,
    PHASE_DOWNLOAD,
    PHASE_VERIFY,
    Pen,
    PenSummary,
    PhaseProgress,
    delete_title,
    find_pen,
    install_title,
    mount_pen,
    pen_device_mounted,
    pen_device_present,
    pen_still_mounted,
    pen_summary,
    unmount_pen,
)

try:
    from PySide6.QtCore import (
        QAbstractTableModel,
        QEvent,
        QLibraryInfo,
        QLocale,
        QModelIndex,
        QObject,
        QPersistentModelIndex,
        QSortFilterProxyModel,
        Qt,
        QThread,
        QTimer,
        QTranslator,
        Signal,
        Slot,
    )
    from PySide6.QtGui import QFont, QIcon
    from PySide6.QtWidgets import (
        QApplication,
        QFileDialog,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QListWidget,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QSplitter,
        QStyle,
        QTableView,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    _PYSIDE6_IMPORT_ERROR: ImportError | None = exc
else:
    _PYSIDE6_IMPORT_ERROR = None


ICON_PATH = Path(__file__).parent / "resources" / "tiptoi.png"


class Column(IntEnum):
    ON_PEN = 0
    NAME = 1
    VERSION = 2


class ConnectionState(Enum):
    DISCONNECTED = auto()
    PLUGGED_IN_UNMOUNTED = auto()
    SAFELY_UNMOUNTED = auto()
    CONNECTED = auto()


T = TypeVar("T")


_COLUMN_HEADERS = (_("On pen"), _("Name"), _("Version"))

_PROGRESS_BAR_FORMATS = {
    PHASE_DOWNLOAD: _("Downloading… %p%"),
    PHASE_COPY: _("Copying to pen… %p%"),
    PHASE_VERIFY: _("Verifying on pen… %p%"),
}

_PROGRESS_STATUS_TEXT = {
    PHASE_DOWNLOAD: _("Downloading…"),
    PHASE_COPY: _("Copying to pen…"),
    PHASE_VERIFY: _("Verifying on pen…"),
}

# WHY: text (and, for CONNECTED, a {path}-formatted template) plus colour for every connection
# state, in one place instead of scattered across a chain of ifs that re-read live pen state
_CONNECTION_TEXT_AND_COLOR: dict[ConnectionState, tuple[str, str]] = {
    ConnectionState.DISCONNECTED: (_("○ Not connected"), ""),
    ConnectionState.PLUGGED_IN_UNMOUNTED: (
        _("○ Pen plugged in but not mounted — open it in your file manager"),
        "darkorange",
    ),
    ConnectionState.SAFELY_UNMOUNTED: (_("✓ Unmounted — safe to unplug"), "green"),
    ConnectionState.CONNECTED: (_("● Connected — {path}"), "green"),
}


def _installed_label_text(n: int) -> str:
    return _("On the &pen ({n})").format(n=n)


def _outdated_label_text(n: int) -> str:
    return _("&Outdated ({n})").format(n=n)


def _catalog_cache_is_stale() -> bool:
    # WHY: a successful download rewrites the cache, so an old mtime means the data came
    # from the stale-cache fallback rather than the network
    try:
        age = time.time() - cache_path().stat().st_mtime
    except OSError:
        return False
    return age > DEFAULT_MAX_AGE_SECONDS


if _PYSIDE6_IMPORT_ERROR is None:

    class ProductTableModel(QAbstractTableModel):
        def __init__(self, products: Sequence[Product] = (), parent: QObject | None = None) -> None:
            super().__init__(parent)
            self._products: tuple[Product, ...] = tuple(products)
            self._installed: frozenset[str] = frozenset()

        def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
            if parent.isValid():
                return 0
            return len(self._products)

        def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
            if parent.isValid():
                return 0
            return len(_COLUMN_HEADERS)

        def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
            if not index.isValid():
                return None
            product = self.product_at(index.row())
            if product is None:
                return None
            column = index.column()
            if column == Column.ON_PEN:
                installed = self._is_installed(product)
                if role == Qt.ItemDataRole.DisplayRole:
                    return "✓" if installed else ""
                if installed and role in (
                    Qt.ItemDataRole.ToolTipRole,
                    Qt.ItemDataRole.AccessibleTextRole,
                ):
                    return _("On the pen")
                return None
            if column == Column.NAME:
                if role == Qt.ItemDataRole.DisplayRole:
                    return product.name.replace("_", " ")
                if role == Qt.ItemDataRole.ToolTipRole:
                    return product.name
                return None
            if column == Column.VERSION:
                if role == Qt.ItemDataRole.DisplayRole:
                    return product.version
                return None
            return None

        def headerData(
            self,
            section: int,
            orientation: Qt.Orientation,
            role: int = Qt.ItemDataRole.DisplayRole,
        ):
            if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
                return None
            if not 0 <= section < len(_COLUMN_HEADERS):
                return None
            return _COLUMN_HEADERS[section]

        def set_products(self, products: Sequence[Product]) -> None:
            new = tuple(products)
            if new == self._products:
                return
            self.beginResetModel()
            self._products = new
            self.endResetModel()

        def product_at(self, row: int) -> Product | None:
            if 0 <= row < len(self._products):
                return self._products[row]
            return None

        def set_installed(self, file_names: Iterable[str]) -> None:
            new = frozenset(name.casefold() for name in file_names)
            if new == self._installed:
                return
            self._installed = new
            if not self._products:
                return
            top_left = self.index(0, Column.ON_PEN)
            bottom_right = self.index(len(self._products) - 1, Column.ON_PEN)
            self.dataChanged.emit(
                top_left,
                bottom_right,
                [
                    Qt.ItemDataRole.DisplayRole,
                    Qt.ItemDataRole.ToolTipRole,
                    Qt.ItemDataRole.AccessibleTextRole,
                ],
            )

        def _is_installed(self, product: Product) -> bool:
            try:
                name = gme_file_name(product)
            except DownloadError:
                return False
            return name.casefold() in self._installed

    class TaskWorker(QObject):
        succeeded = Signal(object)
        failed = Signal(str)
        # object x3 rather than str, int, int|None so a None total survives the queued connection
        progress = Signal(object, object, object)
        finished = Signal()

        def __init__(self, task: Callable[[PhaseProgress], object]) -> None:
            super().__init__()
            self._task = task

        @Slot()
        def run(self) -> None:
            try:
                result = self._task(self.progress.emit)
            except Exception as exc:  # WHY: anything escaping a worker thread would kill the app
                self.failed.emit(str(exc))
            else:
                self.succeeded.emit(result)
            finally:
                self.finished.emit()

    def _noop_progress(phase: str, done: int, total: int | None) -> None:
        return None

    class BackgroundJob(QObject):
        """Runs one task at a time on an unparented QThread and delivers its outcome on the GUI thread.

        Callbacks (on_success/on_failure/on_progress) must not call start() on this same job - it is
        still running while they execute. React to the finished signal instead.
        """

        finished = Signal()  # emitted on the GUI thread after the thread is joined and references dropped

        def __init__(self, parent: QObject) -> None:
            # WHY: this is an ordinary QObject, not a QThread - the "never parent / never
            # deleteLater" rule below applies only to the QThread it creates in start(), so being
            # parented to the window here is fine and lets Qt tear it down normally
            super().__init__(parent)
            self._thread: QThread | None = None
            self._worker: TaskWorker | None = None
            self._task_on_success: Callable[[object], None] = lambda result: None
            self._task_on_failure: Callable[[str], None] = lambda message: None
            self._task_on_progress: PhaseProgress = _noop_progress

        @property
        def is_running(self) -> bool:
            return self._thread is not None

        def start(
            self,
            task: Callable[[PhaseProgress], T],
            on_success: Callable[[T], None],
            on_failure: Callable[[str], None],
            on_progress: PhaseProgress | None = None,
        ) -> bool:
            if self._thread is not None:
                return False  # one task at a time

            self._task_on_success = on_success
            self._task_on_failure = on_failure
            self._task_on_progress = on_progress or _noop_progress

            # WHY: no parent - parenting the thread (e.g. to this job or the window) keeps C++
            # holding a reference to every past worker thread as a QObject child, which leaks one
            # per run
            thread = QThread()
            worker = TaskWorker(task)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            # WHY: only bound methods of a QObject living on the GUI thread may be connected to
            # worker signals. PySide6 runs a lambda/plain-function slot in the *emitting* thread, so
            # a lambda here would update widgets from the worker thread and segfault in libQt6Gui,
            # as an install-success callback once did (coredump 2026-09-28). The slots below run on
            # the GUI thread and then call the caller's stored callbacks from there.
            worker.succeeded.connect(self._on_succeeded)
            worker.failed.connect(self._on_failed)
            worker.progress.connect(self._on_progress)
            worker.finished.connect(thread.quit)
            thread.finished.connect(self._on_thread_finished)
            self._thread = thread
            self._worker = worker
            thread.start()
            return True

        def stop(self, msecs: int) -> None:
            if self._thread is not None:
                self._thread.quit()
                # WHY: quit() cannot interrupt a blocking call in the task, so bound the wait
                # rather than freezing the window for however long the task might still run
                self._thread.wait(msecs)

        @Slot(object)
        def _on_succeeded(self, result: object) -> None:
            self._task_on_success(result)

        @Slot(str)
        def _on_failed(self, message: str) -> None:
            self._task_on_failure(message)

        @Slot(object, object, object)
        def _on_progress(self, phase: object, done: object, total: object) -> None:
            self._task_on_progress(phase, done, total)

        @Slot()
        def _on_thread_finished(self) -> None:
            if self._thread is not None:
                # WHY: returns immediately (the thread has already finished) but guarantees the OS
                # thread is done, so dropping the last reference destroys it deterministically.
                # deleteLater() here would post an event that Qt may process after Python has
                # collected the wrapper, which segfaults at interpreter shutdown.
                self._thread.wait()
            self._thread = None
            self._worker = None
            # WHY: without this, a finished job keeps the last task's callbacks (often closures over
            # a product/pen/summary) alive until the next start() overwrites them
            self._task_on_success = lambda result: None
            self._task_on_failure = lambda message: None
            self._task_on_progress = _noop_progress
            self.finished.emit()

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("tiptoi")
            self.resize(1100, 700)

            self._model = ProductTableModel()
            self._proxy = QSortFilterProxyModel(self)
            self._proxy.setSourceModel(self._model)
            self._proxy.setFilterKeyColumn(Column.NAME)
            self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

            search_label = QLabel(_("&Search:"))
            self._search_box = QLineEdit()
            self._search_box.setPlaceholderText(_("Search products…"))
            self._search_box.setClearButtonEnabled(True)
            self._search_box.setAccessibleName(_("Search products"))
            search_label.setBuddy(self._search_box)
            self._search_box.textChanged.connect(self._proxy.setFilterFixedString)

            self._refresh_button = QPushButton(_("Refresh"))
            self._refresh_button.setIcon(self._icon("view-refresh", QStyle.StandardPixmap.SP_BrowserReload))
            self._refresh_button.clicked.connect(self._start_refresh)

            self._table = QTableView()
            self._table.setModel(self._proxy)
            self._table.setSortingEnabled(True)
            self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
            self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
            self._table.setAlternatingRowColors(True)
            self._table.setAccessibleName(_("Products"))
            self._table.horizontalHeader().setStretchLastSection(False)
            self._table.horizontalHeader().setSectionResizeMode(
                Column.ON_PEN, QHeaderView.ResizeMode.ResizeToContents
            )
            self._table.horizontalHeader().setSectionResizeMode(Column.NAME, QHeaderView.ResizeMode.Stretch)
            self._table.verticalHeader().setVisible(False)
            self._table.selectionModel().selectionChanged.connect(self._update_install_button_enabled)
            self._table.doubleClicked.connect(self._on_table_double_clicked)

            search_row = QHBoxLayout()
            search_row.addWidget(search_label)
            search_row.addWidget(self._search_box)
            search_row.addWidget(self._refresh_button)

            self._connection_label = QLabel()
            self._connection_label.setAccessibleName(_("Pen connection"))

            self._detect_button = QPushButton(_("&Detect pen"))
            self._detect_button.setAccessibleName(_("Detect pen"))
            self._detect_button.setIcon(self._icon("edit-find", QStyle.StandardPixmap.SP_FileDialogContentsView))
            self._detect_button.clicked.connect(lambda: self._detect())

            self._choose_button = QPushButton(_("&Choose folder…"))
            self._choose_button.setAccessibleName(_("Choose pen folder"))
            self._choose_button.setIcon(self._icon("folder-open", QStyle.StandardPixmap.SP_DirOpenIcon))
            self._choose_button.clicked.connect(self._choose_pen_folder)

            self._mount_button = QPushButton(_("&Mount"))
            self._mount_button.setAccessibleName(_("Mount the pen"))
            self._mount_button.setIcon(self._icon("drive-removable-media", QStyle.StandardPixmap.SP_DriveFDIcon))
            self._mount_button.setEnabled(False)
            self._mount_button.clicked.connect(self._start_mount)

            self._unmount_button = QPushButton(_("&Unmount"))
            self._unmount_button.setAccessibleName(_("Unmount the pen so it can be unplugged"))
            self._unmount_button.setIcon(self._icon("media-eject", QStyle.StandardPixmap.SP_MediaStop))
            self._unmount_button.setEnabled(False)
            self._unmount_button.clicked.connect(self._start_unmount)

            self._install_button = QPushButton(_("&Install on pen"))
            self._install_button.setAccessibleName(_("Install selected"))
            self._install_button.setIcon(self._icon("document-save", QStyle.StandardPixmap.SP_DialogSaveButton))
            self._install_button.setEnabled(False)
            self._install_button.clicked.connect(self._start_install_selected)

            self._delete_button = QPushButton(_("De&lete selected"))
            self._delete_button.setAccessibleName(_("Delete selected titles from the pen"))
            self._delete_button.setIcon(self._icon("edit-delete", QStyle.StandardPixmap.SP_TrashIcon))
            self._delete_button.setEnabled(False)
            self._delete_button.clicked.connect(self._start_delete_selected)

            self._progress_bar = QProgressBar()
            self._progress_bar.setAccessibleName(_("Task progress"))
            self._progress_bar.hide()

            self._space_bar = QProgressBar()
            self._space_bar.setAccessibleName(_("Pen storage"))
            self._space_bar.setRange(0, 100)
            self._reset_space_bar()

            self._installed_list = QListWidget()
            self._installed_list.setAccessibleName(_("Titles on the pen"))
            self._installed_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
            self._installed_list.itemSelectionChanged.connect(self._update_delete_button_enabled)

            self._installed_label = QLabel(_installed_label_text(0))
            self._installed_label.setBuddy(self._installed_list)

            self._outdated_list = QListWidget()
            self._outdated_list.setAccessibleName(_("Outdated titles"))

            self._outdated_label = QLabel(_outdated_label_text(0))
            self._outdated_label.setBuddy(self._outdated_list)

            # header bar
            header = QWidget()
            header_layout = QHBoxLayout(header)
            title_label = QLabel("tiptoi")
            title_font = QFont(title_label.font())
            title_font.setPointSize(title_font.pointSize() + 6)
            title_font.setBold(True)
            title_label.setFont(title_font)
            logo_label = QLabel()
            logo_size = title_label.sizeHint().height() + 8
            logo_label.setPixmap(
                QIcon(str(ICON_PATH)).pixmap(logo_size, logo_size, QIcon.Mode.Normal, QIcon.State.Off)
            )
            logo_label.setAccessibleName("tiptoi")
            header_layout.addWidget(logo_label)
            header_layout.addWidget(title_label)
            header_layout.addStretch()
            header_layout.addWidget(self._connection_label)
            header_layout.addWidget(self._mount_button)
            header_layout.addWidget(self._unmount_button)

            # left pane: catalog
            install_row = QHBoxLayout()
            install_row.addStretch()
            install_row.addWidget(self._install_button)

            catalog_layout = QVBoxLayout()
            catalog_layout.addLayout(search_row)
            catalog_layout.addWidget(self._table)
            catalog_layout.addLayout(install_row)
            catalog_pane = QWidget()
            catalog_pane.setLayout(catalog_layout)

            # right pane: pen
            detect_row = QHBoxLayout()
            detect_row.addWidget(self._detect_button)
            detect_row.addWidget(self._choose_button)

            storage_caption = QLabel(_("Storage"))

            right_layout = QVBoxLayout()
            right_layout.addWidget(storage_caption)
            right_layout.addWidget(self._space_bar)
            right_layout.addLayout(detect_row)
            right_layout.addWidget(self._installed_label)
            right_layout.addWidget(self._installed_list)
            right_layout.addWidget(self._outdated_label)
            right_layout.addWidget(self._outdated_list)
            right_layout.addWidget(self._delete_button)
            right_pane = QWidget()
            right_pane.setLayout(right_layout)
            right_pane.setMinimumWidth(250)

            self._splitter = QSplitter(Qt.Orientation.Horizontal)
            self._splitter.addWidget(catalog_pane)
            self._splitter.addWidget(right_pane)
            self._splitter.setChildrenCollapsible(False)
            self._splitter.setSizes([660, 440])

            central = QWidget()
            layout = QVBoxLayout(central)
            layout.addWidget(header)
            layout.addWidget(self._splitter)
            self.setCentralWidget(central)
            self.setTabOrder(self._mount_button, self._unmount_button)
            self.setTabOrder(self._unmount_button, self._search_box)
            self.setTabOrder(self._search_box, self._refresh_button)
            self.setTabOrder(self._refresh_button, self._table)
            self.setTabOrder(self._table, self._install_button)
            self.setTabOrder(self._install_button, self._detect_button)
            self.setTabOrder(self._detect_button, self._choose_button)
            self.setTabOrder(self._choose_button, self._installed_list)
            self.setTabOrder(self._installed_list, self._outdated_list)
            self.setTabOrder(self._outdated_list, self._delete_button)

            self.statusBar().addPermanentWidget(self._progress_bar)

            self._catalog_job = BackgroundJob(self)
            self._catalog_job.finished.connect(self._on_catalog_job_finished)
            self._close_when_idle = False

            self._pen_job = BackgroundJob(self)
            self._pen_job.finished.connect(self._on_pen_job_finished)
            self._progress_phase: str | None = None
            self._summary_refresh_pending = False

            self._pen: Pen | None = None
            self._catalog: Catalog | None = None
            self._installed_names: tuple[str, ...] = ()
            self._connection = ConnectionState.DISCONNECTED
            self._last_connection_label: tuple[str, str] | None = None
            self._update_connection_label()

            # WHY: parented, unlike the QThreads BackgroundJob creates - the "never parent / never
            # deleteLater" rule is specific to QThread; an ordinary QTimer is safely owned and torn
            # down by Qt
            self._poll_timer = QTimer(self)
            self._poll_timer.setInterval(2000)
            self._poll_timer.timeout.connect(self._poll_pen)
            self._poll_timer.start()

        def _icon(self, theme_name: str, fallback: QStyle.StandardPixmap) -> QIcon:
            return QIcon.fromTheme(theme_name, self.style().standardIcon(fallback))

        def _start_load(self, *, force: bool) -> None:
            if not self._catalog_job.start(
                lambda progress: (load_catalog(force=force, allow_stale=not force), _catalog_cache_is_stale()),
                self._on_loaded,
                self._on_failed,
            ):
                return  # a load is already in flight
            self._refresh_button.setEnabled(False)
            self.statusBar().showMessage(_("Loading…"))

        def _start_refresh(self) -> None:
            self._start_load(force=True)

        def start_initial_load(self) -> None:
            self._start_load(force=False)

        def _on_catalog_job_finished(self) -> None:
            self._refresh_button.setEnabled(True)
            if self._close_when_idle:
                self._close_when_idle = False
                self.close()
                # WHY: closeEvent() hid the window (rather than closing it) while this job was still
                # running, so Qt never saw a visible window actually close and
                # quitOnLastWindowClosed never fired - without this the event loop keeps running
                # forever with no window visible once this deferred close goes through.
                QApplication.quit()

        def _on_loaded(self, result: tuple[Catalog, bool]) -> None:
            catalog, stale = result
            self._catalog = catalog
            self._model.set_products(catalog.products)
            n = len(catalog.products)
            if stale:
                text = ngettext(
                    "{n} product (offline — cached copy) · {path}",
                    "{n} products (offline — cached copy) · {path}",
                    n,
                ).format(n=n, path=cache_path())
            else:
                text = ngettext("{n} product · {path}", "{n} products · {path}", n).format(
                    n=n, path=cache_path()
                )
            self.statusBar().showMessage(text)
            if self._pen_job.is_running:
                # WHY: a detect/mount/install is still in flight and captured self._catalog (None or
                # stale) before this load finished - e.g. the startup quiet detect racing the initial
                # catalog load. self._connection may not be CONNECTED yet (self._pen isn't set until
                # that job succeeds), so _refresh_pen_summary()'s assert would fire here; flag it
                # instead and let _on_pen_job_finished refresh once that job's outcome (and
                # self._connection) is settled.
                self._summary_refresh_pending = True
            elif self._connection is ConnectionState.CONNECTED:
                # WHY: no pen job in flight - refresh now so a pen detected before this load finished
                # is summarised against the real catalog instead of the None/stale one it started with
                # (an override pen has source == "" and find_pen() without override would drop it)
                self._refresh_pen_summary()

        def _on_failed(self, message: str) -> None:
            self.statusBar().showMessage(_("error: {message}").format(message=message))
            QMessageBox.warning(self, "tiptoi", message)

        def _selected_product(self) -> Product | None:
            indexes = self._table.selectionModel().selectedRows()
            if not indexes:
                return None
            source_index = self._proxy.mapToSource(indexes[0])
            return self._model.product_at(source_index.row())

        def _on_table_double_clicked(self, index: QModelIndex) -> None:
            if not index.isValid():
                return
            if self._install_button.isEnabled():
                self._start_install_selected()

        def _update_install_button_enabled(self) -> None:
            has_selection = bool(self._table.selectionModel().selectedRows())
            self._install_button.setEnabled(
                self._pen is not None and has_selection and not self._pen_job.is_running
            )

        def _update_delete_button_enabled(self) -> None:
            has_selection = bool(self._installed_list.selectedItems())
            self._delete_button.setEnabled(
                self._pen is not None and has_selection and not self._pen_job.is_running
            )

        def _update_action_buttons_enabled(self) -> None:
            self._detect_button.setEnabled(not self._pen_job.is_running)
            self._choose_button.setEnabled(not self._pen_job.is_running)
            self._update_install_button_enabled()
            self._update_delete_button_enabled()
            self._mount_button.setEnabled(
                not self._pen_job.is_running and self._pen is None and pen_device_present()
            )
            self._unmount_button.setEnabled(
                not self._pen_job.is_running and self._pen is not None and self._pen.source != ""
            )

        def _detect(self, override: Path | None = None, *, quiet: bool = False) -> None:
            catalog = self._catalog  # WHY: captured on the GUI thread; the task runs on a worker thread
            self._run_task(
                lambda progress: pen_summary(find_pen(override=override), catalog),
                self._on_pen_refreshed,
                _("Detecting pen…"),
                on_failure=functools.partial(self._on_detect_failed, quiet=quiet),
            )

        def _choose_pen_folder(self) -> None:
            result = QFileDialog.getExistingDirectory(self, _("Choose pen folder"))
            if not result:
                return
            self._detect(Path(result))

        def _start_mount(self) -> None:
            catalog = self._catalog  # WHY: captured on the GUI thread; the task runs on a worker thread
            self._run_task(
                lambda progress: pen_summary(mount_pen(), catalog),
                self._on_mount_succeeded,
                _("Mounting pen…"),
                on_failure=self._on_mount_failed,
            )

        def _on_mount_succeeded(self, summary: PenSummary) -> None:
            message = _("Pen mounted: {path}").format(path=summary.pen.mountpoint)
            self._on_pen_refreshed(summary, message=message)

        def _on_mount_failed(self, message: str) -> None:
            # WHY: a failed Mount after a deliberate Unmount must not leave the state stuck at
            # SAFELY_UNMOUNTED (the poll never auto-detects there) - fall back to whatever the device
            # presence says, same as a failed detect
            state = ConnectionState.PLUGGED_IN_UNMOUNTED if pen_device_present() else ConnectionState.DISCONNECTED
            self._clear_pen(state)
            self._on_failed(message)

        def _start_unmount(self) -> None:
            pen = self._pen
            if pen is None:
                return
            self._run_task(
                lambda progress: unmount_pen(pen),
                self._on_unmounted,
                _("Unmounting pen…"),
            )

        def _on_unmounted(self, result: object) -> None:
            self._clear_pen(ConnectionState.SAFELY_UNMOUNTED)
            self.statusBar().showMessage(_("Pen unmounted — safe to unplug"))

        def _start_install_selected(self) -> None:
            pen = self._pen
            product = self._selected_product()
            if pen is None or product is None:
                return
            catalog = self._catalog  # WHY: captured on the GUI thread; the task runs on a worker thread

            def task(progress: PhaseProgress) -> PenSummary:
                install_title(pen, product, progress=progress)
                return pen_summary(pen, catalog)

            self._run_task(
                task,
                lambda summary: self._on_install_succeeded(summary, product.name),
                _("Installing {name}…").format(name=product.name),
            )

        def _on_install_succeeded(self, summary: PenSummary, name: str) -> None:
            message = _("Installed {name} — verified on the pen").format(name=name)
            self._on_pen_refreshed(summary, message=message)
            QMessageBox.information(
                self,
                "tiptoi",
                _(
                    "{name} was installed and verified on the pen.\n\n"
                    "Click Unmount before unplugging the pen."
                ).format(name=name),
            )

        def _start_delete_selected(self) -> None:
            pen = self._pen
            items = self._installed_list.selectedItems()
            if pen is None or not items:
                return
            names = [item.text() for item in items]

            reply = QMessageBox.question(
                self,
                "tiptoi",
                _(
                    "Delete these titles from the pen?\n\n{names}\n\n"
                    "The pen's trash will be emptied as well."
                ).format(names="\n".join(names)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

            catalog = self._catalog  # WHY: captured on the GUI thread; the task runs on a worker thread

            def task(progress: PhaseProgress) -> tuple[int, PenSummary]:
                freed = 0
                for name in names:
                    freed += delete_title(pen, name)
                return freed, pen_summary(pen, catalog)

            n = len(names)
            busy_message = ngettext("Deleting {n} title…", "Deleting {n} titles…", n).format(n=n)
            self._run_task(
                task,
                lambda result: self._on_delete_succeeded(result, count=n),
                busy_message,
            )

        def _on_delete_succeeded(self, result: tuple[int, PenSummary], *, count: int) -> None:
            freed, summary = result
            message = ngettext(
                "Deleted {n} title, freed {size}", "Deleted {n} titles, freed {size}", count
            ).format(n=count, size=QLocale().formattedDataSize(freed))
            self._on_pen_refreshed(summary, message=message)

        def _refresh_pen_summary(self) -> None:
            assert self._pen is not None  # WHY: only called while self._connection is CONNECTED
            if self._pen_job.is_running:
                self._summary_refresh_pending = True
                return
            pen = self._pen
            catalog = self._catalog  # WHY: captured on the GUI thread; the task runs on a worker thread
            self._run_task(
                lambda progress: pen_summary(pen, catalog),
                self._on_pen_refreshed,
                _("Pen ready: {path}").format(path=pen.mountpoint),
            )

        def _on_pen_refreshed(self, summary: PenSummary, *, message: str | None = None) -> None:
            self._pen = summary.pen
            self._connection = ConnectionState.CONNECTED
            self._update_installed_list(summary.installed)
            self._model.set_installed(summary.installed)
            self._update_space_bar(summary.free, summary.total)
            self._outdated_list.clear()
            for title in summary.outdated:
                self._outdated_list.addItem(title.describe())
            self._outdated_label.setText(_outdated_label_text(len(summary.outdated)))
            self._update_action_buttons_enabled()
            self._update_connection_label()
            default_message = _("Pen ready: {path}").format(path=summary.pen.mountpoint)
            self.statusBar().showMessage(message if message is not None else default_message)

        def _update_installed_list(self, installed: tuple[str, ...]) -> None:
            self._installed_label.setText(_installed_label_text(len(installed)))
            if installed == self._installed_names:
                return
            self._installed_names = installed
            self._installed_list.clear()
            for name in installed:
                self._installed_list.addItem(name)

        def _update_space_bar(self, free: int, total: int) -> None:
            used_percent = 0 if total <= 0 else min(100, int((total - free) * 100 / total))
            self._space_bar.setValue(used_percent)
            self._space_bar.setFormat(
                _("{free} free of {total}").format(
                    free=QLocale().formattedDataSize(free), total=QLocale().formattedDataSize(total)
                )
            )

        def _reset_space_bar(self) -> None:
            self._space_bar.setValue(0)
            self._space_bar.setFormat("—")

        def _update_connection_label(self) -> None:
            text, color = _CONNECTION_TEXT_AND_COLOR[self._connection]
            if self._connection is ConnectionState.CONNECTED:
                # WHY: self._pen is not None iff self._connection is CONNECTED (see _on_pen_refreshed
                # and _clear_pen, the only two methods that set self._connection)
                assert self._pen is not None
                text = text.format(path=self._pen.mountpoint)
            if (text, color) == self._last_connection_label:
                return  # WHY: setStyleSheet() is expensive to call on every 2s poll for no change
            self._last_connection_label = (text, color)
            self._connection_label.setText(text)
            self._connection_label.setStyleSheet(f"color: {color};" if color else "")

        def _clear_pen(self, state: ConnectionState) -> None:
            # WHY: shared by a failed detect/choose and by the poll noticing a known pen is no longer
            # mounted - Install must never target a pen that's no longer confirmed
            self._pen = None
            self._connection = state
            self._installed_names = ()
            self._installed_list.clear()
            self._outdated_list.clear()
            self._installed_label.setText(_installed_label_text(0))
            self._outdated_label.setText(_outdated_label_text(0))
            self._model.set_installed(())
            self._reset_space_bar()
            self._update_action_buttons_enabled()
            self._update_connection_label()

        def _on_detect_failed(self, message: str, *, quiet: bool = False) -> None:
            # WHY: a failed detect/choose leaves no pen to act on - clear any stale one from a
            # previous successful detect so Install can't target a pen that's no longer confirmed.
            # An install failure must NOT go through this path: the pen itself is still valid then.
            state = ConnectionState.PLUGGED_IN_UNMOUNTED if pen_device_present() else ConnectionState.DISCONNECTED
            self._clear_pen(state)
            if quiet:
                # WHY: an automatic background detect must never interrupt the user with a message
                # box - only the manual Detect/Choose folder path does that
                self.statusBar().showMessage(message)
            else:
                self._on_failed(message)

        def _poll_pen(self) -> None:
            if self._pen_job.is_running:
                return  # never interrupt an install or detect already in flight

            if self._connection is ConnectionState.CONNECTED:
                if not pen_still_mounted(self._pen):
                    self._clear_pen(ConnectionState.DISCONNECTED)
                    self.statusBar().showMessage(_("Pen disconnected"))
                return

            if not pen_device_present():
                if self._connection is not ConnectionState.DISCONNECTED:
                    self._clear_pen(ConnectionState.DISCONNECTED)
                return

            if self._connection is ConnectionState.SAFELY_UNMOUNTED:
                # WHY: the device is still plugged in but was deliberately unmounted - a quiet poll
                # must not immediately remount it; only an explicit Mount (_start_mount) or Detect
                # (_detect), or the device disappearing and reappearing, should resume auto-detect
                return

            if self._connection is ConnectionState.PLUGGED_IN_UNMOUNTED and not pen_device_mounted():
                # WHY: avoids spinning up a QThread + findmnt (and the list-clear/stylesheet/button
                # churn that follows a failed detect) every 2s while the pen just sits there plugged
                # in but unmounted - wait for it to actually be mounted before trying again
                return

            self._detect(quiet=True)

        def _run_task(
            self,
            task: Callable[[PhaseProgress], T],
            on_success: Callable[[T], None],
            busy_message: str,
            *,
            on_failure: Callable[[str], None] | None = None,
        ) -> None:
            if not self._pen_job.start(
                task,
                on_success,
                on_failure if on_failure is not None else self._on_failed,
                on_progress=self._on_task_progress,
            ):
                return  # one pen operation at a time
            self._progress_phase = None
            self.statusBar().showMessage(busy_message)
            self._update_action_buttons_enabled()

        def _on_pen_job_finished(self) -> None:
            self._progress_bar.hide()
            self._update_action_buttons_enabled()
            if self._summary_refresh_pending:
                self._summary_refresh_pending = False
                if self._connection is ConnectionState.CONNECTED:
                    self._refresh_pen_summary()

        def _on_task_progress(self, phase: str, done: int, total: int | None) -> None:
            if not self._progress_bar.isVisible():
                self._progress_bar.show()
            if phase != self._progress_phase:
                self._progress_phase = phase
                self._progress_bar.setFormat(_PROGRESS_BAR_FORMATS.get(phase, "%p%"))
                self.statusBar().showMessage(_PROGRESS_STATUS_TEXT.get(phase, phase))
            if total:
                if self._progress_bar.maximum() != 100:
                    self._progress_bar.setRange(0, 100)
                self._progress_bar.setValue(min(100, int(done * 100 / total)))
            elif self._progress_bar.maximum() != 0:
                self._progress_bar.setRange(0, 0)

        def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
            if self._pen_job.is_running:
                # WHY: a pen write is in flight - exiting now would destroy a running QThread and
                # leave a large .tiptoi-*.tmp on the pen mid-copy
                event.ignore()
                QMessageBox.information(self, "tiptoi", _("Please wait until the pen operation finishes."))
                return
            self._poll_timer.stop()
            self._catalog_job.stop(2000)
            if self._catalog_job.is_running:
                # WHY: stop() only bounds the wait - a catalog load can still be blocked on the
                # network for up to urllib's 30s timeout, and destroying its unparented QThread now
                # would abort the process (exit 134, reproduced). Hide instead and let
                # _on_catalog_job_finished close for real once the thread actually exits.
                self._close_when_idle = True
                self.hide()
                event.ignore()
                return
            super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    if _PYSIDE6_IMPORT_ERROR is not None:
        print(
            _("error: the GUI requires PySide6 - install with: pip install tiptoi-linux[gui]"),
            file=sys.stderr,
        )
        return 1
    app = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
    app.setApplicationName("tiptoi")
    # WHY: KDE/Wayland match the running window to tiptoi.desktop (and its icon) by this name; without
    # it the title bar shows the generic X11 placeholder icon
    app.setDesktopFileName("tiptoi")
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    # WHY: Qt's own strings (dialog buttons, standard context menus) come from Qt's own
    # translation files, not ours; harmless when qtbase_<locale>.qm isn't installed
    translator = QTranslator(app)
    if translator.load(
        QLocale.system(), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    ):
        app.installTranslator(translator)
    window = MainWindow()
    window.show()
    window.start_initial_load()
    # WHY: one quiet detect right away, rather than waiting out the first 2s poll interval
    QTimer.singleShot(0, window._poll_pen)
    exit_code = app.exec()
    # WHY: drain queued deleteLater events before the interpreter tears down, otherwise Qt can
    # process them against a half-destroyed Python and deadlock on exit
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
