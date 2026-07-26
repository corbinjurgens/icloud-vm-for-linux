"""The status window: health rows plus the selective-sync UI (v2 plan 6.2).

Nothing here touches the CIFS mounts on the GUI thread.  Every read, write and
request/response poll is handed to ``run_async`` (a ``QThreadPool`` helper
supplied by ``__main__``) and comes back through a signal, because a sick CIFS
mount can block even a ``stat``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSizePolicy, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget,
)

from . import __version__, bridge, filtering, firstrun, health, listing, power, sizes
from .tray import VM_VIEWER_URL, open_externally

ROLE_PATH = Qt.ItemDataRole.UserRole
ROLE_KIND = Qt.ItemDataRole.UserRole + 1
ROLE_EXTRA = Qt.ItemDataRole.UserRole + 2

COL_NAME, COL_SIZE, COL_ITEMS, COL_INCLUDED, COL_STATE = range(5)

LIST_TIMEOUT_SECONDS = 15
LIST_PAGE = 1000

DOT_COLORS = {health.GREEN: "#2e9e4f", health.YELLOW: "#d99b1a", health.RED: "#c8402c"}
#: Underlined and coloured, so a "Load more…" row reads as something to click.
LINK_COLOR = "#1a5fb4"
#: Setup-check dots. A warning is not a blocker, and must not look like one.
SETUP_COLORS = {
    firstrun.OK: DOT_COLORS[health.GREEN],
    firstrun.WARN: DOT_COLORS[health.YELLOW],
    firstrun.FAIL: DOT_COLORS[health.RED],
}

EXCLUDE_WARNING = (
    "These items will disappear from /mnt/icloud on this computer. Windows will "
    "free their local content after iCloud reports it safe to dehydrate; this "
    "may not be immediate. They remain in iCloud and on your other devices. "
    "Nothing will be deleted."
)
INCLUDE_WARNING = (
    "These items will reappear in /mnt/icloud. Their content is normally "
    "online-only after exclusion and downloads when opened; any content still "
    "cached remains local and uses VM disk space."
)


def _fmt_count(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return f"{value:,}"
    return "-"


class MainWindow(QMainWindow):
    #: Emitted when the user asks for an immediate health refresh; ``__main__``
    #: owns the polling loop so only one gather is ever in flight.
    refresh_requested = Signal()
    #: Emitted when the window is closed with no tray to fall back to; the
    #: controller runs the same confirmed Quit flow (v2 plan D29) rather than
    #: letting ``QuitOnLastWindowClosed`` bypass it.
    quit_requested = Signal()
    #: Emitted by the in-window "Retry start" button after a failed power-on (the
    #: no-tray equivalent of the tray's Retry action).
    retry_start_requested = Signal()
    #: D30: the same two lifecycle actions the tray offers, for a no-tray session
    #: and for anyone who has the window open anyway.
    power_off_requested = Signal()
    start_requested = Signal()
    #: D31 first-run assistant.
    setup_recheck_requested = Signal()
    create_vm_requested = Signal()
    connect_requested = Signal()
    env_file_selected = Signal(str)

    def __init__(self, run_async: Callable[..., None]) -> None:
        super().__init__()
        self._run_async = run_async
        #: When a system tray exists, closing the window only hides it.
        self.hide_on_close = False
        #: While paused (VM starting, or a power-off transition running) the
        #: window dispatches no new bridge I/O — a sick or absent mount must not
        #: be touched before startup succeeds, and unmount must not race an
        #: in-flight read/write (v2 plan D29).
        self._io_paused = False
        #: True only while an Apply write is actually in flight, so the shutdown
        #: gate never begins an unmount mid-write.
        self._apply_writing = False
        self.setWindowTitle("iCloud bridge")
        self.resize(880, 620)

        self._status: dict | None = None
        self._tree: dict | None = None
        self._checks: list[health.Check] = []

        # Selective-sync model
        self._wanted: list[str] = []            # pending selection, canonical casing
        self._loaded_wanted: list[str] = []     # what exclusions.json held when loaded
        self._loaded_revision: int | None = None
        self._config_error: str | None = None
        self._last_written_revision: int | None = None
        self._last_write_at: datetime | None = None
        self._items_by_path: dict[str, QTreeWidgetItem] = {}
        #: Per-folder idle/loading/loaded state and the in-flight list requests;
        #: a Qt-free model so the failure/retry cases are testable.
        self._requests = listing.FolderRequests()
        #: Sizes of files listed during this session, keyed by lowercase path.
        #: tree.json carries recursive sizes for folders only, so this is the
        #: only size source for an excluded *file* (item 7).
        self._file_sizes: dict[str, Any] = {}
        #: Recursive folder sizes harvested from tree.json, same keying.
        self._folder_sizes: dict[str, Any] = {}
        self._polls_in_flight: set[str] = set()
        self._suppress_item_signals = False
        #: Set while the code (not the user) expands items, so filter-driven
        #: expansion never fires a listing request.
        self._suppress_expansion = False
        #: The operator's own expanded/collapsed state, saved when a filter
        #: starts rearranging the tree and restored when it is cleared.
        self._pre_filter_expanded: set[str] | None = None
        self._power_action = power.ACTION_NONE
        self._env_path = ""
        self._tree_generated_at: str | None = None

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)

        # Transitional banner (starting / shutting down / start failed). Hidden in
        # the steady state; the health rows carry ordinary status.
        self._banner = QLabel("")
        self._banner.setWordWrap(True)
        self._banner.setContentsMargins(10, 8, 10, 8)
        self._banner.hide()
        central_layout.addWidget(self._banner)

        # Health incident notices for a session with no tray to notify through.
        # Separate from the banner on purpose: a lifecycle message ("shutdown
        # aborted, a file is in use") must not be overwritten by the next health
        # snapshot, nor the other way round.
        self._notice = QLabel("")
        self._notice.setWordWrap(True)
        self._notice.setContentsMargins(10, 6, 10, 6)
        self._notice.setStyleSheet(f"color: {DOT_COLORS[health.RED]};")
        self._notice.hide()
        central_layout.addWidget(self._notice)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_status_tab(), "Status")
        self._sync_page = self._build_sync_tab()
        self._tabs.addTab(self._sync_page, "Selective Sync")
        # The Setup tab exists only while the bridge is not provisioned; it is
        # inserted in front of the others so it cannot be missed, and removed
        # again once the bridge is running (D31).
        self._setup_page = self._build_setup_tab()
        central_layout.addWidget(self._tabs, 1)
        self.setCentralWidget(central)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1000)
        self._poll_timer.timeout.connect(self._poll_pending)
        self._poll_timer.start()

    # ------------------------------------------------------------- setup tab --

    def _build_setup_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self._setup_title = QLabel("Setup required")
        font = self._setup_title.font()
        font.setBold(True)
        self._setup_title.setFont(font)
        layout.addWidget(self._setup_title)

        self._setup_intro = QLabel("")
        self._setup_intro.setWordWrap(True)
        layout.addWidget(self._setup_intro)

        # Which copy of the compose file and provisioning scripts is in play —
        # shown because it is resolved from the installation, never from the
        # working directory, and the operator should not have to guess (D31).
        self._setup_paths = QLabel("")
        self._setup_paths.setWordWrap(True)
        self._setup_paths.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._setup_paths.setEnabled(False)
        layout.addWidget(self._setup_paths)

        env_row = QHBoxLayout()
        self._env_label = QLabel("Configuration file: (none selected)")
        self._env_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        env_row.addWidget(self._env_label, 1)
        self._env_button = QPushButton("Choose .env file…")
        self._env_button.clicked.connect(self._choose_env_file)
        env_row.addWidget(self._env_button)
        layout.addLayout(env_row)

        self._setup_checks = QWidget()
        self._setup_checks_layout = QVBoxLayout(self._setup_checks)
        self._setup_checks_layout.setContentsMargins(0, 0, 0, 0)
        self._setup_checks_layout.setSpacing(4)
        layout.addWidget(self._setup_checks)

        self._setup_detail = QLabel("")
        self._setup_detail.setWordWrap(True)
        self._setup_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._setup_detail.hide()
        layout.addWidget(self._setup_detail)

        layout.addStretch(1)

        buttons = QHBoxLayout()
        self._setup_recheck = QPushButton("Re-check")
        self._setup_recheck.clicked.connect(self.setup_recheck_requested.emit)
        buttons.addWidget(self._setup_recheck)
        self._setup_create = QPushButton("Create Windows VM")
        self._setup_create.clicked.connect(self.create_vm_requested.emit)
        self._setup_create.setEnabled(False)
        buttons.addWidget(self._setup_create)
        setup_vm = QPushButton("Open VM screen")
        setup_vm.clicked.connect(lambda: open_externally(VM_VIEWER_URL))
        buttons.addWidget(setup_vm)
        self._setup_connect = QPushButton("Check setup and connect")
        self._setup_connect.clicked.connect(self.connect_requested.emit)
        self._setup_connect.hide()
        buttons.addWidget(self._setup_connect)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return page

    def _choose_env_file(self) -> None:
        start = os.path.dirname(self._env_path) if self._env_path else os.path.expanduser("~")
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "Choose the .env file", start, "Environment files (.env *.env);;All files (*)")
        if chosen:
            self.env_file_selected.emit(chosen)

    def show_setup_tab(self) -> None:
        """Put the assistant in front; the other tabs stay but are disabled."""
        if self._tabs.indexOf(self._setup_page) < 0:
            self._tabs.insertTab(0, self._setup_page, "Setup")
        self._tabs.setCurrentWidget(self._setup_page)

    def hide_setup_tab(self) -> None:
        index = self._tabs.indexOf(self._setup_page)
        if index >= 0:
            self._tabs.removeTab(index)

    def update_setup(self, *, title: str, intro: str, checks, paths: str,
                     env_path: str, can_create: bool, show_connect: bool,
                     detail: str = "", busy: bool = False) -> None:
        """Render one assistant state.  Pure presentation of firstrun's answers."""
        self._env_path = env_path
        self._setup_title.setText(title)
        self._setup_intro.setText(intro)
        self._setup_paths.setText(paths)
        self._env_label.setText(f"Configuration file: {env_path or '(none selected)'}")
        self._setup_create.setEnabled(can_create and not busy)
        self._setup_recheck.setEnabled(not busy)
        self._setup_connect.setVisible(show_connect)
        self._setup_connect.setEnabled(not busy)
        self._env_button.setEnabled(not busy)
        if detail:
            self._setup_detail.setText(detail)
            self._setup_detail.show()
        else:
            self._setup_detail.hide()

        while self._setup_checks_layout.count():
            item = self._setup_checks_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for check in checks:
            self._setup_checks_layout.addWidget(self._build_check_row(check))

    def _build_check_row(self, check) -> QWidget:
        row = QWidget()
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(1)
        head = QHBoxLayout()
        dot = QLabel("●")
        dot.setFixedWidth(16)
        dot.setStyleSheet(f"color: {SETUP_COLORS.get(check.status, DOT_COLORS[health.RED])};")
        name = QLabel(check.name)
        name.setMinimumWidth(170)
        name.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        detail = QLabel(check.detail)
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(dot)
        head.addWidget(name)
        head.addWidget(detail, 1)
        row_layout.addLayout(head)
        if check.command and check.status != firstrun.OK:
            command = QLabel(check.command)
            command.setContentsMargins(16, 0, 0, 0)
            command.setStyleSheet("font-family: monospace;")
            command.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            command.setToolTip("Run this yourself in a terminal — the GUI never "
                               "installs packages or runs sudo commands for you")
            row_layout.addWidget(command)
        return row

    # ------------------------------------------------------------ status tab --

    def _build_status_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self._check_rows = QWidget()
        self._check_layout = QVBoxLayout(self._check_rows)
        self._check_layout.setContentsMargins(0, 0, 0, 0)
        self._check_layout.setSpacing(4)
        layout.addWidget(self._check_rows)
        self._check_widgets: dict[str, tuple[QLabel, QLabel]] = {}

        layout.addSpacing(8)
        self._disk_label = QLabel("Guest disk: -")
        self._local_label = QLabel("Fully local content: -")
        self._local_label.setToolTip("partially downloaded files are not counted")
        self._scan_label = QLabel("Last full scan: -")
        for label in (self._disk_label, self._local_label, self._scan_label):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)

        note = QLabel("“Fully local content” is a lower bound: partially downloaded "
                      "files are not counted, so it is not the space iCloud uses in the VM.")
        note.setWordWrap(True)
        note.setEnabled(False)
        layout.addWidget(note)

        layout.addStretch(1)

        buttons = QHBoxLayout()
        open_files = QPushButton("Open iCloud folder")
        open_files.clicked.connect(lambda: open_externally(bridge.mount_dir()))
        self._open_files_button = open_files
        open_vm = QPushButton("Open VM screen")
        open_vm.clicked.connect(lambda: open_externally(VM_VIEWER_URL))
        refresh = QPushButton("Refresh now")
        refresh.clicked.connect(self.request_refresh)
        for button in (open_files, open_vm, refresh):
            buttons.addWidget(button)
        # One lifecycle button, whose meaning the controller sets from the D30
        # state machine: Retry start, Power off bridge, or Start bridge. Hidden
        # whenever no mutating action is safe.
        self._power_button = QPushButton("Retry start")
        self._power_button.clicked.connect(self._on_power_button)
        self._power_button.hide()
        buttons.addWidget(self._power_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        # Selectable so a bug report can carry the exact build; the same string
        # `icloud-bridge-gui --version` prints.
        version = QLabel(f"Version {__version__}")
        version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        version.setEnabled(False)
        layout.addWidget(version)
        return page

    def _ensure_check_row(self, name: str) -> tuple[QLabel, QLabel]:
        if name in self._check_widgets:
            return self._check_widgets[name]
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        dot = QLabel("●")
        dot.setFixedWidth(16)
        title = QLabel(name)
        title.setMinimumWidth(150)
        title.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        detail = QLabel("")
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row_layout.addWidget(dot)
        row_layout.addWidget(title)
        row_layout.addWidget(detail, 1)
        self._check_layout.addWidget(row)
        self._check_widgets[name] = (dot, detail)
        return self._check_widgets[name]

    # -------------------------------------------------------- selective sync --

    def _build_sync_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        intro = QLabel("Unchecked items are hidden from /mnt/icloud and are not downloaded. "
                       "Excluding never deletes anything from iCloud. "
                       "“Included” does not mean “downloaded” — included files "
                       "download when something reads them.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # An honest total, updated on every selection/status/tree change. Never
        # phrased as space saved: sizes are logical and dehydration is
        # asynchronous (item 7).
        self._summary_label = QLabel("")
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)
        summary_note = QLabel(
            "Excluded items are hidden from Linux and requested online-only. "
            "Sizes are logical content size, not space already freed — the "
            "Status tab reports reclamation separately.")
        summary_note.setWordWrap(True)
        summary_note.setEnabled(False)
        layout.addWidget(summary_note)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("folder name or path")
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter_edit, 1)
        layout.addLayout(filter_row)
        self._filter_note = QLabel(
            "Searches the folder list plus files already loaded in this session.")
        self._filter_note.setWordWrap(True)
        self._filter_note.setEnabled(False)
        self._filter_note.hide()
        layout.addWidget(self._filter_note)

        self._sync_error = QLabel("")
        self._sync_error.setWordWrap(True)
        self._sync_error.setStyleSheet(f"color: {DOT_COLORS[health.RED]};")
        self._sync_error.hide()
        layout.addWidget(self._sync_error)

        self._tree_widget = QTreeWidget()
        self._tree_widget.setColumnCount(5)
        self._tree_widget.setHeaderLabels(["Name", "Size", "Items", "Included", "State"])
        self._tree_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree_widget.setUniformRowHeights(True)
        self._tree_widget.itemChanged.connect(self._on_item_changed)
        self._tree_widget.itemExpanded.connect(self._on_item_expanded)
        # "Load more…" responds to a single click, Enter/Space, and a double
        # click alike; the handler is idempotent because one gesture can emit
        # more than one of these.
        self._tree_widget.itemClicked.connect(self._on_item_activated)
        self._tree_widget.itemActivated.connect(self._on_item_activated)
        self._tree_widget.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self._tree_widget, 1)

        buttons = QHBoxLayout()
        self._reload_button = QPushButton("Reload")
        self._reload_button.clicked.connect(self.reload_selective_sync)
        self._remove_button = QPushButton("Remove exclusion")
        self._remove_button.setToolTip("Clear a configured exclusion whose item no longer exists")
        self._remove_button.setEnabled(False)
        self._remove_button.clicked.connect(self._remove_selected_missing)
        self._apply_button = QPushButton("Apply")
        self._apply_button.clicked.connect(self._apply)
        buttons.addWidget(self._reload_button)
        buttons.addWidget(self._remove_button)
        buttons.addStretch(1)
        buttons.addWidget(self._apply_button)
        layout.addLayout(buttons)
        return page

    # ------------------------------------------------------------- refreshing --

    def closeEvent(self, event) -> None:   # noqa: N802 (Qt naming)
        if self.hide_on_close:
            event.ignore()
            self.hide()
        else:
            # No tray: closing the only window is the Quit action, but it must go
            # through the controller's confirmation/transition, not exit under us.
            event.ignore()
            self.quit_requested.emit()

    # ------------------------------------------------------- lifecycle gating --

    BANNER_STYLES = {
        "starting": "background: #e7f0fb; color: #1a3a63;",
        "shutdown": "background: #e7f0fb; color: #1a3a63;",
        # Neutral grey: an intentional off state is not an error (D30).
        "off": "background: #ececec; color: #3a3a3a;",
        "error": f"background: #fbecea; color: {DOT_COLORS[health.RED]};",
    }

    def show_banner(self, text: str, kind: str = "starting") -> None:
        """Show a transitional banner above the tabs."""
        self._banner.setText(text)
        self._banner.setStyleSheet(self.BANNER_STYLES.get(kind, self.BANNER_STYLES["starting"]))
        self._banner.show()

    def hide_banner(self) -> None:
        self._banner.hide()

    def show_notice(self, text: str) -> None:
        """The window's stand-in for a desktop notification (no tray)."""
        self._notice.setText(text)
        self._notice.show()

    def hide_notice(self) -> None:
        self._notice.hide()

    #: Button text per D30 action. ``power.ACTION_SETUP`` has no button here —
    #: first-run guidance is a banner, not a one-click action.
    POWER_BUTTON_TEXT = {
        power.ACTION_RETRY: "Retry start",
        power.ACTION_POWER_OFF: "Power off bridge",
        power.ACTION_START: "Start bridge",
    }

    def set_power_action(self, action: str) -> None:
        """Show the one lifecycle action the current state allows (D30)."""
        self._power_action = action
        text = self.POWER_BUTTON_TEXT.get(action)
        if text is None:
            self._power_button.hide()
            return
        self._power_button.setText(text)
        self._power_button.setToolTip(
            "Unmount both shares and power off the Windows VM. This app keeps running."
            if action == power.ACTION_POWER_OFF else "")
        self._power_button.show()

    def _on_power_button(self) -> None:
        if self._power_action == power.ACTION_POWER_OFF:
            self.power_off_requested.emit()
        elif self._power_action == power.ACTION_START:
            self.start_requested.emit()
        elif self._power_action == power.ACTION_RETRY:
            self.retry_start_requested.emit()

    def clear_health_rows(self) -> None:
        """Blank the health rows when they stop describing anything real.

        An intentionally powered-off bridge must not leave the last snapshot on
        screen: those green/red dots would describe a machine that no longer
        exists (D30).
        """
        self._checks = []
        self._status = None
        for dot, detail in self._check_widgets.values():
            dot.setStyleSheet("color: palette(mid);")
            detail.setText("-")
        self._disk_label.setText("Guest disk: -")
        self._local_label.setText("Fully local content: -")
        self._scan_label.setText("Last full scan: -")

    def set_bridge_controls_enabled(self, enabled: bool) -> None:
        """Enable/disable everything that reads or writes the bridge/mount.

        The Selective Sync tab and the "Open iCloud folder" button touch CIFS;
        the "Open VM screen" button does not and stays available for diagnosis.
        """
        index = self._tabs.indexOf(self._sync_page)
        if index >= 0:
            self._tabs.setTabEnabled(index, enabled)
        if hasattr(self, "_open_files_button"):
            self._open_files_button.setEnabled(enabled)

    def set_io_paused(self, paused: bool) -> None:
        """Gate new bridge I/O; also reflect it in the controls."""
        self._io_paused = paused
        self.set_bridge_controls_enabled(not paused)

    def apply_in_flight(self) -> bool:
        return self._apply_writing

    def quiesce(self) -> None:
        """Stop scheduling bridge I/O and drop any queued list requests.

        In-flight worker tasks still finish; the controller waits for them to
        drain before it lets the helper unmount (v2 plan D29).
        """
        self.set_io_paused(True)
        self._poll_timer.stop()
        self._requests.reset()

    def resume(self) -> None:
        """Undo :meth:`quiesce` after an aborted shutdown."""
        self.set_io_paused(False)
        self._poll_timer.start()

    def last_write_info(self) -> tuple[int | None, datetime | None]:
        """The revision this GUI last wrote, for the D23 revision-lag check."""
        return self._last_written_revision, self._last_write_at

    def request_refresh(self) -> None:
        """Ask the owner (``__main__``) for a health snapshot right away."""
        self.refresh_requested.emit()

    def apply_snapshot(self, snapshot: health.Snapshot) -> None:
        """Update every widget from a freshly gathered snapshot (GUI thread)."""
        self._checks = snapshot.checks
        self._status = snapshot.status
        for check in snapshot.checks:
            dot, detail = self._ensure_check_row(check.name)
            dot.setStyleSheet(f"color: {DOT_COLORS[check.severity]};")
            detail.setText(check.detail)

        status = snapshot.status or {}
        self._disk_label.setText(
            "Guest disk: "
            f"{health.human_bytes(status.get('diskFreeBytes'))} free of "
            f"{health.human_bytes(status.get('diskTotalBytes'))}")
        self._local_label.setText(
            "Fully local content: "
            f"{health.human_bytes(status.get('fullyLocalLogicalBytes'))} "
            "(partially downloaded files are not counted)")
        scan = status.get("scan") if isinstance(status.get("scan"), dict) else {}
        self._scan_label.setText(f"Last full scan: {scan.get('lastCompletedAt') or '-'}")

        generated = (snapshot.tree or {}).get("generatedAt")
        if snapshot.tree is not None and generated != self._tree_generated_at:
            if self._wanted != self._loaded_wanted:
                # The operator has staged, unapplied edits; rebuilding now
                # would silently discard them (the agent rewrites tree.json
                # every ten minutes). Keep showing the stale tree -- the next
                # snapshot after Apply or a reload picks the new one up.
                self._refresh_state_column()
            else:
                self._tree = snapshot.tree
                self._tree_generated_at = generated
                self.reload_selective_sync()
        else:
            self._refresh_state_column()
        # status.json can supply the last applied size for a configured root.
        self._update_excluded_summary()

    # -------------------------------------------------- selective-sync loading --

    def reload_selective_sync(self) -> None:
        """Re-read exclusions.json and rebuild the tree from the last tree.json."""
        if self._io_paused:
            return
        def work():
            return bridge.read_exclusions()

        def done(config):
            self._config_error = None
            self._loaded_revision = config["revision"]
            self._loaded_wanted = list(config["exclusions"])
            self._wanted = list(config["exclusions"])
            self._rebuild_tree()

        def failed(message: str):
            # Fail closed: never present an empty selection that a later Apply
            # would turn into "include everything".
            self._config_error = message
            self._loaded_revision = None
            self._loaded_wanted = []
            self._wanted = []
            self._rebuild_tree()

        self._run_async(work, done, failed)

    def _rebuild_tree(self) -> None:
        # A new tree generation: every folder is idle again and any answer still
        # in flight from the previous tree is discarded rather than applied.
        self._requests.reset()
        self._polls_in_flight.clear()
        self._items_by_path.clear()
        self._file_sizes.clear()
        self._folder_sizes.clear()
        self._suppress_item_signals = True
        try:
            self._tree_widget.clear()
            if self._config_error:
                self._sync_error.setText(
                    f"Cannot read exclusions.json: {self._config_error}. "
                    "Apply is disabled until the configuration is readable again.")
                self._sync_error.show()
            else:
                self._sync_error.hide()

            root_dirs = []
            if isinstance(self._tree, dict):
                node = self._tree.get("root")
                if isinstance(node, dict) and isinstance(node.get("dirs"), list):
                    root_dirs = node["dirs"]

            root_item = QTreeWidgetItem(self._tree_widget, ["iCloud Drive", "", "", "", ""])
            root_item.setData(0, ROLE_PATH, "")
            root_item.setData(0, ROLE_KIND, "root")
            root_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self._items_by_path[""] = root_item
            for child in root_dirs:
                self._add_dir_item(root_item, child)
            root_item.setExpanded(True)

            self._add_missing_group()
        finally:
            self._suppress_item_signals = False
        self._refresh_check_states()
        self._refresh_state_column()
        self._update_buttons()
        self._update_excluded_summary()
        self._update_excluded_summary()
        # A rebuild replaces every row, so a filter typed before it must be
        # re-applied against the new ones.
        self._pre_filter_expanded = None
        self._apply_filter()

    def _add_dir_item(self, parent: QTreeWidgetItem, node: Any) -> None:
        if not isinstance(node, dict):
            return
        path = node.get("path")
        name = node.get("name")
        if not isinstance(path, str) or not isinstance(name, str):
            return
        item = QTreeWidgetItem(parent, [
            name,
            health.human_bytes(node.get("logicalBytes")),
            _fmt_count(node.get("fileCount")),
            "", "",
        ])
        item.setData(0, ROLE_PATH, path)
        item.setData(0, ROLE_KIND, "dir")
        self._items_by_path[path.lower()] = item
        self._folder_sizes[path.lower()] = node.get("logicalBytes")
        for child in node.get("dirs") or []:
            self._add_dir_item(item, child)
        if not item.childCount():
            # Give it an expand arrow so the user can pull in the file listing.
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)

    def _add_missing_group(self) -> None:
        """Configured paths the guest cannot find (renamed, deleted, or not yet arrived)."""
        known = set(self._items_by_path)
        status_states = self._status_exclusion_states()
        missing = [p for p in self._wanted
                   if p.lower() not in known or status_states.get(p.lower()) == "not-found"]
        if not missing:
            return
        group = QTreeWidgetItem(self._tree_widget, ["Missing configured items", "", "", "", ""])
        group.setData(0, ROLE_KIND, "missing-group")
        group.setFlags(Qt.ItemFlag.ItemIsEnabled)
        group.setToolTip(0, "These exclusions name paths the guest cannot see. Until the item "
                            "exists, Windows has no object to protect, so it is reported "
                            "not-found rather than healthy.")
        for path in missing:
            child = QTreeWidgetItem(group, [path, "", "", "", "not-found"])
            child.setData(0, ROLE_PATH, path)
            child.setData(0, ROLE_KIND, "missing")
            child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        group.setExpanded(True)

    # ---------------------------------------------------------------- filter --

    def _filterable_paths(self) -> list[str]:
        """Every row a filter can match: folders, loaded files, missing items."""
        paths = []
        for item in self._iter_items():
            if item.data(0, ROLE_KIND) in ("dir", "file", "missing"):
                path = item.data(0, ROLE_PATH)
                if isinstance(path, str) and path:
                    paths.append(path)
        return paths

    def _capture_expansion(self) -> set[str]:
        expanded = set()
        for item in self._iter_items():
            if item.isExpanded():
                path = item.data(0, ROLE_PATH)
                if isinstance(path, str):
                    expanded.add(filtering.normalize(path))
        return expanded

    def _apply_filter(self, _text: str | None = None) -> None:
        """Show only matching rows and their ancestors; never change selection.

        This is presentation only: ``_wanted``, the check states and the
        in-memory tree are untouched, so clearing the filter restores exactly
        what was there — including which folders the operator had open.
        """
        query = self._filter_edit.text()
        visible = filtering.visible_paths(query, self._filterable_paths())

        # Expanding ancestors to reveal a match must not be read as the operator
        # opening a folder, or every visible folder would fire a list request.
        self._suppress_expansion = True
        try:
            if visible is None:
                self._filter_note.hide()
                for item in self._iter_items():
                    item.setHidden(False)
                if self._pre_filter_expanded is not None:
                    for item in self._iter_items():
                        path = item.data(0, ROLE_PATH)
                        if isinstance(path, str):
                            item.setExpanded(
                                filtering.normalize(path) in self._pre_filter_expanded)
                    self._pre_filter_expanded = None
                return

            if self._pre_filter_expanded is None:
                self._pre_filter_expanded = self._capture_expansion()

            for item in self._iter_items():
                kind = item.data(0, ROLE_KIND)
                if kind == "root":
                    item.setHidden(False)
                    item.setExpanded(True)
                    continue
                if kind in ("missing-group", "more"):
                    continue        # handled below / follows its parent
                path = item.data(0, ROLE_PATH)
                shown = isinstance(path, str) and filtering.normalize(path) in visible
                item.setHidden(not shown)
                if shown and kind == "dir":
                    item.setExpanded(True)
            self._hide_empty_groups()
            self._filter_note.setText(
                "No folders or loaded files match this filter."
                if not visible else
                "Searching the folder list plus files already loaded in this session.")
            self._filter_note.show()
        finally:
            self._suppress_expansion = False

    def _hide_empty_groups(self) -> None:
        """Group rows (Missing configured items, Load more…) follow their children."""
        for index in range(self._tree_widget.topLevelItemCount()):
            group = self._tree_widget.topLevelItem(index)
            if group.data(0, ROLE_KIND) != "missing-group":
                continue
            any_visible = any(not group.child(i).isHidden()
                              for i in range(group.childCount()))
            group.setHidden(not any_visible)
        for item in self._iter_items():
            if item.data(0, ROLE_KIND) == "more":
                parent = item.parent()
                item.setHidden(parent is not None and parent.isHidden())

    # --------------------------------------------------------- size summary --

    def _update_excluded_summary(self) -> None:
        """Restate how much is excluded, from whatever sources currently know."""
        status_sizes = {}
        entries = (self._status or {}).get("exclusions")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    status_sizes[entry["path"].lower()] = entry.get("logicalBytes")
        summary = sizes.summarize(
            self._wanted,
            folder_sizes=self._folder_sizes,
            file_sizes=self._file_sizes,
            status_sizes=status_sizes,
            configured=self._loaded_wanted,
        )
        self._summary_label.setText(summary.text())

    # ------------------------------------------------------------ check state --

    def _iter_items(self):
        iterator = QTreeWidgetItemIterator(self._tree_widget)
        while iterator.value():
            yield iterator.value()
            iterator += 1

    def _refresh_check_states(self) -> None:
        lowered = [w.lower() for w in self._wanted]
        palette = self._tree_widget.palette()
        normal = QBrush(palette.color(QPalette.ColorRole.Text))
        greyed = QBrush(QColor(palette.color(QPalette.ColorRole.Text)).lighter(170))

        self._suppress_item_signals = True
        try:
            for item in self._iter_items():
                kind = item.data(0, ROLE_KIND)
                if kind not in ("dir", "file", "root"):
                    continue
                path = item.data(0, ROLE_PATH) or ""
                low = path.lower()
                excluded_self = low in lowered
                inside = (not excluded_self) and bridge.is_under(path, self._wanted)
                has_excluded_child = any(w.startswith(low + "/") for w in lowered) if path else bool(lowered)

                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if kind != "root" and not inside:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                item.setFlags(flags)

                if excluded_self or inside:
                    state = Qt.CheckState.Unchecked
                elif has_excluded_child:
                    state = Qt.CheckState.PartiallyChecked
                else:
                    state = Qt.CheckState.Checked
                item.setCheckState(COL_INCLUDED, state)

                brush = greyed if (excluded_self or inside) else normal
                for column in range(self._tree_widget.columnCount()):
                    item.setForeground(column, brush)
        finally:
            self._suppress_item_signals = False

    def _status_exclusion_states(self) -> dict[str, str]:
        states: dict[str, str] = {}
        entries = (self._status or {}).get("exclusions")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    state = entry.get("state")
                    if isinstance(state, str):
                        states[entry["path"].lower()] = state
        return states

    def _status_exclusion_details(self) -> dict[str, str]:
        details: dict[str, str] = {}
        entries = (self._status or {}).get("exclusions")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    detail = entry.get("detail")
                    if isinstance(detail, str) and detail:
                        details[entry["path"].lower()] = detail
        return details

    def _refresh_state_column(self) -> None:
        states = self._status_exclusion_states()
        details = self._status_exclusion_details()
        lowered = [w.lower() for w in self._wanted]
        loaded = {w.lower() for w in self._loaded_wanted}
        self._suppress_item_signals = True
        try:
            for item in self._iter_items():
                kind = item.data(0, ROLE_KIND)
                if kind not in ("dir", "file"):
                    continue
                path = (item.data(0, ROLE_PATH) or "").lower()
                if path in lowered:
                    if path not in loaded:
                        text = "will be excluded on Apply"
                    else:
                        text = states.get(path, "applying")
                elif bridge.is_under(item.data(0, ROLE_PATH) or "", self._wanted):
                    text = "excluded (parent)"
                elif path in loaded:
                    text = "will be re-included on Apply"
                else:
                    text = ""
                item.setText(COL_STATE, text)
                item.setToolTip(COL_STATE, details.get(path, ""))
        finally:
            self._suppress_item_signals = False

    # ---------------------------------------------------------- interactions --

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._suppress_item_signals or column != COL_INCLUDED:
            return
        kind = item.data(0, ROLE_KIND)
        if kind not in ("dir", "file"):
            return
        path = item.data(0, ROLE_PATH)
        if not isinstance(path, str) or not path:
            return
        checked = item.checkState(COL_INCLUDED)
        lowered = path.lower()

        if checked == Qt.CheckState.Unchecked:
            # Exclude this item; it subsumes any exclusion inside it (D19).
            self._wanted = [w for w in self._wanted if not w.lower().startswith(lowered + "/")]
            if lowered not in {w.lower() for w in self._wanted}:
                self._wanted.append(path)
        else:
            inner = [w for w in self._wanted if w.lower().startswith(lowered + "/")]
            if lowered in {w.lower() for w in self._wanted}:
                self._wanted = [w for w in self._wanted if w.lower() != lowered]
            elif inner:
                answer = QMessageBox.question(
                    self, "Include everything in this folder?",
                    f"“{path}” contains {len(inner)} excluded item(s):\n\n"
                    + "\n".join(f"  {w}" for w in inner[:20])
                    + ("\n  …" if len(inner) > 20 else "")
                    + "\n\nInclude all of them?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    self._refresh_check_states()
                    return
                self._wanted = [w for w in self._wanted if not w.lower().startswith(lowered + "/")]
        self._refresh_check_states()
        self._refresh_state_column()
        self._update_buttons()
        self._update_excluded_summary()

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if self._suppress_expansion:
            return          # a filter expanded this, not the user
        kind = item.data(0, ROLE_KIND)
        if kind not in ("dir", "root"):
            return
        path = item.data(0, ROLE_PATH) or ""
        if bridge.is_under(path, self._wanted) and path:
            return   # the whole subtree is excluded; tree.json does not recurse there
        # idle -> loading. A folder already loading or loaded is left alone, so
        # collapsing and re-expanding cannot queue a duplicate request; a folder
        # that failed is back at idle, so the same gesture retries it.
        if not self._requests.begin_first_page(path):
            return
        self._request_files(path, 0, listing.FIRST_PAGE)

    def _on_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        """Activate a **Load more…** row (single click, Enter, or double click).

        Qt can emit both ``itemClicked`` and ``itemActivated`` for one gesture,
        so this must be idempotent: the row is consumed on the first call and
        the second finds nothing to do.
        """
        if item.data(0, ROLE_KIND) != "more":
            return
        extra = item.data(0, ROLE_EXTRA)
        if not isinstance(extra, dict):
            return          # already consumed, or already in flight
        # Consume the row's payload and show it as busy rather than removing it:
        # if the request fails the same offset is restored in place.
        item.setData(0, ROLE_EXTRA, None)
        item.setText(COL_NAME, "Loading…")
        item.setDisabled(True)
        self._request_files(extra.get("path", ""), int(extra.get("offset", 0)),
                            listing.MORE)

    def _more_row(self, parent: QTreeWidgetItem, path: str, offset: int) -> None:
        """Add (or restore) the continuation row under ``parent``."""
        more = QTreeWidgetItem(parent, ["Load more…", "", "", "", ""])
        more.setData(0, ROLE_KIND, "more")
        more.setData(0, ROLE_EXTRA, {"path": path, "offset": offset})
        more.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        # Link-like, so it reads as something to click rather than a file named
        # "Load more…"; one click or a keyboard activation is enough.
        font = more.font(COL_NAME)
        font.setUnderline(True)
        more.setFont(COL_NAME, font)
        more.setForeground(COL_NAME, QBrush(QColor(LINK_COLOR)))

    def _restore_more_row(self, request: listing.PendingRequest) -> None:
        """Put a failed continuation back at its original offset so it can retry."""
        parent = self._items_by_path.get((request.path or "").lower())
        if parent is None:
            return
        self._suppress_item_signals = True
        try:
            for index in range(parent.childCount()):
                child = parent.child(index)
                if child.data(0, ROLE_KIND) == "more":
                    parent.removeChild(child)
                    break
            self._more_row(parent, request.path, request.offset)
        finally:
            self._suppress_item_signals = False

    def _request_files(self, path: str, offset: int, kind: str) -> None:
        if self._io_paused:
            # Nothing was dispatched, so leave no state claiming otherwise.
            self._on_request_dropped(path, offset, kind)
            return

        def work():
            return bridge.request_listing(path, offset=offset, limit=LIST_PAGE)

        def done(request_id: str):
            self._requests.dispatched(
                request_id, path, offset, kind,
                datetime.now(timezone.utc).timestamp() + LIST_TIMEOUT_SECONDS)

        def failed(message: str):
            self._on_request_dropped(path, offset, kind)
            self._sync_error.setText(f"Cannot ask the guest agent for a file listing: {message}")
            self._sync_error.show()

        self._run_async(work, done, failed)

    def _on_request_dropped(self, path: str, offset: int, kind: str) -> None:
        """A request that never reached the bridge: undo the UI it claimed."""
        if kind == listing.FIRST_PAGE:
            self._requests.release(path)
            return
        parent = self._items_by_path.get((path or "").lower())
        if parent is not None:
            self._restore_more_row(
                listing.PendingRequest("", path, offset, kind,
                                       self._requests.generation, 0.0))

    def _poll_pending(self) -> None:
        if self._io_paused:
            return
        now = datetime.now(timezone.utc).timestamp()
        for expired in self._requests.expired(now):
            self._fail_request(expired.request_id,
                               "Guest agent not responding to file-listing requests.")
            self._run_async(lambda rid=expired.request_id: bridge.cancel_request(rid),
                            lambda _r: None, lambda _m: None)
        for request_id in self._requests.pending_ids():
            if request_id in self._polls_in_flight:
                continue
            self._polls_in_flight.add(request_id)
            self._run_async(
                lambda rid=request_id: (rid, bridge.poll_response(rid)),
                self._on_response,
                lambda message, rid=request_id: self._on_response_failed(rid, message),
            )

    def _fail_request(self, request_id: str, message: str) -> None:
        """Common failure path: back to idle, or restore the continuation row."""
        request = self._requests.fail(request_id)
        self._sync_error.setText(message)
        self._sync_error.show()
        if request is not None and not request.is_first_page:
            self._restore_more_row(request)

    def _on_response_failed(self, request_id: str, message: str) -> None:
        self._polls_in_flight.discard(request_id)
        self._fail_request(request_id, f"Bad reply from the guest agent: {message}")

    def _on_response(self, payload) -> None:
        request_id, response = payload
        self._polls_in_flight.discard(request_id)
        if response is None:
            return          # not answered yet; the request stays pending
        request = self._requests.take(request_id)
        if request is None:
            return          # unknown, or answered after a Reload rebuilt the tree
        parent = self._items_by_path.get((request.path or "").lower())
        if parent is None:
            if request.is_first_page:
                self._requests.release(request.path)
            return
        error = response.get("error")
        if isinstance(error, str) and error:
            # A guest-side error leaves the folder retryable rather than
            # permanently empty.
            if request.is_first_page:
                self._requests.release(request.path)
            else:
                self._restore_more_row(request)
            self._sync_error.setText(f"{request.path or 'iCloud Drive'}: {error}")
            self._sync_error.show()
            return
        files = response.get("files")
        if files is not None and not isinstance(files, list):
            self._fail_request(
                request_id,
                f"{request.path or 'iCloud Drive'}: malformed file listing from the guest agent.")
            if request.is_first_page:
                self._requests.release(request.path)
            elif request.offset:
                self._restore_more_row(request)
            return
        # A successful listing must not clear the config-error banner: it
        # explains why Apply is disabled (fail closed) until reload succeeds.
        if not self._config_error:
            self._sync_error.hide()

        self._suppress_item_signals = True
        try:
            # The continuation row (now showing "Loading…") is replaced by the
            # page it fetched, plus a fresh row if there is still more.
            for index in range(parent.childCount()):
                child = parent.child(index)
                if child.data(0, ROLE_KIND) == "more":
                    parent.removeChild(child)
                    break
            for entry in files or []:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                path = entry.get("path")
                if not isinstance(name, str) or not isinstance(path, str):
                    continue
                item = QTreeWidgetItem(parent, [
                    name,
                    health.human_bytes(entry.get("logicalBytes")),
                    "",
                    "",
                    "online-only" if entry.get("dataless") else "",
                ])
                item.setData(0, ROLE_PATH, path)
                item.setData(0, ROLE_KIND, "file")
                self._file_sizes[path.lower()] = entry.get("logicalBytes")
            next_offset = response.get("nextOffset")
            if isinstance(next_offset, int) and not isinstance(next_offset, bool):
                self._more_row(parent, request.path, next_offset)
        finally:
            self._suppress_item_signals = False
        # An empty first page is still a successful load: the folder has no
        # files, and re-expanding it must not ask again.
        if request.is_first_page:
            self._requests.mark_loaded(request.path)
        self._refresh_check_states()
        self._refresh_state_column()
        self._update_excluded_summary()

    # ------------------------------------------------------------------ apply --

    def _update_buttons(self) -> None:
        selected = self._tree_widget.selectedItems()
        is_missing = bool(selected) and selected[0].data(0, ROLE_KIND) == "missing"
        self._remove_button.setEnabled(is_missing)
        dirty = sorted(w.lower() for w in self._wanted) != sorted(w.lower() for w in self._loaded_wanted)
        self._apply_button.setEnabled(dirty and self._config_error is None)

    def _remove_selected_missing(self) -> None:
        selected = self._tree_widget.selectedItems()
        if not selected:
            return
        path = selected[0].data(0, ROLE_PATH)
        if not isinstance(path, str):
            return
        self._wanted = [w for w in self._wanted if w.lower() != path.lower()]
        parent = selected[0].parent()
        if parent is not None:
            parent.removeChild(selected[0])
            if parent.childCount() == 0:
                index = self._tree_widget.indexOfTopLevelItem(parent)
                if index >= 0:
                    self._tree_widget.takeTopLevelItem(index)
        self._refresh_check_states()
        self._refresh_state_column()
        self._update_buttons()
        self._update_excluded_summary()

    def _apply(self) -> None:
        if self._io_paused:
            return
        try:
            wanted = bridge.canonicalize(self._wanted)
        except ValueError as exc:
            QMessageBox.critical(self, "Invalid selection", str(exc))
            return
        loaded = {w.lower() for w in self._loaded_wanted}
        added = [w for w in wanted if w.lower() not in loaded]
        removed = [w for w in self._loaded_wanted if w.lower() not in {x.lower() for x in wanted}]
        if not added and not removed:
            return

        parts: list[str] = []
        if added:
            parts.append("Exclude:\n" + "\n".join(f"  {p}" for p in added) + "\n\n" + EXCLUDE_WARNING)
        if removed:
            parts.append("Re-include:\n" + "\n".join(f"  {p}" for p in removed) + "\n\n" + INCLUDE_WARNING)
        answer = QMessageBox.question(
            self, "Apply selective sync changes?", "\n\n".join(parts),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return

        expect = self._loaded_revision
        applied = (self._status or {}).get("appliedRevision")
        last_written = self._last_written_revision

        def work():
            return bridge.write_exclusions(wanted, expect_revision=expect,
                                           applied_revision=applied, last_written=last_written)

        def done(revision: int):
            self._apply_writing = False
            self._last_written_revision = revision
            self._last_write_at = datetime.now(timezone.utc)
            self._loaded_wanted = list(wanted)
            self._loaded_revision = revision
            self._wanted = list(wanted)
            self._refresh_check_states()
            self._refresh_state_column()
            self._update_buttons()
            self._update_excluded_summary()

        def failed(message: str):
            self._apply_writing = False
            QMessageBox.warning(
                self, "Could not apply",
                f"{message}\n\nNothing was changed. Reloading the current configuration.")
            self.reload_selective_sync()

        self._apply_button.setEnabled(False)
        self._apply_writing = True
        self._run_async(work, done, failed)
