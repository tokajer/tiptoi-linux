"""Desktop GUI for browsing the tiptoi product catalog."""

from __future__ import annotations

import functools
import sys
import traceback
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum, IntEnum, auto
from pathlib import Path
from typing import TypeVar

from tiptoi_linux.catalog import Catalog, CatalogError, LoadedCatalog, Product, cache_path, gme_file_name, load_catalog
from tiptoi_linux.errors import TiptoiError
from tiptoi_linux.i18n import _, ngettext
from tiptoi_linux.pen import (
    Pen,
    PenSummary,
    Phase,
    PhaseProgress,
    delete_title,
    device_mounted,
    device_plugged_in,
    find_pen,
    install_title,
    mount_pen,
    pen_mount_alive,
    pen_summary,
    unmount_pen,
)
from tiptoi_linux.streams import percent

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
POLL_INTERVAL_MS = 2000


class Column(IntEnum):
    ON_PEN = 0
    NAME = 1
    VERSION = 2


class ConnectionState(Enum):
    DISCONNECTED = auto()
    PLUGGED_IN_UNMOUNTED = auto()
    SAFELY_UNMOUNTED = auto()


@dataclass(frozen=True)
class Connected:
    pen: Pen


# WHY: the pen travels inside the connected state, so "there is a pen to act on" and "we are
# connected" can never disagree
Connection = ConnectionState | Connected


class PollAction(Enum):
    NOTHING = auto()
    PEN_LOST = auto()  # a connected pen is no longer mounted
    DEVICE_GONE = auto()  # a not-connected pen was unplugged
    DETECT = auto()


def next_poll_action(
    connection: Connection,
    *,
    job_running: bool,
    device_plugged_in: Callable[[], bool],
    device_mounted: Callable[[], bool],
    pen_mount_alive: Callable[[Pen], bool],
) -> PollAction:
    """What the periodic pen poll should do. The probes are callables so only the needed ones run."""
    if job_running:
        return PollAction.NOTHING  # never interrupt an install or detect already in flight

    if isinstance(connection, Connected):
        return PollAction.NOTHING if pen_mount_alive(connection.pen) else PollAction.PEN_LOST

    if not device_plugged_in():
        return PollAction.NOTHING if connection is ConnectionState.DISCONNECTED else PollAction.DEVICE_GONE

    if connection is ConnectionState.SAFELY_UNMOUNTED:
        # WHY: the device is still plugged in but was deliberately unmounted - a quiet poll must not
        # immediately remount it; only an explicit Mount or Detect, or the device disappearing and
        # reappearing, should resume auto-detect
        return PollAction.NOTHING

    if connection is ConnectionState.PLUGGED_IN_UNMOUNTED and not device_mounted():
        # WHY: avoids spinning up a QThread + findmnt (and the list-clear/stylesheet/button churn
        # that follows a failed detect) every poll while the pen just sits there plugged in but
        # unmounted - wait for it to actually be mounted before trying again
        return PollAction.NOTHING

    return PollAction.DETECT


@dataclass
class Services:
    """Everything the window calls outside Qt; tests pass fakes instead of patching module globals."""

    load_catalog: Callable[..., LoadedCatalog] = load_catalog
    find_pen: Callable[..., Pen] = find_pen
    mount_pen: Callable[[], Pen] = mount_pen
    unmount_pen: Callable[[Pen], None] = unmount_pen
    install_title: Callable[..., Path] = install_title
    delete_title: Callable[[Pen, str], int] = delete_title
    pen_summary: Callable[[Pen, Catalog | None], PenSummary] = pen_summary
    device_plugged_in: Callable[[], bool] = device_plugged_in
    device_mounted: Callable[[], bool] = device_mounted
    pen_mount_alive: Callable[[Pen], bool] = pen_mount_alive


T = TypeVar("T")


_COLUMN_HEADERS = (_("On pen"), _("Name"), _("Version"))

_PHASE_STATUS_TEXT = {
    Phase.DOWNLOAD: _("Downloading…"),
    Phase.COPY: _("Copying to pen…"),
    Phase.VERIFY: _("Verifying on pen…"),
}

# WHY: text plus colour for every not-connected state, in one place instead of scattered across a
# chain of ifs that re-read live pen state; Connected has its own {path} template
_CONNECTION_TEXT_AND_COLOR: dict[ConnectionState, tuple[str, str]] = {
    ConnectionState.DISCONNECTED: (_("○ Not connected"), ""),
    ConnectionState.PLUGGED_IN_UNMOUNTED: (
        _("○ Pen plugged in but not mounted — open it in your file manager"),
        "darkorange",
    ),
    ConnectionState.SAFELY_UNMOUNTED: (_("✓ Unmounted — safe to unplug"), "green"),
}
_CONNECTED_TEXT = _("● Connected — {path}")
_CONNECTED_COLOR = "green"


def _installed_label_text(n: int) -> str:
    return _("On the &pen ({n})").format(n=n)


def _outdated_label_text(n: int) -> str:
    return _("&Outdated ({n})").format(n=n)


if _PYSIDE6_IMPORT_ERROR is None:

    def _themed_icon(widget: QWidget, theme_name: str, fallback: QStyle.StandardPixmap) -> QIcon:
        return QIcon.fromTheme(theme_name, widget.style().standardIcon(fallback))

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
            except CatalogError:
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
            except TiptoiError as exc:
                self.failed.emit(str(exc))
            except Exception as exc:  # WHY: anything escaping a worker thread would kill the app
                # WHY: an unexpected exception is a bug, not a pen/network condition - keep the
                # traceback on stderr so it can be reported, and say so in the message
                traceback.print_exc()
                self.failed.emit(_("unexpected error: {error}").format(error=exc))
            else:
                self.succeeded.emit(result)
            finally:
                self.finished.emit()

    def _noop_progress(phase: Phase, done: int, total: int | None) -> None:
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

    class PenPanel(QWidget):
        """The right-hand pane: storage bar, detect/choose buttons, installed and outdated titles."""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)

            self.space_bar = QProgressBar()
            self.space_bar.setAccessibleName(_("Pen storage"))
            self.space_bar.setRange(0, 100)

            self.detect_button = QPushButton(_("&Detect pen"))
            self.detect_button.setAccessibleName(_("Detect pen"))
            self.detect_button.setIcon(
                _themed_icon(self, "edit-find", QStyle.StandardPixmap.SP_FileDialogContentsView)
            )

            self.choose_button = QPushButton(_("&Choose folder…"))
            self.choose_button.setAccessibleName(_("Choose pen folder"))
            self.choose_button.setIcon(_themed_icon(self, "folder-open", QStyle.StandardPixmap.SP_DirOpenIcon))

            self.installed_list = QListWidget()
            self.installed_list.setAccessibleName(_("Titles on the pen"))
            self.installed_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
            self.installed_label = QLabel()
            self.installed_label.setBuddy(self.installed_list)

            self.outdated_list = QListWidget()
            self.outdated_list.setAccessibleName(_("Outdated titles"))
            self.outdated_label = QLabel()
            self.outdated_label.setBuddy(self.outdated_list)

            self.delete_button = QPushButton(_("De&lete selected"))
            self.delete_button.setAccessibleName(_("Delete selected titles from the pen"))
            self.delete_button.setIcon(_themed_icon(self, "edit-delete", QStyle.StandardPixmap.SP_TrashIcon))
            self.delete_button.setEnabled(False)

            detect_row = QHBoxLayout()
            detect_row.addWidget(self.detect_button)
            detect_row.addWidget(self.choose_button)

            layout = QVBoxLayout(self)
            layout.addWidget(QLabel(_("Storage")))
            layout.addWidget(self.space_bar)
            layout.addLayout(detect_row)
            layout.addWidget(self.installed_label)
            layout.addWidget(self.installed_list)
            layout.addWidget(self.outdated_label)
            layout.addWidget(self.outdated_list)
            layout.addWidget(self.delete_button)
            self.setMinimumWidth(250)

            self._installed_names: tuple[str, ...] = ()
            self.reset()

        def show_summary(self, summary: PenSummary) -> None:
            self._show_installed(summary.installed)
            self._show_space(summary.free, summary.total)
            self.outdated_list.clear()
            for title in summary.outdated:
                self.outdated_list.addItem(title.describe())
            self.outdated_label.setText(_outdated_label_text(len(summary.outdated)))

        def reset(self) -> None:
            self._installed_names = ()
            self.installed_list.clear()
            self.outdated_list.clear()
            self.installed_label.setText(_installed_label_text(0))
            self.outdated_label.setText(_outdated_label_text(0))
            self.space_bar.setValue(0)
            self.space_bar.setFormat("—")

        def selected_names(self) -> list[str]:
            return [item.text() for item in self.installed_list.selectedItems()]

        def _show_installed(self, installed: tuple[str, ...]) -> None:
            self.installed_label.setText(_installed_label_text(len(installed)))
            if installed == self._installed_names:
                return
            self._installed_names = installed
            self.installed_list.clear()
            for name in installed:
                self.installed_list.addItem(name)

        def _show_space(self, free: int, total: int) -> None:
            used_percent = 0 if total <= 0 else percent(total - free, total)
            self.space_bar.setValue(used_percent)
            self.space_bar.setFormat(
                _("{free} free of {total}").format(
                    free=QLocale().formattedDataSize(free), total=QLocale().formattedDataSize(total)
                )
            )

    class MainWindow(QMainWindow):
        def __init__(self, services: Services | None = None) -> None:
            super().__init__()
            self._services = services if services is not None else Services()
            self.setWindowTitle("tiptoi")
            self.resize(1100, 700)

            self._model = ProductTableModel()
            self._proxy = QSortFilterProxyModel(self)
            self._proxy.setSourceModel(self._model)
            self._proxy.setFilterKeyColumn(Column.NAME)
            self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

            self._pen_panel = PenPanel()
            self._pen_panel.detect_button.clicked.connect(lambda: self._detect())
            self._pen_panel.choose_button.clicked.connect(self._choose_pen_folder)
            self._pen_panel.delete_button.clicked.connect(self._start_delete_selected)
            self._pen_panel.installed_list.itemSelectionChanged.connect(self._update_delete_button_enabled)

            header = self._build_header()
            catalog_pane = self._build_catalog_pane()

            self._splitter = QSplitter(Qt.Orientation.Horizontal)
            self._splitter.addWidget(catalog_pane)
            self._splitter.addWidget(self._pen_panel)
            self._splitter.setChildrenCollapsible(False)
            self._splitter.setSizes([660, 440])

            central = QWidget()
            layout = QVBoxLayout(central)
            layout.addWidget(header)
            layout.addWidget(self._splitter)
            self.setCentralWidget(central)
            self._set_tab_order()

            self._progress_bar = QProgressBar()
            self._progress_bar.setAccessibleName(_("Task progress"))
            self._progress_bar.hide()
            self.statusBar().addPermanentWidget(self._progress_bar)

            self._catalog_job = BackgroundJob(self)
            self._catalog_job.finished.connect(self._on_catalog_job_finished)
            self._close_when_idle = False

            self._pen_job = BackgroundJob(self)
            self._pen_job.finished.connect(self._on_pen_job_finished)
            self._progress_phase: Phase | None = None
            self._summary_refresh_pending = False

            self._catalog: Catalog | None = None
            self._connection: Connection = ConnectionState.DISCONNECTED
            self._last_connection_label: tuple[str, str] | None = None
            self._update_connection_label()

            # WHY: parented, unlike the QThreads BackgroundJob creates - the "never parent / never
            # deleteLater" rule is specific to QThread; an ordinary QTimer is safely owned and torn
            # down by Qt
            self._poll_timer = QTimer(self)
            self._poll_timer.setInterval(POLL_INTERVAL_MS)
            self._poll_timer.timeout.connect(self._poll_pen)
            self._poll_timer.start()

        def _build_header(self) -> QWidget:
            self._connection_label = QLabel()
            self._connection_label.setAccessibleName(_("Pen connection"))

            self._mount_button = QPushButton(_("&Mount"))
            self._mount_button.setAccessibleName(_("Mount the pen"))
            self._mount_button.setIcon(
                _themed_icon(self, "drive-removable-media", QStyle.StandardPixmap.SP_DriveFDIcon)
            )
            self._mount_button.setEnabled(False)
            self._mount_button.clicked.connect(self._start_mount)

            self._unmount_button = QPushButton(_("&Unmount"))
            self._unmount_button.setAccessibleName(_("Unmount the pen so it can be unplugged"))
            self._unmount_button.setIcon(_themed_icon(self, "media-eject", QStyle.StandardPixmap.SP_MediaStop))
            self._unmount_button.setEnabled(False)
            self._unmount_button.clicked.connect(self._start_unmount)

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

            header = QWidget()
            header_layout = QHBoxLayout(header)
            header_layout.addWidget(logo_label)
            header_layout.addWidget(title_label)
            header_layout.addStretch()
            header_layout.addWidget(self._connection_label)
            header_layout.addWidget(self._mount_button)
            header_layout.addWidget(self._unmount_button)
            return header

        def _build_catalog_pane(self) -> QWidget:
            search_label = QLabel(_("&Search:"))
            self._search_box = QLineEdit()
            self._search_box.setPlaceholderText(_("Search products…"))
            self._search_box.setClearButtonEnabled(True)
            self._search_box.setAccessibleName(_("Search products"))
            search_label.setBuddy(self._search_box)
            self._search_box.textChanged.connect(self._proxy.setFilterFixedString)

            self._refresh_button = QPushButton(_("Refresh"))
            self._refresh_button.setIcon(_themed_icon(self, "view-refresh", QStyle.StandardPixmap.SP_BrowserReload))
            self._refresh_button.clicked.connect(self._start_refresh)

            self._table = QTableView()
            self._table.setModel(self._proxy)
            self._table.setSortingEnabled(True)
            self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
            self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
            self._table.setAlternatingRowColors(True)
            self._table.setAccessibleName(_("Products"))
            header_view = self._table.horizontalHeader()
            header_view.setStretchLastSection(False)
            header_view.setSectionResizeMode(Column.ON_PEN, QHeaderView.ResizeMode.ResizeToContents)
            header_view.setSectionResizeMode(Column.NAME, QHeaderView.ResizeMode.Stretch)
            self._table.verticalHeader().setVisible(False)
            self._table.selectionModel().selectionChanged.connect(self._update_install_button_enabled)
            self._table.doubleClicked.connect(self._on_table_double_clicked)

            self._install_button = QPushButton(_("&Install on pen"))
            self._install_button.setAccessibleName(_("Install selected"))
            self._install_button.setIcon(
                _themed_icon(self, "document-save", QStyle.StandardPixmap.SP_DialogSaveButton)
            )
            self._install_button.setEnabled(False)
            self._install_button.clicked.connect(self._start_install_selected)

            search_row = QHBoxLayout()
            search_row.addWidget(search_label)
            search_row.addWidget(self._search_box)
            search_row.addWidget(self._refresh_button)

            install_row = QHBoxLayout()
            install_row.addStretch()
            install_row.addWidget(self._install_button)

            pane = QWidget()
            layout = QVBoxLayout(pane)
            layout.addLayout(search_row)
            layout.addWidget(self._table)
            layout.addLayout(install_row)
            return pane

        def _set_tab_order(self) -> None:
            panel = self._pen_panel
            order = (
                self._mount_button,
                self._unmount_button,
                self._search_box,
                self._refresh_button,
                self._table,
                self._install_button,
                panel.detect_button,
                panel.choose_button,
                panel.installed_list,
                panel.outdated_list,
                panel.delete_button,
            )
            for first, second in zip(order, order[1:]):
                self.setTabOrder(first, second)

        @property
        def _pen(self) -> Pen | None:
            return self._connection.pen if isinstance(self._connection, Connected) else None

        # --- catalog -------------------------------------------------------------------------

        def _start_load(self, *, force: bool) -> None:
            services = self._services  # WHY: captured on the GUI thread; the task runs on a worker thread
            if not self._catalog_job.start(
                lambda progress: services.load_catalog(force=force, allow_stale=not force),
                self._on_loaded,
                self._show_error,
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

        def _on_loaded(self, loaded: LoadedCatalog) -> None:
            catalog = loaded.catalog
            self._catalog = catalog
            self._model.set_products(catalog.products)
            n = len(catalog.products)
            if loaded.stale_reason is not None:
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
                # catalog load. Flag it and let _on_pen_job_finished refresh once that job's outcome
                # (and self._connection) is settled.
                self._summary_refresh_pending = True
            elif isinstance(self._connection, Connected):
                # WHY: no pen job in flight - refresh now so a pen detected before this load finished
                # is summarised against the real catalog instead of the None/stale one it started with
                # (a folder pen has no source and find_pen() without override would drop it)
                self._refresh_pen_summary(self._connection.pen)

        def _show_error(self, message: str) -> None:
            self.statusBar().showMessage(_("error: {message}").format(message=message))
            QMessageBox.warning(self, "tiptoi", message)

        # --- selection and buttons -------------------------------------------------------------

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

        def _pen_idle(self) -> bool:
            return self._pen is not None and not self._pen_job.is_running

        def _update_install_button_enabled(self) -> None:
            has_selection = bool(self._table.selectionModel().selectedRows())
            self._install_button.setEnabled(self._pen_idle() and has_selection)

        def _update_delete_button_enabled(self) -> None:
            has_selection = bool(self._pen_panel.installed_list.selectedItems())
            self._pen_panel.delete_button.setEnabled(self._pen_idle() and has_selection)

        def _update_action_buttons_enabled(self) -> None:
            running = self._pen_job.is_running
            self._pen_panel.detect_button.setEnabled(not running)
            self._pen_panel.choose_button.setEnabled(not running)
            self._update_install_button_enabled()
            self._update_delete_button_enabled()
            self._mount_button.setEnabled(
                not running and self._pen is None and self._services.device_plugged_in()
            )
            pen = self._pen
            self._unmount_button.setEnabled(not running and pen is not None and not pen.is_folder)

        # --- pen operations --------------------------------------------------------------------

        def _run_task(
            self,
            task: Callable[[PhaseProgress], T],
            on_success: Callable[[T], None],
            busy_message: str | None,
            *,
            on_failure: Callable[[str], None] | None = None,
        ) -> None:
            if not self._pen_job.start(
                task,
                on_success,
                on_failure if on_failure is not None else self._show_error,
                on_progress=self._on_task_progress,
            ):
                return  # one pen operation at a time
            self._progress_phase = None
            if busy_message is not None:
                self.statusBar().showMessage(busy_message)
            self._update_action_buttons_enabled()

        def _run_pen_op(
            self,
            op: Callable[[PhaseProgress], tuple[Pen, T]],
            busy_message: str | None,
            *,
            describe: Callable[[T], str] | None = None,
            then: Callable[[T], None] | None = None,
            on_failure: Callable[[str], None] | None = None,
            changes_pen: bool = False,
            announce: bool = True,
        ) -> None:
            """Run op on the worker thread, then re-summarise the pen it returns and show that summary."""
            catalog = self._catalog  # WHY: captured on the GUI thread; the task runs on a worker thread
            services = self._services

            def task(progress: PhaseProgress) -> tuple[T, PenSummary]:
                pen, result = op(progress)
                return result, services.pen_summary(pen, catalog)

            def succeeded(outcome: tuple[T, PenSummary]) -> None:
                result, summary = outcome
                self._on_pen_refreshed(
                    summary, message=describe(result) if describe else None, announce=announce
                )
                if then is not None:
                    then(result)

            def failed(message: str) -> None:
                if changes_pen and isinstance(self._connection, Connected):
                    # WHY: an install or delete can fail after changing the pen (the 2nd of two
                    # deletes, a verify after the rename) - re-read it once the job has finished so
                    # the lists never show titles that are no longer there
                    self._summary_refresh_pending = True
                (on_failure if on_failure is not None else self._show_error)(message)

            self._run_task(task, succeeded, busy_message, on_failure=failed)

        def _detect(self, override: Path | None = None, *, quiet: bool = False) -> None:
            services = self._services
            self._run_pen_op(
                lambda progress: (services.find_pen(override=override), None),
                _("Detecting pen…"),
                on_failure=functools.partial(self._on_detect_failed, quiet=quiet),
            )

        def _choose_pen_folder(self) -> None:
            result = QFileDialog.getExistingDirectory(self, _("Choose pen folder"))
            if not result:
                return
            self._detect(Path(result))

        def _start_mount(self) -> None:
            services = self._services

            def mount(progress: PhaseProgress) -> tuple[Pen, Pen]:
                pen = services.mount_pen()
                return pen, pen

            self._run_pen_op(
                mount,
                _("Mounting pen…"),
                describe=lambda pen: _("Pen mounted: {path}").format(path=pen.mountpoint),
                on_failure=self._on_mount_failed,
            )

        def _on_mount_failed(self, message: str) -> None:
            # WHY: a failed Mount after a deliberate Unmount must not leave the state stuck at
            # SAFELY_UNMOUNTED (the poll never auto-detects there) - fall back to whatever the device
            # presence says, same as a failed detect
            self._clear_pen(self._state_without_pen())
            self._show_error(message)

        def _start_unmount(self) -> None:
            pen = self._pen
            if pen is None:
                return
            services = self._services
            self._run_task(
                lambda progress: services.unmount_pen(pen),
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
            services = self._services

            def install(progress: PhaseProgress) -> tuple[Pen, None]:
                services.install_title(pen, product, progress=progress)
                return pen, None

            self._run_pen_op(
                install,
                _("Installing {name}…").format(name=product.name),
                describe=lambda _result: _("Installed {name} — verified on the pen").format(name=product.name),
                then=lambda _result: self._show_install_done(product.name),
                changes_pen=True,
            )

        def _show_install_done(self, name: str) -> None:
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
            names = self._pen_panel.selected_names()
            if pen is None or not names:
                return

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

            services = self._services

            def delete(progress: PhaseProgress) -> tuple[Pen, int]:
                return pen, sum(services.delete_title(pen, name) for name in names)

            n = len(names)
            self._run_pen_op(
                delete,
                ngettext("Deleting {n} title…", "Deleting {n} titles…", n).format(n=n),
                describe=lambda freed: ngettext(
                    "Deleted {n} title, freed {size}", "Deleted {n} titles, freed {size}", n
                ).format(n=n, size=QLocale().formattedDataSize(freed)),
                changes_pen=True,
            )

        def _refresh_pen_summary(self, pen: Pen, *, quiet: bool = False) -> None:
            if self._pen_job.is_running:
                self._summary_refresh_pending = True
                return
            # WHY: a quiet refresh only updates the lists - it must not replace whatever the status
            # bar is saying (e.g. the error of the install that made the refresh necessary)
            self._run_pen_op(
                lambda progress: (pen, None),
                None if quiet else _("Pen ready: {path}").format(path=pen.mountpoint),
                announce=not quiet,
            )

        def _on_pen_job_finished(self) -> None:
            self._progress_bar.hide()
            self._update_action_buttons_enabled()
            if self._summary_refresh_pending:
                self._summary_refresh_pending = False
                if isinstance(self._connection, Connected):
                    self._refresh_pen_summary(self._connection.pen, quiet=True)

        def _on_task_progress(self, phase: Phase, done: int, total: int | None) -> None:
            if not self._progress_bar.isVisible():
                self._progress_bar.show()
            if phase != self._progress_phase:
                self._progress_phase = phase
                status_text = _PHASE_STATUS_TEXT[phase]
                self._progress_bar.setFormat(f"{status_text} %p%")
                self.statusBar().showMessage(status_text)
            if total:
                if self._progress_bar.maximum() != 100:
                    self._progress_bar.setRange(0, 100)
                self._progress_bar.setValue(percent(done, total))
            elif self._progress_bar.maximum() != 0:
                self._progress_bar.setRange(0, 0)

        # --- connection state ------------------------------------------------------------------

        def _on_pen_refreshed(
            self, summary: PenSummary, *, message: str | None = None, announce: bool = True
        ) -> None:
            self._connection = Connected(summary.pen)
            self._pen_panel.show_summary(summary)
            self._model.set_installed(summary.installed)
            self._update_action_buttons_enabled()
            self._update_connection_label()
            if announce:
                default_message = _("Pen ready: {path}").format(path=summary.pen.mountpoint)
                self.statusBar().showMessage(message if message is not None else default_message)

        def _update_connection_label(self) -> None:
            if isinstance(self._connection, Connected):
                text = _CONNECTED_TEXT.format(path=self._connection.pen.mountpoint)
                color = _CONNECTED_COLOR
            else:
                text, color = _CONNECTION_TEXT_AND_COLOR[self._connection]
            if (text, color) == self._last_connection_label:
                return  # WHY: setStyleSheet() is expensive to call on every poll for no change
            self._last_connection_label = (text, color)
            self._connection_label.setText(text)
            self._connection_label.setStyleSheet(f"color: {color};" if color else "")

        def _clear_pen(self, state: ConnectionState) -> None:
            # WHY: shared by a failed detect/choose and by the poll noticing a known pen is no longer
            # mounted - Install must never target a pen that's no longer confirmed
            self._connection = state
            self._pen_panel.reset()
            self._model.set_installed(())
            self._update_action_buttons_enabled()
            self._update_connection_label()

        def _state_without_pen(self) -> ConnectionState:
            if self._services.device_plugged_in():
                return ConnectionState.PLUGGED_IN_UNMOUNTED
            return ConnectionState.DISCONNECTED

        def _on_detect_failed(self, message: str, *, quiet: bool = False) -> None:
            # WHY: a failed detect/choose leaves no pen to act on - clear any stale one from a
            # previous successful detect so Install can't target a pen that's no longer confirmed.
            # An install failure must NOT go through this path: the pen itself is still valid then.
            self._clear_pen(self._state_without_pen())
            if quiet:
                # WHY: an automatic background detect must never interrupt the user with a message
                # box - only the manual Detect/Choose folder path does that
                self.statusBar().showMessage(message)
            else:
                self._show_error(message)

        def _poll_pen(self) -> None:
            action = next_poll_action(
                self._connection,
                job_running=self._pen_job.is_running,
                device_plugged_in=self._services.device_plugged_in,
                device_mounted=self._services.device_mounted,
                pen_mount_alive=self._services.pen_mount_alive,
            )
            if action is PollAction.PEN_LOST:
                self._clear_pen(ConnectionState.DISCONNECTED)
                self.statusBar().showMessage(_("Pen disconnected"))
            elif action is PollAction.DEVICE_GONE:
                self._clear_pen(ConnectionState.DISCONNECTED)
            elif action is PollAction.DETECT:
                self._detect(quiet=True)

        def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
            if self._pen_job.is_running:
                # WHY: a pen write is in flight - exiting now would destroy a running QThread and
                # leave a large .tiptoi-*.tmp on the pen mid-copy
                event.ignore()
                QMessageBox.information(self, "tiptoi", _("Please wait until the pen operation finishes."))
                return
            self._poll_timer.stop()
            # WHY: no wait - a still-running catalog load is handled below by hiding instead
            self._catalog_job.stop(0)
            if self._catalog_job.is_running:
                # WHY: a catalog load can still be blocked on the network for up to urllib's 30s
                # timeout, and destroying its unparented QThread now would abort the process
                # (exit 134, reproduced). Hide instead and let _on_catalog_job_finished close for
                # real once the thread actually exits.
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
    # WHY: one quiet detect right away, rather than waiting out the first poll interval
    QTimer.singleShot(0, window._poll_pen)
    exit_code = app.exec()
    # WHY: drain queued deleteLater events before the interpreter tears down, otherwise Qt can
    # process them against a half-destroyed Python and deadlock on exit
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
