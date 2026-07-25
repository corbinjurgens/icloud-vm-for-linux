"""The status window: health rows plus the selective-sync UI (v2 plan 6.2).

Nothing here touches the CIFS mounts on the GUI thread.  Every read, write and
request/response poll is handed to ``run_async`` (a ``QThreadPool`` helper
supplied by ``__main__``) and comes back through a signal, because a sick CIFS
mount can block even a ``stat``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QSizePolicy, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QTreeWidgetItemIterator, QVBoxLayout, QWidget,
)

from . import bridge, health
from .tray import VM_VIEWER_URL, open_externally

ROLE_PATH = Qt.ItemDataRole.UserRole
ROLE_KIND = Qt.ItemDataRole.UserRole + 1
ROLE_EXTRA = Qt.ItemDataRole.UserRole + 2

COL_NAME, COL_SIZE, COL_ITEMS, COL_INCLUDED, COL_STATE = range(5)

LIST_TIMEOUT_SECONDS = 15
LIST_PAGE = 1000

DOT_COLORS = {health.GREEN: "#2e9e4f", health.YELLOW: "#d99b1a", health.RED: "#c8402c"}

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
        self._pending_requests: dict[str, dict] = {}
        self._polls_in_flight: set[str] = set()
        self._suppress_item_signals = False
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

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_status_tab(), "Status")
        self._tabs.addTab(self._build_sync_tab(), "Selective Sync")
        central_layout.addWidget(self._tabs, 1)
        self.setCentralWidget(central)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1000)
        self._poll_timer.timeout.connect(self._poll_pending)
        self._poll_timer.start()

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
        # Shown only after a failed power-on; the controller wires it to Retry.
        self._retry_button = QPushButton("Retry start")
        self._retry_button.clicked.connect(self.retry_start_requested.emit)
        self._retry_button.hide()
        buttons.addWidget(self._retry_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
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
        self._tree_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
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
        "starting": f"background: #e7f0fb; color: #1a3a63;",
        "shutdown": f"background: #e7f0fb; color: #1a3a63;",
        "error": f"background: #fbecea; color: {DOT_COLORS[health.RED]};",
    }

    def show_banner(self, text: str, kind: str = "starting") -> None:
        """Show a transitional banner above the tabs."""
        self._banner.setText(text)
        self._banner.setStyleSheet(self.BANNER_STYLES.get(kind, self.BANNER_STYLES["starting"]))
        self._banner.show()

    def hide_banner(self) -> None:
        self._banner.hide()

    def show_retry_start(self, visible: bool) -> None:
        self._retry_button.setVisible(visible)

    def set_bridge_controls_enabled(self, enabled: bool) -> None:
        """Enable/disable everything that reads or writes the bridge/mount.

        The Selective Sync tab and the "Open iCloud folder" button touch CIFS;
        the "Open VM screen" button does not and stays available for diagnosis.
        """
        self._tabs.setTabEnabled(1, enabled)
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
        self._pending_requests.clear()

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
        self._pending_requests.clear()
        self._polls_in_flight.clear()
        self._items_by_path.clear()
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

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        kind = item.data(0, ROLE_KIND)
        if kind not in ("dir", "root"):
            return
        if item.data(0, ROLE_EXTRA) == "files-loaded":
            return
        path = item.data(0, ROLE_PATH) or ""
        if bridge.is_under(path, self._wanted) and path:
            return   # the whole subtree is excluded; tree.json does not recurse there
        item.setData(0, ROLE_EXTRA, "files-loaded")
        self._request_files(path, 0)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(0, ROLE_KIND) != "more":
            return
        extra = item.data(0, ROLE_EXTRA) or {}
        parent = item.parent()
        if parent is not None:
            parent.removeChild(item)
        self._request_files(extra.get("path", ""), int(extra.get("offset", 0)))

    def _request_files(self, path: str, offset: int) -> None:
        if self._io_paused:
            return
        def work():
            return bridge.request_listing(path, offset=offset, limit=LIST_PAGE)

        def done(request_id: str):
            self._pending_requests[request_id] = {
                "path": path,
                "offset": offset,
                "deadline": datetime.now(timezone.utc).timestamp() + LIST_TIMEOUT_SECONDS,
            }

        def failed(message: str):
            self._sync_error.setText(f"Cannot ask the guest agent for a file listing: {message}")
            self._sync_error.show()

        self._run_async(work, done, failed)

    def _poll_pending(self) -> None:
        if self._io_paused:
            return
        now = datetime.now(timezone.utc).timestamp()
        for request_id, info in list(self._pending_requests.items()):
            if request_id in self._polls_in_flight:
                continue
            if now > info["deadline"]:
                del self._pending_requests[request_id]
                self._sync_error.setText("Guest agent not responding to file-listing requests.")
                self._sync_error.show()
                self._run_async(lambda rid=request_id: bridge.cancel_request(rid), lambda _r: None,
                                lambda _m: None)
                continue
            self._polls_in_flight.add(request_id)
            self._run_async(
                lambda rid=request_id: (rid, bridge.poll_response(rid)),
                self._on_response,
                lambda message, rid=request_id: self._on_response_failed(rid, message),
            )

    def _on_response_failed(self, request_id: str, message: str) -> None:
        self._polls_in_flight.discard(request_id)
        self._pending_requests.pop(request_id, None)
        self._sync_error.setText(f"Bad reply from the guest agent: {message}")
        self._sync_error.show()

    def _on_response(self, payload) -> None:
        request_id, response = payload
        self._polls_in_flight.discard(request_id)
        if response is None:
            return
        info = self._pending_requests.pop(request_id, None)
        if info is None:
            return
        parent = self._items_by_path.get((info["path"] or "").lower())
        if parent is None:
            return
        error = response.get("error")
        if isinstance(error, str) and error:
            self._sync_error.setText(f"{info['path'] or 'iCloud Drive'}: {error}")
            self._sync_error.show()
            return
        # A successful listing must not clear the config-error banner: it
        # explains why Apply is disabled (fail closed) until reload succeeds.
        if not self._config_error:
            self._sync_error.hide()

        self._suppress_item_signals = True
        try:
            for entry in response.get("files") or []:
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
            next_offset = response.get("nextOffset")
            if isinstance(next_offset, int) and not isinstance(next_offset, bool):
                more = QTreeWidgetItem(parent, ["Load more…", "", "", "", ""])
                more.setData(0, ROLE_KIND, "more")
                more.setData(0, ROLE_EXTRA, {"path": info["path"], "offset": next_offset})
                more.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        finally:
            self._suppress_item_signals = False
        self._refresh_check_states()
        self._refresh_state_column()

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

        def failed(message: str):
            self._apply_writing = False
            QMessageBox.warning(
                self, "Could not apply",
                f"{message}\n\nNothing was changed. Reloading the current configuration.")
            self.reload_selective_sync()

        self._apply_button.setEnabled(False)
        self._apply_writing = True
        self._run_async(work, done, failed)
