"""The status window: health rows, selective sync, and Safe Workspaces.

V2 plan 6.2 for the first two; `docs/plan-safe-local-workspaces.md` §11 for the
third.

Nothing here touches the CIFS mounts on the GUI thread.  Every read, write and
request/response poll is handed to ``run_async`` (a ``QThreadPool`` helper
supplied by ``__main__``) and comes back through a signal, because a sick CIFS
mount can block even a ``stat``.  A file dialog is never pointed at CIFS at all.
"""

from __future__ import annotations

import os
import stat
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from PySide6.QtCore import QEvent, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QGuiApplication, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QScrollArea,
    QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSizePolicy, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget,
)

from . import (__version__, backup, bridge, diagnostics, filtering, firstrun,
               guestprov, health, listing, power, sizes, workspace_sync,
               theme, uistate, workspaces)
from .tray import VM_VIEWER_URL, current_scheme, open_externally

ROLE_PATH = Qt.ItemDataRole.UserRole
ROLE_KIND = Qt.ItemDataRole.UserRole + 1
ROLE_EXTRA = Qt.ItemDataRole.UserRole + 2

COL_NAME, COL_SIZE, COL_ITEMS, COL_INCLUDED, COL_STATE = range(5)

LIST_TIMEOUT_SECONDS = 15
LIST_PAGE = 1000

# ------------------------- app-driven guest provisioning (D40-D44, §4.1/§4.2) --
# Every word and colour below is this app's own.  The guest supplies a state
# name from a closed enum, a work ID from a closed enum, and one bounded human
# line; it never supplies a label, never picks an icon, and never decides what
# code runs.  `detail` is displayed and nothing else.

#: The fixed checklist of §4.2, keyed exactly as `guestprov.CHECK_KEYS`.
PROVISION_CHECK_NAMES = {
    "icloudPackage": "iCloud for Windows",
    "syncRoot": "iCloud Drive folder",
    "shareAccount": "Share account",
    "shareCredential": "Share password",
    "dataShare": "iCloud share",
    "bridgeBoundary": "Bridge share and permissions",
    "agentInstall": "Bridge agent files",
    "agentRuntime": "Bridge agent running",
}

#: The fixed work IDs of §4.1.
PROVISION_WORK_NAMES = {
    "install-icloud": "Install iCloud for Windows",
    "wait-for-signin": "Wait for you to sign in to iCloud",
    "create-share-account": "Create the share account",
    "reset-share-credential": "Set the share password",
    "repair-data-share": "Repair the iCloud share",
    "repair-bridge-boundary": "Repair the bridge share and permissions",
    "update-agent": "Update bridge agent",
}

#: Which work item owns each check, so a row can say what is planned for it.
PROVISION_CHECK_WORK = {
    "icloudPackage": "install-icloud",
    "syncRoot": "wait-for-signin",
    "shareAccount": "create-share-account",
    "shareCredential": "reset-share-credential",
    "dataShare": "repair-data-share",
    "bridgeBoundary": "repair-bridge-boundary",
    "agentInstall": "update-agent",
    "agentRuntime": "update-agent",
}

#: Five classes that must be told apart at a glance: ready, work needed,
#: waiting for the operator, blocked, and the credential — which is
#: `unverifiable` by construction and must never look like a green claim
#: (§4.2).  Monochrome glyphs as well as colour, so the distinction survives a
#: colour-blind reader and a monochrome screenshot.
PROVISION_READY = "ready"
PROVISION_WORK = "work"
PROVISION_WAIT = "wait"
PROVISION_BLOCKED = "blocked"
PROVISION_CREDENTIAL = "credential"
PROVISION_PENDING = "pending"

SEVERITY_GLYPHS = {
    health.GREEN: "●",
    health.YELLOW: "▲",
    health.RED: "■",
}

#: No severity to report yet: a row cleared with `clear_health_rows`, a check not
#: yet inspected. Hollow rather than filled, so it cannot be mistaken for green in
#: a monochrome screenshot.
NEUTRAL_GLYPH = "○"

SEVERITY_WORDS = {
    health.GREEN: "healthy",
    health.YELLOW: "warning",
    health.RED: "failed",
}

PROVISION_GLYPHS = {
    PROVISION_READY: SEVERITY_GLYPHS[health.GREEN],
    PROVISION_WORK: SEVERITY_GLYPHS[health.YELLOW],
    PROVISION_WAIT: "▶",
    PROVISION_BLOCKED: SEVERITY_GLYPHS[health.RED],
    PROVISION_CREDENTIAL: "◆",
    PROVISION_PENDING: NEUTRAL_GLYPH,
}


class SelectiveSyncTree(QTreeWidget):
    """Make the selectable sync rows operable without changing mouse behavior."""

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if event.key() not in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            super().keyPressEvent(event)
            return
        item = self.currentItem()
        if item is None:
            event.accept()
            return
        kind = item.data(0, ROLE_KIND)
        if kind in ("dir", "file"):
            # Qt's own rule for a non-tristate checkbox, so the keyboard and the
            # mouse cannot disagree: only `Checked` toggles off.  A partially
            # checked folder therefore becomes `Checked` either way, which is what
            # raises the "include everything in this folder?" confirmation instead
            # of silently excluding the whole subtree.
            state = item.checkState(COL_INCLUDED)
            item.setCheckState(
                COL_INCLUDED,
                Qt.CheckState.Unchecked if state == Qt.CheckState.Checked
                else Qt.CheckState.Checked)
        elif kind == "more":
            self.itemActivated.emit(item, COL_NAME)
        event.accept()

#: What each phase means, in this app's words.  Includes the three host-side
#: classifications `guestprov` returns instead of a guest phase.
PROVISION_PHASE_TEXT = {
    guestprov.PHASE_STAGING: "Copying the provisioning scripts into the VM",
    guestprov.PHASE_INSPECTING: "Checking what the VM already has",
    guestprov.PHASE_INSTALLING_ICLOUD: "Installing iCloud for Windows",
    guestprov.PHASE_LAUNCHING_ICLOUD: "Starting iCloud for Windows",
    guestprov.PHASE_WAITING_FOR_SIGNIN: "Waiting for you to sign in to iCloud",
    guestprov.PHASE_WAITING_FOR_SECRET: "Waiting for the share password",
    guestprov.PHASE_CREATING_SHARE: "Creating the iCloud share",
    guestprov.PHASE_INSTALLING_BRIDGE_BOUNDARY:
        "Setting up the bridge share and permissions",
    guestprov.PHASE_INSTALLING_AGENT: "Installing the bridge agent",
    guestprov.PHASE_VERIFYING: "Checking the result",
    guestprov.PHASE_DONE: "Windows setup finished",
    guestprov.PHASE_ABSENT: "Waiting for the VM to pick up this request",
    guestprov.PHASE_STALE: "Waiting for the VM to pick up this request",
    guestprov.PHASE_UNREADABLE: "The VM's progress report could not be read",
}

#: The administrator-only copy the orchestrator keeps for a diagnosed failure
#: (D42).  It is never an execution source for the app and never `C:\\OEM`.
PROVISION_FALLBACK_DIR = r"C:\ProgramData\icloud-bridge-provision\current"
#: The protected manual fallback for the component that failed, by phase.
PROVISION_FALLBACK_SCRIPTS = {
    guestprov.PHASE_WAITING_FOR_SECRET: "03-create-share.ps1",
    guestprov.PHASE_CREATING_SHARE: "03-create-share.ps1",
    guestprov.PHASE_INSTALLING_BRIDGE_BOUNDARY: "04-bridge-agent.ps1 -Scope Boundary",
    guestprov.PHASE_INSTALLING_AGENT: "04-bridge-agent.ps1 -Scope Agent",
    guestprov.PHASE_VERIFYING: "04-bridge-agent.ps1 -Scope All",
}
PROVISION_FALLBACK_NOTES = {
    guestprov.PHASE_INSTALLING_ICLOUD:
        "Manual fallback: in the VM, install “iCloud” from the Microsoft Store "
        "yourself, then try the inspection again.",
    guestprov.PHASE_LAUNCHING_ICLOUD:
        "Manual fallback: in the VM, start iCloud for Windows yourself, then try "
        "the inspection again.",
    guestprov.PHASE_WAITING_FOR_SIGNIN:
        "Manual fallback: open the VM screen and sign in to iCloud, leaving "
        "iCloud Drive and Files On-Demand switched on.",
    guestprov.PHASE_INSPECTING:
        "Nothing in the VM was changed. Open the VM screen to look at the item "
        "above, then try the inspection again.",
}

PROVISION_SIGNIN_CARD = (
    "Sign in to iCloud in the VM now. Open the VM screen, sign in with your "
    "Apple ID (including two-factor authentication), and leave iCloud Drive and "
    "Files On-Demand switched on. This app carries on by itself as soon as the "
    "iCloud Drive folder appears — there is nothing to click here."
)
PROVISION_SECRET_CARD = (
    "The VM is waiting for the share password. Choose the .env file holding "
    "SHARE_PASS; its value is sent straight into the VM and is never stored, "
    "logged, or shown by this app.\n"
    "This sets the guest “syncshare” account password, which must match the "
    "credentials this host mounts with. This app cannot read "
    "/etc/credentials-icloud (it is root-only), so if you are deliberately "
    "choosing a different password, run the command shown afterwards to update "
    "the host as well."
)
PROVISION_SECRET_RESELECT = (
    "This app never stores the path of your .env file, or anything in it, so "
    "after a restart it has to be selected again. The password itself is "
    "re-sent, not recovered."
)

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


# --------------------------------------------- Safe Workspaces (plan §11) --
# The tab is persistent and stays readable while the bridge is powered off:
# local folders and the last status are exactly what an operator wants then.
# Only the actions that would touch /mnt/icloud are disabled in that state.

WS_COL_NAME, WS_COL_LOCAL, WS_COL_REMOTE, WS_COL_ENABLED, WS_COL_SYNCED, \
    WS_COL_STATE = range(6)

#: §11.1: the machine tokens `workspace_sync` persists, and the words they are
#: rendered as. This is also the roster that says whether a state read back from
#: a hand-editable `status.json` is one this app knows.
WORKSPACE_STATE_LABELS = {
    workspace_sync.STATE_WAITING: "waiting",
    workspace_sync.STATE_STABILIZING: "stabilizing",
    workspace_sync.STATE_SYNCING: "syncing",
    workspace_sync.STATE_UP_TO_DATE: "up to date",
    workspace_sync.STATE_PAUSED: "paused",
    workspace_sync.STATE_CONFLICT: "conflict",
    workspace_sync.STATE_GUARDED: "guarded",
    workspace_sync.STATE_ERROR: "error",
}

#: The states that colour a row and make the health row yellow (§12.1).
WORKSPACE_ATTENTION_STATES = health.WORKSPACE_ATTENTION_STATES

#: §5.2: the default parent the GUI offers. Nothing enforces it, and no real
#: vault path or note name is ever a default anywhere in this feature.
WORKSPACE_PARENT_NAME = "iCloud Workspaces"

WORKSPACE_INTRO = (
    "A Safe Workspace keeps an ordinary folder on this computer in step with a "
    "folder in iCloud, so an editor never opens a file whose metadata the cloud "
    "client rewrites underneath it. Edit the local folder; this app propagates "
    "changes both ways once both sides have been still for a moment. Nothing is "
    "ever merged automatically, and nothing is deleted when you forget a "
    "workspace."
)

#: §11.3, character for character. Do not reword: it is the one instruction that
#: keeps two Obsidian windows off the same vault.
WORKSPACE_ADD_WARNING = (
    "Close the iCloud copy of this vault in Obsidian before continuing. After "
    "the first sync, open only the local workspace in Obsidian."
)

WORKSPACE_FIRST_COPY = (
    "The first sync copies the iCloud folder into the local folder. Nothing is "
    "deleted on either side, and nothing is copied back to iCloud until both "
    "folders have been unchanged from one poll to the next."
)

#: §11.5, character for character in everything but the four locations, which
#: are this workspace's own.
WORKSPACE_FORGET_TEXT = (
    "Forgetting a workspace removes only its entry in this app.\n"
    "\n"
    "These are kept, exactly as they are:\n"
    "  Local workspace: {local}\n"
    "  iCloud folder:   {remote}\n"
    "  Sync state:      {state}\n"
    "  Backups:         {backups}\n"
    "\n"
    "Nothing is deleted from this computer or from iCloud."
)

WORKSPACE_OBSIDIAN_SCHEME = "obsidian://open?path="


def obsidian_url(path: str) -> str:
    """`obsidian://open?path=<absolute path>`, percent-encoded whole (§11.4).

    ``safe=""`` so the separators are encoded too: the value is one query
    parameter, not a path, and a folder name containing `&`, `#` or `?` would
    otherwise change the URL's meaning.
    """
    return WORKSPACE_OBSIDIAN_SCHEME + urllib.parse.quote(path, safe="")


def default_workspace_parent() -> str:
    """`~/iCloud Workspaces` — offered, never enforced (§5.2)."""
    return os.path.join(os.path.expanduser("~"), WORKSPACE_PARENT_NAME)


def workspace_remote_display(workspace: workspaces.Workspace) -> str:
    """The absolute iCloud path of a workspace, for display and for opening.

    A stored `remote` has already been normalized, so this cannot normally
    fail; the fallback keeps a hand-edited configuration displayable rather than
    breaking the whole tab.
    """
    try:
        return workspaces.remote_root(workspace.remote)
    except workspaces.WorkspaceError:
        return os.path.join(workspaces.mount_root(), workspace.remote)


@dataclass(frozen=True)
class WorkspaceRow:
    """One classified row of the tab, built by the controller.

    Everything here is already bounded and free of file content: `detail` is the
    engine's own single line and `paths` at most 20 relative names (§7.9). The
    window renders these and never reaches for anything else.
    """

    workspace: workspaces.Workspace
    state: str
    last_success_at: str = ""
    detail: str = ""
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceProbe:
    """What the add dialog's worker found out, off the GUI thread (§11.3)."""

    remote_root: str
    remote_entries: int = 0
    remote_bytes: int = 0
    free_bytes: int = 0
    local_exists: bool = False


class AddWorkspaceDialog(QDialog):
    """Collect one new workspace, validating every string rule as it is typed.

    Only `workspaces.validate_fields` runs here. It is side-effect-free by
    design (§4.3), which is what makes it safe on the GUI thread on every
    keystroke; everything that stats the filesystem or reads the mount happens
    in the worker the caller starts once this dialog is accepted.

    The iCloud folder is **typed**, never browsed: a file dialog in this app is
    never pointed at CIFS, and the local browser starts under the local default
    parent (§11.2).
    """

    def __init__(self, parent=None, *,
                 existing: Sequence[workspaces.Workspace] = ()) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Safe Workspace")
        self._existing = tuple(existing)
        self._candidate: workspaces.Workspace | None = None
        #: Once the operator types a local folder themselves, the name field
        #: stops proposing one for them.
        self._local_chosen = False

        layout = QVBoxLayout(self)
        intro = QLabel(
            "The iCloud folder is written as a path inside the iCloud mount, "
            "such as Documents/Vault. The local folder must be an empty or "
            "not-yet-created folder on a local disk.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self._name = QLineEdit()
        self._name.setPlaceholderText("Vault")
        self._name.textChanged.connect(self._on_name_changed)
        form.addRow("Name:", self._name)

        self._remote = QLineEdit()
        self._remote.setPlaceholderText("Documents/Vault")
        self._remote.textChanged.connect(self._validate)
        form.addRow("iCloud folder:", self._remote)

        local_row = QHBoxLayout()
        self._local = QLineEdit()
        self._local.setPlaceholderText(
            os.path.join(default_workspace_parent(), "Vault"))
        self._local.textEdited.connect(self._on_local_edited)
        self._local.textChanged.connect(self._validate)
        local_row.addWidget(self._local, 1)
        self._browse = QPushButton("Browse…")
        self._browse.clicked.connect(self._choose_local)
        local_row.addWidget(self._browse)
        form.addRow("Local folder:", local_row)
        layout.addLayout(form)

        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._message.setStyleSheet(
            f"color: {theme.severity_color(current_scheme(), health.YELLOW)};")
        layout.addWidget(self._message)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)
        self._validate()

    def candidate(self) -> workspaces.Workspace | None:
        """The validated, normalized workspace, or ``None`` if the fields fail."""
        return self._candidate

    def set_fields(self, name: str, remote: str, local: str) -> None:
        """Fill the three fields at once. For the tests and for a retry."""
        self._name.setText(name)
        self._remote.setText(remote)
        self._local_chosen = True
        self._local.setText(local)

    def message(self) -> str:
        return self._message.text()

    def _on_name_changed(self, text: str) -> None:
        if not self._local_chosen:
            name = text.strip()
            self._local.setText(
                os.path.join(default_workspace_parent(), name) if name else "")
        self._validate()

    def _on_local_edited(self, _text: str) -> None:
        self._local_chosen = True

    def _choose_local(self) -> None:
        """Pick the local folder from a **local-only** dialog (§11.2).

        The starting directory is the local default parent's nearest existing
        ancestor, never the iCloud mount, so no CIFS directory is ever
        enumerated on the GUI thread.
        """
        start = self._local.text().strip() or default_workspace_parent()
        while start and not os.path.isdir(start):
            parent = os.path.dirname(start)
            if parent == start:
                break
            start = parent
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose the local workspace folder", start,
            QFileDialog.Option.ShowDirsOnly
            | QFileDialog.Option.DontResolveSymlinks)
        if chosen:
            self._local_chosen = True
            self._local.setText(chosen)

    def _validate(self, _text: str | None = None) -> None:
        self._candidate = None
        message = ""
        try:
            self._candidate = workspaces.validate_fields(
                self._name.text(), self._remote.text(), self._local.text(),
                existing=self._existing)
        except workspaces.WorkspaceError as exc:
            message = str(exc)
        self._message.setText(message)
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(self._candidate is not None)


def _fmt_count(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return f"{value:,}"
    return "-"


def _write_report(path: str, text: str) -> None:
    """Write the report mode 0600, refusing anything but a plain file.

    A report goes wherever the operator picked in a save dialog, so following a
    symlink or opening a device or FIFO there could truncate — or block on —
    something else entirely. `O_NOFOLLOW` covers the link; the `lstat` covers
    the rest.
    """
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise OSError(f"{path} is not a regular file; refusing to write to it")

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    handle = os.open(path, flags, 0o600)
    try:
        # Explicit, because O_CREAT's mode is ignored when the file exists.
        os.fchmod(handle, 0o600)
    except BaseException:
        os.close(handle)
        raise
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)


def _save_backup(exclusions, revision, source) -> str:
    """Write the D36 snapshot and describe the outcome, never raising.

    The bridge operation and the local snapshot are two results. This returns a
    message when the operator should be told something, and an empty string when
    the snapshot is fine — so a caller can never accidentally turn a backup
    problem into a bridge failure.
    """
    try:
        outcome = backup.save(exclusions, revision, source)
    except backup.BackupError as exc:
        return (f"Your selective-sync choices are not backed up on this computer: "
                f"{exc}")
    if outcome == backup.KEPT_NEWER:
        return ("The saved copy of your selective-sync choices is newer than the "
                "configuration in the VM, so it was kept. If the VM was rebuilt, "
                "use Restore from backup.")
    if outcome == backup.CONFLICT:
        return ("The saved copy of your selective-sync choices differs from the "
                "VM's at the same revision, so it was kept. Reload, then Apply or "
                "Restore from backup to settle which one is right.")
    return ""


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
    #: D39: forget an interrupted-provisioning record Docker has disproved.
    discard_record_requested = Signal()
    env_file_selected = Signal(str)
    configuration_requested = Signal(str, str, str)
    #: D40-D44 app-driven guest provisioning. `reprovision_requested` has three
    #: emitters — the Status-tab button, the skew/incompatible banner's button
    #: and the tray menu — because D35 asks for one action with one set of
    #: enablement rules, not one implementation per surface.
    provision_requested = Signal()
    provision_retry_requested = Signal()
    provision_env_selected = Signal(str)
    reprovision_requested = Signal()
    #: Safe Workspaces (plan §11). The window owns the configuration edits —
    #: they are local-disk JSON, not bridge I/O — and tells the controller to
    #: re-read the file afterwards (§10). Sync now asks for one pass through the
    #: controller's single-flight path, never a second execution route.
    workspaces_changed = Signal()
    workspace_sync_requested = Signal()

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
        #: The D35 protocol/agent-build classification from the last snapshot.
        #: Starts `unknown`, which is *closed*: no write and no list request may
        #: be dispatched until a status document has actually proved the guest
        #: agent speaks this protocol.
        self._compatibility = bridge.Compatibility()
        #: Set by the controller: returns the allowlisted `diagnostics.Facts`
        #: for a report. The controller owns the lifecycle/marker/install facts;
        #: this window only presses the button (D37).
        self.diagnostics_facts: Callable[[], diagnostics.Facts] | None = None
        self.setWindowTitle("iCloud bridge")
        self.setMinimumSize(760, 520)
        self.resize(880, 620)
        self._uistate_restored = False

        self._status: dict | None = None
        self._tree: dict | None = None
        self._checks: list[health.Check] = []
        self._banner_kind = "starting"

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
        #: Bumped by every path that adds or removes a tree row. It is part of
        #: the ``_refresh_state_column`` memo key, so a new row can never inherit
        #: a cached "nothing changed" decision and keep an empty state cell.
        self._row_epoch = 0
        #: Inputs the state column was last rendered from; ``None`` means the
        #: column must be rendered. See ``_refresh_state_column``.
        self._state_column_key: tuple | None = None

        # Safe Workspaces model (plan §11). `_workspace_rows` is whatever the
        # controller last classified, `_workspace_busy` an add probe in flight,
        # and `_awaiting_seed` the ids added in this session whose first
        # successful sync should offer to open Obsidian (§11.4).
        self._workspace_rows: list[WorkspaceRow] = []
        #: What the tree currently shows, so an unchanged pass does not rebuild
        #: it under the operator. See ``update_workspaces``.
        self._workspace_painted: tuple = ()
        self._workspace_busy = False
        self._awaiting_seed: set[str] = set()
        self._workspace_intro_count: int | None = None

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
        self._notice.setStyleSheet(
            f"color: {theme.severity_color(current_scheme(), health.RED)};")
        self._notice.hide()
        central_layout.addWidget(self._notice)

        # The D35 protocol/agent-build banner. Its own widget for the same
        # reason as the notice: skew persists across snapshots and must survive
        # whatever the lifecycle banner is currently saying. Selectable so the
        # operator can copy the diagnostic out of it.
        self._protocol_box = QWidget()
        protocol_layout = QVBoxLayout(self._protocol_box)
        protocol_layout.setContentsMargins(0, 0, 0, 0)
        protocol_layout.setSpacing(2)
        self._protocol = QLabel("")
        self._protocol.setWordWrap(True)
        self._protocol.setContentsMargins(10, 6, 10, 6)
        self._protocol.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        protocol_layout.addWidget(self._protocol)
        # D35's recovery is one confirmed action, not a copyable instruction and
        # not a different code path per state: this button, the Status-tab
        # button and the tray item all emit the same signal.
        banner_buttons = QHBoxLayout()
        banner_buttons.setContentsMargins(10, 0, 10, 6)
        self._protocol_button = QPushButton("Re-run Windows provisioning…")
        self._protocol_button.clicked.connect(self.reprovision_requested.emit)
        banner_buttons.addWidget(self._protocol_button)
        banner_buttons.addStretch(1)
        protocol_layout.addLayout(banner_buttons)
        self._protocol_box.hide()
        central_layout.addWidget(self._protocol_box)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_status_tab(), "Status")
        self._sync_page = self._build_sync_tab()
        self._tabs.addTab(self._sync_page, "Selective Sync")
        # Persistent, and deliberately still readable while the bridge is off:
        # local folders and the last status are what an operator wants then
        # (§11.1). Only the mount-touching actions are disabled in that state.
        self._workspaces_page = self._build_workspaces_tab()
        self._tabs.addTab(self._workspaces_page, "Safe Workspaces")
        # The Setup tab exists only while the bridge is not provisioned; it is
        # inserted in front of the others so it cannot be missed, and removed
        # again once the bridge is running (D31).
        self._setup_page = self._build_setup_tab()
        central_layout.addWidget(self._tabs, 1)
        self.setCentralWidget(central)
        self.statusBar()

        # The one-second response poll. It is armed by a dispatched list request
        # and stopped again when none is outstanding, so a tray session that
        # never expands a folder does not wake 86 400 times a day to iterate an
        # empty list. See `_sync_poll_timer`.
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1000)
        self._poll_timer.timeout.connect(self._poll_pending)
        # Window shortcuts, deliberately *not* application-wide: an
        # `ApplicationShortcut` would also fire while one of this app's own modal
        # dialogs holds focus, so Ctrl+Q inside the Add-Workspace dialog would
        # start a quit. They are kept as named attributes so a test can assert the
        # binding rather than depending on which window the platform considers
        # active.
        self._refresh_action = self._add_shortcut(
            [QKeySequence("F5"), QKeySequence("Ctrl+R")], self.request_refresh)
        self._close_action = self._add_shortcut([QKeySequence("Ctrl+W")], self.close)
        self._quit_action = self._add_shortcut([QKeySequence("Ctrl+Q")],
                                               self.quit_requested.emit)
        self._apply_theme()

    def _add_shortcut(self, sequences, slot) -> QAction:
        action = QAction(self)
        action.setShortcuts(sequences)
        action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        action.triggered.connect(slot)
        self.addAction(action)
        return action

    def changeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().changeEvent(event)
        theme_change = getattr(QEvent.Type, "ThemeChange", None)
        if event.type() in (QEvent.Type.ApplicationPaletteChange, theme_change):
            self._apply_theme()

    def _apply_theme(self) -> None:
        """Restyle persistent surfaces after the desktop palette changes."""
        scheme = current_scheme()
        self._notice.setStyleSheet(f"color: {theme.severity_color(scheme, health.RED)};")
        self._prov_instruction.setStyleSheet(theme.instruction_style(scheme))
        self._prov_warning.setStyleSheet(
            f"color: {theme.severity_color(scheme, health.YELLOW)};")
        self._prov_error.setStyleSheet(f"color: {theme.severity_color(scheme, health.RED)};")
        self._sync_error.setStyleSheet(f"color: {theme.severity_color(scheme, health.RED)};")
        self._backup_warning.setStyleSheet(
            f"color: {theme.severity_color(scheme, health.YELLOW)};")
        self._workspace_error_label.setStyleSheet(
            f"color: {theme.severity_color(scheme, health.RED)};")
        for label in (self._setup_paths, self._status_note, self._version_label,
                      self._summary_note, self._filter_note):
            label.setStyleSheet(f"color: {theme.muted_color(scheme)};")
        self._banner.setStyleSheet(theme.banner_style(
            scheme, self._banner_kind if self._banner_kind in theme.BANNER_STYLES[theme.LIGHT]
            else "starting"))
        if self._compatibility.state in theme.PROTOCOL_STYLES[theme.LIGHT]:
            self._protocol.setStyleSheet(theme.protocol_style(scheme, self._compatibility.state))
        for label in self.findChildren(QLabel):
            kind = label.property("provision_kind")
            if kind:
                label.setStyleSheet(f"color: {theme.provision_color(scheme, kind)};")
            severity = label.property("severity")
            if severity:
                label.setStyleSheet(f"color: {theme.severity_color(scheme, severity)};")
        for check in self._checks:
            dot, _detail = self._ensure_check_row(check.name)
            dot.setStyleSheet(f"color: {theme.severity_color(scheme, check.severity)};")
        # A "Load more…" row is painted rather than styled, so it needs the same
        # repaint the check states and the workspace rows get below; otherwise a
        # theme switch leaves the previous scheme's link colour on screen until
        # something happens to rebuild the tree.
        link = QBrush(QColor(theme.link_color(scheme)))
        for item in self._iter_items():
            if item.data(0, ROLE_KIND) == "more":
                item.setForeground(COL_NAME, link)
        self._refresh_check_states()
        self._rebuild_workspace_rows()

    @staticmethod
    def _secondary_label(label: QLabel) -> None:
        """Apply the smaller type shared by secondary explanatory copy."""
        font = label.font()
        if font.pointSize() > 1:
            font.setPointSize(font.pointSize() - 1)
        label.setFont(font)

    def show_transient(self, text: str) -> None:
        """Show short-lived feedback without changing a control's purpose."""
        self.statusBar().showMessage(text, 4000)

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
        self._setup_paths.setStyleSheet(f"color: {theme.muted_color(current_scheme())};")
        self._secondary_label(self._setup_paths)
        layout.addWidget(self._setup_paths)

        env_row = QHBoxLayout()
        self._env_label = QLabel("Configuration file: (none found)")
        self._env_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        env_row.addWidget(self._env_label, 1)
        self._env_button = QPushButton("Use an existing .env...")
        self._env_button.clicked.connect(self._choose_env_file)
        env_row.addWidget(self._env_button)
        layout.addLayout(env_row)

        resources = QHBoxLayout()
        resources.addWidget(QLabel("Disk:"))
        self._disk_size = QLineEdit()
        self._disk_size.setMaximumWidth(90)
        resources.addWidget(self._disk_size)
        resources.addWidget(QLabel("RAM:"))
        self._ram_size = QLineEdit()
        self._ram_size.setMaximumWidth(90)
        resources.addWidget(self._ram_size)
        resources.addWidget(QLabel("vCPUs:"))
        self._cpu_cores = QLineEdit()
        self._cpu_cores.setMaximumWidth(90)
        resources.addWidget(self._cpu_cores)
        self._configuration_button = QPushButton("Create configuration")
        self._configuration_button.clicked.connect(self._create_configuration)
        resources.addWidget(self._configuration_button)
        resources.addStretch(1)
        layout.addLayout(resources)

        self._setup_checks = QWidget()
        self._setup_checks_layout = QVBoxLayout(self._setup_checks)
        self._setup_checks_layout.setContentsMargins(0, 0, 0, 0)
        self._setup_checks_layout.setSpacing(4)
        layout.addWidget(self._setup_checks)

        layout.addWidget(self._build_provisioning_box())

        # The manual guest sequence the provisioning state has always shown. The
        # app now drives that sequence itself, so it collapses behind a toggle
        # rather than disappearing: an unusual VM must never become a dead end.
        self._setup_manual_text = QLabel("")
        self._setup_manual_text.setWordWrap(True)
        self._setup_manual_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._setup_manual_text.hide()
        layout.addWidget(self._setup_manual_text)

        self._setup_detail = QLabel("")
        self._setup_detail.setWordWrap(True)
        self._setup_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._setup_detail.hide()
        layout.addWidget(self._setup_detail)

        layout.addStretch(1)

        self._setup_primary_row = QHBoxLayout()
        self._setup_recheck = QPushButton("Re-check")
        self._setup_recheck.clicked.connect(self.setup_recheck_requested.emit)
        self._setup_primary_row.addWidget(self._setup_recheck)
        self._setup_create = QPushButton("Create Windows VM")
        self._setup_create.clicked.connect(self.create_vm_requested.emit)
        self._setup_create.setEnabled(False)
        self._setup_primary_row.addWidget(self._setup_create)
        # D40-D44: the confirmed first run. It stages this app's own scripts and
        # watches their effects; the only step left inside the VM is the Apple
        # sign-in.
        self._setup_provision = QPushButton("Set up Windows automatically")
        self._setup_provision.setToolTip(
            "Install iCloud for Windows, wait while you sign in, create the SMB "
            "share and install the bridge agent.")
        self._setup_provision.clicked.connect(self.provision_requested.emit)
        self._setup_provision.hide()
        self._setup_primary_row.addWidget(self._setup_provision)
        self._setup_connect = QPushButton("Check setup and connect")
        self._setup_connect.clicked.connect(self.connect_requested.emit)
        self._setup_connect.hide()
        self._setup_primary_row.addWidget(self._setup_connect)
        self._setup_primary_row.addStretch(1)
        layout.addLayout(self._setup_primary_row)

        self._setup_secondary_row = QHBoxLayout()
        setup_vm = QPushButton("Open VM screen")
        setup_vm.clicked.connect(lambda: self._open_externally(VM_VIEWER_URL))
        self._setup_secondary_row.addWidget(setup_vm)
        # D39: only offered when Docker has proved the recorded container is
        # absent or different. It removes a local record and nothing else — no
        # container, no VM disk, no env file, no bundle.
        self._setup_discard = QPushButton("Discard failed setup record")
        self._setup_discard.setToolTip(
            "Forget this app's note that a VM creation was in progress. "
            "Nothing is deleted: no container, no disk image, no settings.")
        self._setup_discard.clicked.connect(self.discard_record_requested.emit)
        self._setup_discard.hide()
        self._setup_secondary_row.addWidget(self._setup_discard)
        self._setup_manual = QPushButton("Show manual steps")
        self._setup_manual.setCheckable(True)
        self._setup_manual.setToolTip(
            "The documented sequence you can run in the VM yourself, if you "
            "would rather not let the app do it.")
        self._setup_manual.toggled.connect(self._toggle_manual_steps)
        self._setup_manual.hide()
        self._setup_secondary_row.addWidget(self._setup_manual)
        self._setup_secondary_row.addStretch(1)
        layout.addLayout(self._setup_secondary_row)
        return page

    def _toggle_manual_steps(self, shown: bool) -> None:
        self._setup_manual.setText("Hide manual steps" if shown
                                   else "Show manual steps")
        self._setup_manual_text.setVisible(shown and bool(
            self._setup_manual_text.text()))

    # ------------------------------------- the provisioning run (D40-D44) --

    def _build_provisioning_box(self) -> QWidget:
        """The run surface: busy line, instruction card, plan, and checklist."""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(4)

        # D38 conventions: the phase and its elapsed time, never a percentage,
        # never an estimate, and no control that would interrupt the guest run.
        self._prov_busy_label = QLabel("")
        self._prov_busy_label.setWordWrap(True)
        font = self._prov_busy_label.font()
        font.setBold(True)
        self._prov_busy_label.setFont(font)
        layout.addWidget(self._prov_busy_label)

        self._prov_note = QLabel("")
        self._prov_note.setWordWrap(True)
        self._prov_note.hide()
        layout.addWidget(self._prov_note)

        self._prov_instruction = QLabel("")
        self._prov_instruction.setWordWrap(True)
        self._prov_instruction.setContentsMargins(10, 6, 10, 6)
        self._prov_instruction.setStyleSheet(theme.instruction_style(current_scheme()))
        self._prov_instruction.hide()
        layout.addWidget(self._prov_instruction)

        self._prov_work_label = QLabel("")
        self._prov_work_label.setWordWrap(True)
        self._prov_work_label.hide()
        layout.addWidget(self._prov_work_label)

        self._prov_rows = QWidget()
        self._prov_rows_layout = QVBoxLayout(self._prov_rows)
        self._prov_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._prov_rows_layout.setSpacing(2)
        layout.addWidget(self._prov_rows)

        self._prov_warning = QLabel("")
        self._prov_warning.setWordWrap(True)
        self._prov_warning.setStyleSheet(
            f"color: {theme.severity_color(current_scheme(), health.YELLOW)};")
        self._prov_warning.hide()
        layout.addWidget(self._prov_warning)

        self._prov_error = QLabel("")
        self._prov_error.setWordWrap(True)
        self._prov_error.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._prov_error.setStyleSheet(
            f"color: {theme.severity_color(current_scheme(), health.RED)};")
        self._prov_error.hide()
        layout.addWidget(self._prov_error)

        # Three copyable one-liners, each in the Setup tab's existing command
        # row style: the one-time guest bootstrap, the protected manual fallback
        # for a failed component, and the host-side credential follow-up.
        self._prov_bootstrap = self._build_command_row(layout)
        self._prov_fallback = self._build_command_row(layout)
        self._prov_follow_up = self._build_command_row(layout)

        row = QHBoxLayout()
        self._prov_env_button = QPushButton("Choose .env file for the password…")
        self._prov_env_button.clicked.connect(self._choose_provision_env)
        self._prov_env_button.hide()
        row.addWidget(self._prov_env_button)
        self._prov_retry_button = QPushButton("Try inspection and repair again")
        self._prov_retry_button.clicked.connect(
            self.provision_retry_requested.emit)
        self._prov_retry_button.hide()
        row.addWidget(self._prov_retry_button)
        row.addStretch(1)
        layout.addLayout(row)

        self._prov_box = box
        box.hide()
        return box

    @staticmethod
    def _build_command_row(layout) -> tuple[QLabel, QLabel]:
        """A note plus the monospace command it explains; both start hidden."""
        note = QLabel("")
        note.setWordWrap(True)
        note.hide()
        layout.addWidget(note)
        command = QLabel("")
        command.setContentsMargins(16, 0, 0, 0)
        command.setStyleSheet("font-family: monospace;")
        command.setWordWrap(True)
        command.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        command.setToolTip("Select and copy this — the GUI never runs it for you")
        command.hide()
        layout.addWidget(command)
        return note, command

    @staticmethod
    def _set_command_row(row: tuple[QLabel, QLabel], note: str, command: str) -> None:
        note_label, command_label = row
        note_label.setText(note)
        note_label.setVisible(bool(note))
        command_label.setText(command)
        command_label.setVisible(bool(command))

    def _choose_provision_env(self) -> None:
        """Pick the env file whose ``SHARE_PASS`` the VM is waiting for (D41).

        A selection made in *this* process, every time: the path is never
        persisted, so there is nothing to recover after a restart.
        """
        start = (os.path.dirname(self._env_path) if self._env_path
                 else os.path.expanduser("~"))
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "Choose the .env file holding SHARE_PASS", start,
            "Environment files (.env *.env);;All files (*)")
        if chosen:
            self.provision_env_selected.emit(chosen)

    def update_provisioning(self, *, visible: bool, phase: str = "",
                            detail: str = "", elapsed: str = "",
                            checks: dict | None = None, work=(),
                            reset_credential: bool = False,
                            note: str = "", warning: str = "", error: str = "",
                            show_bootstrap: bool = False, bootstrap: str = "",
                            bootstrap_note: str = "",
                            show_env_button: bool = False,
                            env_reselect: bool = False, follow_up: str = "",
                            retry_label: str = "", busy: bool = False) -> None:
        """Render one reading of a provisioning run.  Presentation only.

        ``phase`` is the *host's* classification from :class:`guestprov.Status`,
        so it is either a real guest phase or one of the absent/unreadable/stale
        markers; ``detail`` is the guest's own bounded line and is displayed,
        never parsed.
        """
        self._prov_box.setVisible(visible)
        if not visible:
            return

        title = PROVISION_PHASE_TEXT.get(phase, "Setting up Windows")
        if elapsed:
            title = f"{title} — {elapsed}"
        # The guest's own one-line `detail` rides along as text and nothing
        # else: it is untrusted output from a guest-writable file (§4.1).
        self._prov_busy_label.setText(f"{title}\n{detail}" if detail else title)

        self._prov_note.setText(note)
        self._prov_note.setVisible(bool(note))

        instruction = ""
        if phase == guestprov.PHASE_WAITING_FOR_SIGNIN:
            instruction = PROVISION_SIGNIN_CARD
        elif phase == guestprov.PHASE_WAITING_FOR_SECRET:
            instruction = PROVISION_SECRET_CARD
            if env_reselect:
                instruction = f"{instruction}\n{PROVISION_SECRET_RESELECT}"
        self._prov_instruction.setText(instruction)
        self._prov_instruction.setVisible(bool(instruction))

        planned = [PROVISION_WORK_NAMES[item] for item in work
                   if item in PROVISION_WORK_NAMES]
        if planned:
            self._prov_work_label.setText("Planned: " + "; ".join(planned))
        elif checks and phase not in (guestprov.PHASE_STAGING,
                                      guestprov.PHASE_INSPECTING):
            self._prov_work_label.setText("Planned: nothing — every part of the "
                                          "VM already matches this app.")
        else:
            self._prov_work_label.setText("")
        self._prov_work_label.setVisible(bool(self._prov_work_label.text()))

        while self._prov_rows_layout.count():
            item = self._prov_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for key in guestprov.CHECK_KEYS:
            state = (checks or {}).get(key)
            if state is None:
                continue
            kind, text = self._provision_row_text(
                key, state, planned=tuple(work), phase=phase,
                reset_credential=reset_credential)
            self._prov_rows_layout.addWidget(
                self._build_provision_row(PROVISION_CHECK_NAMES[key], kind, text))

        self._prov_warning.setText(warning)
        self._prov_warning.setVisible(bool(warning))

        if error:
            # Name the failing phase when the guest reached one; a run that fell
            # over before any status arrived has no phase to blame.
            self._prov_error.setText(
                f"{PROVISION_PHASE_TEXT[phase]} failed: {error}"
                if phase in guestprov.PHASES else
                f"Windows setup could not continue: {error}")
            self._prov_error.show()
            script = PROVISION_FALLBACK_SCRIPTS.get(phase, "")
            if script:
                self._set_command_row(
                    self._prov_fallback,
                    "Manual fallback: run this in an elevated PowerShell inside "
                    "the VM. It is the protected copy of the script this run "
                    "used.",
                    "powershell -ExecutionPolicy Bypass -NoProfile -File "
                    f"{PROVISION_FALLBACK_DIR}\\{script}")
            else:
                self._set_command_row(
                    self._prov_fallback,
                    PROVISION_FALLBACK_NOTES.get(phase, ""), "")
        else:
            self._prov_error.hide()
            self._set_command_row(self._prov_fallback, "", "")

        self._set_command_row(self._prov_bootstrap,
                              bootstrap_note if show_bootstrap else "",
                              bootstrap if show_bootstrap else "")
        self._set_command_row(
            self._prov_follow_up,
            ("If that password differs from the one this host mounts with, run "
             "this on this computer too — the GUI cannot read the root-only "
             "/etc/credentials-icloud and has not changed it." if follow_up
             else ""),
            follow_up)

        self._prov_env_button.setVisible(show_env_button)
        self._prov_env_button.setEnabled(not busy)
        self._prov_retry_button.setVisible(bool(retry_label))
        self._prov_retry_button.setEnabled(not busy)
        if retry_label:
            self._prov_retry_button.setText(retry_label)

    @staticmethod
    def _provision_row_text(key: str, state: str, *, planned: tuple,
                            phase: str, reset_credential: bool) -> tuple[str, str]:
        """``(class, text)`` for one checklist row — all locally owned.

        The password row is special by design (§4.2): Windows cannot read an
        account password back, so it is always `unverifiable` and this app says
        only whether the run reset it or preserved it.  It never renders green.
        """
        work = PROVISION_CHECK_WORK.get(key, "")
        work_name = PROVISION_WORK_NAMES.get(work, "")
        if state == "blocked":
            return PROVISION_BLOCKED, "needs attention — nothing was changed"
        if state == "unknown":
            return PROVISION_BLOCKED, "could not be determined — nothing was changed"
        if key == "shareCredential":
            if state == "pending":
                # Before the inspection has run, the honest thing to report is
                # the *intent* — saying "reset during this run" of a run that
                # has not touched the account yet would be a claim about the
                # future.
                return PROVISION_CREDENTIAL, (
                    "will be set from the .env file you choose" if reset_credential
                    else "will be left exactly as it is")
            if reset_credential:
                return PROVISION_CREDENTIAL, (
                    "reset during this run — Windows never reveals a password, "
                    "so this app cannot confirm it; connecting is the proof")
            return PROVISION_CREDENTIAL, (
                "preserved — Windows never reveals a password, so this app "
                "cannot confirm it; connecting is the proof")
        if state == "ok":
            return PROVISION_READY, "ready"
        if state == "pending":
            return PROVISION_PENDING, "not checked yet"
        if state == "unverifiable":
            return PROVISION_CREDENTIAL, "cannot be verified from here"
        if phase == guestprov.PHASE_WAITING_FOR_SIGNIN and work == "wait-for-signin":
            return PROVISION_WAIT, "waiting for you — sign in on the VM screen"
        if work in planned and work_name:
            prefix = "missing" if state == "missing" else "needs repair"
            return PROVISION_WORK, f"{prefix} — {work_name.lower()}"
        return PROVISION_WORK, ("missing" if state == "missing" else "needs repair")

    def _build_provision_row(self, name: str, kind: str, text: str) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        dot = QLabel(PROVISION_GLYPHS[kind])
        dot.setFixedWidth(16)
        dot.setProperty("provision_kind", kind)
        dot.setStyleSheet(f"color: {theme.provision_color(current_scheme(), kind)};")
        title = QLabel(name)
        title.setMinimumWidth(170)
        title.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        detail = QLabel(text)
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row_layout.addWidget(dot)
        row_layout.addWidget(title)
        row_layout.addWidget(detail, 1)
        return row

    def _choose_env_file(self) -> None:
        start = os.path.dirname(self._env_path) if self._env_path else os.path.expanduser("~")
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "Choose the .env file", start, "Environment files (.env *.env);;All files (*)")
        if chosen:
            self.env_file_selected.emit(chosen)

    def _create_configuration(self) -> None:
        self.configuration_requested.emit(self._disk_size.text(), self._ram_size.text(),
                                          self._cpu_cores.text())

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
                     detail: str = "", busy: bool = False,
                     show_discard: bool = False, manual: str = "",
                     show_provision: bool = False,
                     can_provision: bool = False,
                     resource_defaults: firstrun.ResourceDefaults | None = None) -> None:
        """Render one assistant state.  Pure presentation of firstrun's answers."""
        self._env_path = env_path
        self._setup_title.setText(title)
        self._setup_intro.setText(intro)
        self._setup_paths.setText(paths)
        self._env_label.setText(f"Configuration file: {env_path or '(none found)'}")
        if resource_defaults is not None:
            for field, value in ((self._disk_size, resource_defaults.disk_size),
                                 (self._ram_size, resource_defaults.ram_size),
                                 (self._cpu_cores, resource_defaults.cpu_cores)):
                if not field.text():
                    field.setText(value)
        self._setup_create.setEnabled(can_create and not busy)
        self._setup_provision.setVisible(show_provision)
        self._setup_provision.setEnabled(can_provision and not busy)
        self._setup_manual_text.setText(manual)
        self._setup_manual.setVisible(bool(manual))
        if not manual:
            self._setup_manual.setChecked(False)
        self._setup_manual_text.setVisible(bool(manual)
                                           and self._setup_manual.isChecked())
        self._setup_recheck.setEnabled(not busy)
        self._setup_connect.setVisible(show_connect)
        self._setup_connect.setEnabled(not busy)
        self._setup_discard.setVisible(show_discard)
        self._setup_discard.setEnabled(not busy)
        self._env_button.setEnabled(not busy)
        self._configuration_button.setEnabled(not busy and not env_path)
        self._disk_size.setEnabled(not busy and not env_path)
        self._ram_size.setEnabled(not busy and not env_path)
        self._cpu_cores.setEnabled(not busy and not env_path)
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
        severity = {firstrun.OK: health.GREEN, firstrun.WARN: health.YELLOW,
                    firstrun.FAIL: health.RED}.get(check.status, health.RED)
        dot = QLabel(SEVERITY_GLYPHS[severity])
        dot.setFixedWidth(16)
        dot.setProperty("severity", severity)
        severity_text = f"{check.name}: {SEVERITY_WORDS[severity]}"
        dot.setAccessibleName(severity_text)
        dot.setToolTip(severity_text)
        dot.setStyleSheet(f"color: {theme.severity_color(current_scheme(), severity)};")
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

    # ------------------------------------------------- the D37 report export --

    def _run_diagnostics(self, deliver) -> None:
        """Collect off the GUI thread, then hand the text back to ``deliver``."""
        provider = self.diagnostics_facts
        if provider is None:
            return
        facts = provider()
        include = self._diag_paths.isChecked()

        def work():
            # `collect` runs `systemctl is-active` and the two `sudo -n -l`
            # probes; none touches a mount, but they are still subprocesses.
            return diagnostics.report_text(facts, include_paths=include)

        def failed(message: str):
            QMessageBox.warning(self, "Could not build the report", message)

        self._run_async(work, deliver, failed)

    def _copy_diagnostics(self) -> None:
        def deliver(text: str) -> None:
            # The only route to the clipboard: an explicit Copy action.
            QApplication.clipboard().setText(text)
            self.show_transient("Diagnostic report copied to the clipboard")

        self._run_diagnostics(deliver)

    def _save_diagnostics(self) -> None:
        def deliver(text: str) -> None:
            suggested = os.path.join(
                os.path.expanduser("~"),
                diagnostics.default_filename(
                    datetime.now().strftime("%Y%m%d-%H%M%S")))
            chosen, _filter = QFileDialog.getSaveFileName(
                self, "Save diagnostic report", suggested, "Text files (*.txt)")
            if not chosen:
                return
            try:
                _write_report(chosen, text)
            except OSError as exc:
                QMessageBox.warning(self, "Could not save the report", str(exc))

        self._run_diagnostics(deliver)

    # ------------------------------------------------------------ status tab --

    def _build_status_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        health_box = QGroupBox("Bridge health")
        health_layout = QVBoxLayout(health_box)
        self._check_rows = QWidget()
        self._check_layout = QVBoxLayout(self._check_rows)
        self._check_layout.setContentsMargins(0, 0, 0, 0)
        self._check_layout.setSpacing(4)
        health_layout.addWidget(self._check_rows)
        layout.addWidget(health_box)
        self._check_widgets: dict[str, tuple[QLabel, QLabel]] = {}

        capacity_box = QGroupBox("Capacity")
        capacity_layout = QVBoxLayout(capacity_box)
        self._disk_label = QLabel("Guest disk: -")
        self._local_label = QLabel("Fully local content: -")
        self._local_label.setToolTip("partially downloaded files are not counted")
        self._scan_label = QLabel("Last full scan: -")
        for label in (self._disk_label, self._local_label, self._scan_label):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            capacity_layout.addWidget(label)

        note = QLabel("“Fully local content” is a lower bound: partially downloaded "
                      "files are not counted, so it is not the space iCloud uses in the VM.")
        note.setWordWrap(True)
        self._status_note = note
        note.setStyleSheet(f"color: {theme.muted_color(current_scheme())};")
        self._secondary_label(note)
        capacity_layout.addWidget(note)
        layout.addWidget(capacity_box)

        layout.addStretch(1)

        # D37: support export. Deliberately on the Status tab and available in
        # every lifecycle state, because a failure state is exactly when a
        # report is worth having.
        support_box = QGroupBox("Support")
        diag = QHBoxLayout(support_box)
        self._diag_copy = QPushButton("Copy diagnostics")
        self._diag_copy.setToolTip(
            "Put a privacy-safe diagnostic report on the clipboard")
        self._diag_copy.clicked.connect(self._copy_diagnostics)
        self._diag_save = QPushButton("Save diagnostic report…")
        self._diag_save.clicked.connect(self._save_diagnostics)
        self._diag_paths = QCheckBox("Include folder names")
        self._diag_paths.setToolTip(
            "Off by default: folder names are replaced with placeholders. "
            "Passwords, credentials files, command environments and file "
            "contents are never included either way.")
        diag.addWidget(self._diag_copy)
        diag.addWidget(self._diag_save)
        diag.addWidget(self._diag_paths)
        diag.addStretch(1)
        layout.addWidget(support_box)

        buttons = QHBoxLayout()
        open_files = QPushButton("Open iCloud folder")
        open_files.clicked.connect(lambda: self._open_externally(bridge.mount_dir()))
        self._open_files_button = open_files
        open_vm = QPushButton("Open VM screen")
        open_vm.clicked.connect(lambda: self._open_externally(VM_VIEWER_URL))
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
        # D35/D40-D44: the same confirmed action the tray item and the skew
        # banner's button invoke. Deliberately available while the protocol is
        # skewed or incompatible — that gate closes ordinary bridge writes, and
        # this is what its banner points at.
        self._reprovision_button = QPushButton("Re-run Windows provisioning…")
        self._reprovision_button.setToolTip(
            "Inspect the Windows VM and repair only what no longer matches this "
            "app: the iCloud share, its permissions, and the bridge agent.")
        self._reprovision_button.clicked.connect(self.reprovision_requested.emit)
        self._reprovision_button.hide()
        buttons.addWidget(self._reprovision_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        # Selectable so a bug report can carry the exact build; the same string
        # `icloud-bridge-gui --version` prints.
        version = QLabel(f"Version {__version__}")
        version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._version_label = version
        version.setStyleSheet(f"color: {theme.muted_color(current_scheme())};")
        layout.addWidget(version)
        return page

    def _ensure_check_row(self, name: str) -> tuple[QLabel, QLabel]:
        if name in self._check_widgets:
            return self._check_widgets[name]
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        dot = QLabel(NEUTRAL_GLYPH)
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
        self._summary_note = summary_note
        summary_note.setStyleSheet(f"color: {theme.muted_color(current_scheme())};")
        self._secondary_label(summary_note)
        layout.addWidget(summary_note)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("folder name or path")
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(150)
        self._filter_timer.timeout.connect(self._apply_filter)
        self._filter_edit.textChanged.connect(self._restart_filter_timer)
        filter_row.addWidget(self._filter_edit, 1)
        layout.addLayout(filter_row)
        self._filter_note = QLabel(
            "Searches the folder list plus files already loaded in this session.")
        self._filter_note.setWordWrap(True)
        self._filter_note.setStyleSheet(f"color: {theme.muted_color(current_scheme())};")
        self._secondary_label(self._filter_note)
        self._filter_note.hide()
        layout.addWidget(self._filter_note)

        self._sync_error = QLabel("")
        self._sync_error.setWordWrap(True)
        self._sync_error.setStyleSheet(
            f"color: {theme.severity_color(current_scheme(), health.RED)};")
        self._sync_error.hide()
        layout.addWidget(self._sync_error)

        # The D36 backup is a *second* result: a bridge read or Apply that
        # succeeded is still a success when only the local snapshot write
        # failed, so this warns persistently rather than failing the operation.
        self._backup_warning = QLabel("")
        self._backup_warning.setWordWrap(True)
        self._backup_warning.setStyleSheet(
            f"color: {theme.severity_color(current_scheme(), health.YELLOW)};")
        self._backup_warning.hide()
        layout.addWidget(self._backup_warning)

        self._tree_widget = SelectiveSyncTree()
        self._tree_widget.setColumnCount(5)
        self._tree_widget.setHeaderLabels(["Name", "Size", "Items", "Included", "State"])
        header = self._tree_widget.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for column in (COL_SIZE, COL_ITEMS, COL_INCLUDED, COL_STATE):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
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
        self._reload_button = QPushButton("&Reload")
        self._reload_button.clicked.connect(self.reload_selective_sync)
        self._remove_button = QPushButton("Remove exclusion")
        self._remove_button.setToolTip("Clear a configured exclusion whose item no longer exists")
        self._remove_button.setEnabled(False)
        self._remove_button.clicked.connect(self._remove_selected_missing)
        self._restore_button = QPushButton("Restore from backup…")
        self._restore_button.setToolTip(
            "Preview and re-apply the selective-sync choices saved on this "
            "computer. Use this after rebuilding the Windows VM.")
        self._restore_button.setEnabled(False)
        self._restore_button.clicked.connect(self._restore_from_backup)
        self._apply_button = QPushButton("&Apply")
        self._apply_button.clicked.connect(self._apply)
        buttons.addWidget(self._reload_button)
        buttons.addWidget(self._remove_button)
        buttons.addWidget(self._restore_button)
        buttons.addStretch(1)
        buttons.addWidget(self._apply_button)
        layout.addLayout(buttons)
        return page

    # -------------------------------------------------------- safe workspaces --
    # Plan §11. The controller owns the configuration this renders, the timer
    # that syncs it, and the classification of every row; this tab shows those
    # rows, edits the configuration file, and asks for a pass. It never runs a
    # cycle itself and never reads the mount on the GUI thread.

    def _build_workspaces_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self._workspace_intro_toggle = QPushButton("What is a Safe Workspace?")
        self._workspace_intro_toggle.setCheckable(True)
        self._workspace_intro_toggle.setChecked(True)
        layout.addWidget(self._workspace_intro_toggle)
        self._workspace_intro = QLabel(WORKSPACE_INTRO)
        self._workspace_intro.setWordWrap(True)
        self._workspace_intro_toggle.toggled.connect(self._workspace_intro.setVisible)
        layout.addWidget(self._workspace_intro)

        # A configuration this app refuses to read stops every cycle, so it is
        # said plainly rather than left as an empty list (§4.1).
        self._workspace_error_label = QLabel("")
        self._workspace_error_label.setWordWrap(True)
        self._workspace_error_label.setStyleSheet(
            f"color: {theme.severity_color(current_scheme(), health.RED)};")
        self._workspace_error_label.hide()
        layout.addWidget(self._workspace_error_label)

        self._workspace_tree = QTreeWidget()
        self._workspace_tree.setColumnCount(6)
        self._workspace_tree.setHeaderLabels(
            ["Name", "Local folder", "iCloud folder", "Enabled",
             "Last successful sync", "Status"])
        self._workspace_tree.setSortingEnabled(True)
        self._workspace_tree.sortByColumn(WS_COL_NAME, Qt.SortOrder.AscendingOrder)
        self._workspace_tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        header = self._workspace_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(WS_COL_NAME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(WS_COL_LOCAL, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(WS_COL_REMOTE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(WS_COL_ENABLED, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(WS_COL_SYNCED, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(WS_COL_STATE, QHeaderView.ResizeMode.ResizeToContents)
        self._workspace_tree.setRootIsDecorated(False)
        self._workspace_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._workspace_tree.setUniformRowHeights(True)
        self._workspace_tree.itemSelectionChanged.connect(
            self._on_workspace_selection_changed)
        layout.addWidget(self._workspace_tree, 1)

        # The selected workspace's own message: the engine's bounded line, the
        # relative paths it named, and where to look. Never file content (§9).
        self._workspace_detail = QLabel("")
        self._workspace_detail.setWordWrap(True)
        self._workspace_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._workspace_detail_scroll = QScrollArea()
        self._workspace_detail_scroll.setMaximumHeight(140)
        self._workspace_detail_scroll.setWidgetResizable(True)
        self._workspace_detail_scroll.setWidget(self._workspace_detail)
        layout.addWidget(self._workspace_detail_scroll)

        buttons = QHBoxLayout()
        self._workspace_add = QPushButton("&Add workspace…")
        self._workspace_add.clicked.connect(self._add_workspace)
        self._workspace_sync = QPushButton("&Sync now")
        self._workspace_sync.setToolTip(
            "Ask for one pass now. It runs through the same single-flight path "
            "as the timer, so it never starts a second cycle.")
        self._workspace_sync.clicked.connect(self.workspace_sync_requested.emit)
        self._workspace_pause = QPushButton("Pause")
        self._workspace_pause.setToolTip(
            "Stop scheduling this workspace. A cycle already running finishes "
            "on its own; nothing is interrupted and nothing is deleted.")
        self._workspace_pause.clicked.connect(lambda: self._set_workspace_enabled(False))
        self._workspace_resume = QPushButton("Resume")
        self._workspace_resume.clicked.connect(lambda: self._set_workspace_enabled(True))
        self._workspace_open_local = QPushButton("Open local folder")
        self._workspace_open_local.clicked.connect(self._open_workspace_local)
        self._workspace_open_remote = QPushButton("Open iCloud folder")
        self._workspace_open_remote.clicked.connect(self._open_workspace_remote)
        self._workspace_forget = QPushButton("Forget workspace…")
        self._workspace_forget.setToolTip(
            "Remove this workspace's entry from this app. The local folder, the "
            "iCloud folder, the sync state and the backups are all kept.")
        self._workspace_forget.clicked.connect(self._forget_workspace)
        buttons.addWidget(self._workspace_add)
        buttons.addWidget(self._workspace_sync)
        buttons.addSpacing(16)
        buttons.addWidget(self._workspace_pause)
        buttons.addWidget(self._workspace_resume)
        buttons.addSpacing(16)
        buttons.addWidget(self._workspace_open_local)
        buttons.addWidget(self._workspace_open_remote)
        buttons.addStretch(1)
        buttons.addWidget(self._workspace_forget)
        layout.addLayout(buttons)
        self._update_workspace_buttons()
        return page

    def update_workspaces(self, rows, *, error: str = "") -> None:
        """Render the tab from the controller's classified rows (§11.1).

        Presentation only, and no I/O of any kind: every row was classified from
        a cycle result or from the persisted status document, both of which the
        controller reads without touching the mount.
        """
        self._workspace_rows = list(rows)
        if self._workspace_intro_count != len(self._workspace_rows):
            self._workspace_intro_count = len(self._workspace_rows)
            self._workspace_intro_toggle.setChecked(not self._workspace_rows)
        # A pass completes every five seconds, and most of them change nothing
        # a column shows. Rebuilding the tree anyway would reset the operator's
        # scroll position and selection while they are reading it, so the rows
        # are rebuilt only when their rendered content actually moved.
        painted = tuple((row.workspace.id, self._workspace_columns(row))
                        for row in self._workspace_rows)
        if painted != self._workspace_painted:
            self._workspace_painted = painted
            self._rebuild_workspace_rows()
        if error:
            self._workspace_error_label.setText(
                f"Cannot read the workspace configuration: {error}. No "
                "workspace is being synchronized until it is readable again; "
                "the file has not been changed.")
            self._workspace_error_label.show()
        else:
            self._workspace_error_label.hide()
        self._update_workspace_buttons()
        self._render_workspace_detail()
        self._offer_obsidian()

    @staticmethod
    def _workspace_columns(row: WorkspaceRow) -> tuple[str, ...]:
        """One row's six columns, in the order §11.1 fixes."""
        return (row.workspace.name, row.workspace.local, row.workspace.remote,
                "yes" if row.workspace.enabled else "no",
                row.last_success_at or "never",
                WORKSPACE_STATE_LABELS.get(row.state, row.state))

    def _rebuild_workspace_rows(self) -> None:
        selected = self._selected_workspace_id()
        self._workspace_tree.clear()
        for row in self._workspace_rows:
            item = QTreeWidgetItem(self._workspace_tree,
                                   list(self._workspace_columns(row)))
            item.setData(0, ROLE_PATH, row.workspace.id)
            # The cells elide in the middle, so both path columns carry the whole
            # path as a tooltip. The remote *column* stays the iCloud-relative
            # folder §11.1 fixes; only the tooltip resolves it against the mount.
            item.setToolTip(WS_COL_LOCAL, row.workspace.local)
            item.setToolTip(WS_COL_REMOTE, workspace_remote_display(row.workspace))
            if row.state in WORKSPACE_ATTENTION_STATES:
                brush = QBrush(QColor(theme.severity_color(current_scheme(), health.YELLOW)))
                for column in range(self._workspace_tree.columnCount()):
                    item.setForeground(column, brush)
            if row.workspace.id == selected:
                item.setSelected(True)
                self._workspace_tree.setCurrentItem(item)

    def _selected_workspace_id(self) -> str:
        items = self._workspace_tree.selectedItems()
        if not items:
            return ""
        value = items[0].data(0, ROLE_PATH)
        return value if isinstance(value, str) else ""

    def _selected_workspace(self) -> WorkspaceRow | None:
        identifier = self._selected_workspace_id()
        for row in self._workspace_rows:
            if row.workspace.id == identifier:
                return row
        return None

    def _on_workspace_selection_changed(self) -> None:
        self._update_workspace_buttons()
        self._render_workspace_detail()

    def _update_workspace_buttons(self) -> None:
        """Everything that would touch /mnt/icloud is off while I/O is paused.

        Pause, Resume and Forget stay available: they only rewrite this app's
        own configuration file, which is exactly what an operator may still want
        to do with the bridge powered off (§11.1).
        """
        row = self._selected_workspace()
        mount_ready = not self._io_paused
        self._workspace_add.setEnabled(mount_ready and not self._workspace_busy)
        self._workspace_sync.setEnabled(
            mount_ready and any(item.workspace.enabled
                                for item in self._workspace_rows))
        self._workspace_pause.setEnabled(row is not None and row.workspace.enabled)
        self._workspace_resume.setEnabled(row is not None
                                          and not row.workspace.enabled)
        self._workspace_open_local.setEnabled(row is not None)
        self._workspace_open_remote.setEnabled(row is not None and mount_ready)
        self._workspace_forget.setEnabled(row is not None)

    def _render_workspace_detail(self) -> None:
        row = self._selected_workspace()
        if row is None:
            self._workspace_detail.setText(
                "" if self._workspace_rows else
                "No workspaces yet. Add one to keep a local folder in step with "
                "a folder in iCloud.")
            return
        label = WORKSPACE_STATE_LABELS.get(row.state, row.state)
        parts = [f"{row.workspace.name}: {label}"
                 + (f" — {row.detail}" if row.detail else "")]
        if row.paths:
            # Names only, bounded by `workspace_sync` before they ever get here.
            parts.append(f"Affected paths ({len(row.paths)} shown):\n"
                         + "\n".join(f"  {path}" for path in row.paths))
        if row.state in WORKSPACE_ATTENTION_STATES:
            parts.append(
                "Both folders keep their own version; nothing was merged.\n"
                f"  Local workspace: {row.workspace.local}\n"
                f"  iCloud folder:   {workspace_remote_display(row.workspace)}\n"
                f"  Backups:         {workspaces.backups_dir(row.workspace.id)}")
        self._workspace_detail.setText("\n".join(parts))

    def _open_workspace_local(self) -> None:
        row = self._selected_workspace()
        if row is not None:
            self._open_externally(row.workspace.local)

    def _open_workspace_remote(self) -> None:
        row = self._selected_workspace()
        if row is None or self._io_paused:
            return
        self._open_externally(workspace_remote_display(row.workspace))

    def _offer_obsidian(self) -> None:
        """After a first seed, offer Obsidian — and keep the plain folder action.

        An `obsidian://` handler is not guaranteed to be registered, so this is
        an offer rather than the only way in: **Open local folder** is always
        there as well (§11.4).
        """
        for row in self._workspace_rows:
            if row.workspace.id not in self._awaiting_seed:
                continue
            if row.state != workspace_sync.STATE_UP_TO_DATE or not row.last_success_at:
                continue
            self._awaiting_seed.discard(row.workspace.id)
            answer = QMessageBox.question(
                self, "Open the local workspace?",
                f"“{row.workspace.name}” finished its first sync into\n"
                f"{row.workspace.local}\n\n"
                "Open the local workspace in Obsidian now? This needs Obsidian "
                "installed and registered for obsidian:// links; “Open local "
                "folder” always works.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer == QMessageBox.StandardButton.Yes:
                self._open_externally(obsidian_url(row.workspace.local))

    # ------------------------------------------- workspace configuration edits --

    def _edit_workspace_config(self, change) -> bool:
        """Read-modify-write `workspaces.json`, then tell the controller.

        The file is re-read first, so an edit never writes back a copy that was
        stale before it started, and `workspaces.save` re-applies the collision
        rules — a configuration this app would refuse to read is never one it
        writes. Local disk only; no CIFS is involved in any of it.
        """
        try:
            workspaces.save(change(workspaces.load()))
        except workspaces.WorkspaceError as exc:
            QMessageBox.warning(self, "Could not change the workspace",
                                f"{exc}\n\nNothing was changed.")
            return False
        self.workspaces_changed.emit()
        return True

    def _set_workspace_enabled(self, enabled: bool) -> None:
        """Pause or resume: the `enabled` flag and nothing else (§11.2).

        A cycle already running is deliberately left alone. It is bounded by its
        own subprocess timeout, and killing a running Unison is precisely the
        thing this feature never does.
        """
        row = self._selected_workspace()
        if row is None:
            return
        identifier = row.workspace.id
        self._edit_workspace_config(
            lambda config: workspaces.with_enabled(config, identifier, enabled))

    def _forget_workspace(self) -> None:
        row = self._selected_workspace()
        if row is None:
            return
        identifier = row.workspace.id
        answer = QMessageBox.question(
            self, "Forget this workspace?",
            WORKSPACE_FORGET_TEXT.format(
                local=row.workspace.local,
                remote=workspace_remote_display(row.workspace),
                state=workspaces.state_dir(identifier),
                backups=workspaces.backups_dir(identifier)),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return
        self._awaiting_seed.discard(identifier)
        self._edit_workspace_config(
            lambda config: workspaces.without_workspace(config, identifier))

    # ------------------------------------------------------ adding a workspace --

    def _add_workspace(self) -> None:
        if self._io_paused or self._workspace_busy:
            return
        dialog = AddWorkspaceDialog(
            self, existing=tuple(row.workspace for row in self._workspace_rows))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        candidate = dialog.candidate()
        if candidate is not None:
            self._probe_new_workspace(candidate)

    def _probe_new_workspace(self, candidate: workspaces.Workspace) -> None:
        """The filesystem half of the validation, on a worker (§4.3, §11.3).

        `check_local_state` stats ancestors and parses `/proc/self/mountinfo`;
        the remote scan reads CIFS. Neither may run on the GUI thread, and
        nothing here creates anything: the local root is created by the first
        cycle, after the operator has confirmed.
        """
        def work() -> WorkspaceProbe:
            workspaces.check_local_state(candidate.local)
            workspaces.check_local_occupancy(candidate.local)
            mount = os.path.normpath(workspaces.mount_root())
            if not os.path.ismount(mount):
                raise workspaces.WorkspaceError(
                    f"{mount} is not mounted, so the iCloud folder cannot be "
                    "checked yet.")
            remote_root = workspaces.remote_root(candidate.remote)
            snapshot = workspace_sync.scan(remote_root)
            if not snapshot:
                raise workspaces.WorkspaceError(
                    f"{remote_root} is empty, so there is nothing to copy into "
                    "a new workspace yet.")
            return WorkspaceProbe(
                remote_root=remote_root,
                remote_entries=len(snapshot),
                remote_bytes=workspace_sync.logical_size(snapshot),
                free_bytes=workspace_sync.free_bytes(candidate.local),
                local_exists=os.path.isdir(candidate.local))

        def done(probe: WorkspaceProbe) -> None:
            self._workspace_busy = False
            self.statusBar().clearMessage()
            self._update_workspace_buttons()
            self._confirm_new_workspace(candidate, probe)

        def failed(message: str) -> None:
            self._workspace_busy = False
            self.statusBar().clearMessage()
            self._update_workspace_buttons()
            QMessageBox.warning(self, "Cannot add this workspace",
                                f"{message}\n\nNothing was created.")

        self._workspace_busy = True
        self.statusBar().showMessage("Checking the iCloud folder…")
        self._update_workspace_buttons()
        self._run_async(work, done, failed)

    def _confirm_new_workspace(self, candidate: workspaces.Workspace,
                               probe: WorkspaceProbe) -> None:
        """State what the first copy will do, then ask (§11.3)."""
        needed = probe.remote_bytes + workspace_sync.FREE_SPACE_MARGIN_BYTES
        space = (""
                 if probe.free_bytes > needed else
                 "\n\nThere is not enough free space for the first copy "
                 f"({health.human_bytes(needed)} is needed, including the 1 GiB "
                 "margin), so it will refuse to start until there is.")
        answer = QMessageBox.question(
            self, "Add this Safe Workspace?",
            f"Name: {candidate.name}\n"
            f"iCloud folder: {probe.remote_root}\n"
            f"  {probe.remote_entries} item(s), "
            f"{health.human_bytes(probe.remote_bytes)}\n"
            f"Local folder: {candidate.local}\n"
            f"  {'empty' if probe.local_exists else 'will be created'}, "
            f"{health.human_bytes(probe.free_bytes)} free"
            f"{space}\n\n"
            f"{WORKSPACE_FIRST_COPY}\n\n{WORKSPACE_ADD_WARNING}",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return
        if self._edit_workspace_config(
                lambda config: workspaces.with_workspace(config, candidate)):
            # §11.4: this is the seeding whose success offers Obsidian.
            self._awaiting_seed.add(candidate.id)
            self.workspace_sync_requested.emit()

    # ------------------------------------------------------------- refreshing --

    def closeEvent(self, event) -> None:   # noqa: N802 (Qt naming)
        self._save_uistate()
        if self.hide_on_close:
            event.ignore()
            self.hide()
        else:
            # No tray: closing the only window is the Quit action, but it must go
            # through the controller's confirmation/transition, not exit under us.
            event.ignore()
            self.quit_requested.emit()

    def showEvent(self, event) -> None:   # noqa: N802 (Qt naming)
        super().showEvent(event)
        if not self._uistate_restored:
            self._uistate_restored = True
            self._restore_uistate()

    def _restore_uistate(self) -> None:
        """Put the window back where it was, or leave the defaults alone.

        A stored geometry that no longer meets any screen — an unplugged monitor,
        a smaller session — is discarded rather than applied, because a window
        nobody can see is worse than one in the wrong place. The stored tab may
        also be **Setup**, which the lifecycle inserts and removes (D31), so a
        name that is not currently a tab is simply ignored.
        """
        saved = uistate.load()
        candidate = QRect(saved.x, saved.y, saved.width, saved.height)
        if any(screen.availableGeometry().intersects(candidate)
               for screen in QGuiApplication.screens()):
            self.setGeometry(candidate)
        for index in range(self._tabs.count()):
            if self._tabs.tabText(index) == saved.tab:
                self._tabs.setCurrentIndex(index)
                return

    def _save_uistate(self) -> None:
        uistate.save(self.width(), self.height(), self.x(), self.y(),
                     self._tabs.tabText(self._tabs.currentIndex()))

    def _open_externally(self, target: str) -> None:
        if not open_externally(target):
            self.show_transient(
                f"Could not open {target}: no desktop handler could be started.")

    # ------------------------------------------------------- lifecycle gating --

    def show_banner(self, text: str, kind: str = "starting") -> None:
        """Show a transitional banner above the tabs."""
        self._banner.setText(text)
        self._banner_kind = kind
        self._banner.setStyleSheet(theme.banner_style(
            current_scheme(), kind if kind in theme.BANNER_STYLES[theme.LIGHT] else "starting"))
        self._banner.show()

    def hide_banner(self) -> None:
        self._banner.hide()

    def show_notice(self, text: str) -> None:
        """The window's stand-in for a desktop notification (no tray)."""
        self._notice.setText(text)
        self._notice.show()

    def hide_notice(self) -> None:
        self._notice.hide()

    # ------------------------------------------------- the D35 version gate --

    def _apply_compatibility(self, compatibility: bridge.Compatibility) -> None:
        """Record the classification and show the matching persistent banner.

        `unknown` shows nothing — a status document that has not arrived yet is
        an ordinary transient, already reported by the Guest agent health row —
        but it still leaves the write gate closed, which `writable` decides.
        """
        self._compatibility = compatibility
        if compatibility.state == bridge.COMPAT_SKEWED:
            self._protocol.setText(f"{bridge.SKEW_BANNER}\n{compatibility.detail}")
        elif compatibility.state == bridge.COMPAT_INCOMPATIBLE:
            self._protocol.setText(
                "The guest agent is not speaking this app's bridge protocol, so "
                "nothing will be written to it.\n"
                f"{compatibility.detail} {bridge.UPDATE_AGENT_INSTRUCTION}")
        else:
            self._protocol_box.hide()
            self._update_buttons()
            return
        self._protocol.setStyleSheet(theme.protocol_style(
            current_scheme(), compatibility.state))
        self._protocol_box.show()
        self._update_buttons()

    def _refuse_incompatible(self) -> bool:
        """True when the protocol gate is closed; also explains why on the tab."""
        if self._compatibility.writable:
            return False
        self._sync_error.setText(
            "Selective sync is unavailable until the guest agent matches this "
            f"app. {self._compatibility.detail} {bridge.UPDATE_AGENT_INSTRUCTION}")
        self._sync_error.show()
        return True

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

    def set_reprovision_action(self, available: bool, *, first_run: bool) -> None:
        """Offer (or withdraw) the D35/D40-D44 re-provision action.

        One rule, decided by the controller, applied to both surfaces: the
        Status-tab button appears only when the action is possible, while the
        banner's button stays with its banner and is merely disabled — a banner
        pointing at a control that is not there would be worse than one pointing
        at a control that is greyed out.
        """
        if first_run:
            self._reprovision_button.setText("Set up Windows automatically")
            self._reprovision_button.setToolTip(
                "Inspect the Windows VM and recreate its missing iCloud shares.")
        else:
            self._reprovision_button.setText("Re-run Windows provisioning…")
            self._reprovision_button.setToolTip(
                "Inspect the Windows VM and repair only what no longer matches this "
                "app: the iCloud share, its permissions, and the bridge agent.")
        self._reprovision_button.setVisible(available)
        self._protocol_button.setEnabled(available)

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
        # The last version classification described an agent that is no longer
        # reachable; back to `unknown`, which also re-closes the D35 write gate.
        self._apply_compatibility(bridge.Compatibility())
        for dot, detail in self._check_widgets.values():
            dot.setText(NEUTRAL_GLYPH)
            dot.setAccessibleName("status unavailable")
            dot.setToolTip("status unavailable")
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
        self._open_files_button.setEnabled(enabled)

    def set_io_paused(self, paused: bool) -> None:
        """Gate new bridge I/O; also reflect it in the controls."""
        self._io_paused = paused
        self.set_bridge_controls_enabled(not paused)
        # The Safe Workspaces tab itself stays enabled — local folders and the
        # last status remain inspectable — but every action that would touch
        # /mnt/icloud follows the same gate (§11.1).
        self._update_workspace_buttons()
        self._sync_poll_timer()

    def apply_in_flight(self) -> bool:
        return self._apply_writing

    def quiesce(self) -> None:
        """Stop scheduling bridge I/O and drop any queued list requests.

        In-flight worker tasks still finish; the controller waits for them to
        drain before it lets the helper unmount (v2 plan D29).
        """
        self.set_io_paused(True)     # this also stops the response poll
        self._requests.reset()

    def resume(self) -> None:
        """Undo :meth:`quiesce` after an aborted shutdown.

        The response poll restarts only if something is still outstanding, which
        after a :meth:`quiesce` normally means nothing at all.
        """
        self.set_io_paused(False)

    def selection_facts(self) -> tuple[int | None, tuple[str, ...]]:
        """The loaded exclusion revision and paths, for a D37 report.

        The *loaded* selection, not the staged one: a report should describe what
        the bridge actually holds, not what the operator is part-way through
        choosing.
        """
        return self._loaded_revision, tuple(self._loaded_wanted)

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
        self._apply_compatibility(snapshot.compatibility)
        for check in snapshot.checks:
            dot, detail = self._ensure_check_row(check.name)
            dot.setText(SEVERITY_GLYPHS[check.severity])
            severity_text = f"{check.name}: {SEVERITY_WORDS[check.severity]}"
            dot.setAccessibleName(severity_text)
            dot.setToolTip(severity_text)
            dot.setStyleSheet(
                f"color: {theme.severity_color(current_scheme(), check.severity)};")
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
            # The D36 snapshot is written on this worker thread, immediately
            # after the read that produced it: it is local disk, not CIFS, and
            # keeping the two together is what stops a stale selection being
            # backed up later on the GUI thread.
            config = bridge.read_exclusions()
            return config, _save_backup(config["exclusions"], config["revision"],
                                        backup.SOURCE_READ)

        def done(result):
            config, backup_note = result
            self._config_error = None
            self._loaded_revision = config["revision"]
            self._loaded_wanted = list(config["exclusions"])
            self._wanted = list(config["exclusions"])
            self._set_backup_warning(backup_note)
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
        self._row_epoch += 1
        self._requests.reset()
        self._polls_in_flight.clear()
        self._sync_poll_timer()
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

    def _restart_filter_timer(self, _text: str) -> None:
        self._filter_timer.start()

    def _apply_filter(self, _text: str | None = None) -> None:
        """Show only matching rows and their ancestors; never change selection.

        This is presentation only: ``_wanted``, the check states and the
        in-memory tree are untouched, so clearing the filter restores exactly
        what was there — including which folders the operator had open.
        """
        if self._filter_timer.isActive():
            self._filter_timer.stop()
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
        greyed = QBrush(QColor(theme.dim_color(current_scheme())))

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
        """Rewrite the State column, skipping the walk when nothing feeding it moved.

        The 5 s tick (``REFRESH_INTERVAL_MS``) calls ``apply_snapshot``
        unconditionally, and every non-rebuild pass ends here — including while
        the window is hidden in the tray, and against a ``status.json`` the agent
        only rewrites every 15 s. The text this produces is a pure function of
        the exclusion states and details in that status, the wanted and loaded
        selections, and which rows exist; when none of those moved, the whole
        per-row walk is recomputing an identical answer.

        Measured at 5 219 directory rows: ``apply_snapshot`` cost 12.07-13.43 ms
        per tick, of which this function was 12.4-12.7 ms; with the early-out it
        is 0.016-0.034 ms. The saving is small in absolute terms against a guest
        that burns a fifth of a core, but it scales linearly with the library
        (~2.4 us per row per tick) and it removes a periodic block of the GUI
        thread. Extends D34's host-side rule from the document *parse* to the
        *render*.
        """
        states = self._status_exclusion_states()
        details = self._status_exclusion_details()
        key = (self._row_epoch,
               tuple(sorted(states.items())),
               tuple(sorted(details.items())),
               tuple(self._wanted),
               tuple(self._loaded_wanted))
        if key == self._state_column_key:
            return
        self._state_column_key = key
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
        self._row_epoch += 1
        more = QTreeWidgetItem(parent, ["Load more…", "", "", "", ""])
        more.setData(0, ROLE_KIND, "more")
        more.setData(0, ROLE_EXTRA, {"path": path, "offset": offset})
        more.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        # Link-like, so it reads as something to click rather than a file named
        # "Load more…"; one click or a keyboard activation is enough.
        font = more.font(COL_NAME)
        font.setUnderline(True)
        more.setFont(COL_NAME, font)
        more.setForeground(COL_NAME, QBrush(QColor(theme.link_color(current_scheme()))))

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
        if self._io_paused or not self._compatibility.writable:
            # Nothing was dispatched, so leave no state claiming otherwise. The
            # D35 gate belongs here rather than at the call sites: this is the
            # single point every list request passes through.
            self._on_request_dropped(path, offset, kind)
            self._refuse_incompatible()
            return

        def work():
            return bridge.request_listing(path, offset=offset, limit=LIST_PAGE)

        def done(request_id: str):
            self._requests.dispatched(
                request_id, path, offset, kind,
                datetime.now(timezone.utc).timestamp() + LIST_TIMEOUT_SECONDS)
            self._sync_poll_timer()

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

    def _sync_poll_timer(self) -> None:
        """Run the response poll exactly while there is something to poll for.

        Its inputs are the outstanding list requests, the response polls already
        dispatched for them, and the D29 I/O pause. Every path that changes one
        of those calls this, so the timer has no lifecycle of its own and no
        steady-state tick: an idle window polls nothing and wakes for nothing.
        """
        wanted = not self._io_paused and bool(
            self._requests.pending_ids() or self._polls_in_flight)
        if wanted and not self._poll_timer.isActive():
            self._poll_timer.start()
        elif not wanted and self._poll_timer.isActive():
            self._poll_timer.stop()

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
        self._sync_poll_timer()

    def _fail_request(self, request_id: str, message: str) -> None:
        """Common failure path: back to idle, or restore the continuation row."""
        request = self._requests.fail(request_id)
        self._sync_error.setText(message)
        self._sync_error.show()
        if request is not None and not request.is_first_page:
            self._restore_more_row(request)
        self._sync_poll_timer()

    def _on_response_failed(self, request_id: str, message: str) -> None:
        self._polls_in_flight.discard(request_id)
        self._fail_request(request_id, f"Bad reply from the guest agent: {message}")

    def _on_response(self, payload) -> None:
        # Every exit below either consumed a request or left one outstanding, so
        # the timer decision belongs to all of them rather than to each return.
        try:
            self._apply_response(payload)
        finally:
            self._sync_poll_timer()

    def _apply_response(self, payload) -> None:
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

        # New file rows land here, so the state column's memo must not survive
        # this: a freshly listed file needs its own state cell computed.
        self._row_epoch += 1
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

    # ----------------------------------------------- the D36 backup/restore --

    def _set_backup_warning(self, message: str) -> None:
        """Persistently warn about the local snapshot; empty text clears it."""
        if message:
            self._backup_warning.setText(message)
            self._backup_warning.show()
        else:
            self._backup_warning.hide()

    def _can_restore(self) -> bool:
        """Restore needs a settled, writable, fully loaded selective-sync tab.

        A staged but unapplied selection is deliberately a blocker rather than
        something to discard silently: the operator asked for those changes.
        """
        if self._io_paused or self._apply_writing:
            return False
        if self._loaded_revision is None or self._config_error is not None:
            return False
        if not self._compatibility.writable:
            return False
        return not self._selection_is_dirty()

    def _selection_is_dirty(self) -> bool:
        return (sorted(w.lower() for w in self._wanted)
                != sorted(w.lower() for w in self._loaded_wanted))

    def _restore_from_backup(self) -> None:
        """Explicit, previewed restore. Never automatic (D36)."""
        if not self._can_restore():
            if self._selection_is_dirty():
                QMessageBox.information(
                    self, "Apply or reload first",
                    "You have selective-sync changes that have not been applied. "
                    "Apply them, or press Reload to discard them, before "
                    "restoring the saved copy.")
            return

        def work():
            return backup.load()

        def done(saved):
            self._confirm_and_restore(saved)

        def failed(message: str):
            QMessageBox.critical(
                self, "Cannot restore",
                f"{message}\n\nNothing was changed.")

        self._run_async(work, done, failed)

    def _confirm_and_restore(self, saved) -> None:
        result = backup.preview(saved, self._loaded_wanted)
        if not result.changes_anything:
            QMessageBox.information(
                self, "Nothing to restore",
                "The saved copy matches what is configured in the VM already.")
            return

        parts = [f"Saved {saved.saved_at or 'at an unknown time'} "
                 f"(revision {saved.revision})."]
        if result.additions:
            parts.append("Exclude:\n" + "\n".join(f"  {p}" for p in result.additions)
                         + "\n\n" + EXCLUDE_WARNING)
        if result.removals:
            parts.append("Re-include:\n" + "\n".join(f"  {p}" for p in result.removals)
                         + "\n\n" + INCLUDE_WARNING)
        answer = QMessageBox.question(
            self, "Restore selective-sync choices?", "\n\n".join(parts),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return

        wanted = list(saved.exclusions)
        expect = self._loaded_revision
        applied = (self._status or {}).get("appliedRevision")
        last_written = self._last_written_revision
        # Strictly above every revision anyone has seen, including the backup's
        # own — restoring an old snapshot must still move the config forwards.
        minimum = saved.revision

        def work():
            revision = bridge.write_exclusions(
                wanted, expect_revision=expect, applied_revision=applied,
                last_written=last_written, minimum_revision=minimum)
            return revision, _save_backup(wanted, revision, backup.SOURCE_APPLY)

        def done(outcome):
            revision, backup_note = outcome
            self._apply_writing = False
            self._last_written_revision = revision
            self._last_write_at = datetime.now(timezone.utc)
            self._loaded_wanted = list(wanted)
            self._loaded_revision = revision
            self._wanted = list(wanted)
            self._set_backup_warning(backup_note)
            self._rebuild_tree()

        def failed(message: str):
            self._apply_writing = False
            QMessageBox.warning(
                self, "Could not restore",
                f"{message}\n\nNothing was changed. Reloading the current "
                "configuration.")
            self.reload_selective_sync()

        self._apply_writing = True
        self._run_async(work, done, failed)

    # ------------------------------------------------------------------ apply --

    def _update_buttons(self) -> None:
        selected = self._tree_widget.selectedItems()
        is_missing = bool(selected) and selected[0].data(0, ROLE_KIND) == "missing"
        self._remove_button.setEnabled(is_missing)
        dirty = self._selection_is_dirty()
        self._apply_button.setEnabled(
            dirty and self._config_error is None and self._compatibility.writable)
        self._restore_button.setEnabled(self._can_restore())

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
            self._row_epoch += 1
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
        if self._refuse_incompatible():
            # Fail closed: leave the current exclusions.json exactly as it is.
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
            revision = bridge.write_exclusions(
                wanted, expect_revision=expect, applied_revision=applied,
                last_written=last_written)
            # Apply has already succeeded at this point. A failed snapshot below
            # is reported as a warning and must never route through the "Nothing
            # was changed" dialog (D36).
            return revision, _save_backup(wanted, revision, backup.SOURCE_APPLY)

        def done(result):
            revision, backup_note = result
            self._apply_writing = False
            self._last_written_revision = revision
            self._last_write_at = datetime.now(timezone.utc)
            self._loaded_wanted = list(wanted)
            self._loaded_revision = revision
            self._wanted = list(wanted)
            self._set_backup_warning(backup_note)
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
