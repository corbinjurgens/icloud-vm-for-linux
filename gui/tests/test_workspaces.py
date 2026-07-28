"""Safe Workspaces configuration, path rules, and state layout (D52).

No Qt, no mount, no Docker, no Unison binary: every rule in sections 4 and 5 of
`docs/plan-safe-local-workspaces.md` is string work, local-disk work against an
injected base directory, or a parse of a `/proc/self/mountinfo` fixture, so all
of it is checkable here.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import workspaces  # noqa: E402


BASE_MOUNTINFO = "36 35 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"


class Env:
    """The injected XDG/mount layout a test runs against."""

    def __init__(self, tmp_path):
        self.home = str(tmp_path / "home" / "user")
        self.config = str(tmp_path / "config")
        self.state = str(tmp_path / "state")
        self.mount = str(tmp_path / "mnt" / "icloud")
        self.share = str(tmp_path / "mnt" / "icloud_bridge")
        self.local = str(tmp_path / "home" / "user" / "iCloud Workspaces" / "Vault")
        self.other = str(tmp_path / "home" / "user" / "iCloud Workspaces" / "Other")
        self.root = str(tmp_path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    layout = Env(tmp_path)
    os.makedirs(layout.home, exist_ok=True)
    monkeypatch.setenv("HOME", layout.home)
    monkeypatch.setenv("XDG_CONFIG_HOME", layout.config)
    monkeypatch.setenv("XDG_STATE_HOME", layout.state)
    monkeypatch.setenv("ICLOUD_MOUNT_DIR", layout.mount)
    monkeypatch.setenv("ICLOUD_BRIDGE_DIR", layout.share)
    return layout


@pytest.fixture
def mountinfo(tmp_path):
    """Write a mountinfo fixture and return its path."""
    def write(*extra_lines: str) -> str:
        path = tmp_path / "mountinfo"
        path.write_text(BASE_MOUNTINFO + "".join(
            line if line.endswith("\n") else line + "\n" for line in extra_lines),
            encoding="utf-8")
        return str(path)
    return write


def mount_line(mount_point: str, fstype: str) -> str:
    """One mountinfo line for ``mount_point``, with mountinfo's own escaping."""
    escaped = mount_point.replace("\\", "\\134").replace(" ", "\\040")
    return f"40 36 0:44 / {escaped} rw,relatime - {fstype} source rw"


def workspace(env, **overrides) -> workspaces.Workspace:
    fields = {
        "id": "9f14c0a7b3e25d68",
        "name": "Vault",
        "remote": "Documents/Vault",
        "local": env.local,
        "enabled": True,
    }
    fields.update(overrides)
    return workspaces.Workspace(**fields)


def mode_of(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# ------------------------------------------------------------------- paths --

def test_xdg_bases_fall_back_when_unset_empty_or_relative(env):
    for base, default in ((workspaces.config_base, os.path.join(env.home, ".config")),
                          (workspaces.state_base,
                           os.path.join(env.home, ".local", "state"))):
        assert base({}) == default
        assert base({"XDG_CONFIG_HOME": "", "XDG_STATE_HOME": ""}) == default
        assert base({"XDG_CONFIG_HOME": "rel/ative",
                     "XDG_STATE_HOME": "rel/ative"}) == default
    assert workspaces.config_base({"XDG_CONFIG_HOME": "/srv/config"}) == "/srv/config"
    assert workspaces.state_base({"XDG_STATE_HOME": "/srv/state"}) == "/srv/state"


def test_home_dir_ignores_a_relative_home(env):
    assert workspaces.home_dir({"HOME": "not/absolute"}) == env.home
    assert workspaces.home_dir({"HOME": "/srv/person"}) == "/srv/person"


def test_config_path_lives_in_the_application_directory(env):
    assert workspaces.config_path() == os.path.join(
        env.config, "icloud-bridge-gui", "workspaces.json")
    assert workspaces.config_path("/srv/config") == os.path.join(
        "/srv/config", "icloud-bridge-gui", "workspaces.json")


def test_the_mount_and_share_roots_follow_the_bridge_overrides(env):
    assert workspaces.mount_root() == env.mount
    assert workspaces.bridge_share_root() == env.share
    assert workspaces.mount_root({}) == "/mnt/icloud"
    assert workspaces.bridge_share_root({}) == "/mnt/icloud_bridge"


def test_every_state_path_hangs_off_the_workspace_directory(env):
    identifier = "9f14c0a7b3e25d68"
    root = os.path.join(env.state, "icloud-bridge-gui", "workspaces", identifier)
    assert workspaces.state_dir(identifier) == root
    assert workspaces.unison_dir(identifier) == os.path.join(root, "unison")
    assert workspaces.backups_dir(identifier) == os.path.join(root, "backups")
    assert workspaces.snapshot_path(identifier) == os.path.join(root, "snapshot.json")
    assert workspaces.baseline_path(identifier) == os.path.join(root, "baseline.json")
    assert workspaces.status_path(identifier) == os.path.join(root, "status.json")
    assert workspaces.log_path(identifier) == os.path.join(root, "sync.log")
    assert workspaces.lock_path(identifier) == os.path.join(root, "lock")


@pytest.mark.parametrize("identifier", [
    "../escape", "9F14C0A7B3E25D68", "9f14c0a7b3e25d6", "9f14c0a7b3e25d688",
    "", "not-hex-at-all!!", 7, None,
])
def test_a_state_path_refuses_an_id_that_is_not_the_documented_shape(env, identifier):
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.state_dir(identifier)


def test_ensure_state_dir_creates_every_level_0700(env):
    identifier = "9f14c0a7b3e25d68"
    workspaces.ensure_state_dir(identifier)
    for path in (workspaces.state_root(), workspaces.state_dir(identifier),
                 workspaces.unison_dir(identifier),
                 workspaces.backups_dir(identifier)):
        assert os.path.isdir(path)
        assert mode_of(path) == 0o700


def test_ensure_state_dir_tightens_a_loose_existing_directory(env):
    identifier = "9f14c0a7b3e25d68"
    workspaces.ensure_state_dir(identifier)
    os.chmod(workspaces.state_dir(identifier), 0o755)
    workspaces.ensure_state_dir(identifier)
    assert mode_of(workspaces.state_dir(identifier)) == 0o700


def test_a_symlinked_state_directory_is_refused(env, tmp_path):
    identifier = "9f14c0a7b3e25d68"
    os.makedirs(workspaces.state_root(), mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(str(elsewhere), workspaces.state_dir(identifier))
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.ensure_state_dir(identifier)


# ---------------------------------------------------------- remote paths --

@pytest.mark.parametrize("given,expected", [
    ("Documents/Vault", "Documents/Vault"),
    ("Documents///Vault//", "Documents/Vault"),
    ("Documents//Vault/", "Documents/Vault"),
    ("Documents/Vault/", "Documents/Vault"),
    ("Documents", "Documents"),
    ("Notes/My Vault/Daily notes", "Notes/My Vault/Daily notes"),
    ("Notes/Ñandu", "Notes/Ñandu"),
    ("Notes/N\u0303andu", "Notes/\u00d1andu"),   # a decomposed name is stored as NFC
])
def test_remote_normalization(given, expected):
    assert workspaces.normalize_remote(given) == expected


@pytest.mark.parametrize("given", [
    "", "/", "//", ".", "..", "./Documents", "Documents/..",
    "Documents/./Vault", "/mnt/icloud/Documents", "/Documents",
    "Documents\\Vault", "Documents\x00Vault", "Documents\nVault",
    "Documents\tVault", "a" * 1025, "Documents/" + "b" * 256,
    7, None, ["Documents"],
])
def test_remote_rejections(given):
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.normalize_remote(given)


@pytest.mark.parametrize("given", [
    # A character Windows cannot use in a folder name (bridge.BAD_SEGMENT_CHARS).
    'Documents/Vault<x', 'Documents/Vault>x', 'Documents/Vault:x',
    'Documents/Vault"x', "Documents/Vault|x", "Documents/Vault?x",
    "Documents/Vault*x", "<Documents>/Vault",
    # A segment Windows cannot name because it ends with a space or a dot.
    "Documents /Vault", "Documents./Vault", "Documents/Vault ",
    "Documents/Vault.", "Documents/Vault..", "Trailing dot./Vault",
    # A segment that is only whitespace.
    "Documents/   ", "   /Vault", "Documents/　",
])
def test_remote_rejects_windows_invalid_segments(given):
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.normalize_remote(given)


@pytest.mark.parametrize("given", [
    "My Notes", "Documents/My Notes", "Café", "Documents/Café",
    "Notes/My Vault/Daily notes", "a.b/c.d", "a b.c d",
])
def test_remote_accepts_legitimate_spaces_dots_and_unicode(given):
    assert workspaces.normalize_remote(given) == given


def test_a_multibyte_segment_is_measured_in_bytes_not_characters():
    # 128 two-byte characters is 256 bytes, one over the limit.
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.normalize_remote("Notes/" + "ñ" * 128)
    workspaces.normalize_remote("Notes/" + "ñ" * 127)


def test_remote_comparison_is_case_insensitive_and_unicode_normalized():
    assert workspaces.remote_key("Documents/Vault") == "documents/vault"
    # A decomposed name and a differently cased composed one are the same
    # folder to the Windows endpoint.
    assert (workspaces.remote_key("Notes/N\u0303andu")
            == workspaces.remote_key("Notes/\u00d1ANDU"))


def test_the_remote_root_joins_onto_the_configured_mount(env):
    assert workspaces.remote_root("Documents/Vault") == os.path.join(
        env.mount, "Documents/Vault")
    assert workspaces.remote_root("Documents/Vault", {}) == "/mnt/icloud/Documents/Vault"


def test_the_mount_root_itself_is_not_a_workspace(env):
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.remote_root("")


# ----------------------------------------------------------- local paths --

def test_local_normalization_keeps_spaces_and_unicode(env):
    given = os.path.join(env.home, "iCloud Workspaces", "Nötes", "")
    assert workspaces.normalize_local(given) == os.path.join(
        env.home, "iCloud Workspaces", "Nötes")
    assert workspaces.normalize_local(
        os.path.join(env.home, "a", "..", "b")) == os.path.join(env.home, "b")


def test_a_local_root_may_not_sit_inside_a_reserved_root(env):
    for reserved in (env.mount, env.share, env.state, env.config):
        with pytest.raises(workspaces.WorkspaceError):
            workspaces.normalize_local(os.path.join(reserved, "Vault"))
        with pytest.raises(workspaces.WorkspaceError):
            workspaces.normalize_local(reserved)


def test_a_local_root_may_not_contain_a_reserved_root(env):
    # tmp_path holds the mount, the share, and both XDG bases.
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.normalize_local(env.root)


@pytest.mark.parametrize("given", [
    "relative/path", "~/Vault", "", "   ", "/", "/home", "/local\x00root",
    "/" + "x" * 4100, 7, None,
])
def test_local_rejections(env, given):
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.normalize_local(given)


def test_the_home_directory_itself_is_refused(env):
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.normalize_local(env.home)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.normalize_local(env.home + "/")


def test_a_symlinked_local_root_is_refused(env, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.makedirs(os.path.dirname(env.local), exist_ok=True)
    os.symlink(str(elsewhere), env.local)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.check_local_directory(env.local)


def test_a_symlinked_ancestor_of_the_local_root_is_refused(env, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    parent = os.path.dirname(env.local)
    os.makedirs(os.path.dirname(parent), exist_ok=True)
    os.symlink(str(elsewhere), parent)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.check_local_directory(env.local)


def test_a_local_root_that_is_not_a_directory_is_refused(env):
    os.makedirs(os.path.dirname(env.local), exist_ok=True)
    with open(env.local, "w", encoding="utf-8") as handle:
        handle.write("not a directory")
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.check_local_directory(env.local)


def test_a_nonexistent_local_root_passes_the_directory_check(env):
    workspaces.check_local_directory(env.local)


def test_occupancy_accepts_only_a_missing_or_empty_directory(env):
    workspaces.check_local_occupancy(env.local)          # nonexistent
    os.makedirs(env.local)
    workspaces.check_local_occupancy(env.local)          # empty
    with open(os.path.join(env.local, "note.md"), "w", encoding="utf-8") as handle:
        handle.write("hello")
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.check_local_occupancy(env.local)


def test_create_local_root_is_0700_regardless_of_the_umask(env):
    previous = os.umask(0o000)
    try:
        workspaces.create_local_root(env.local)
    finally:
        os.umask(previous)
    assert mode_of(env.local) == 0o700
    # Idempotent: a second call neither fails nor loosens the mode.
    workspaces.create_local_root(env.local)
    assert mode_of(env.local) == 0o700


def test_create_local_root_refuses_a_symlink(env, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.makedirs(os.path.dirname(env.local), exist_ok=True)
    os.symlink(str(elsewhere), env.local)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.create_local_root(env.local)
    assert list(elsewhere.iterdir()) == []


# ------------------------------------------------- filesystem classification --

def test_mountinfo_parsing_unescapes_and_keeps_file_order():
    text = (BASE_MOUNTINFO
            + mount_line("/mnt/My Files", "btrfs") + "\n"
            + "garbage line\n"
            + mount_line("/mnt/icloud", "cifs") + "\n")
    assert workspaces.parse_mountinfo(text) == (
        ("/", "ext4"), ("/mnt/My Files", "btrfs"), ("/mnt/icloud", "cifs"))


def test_mountinfo_without_optional_fields_still_parses():
    text = "36 35 8:1 / /data rw - xfs /dev/sdb1 rw\n"
    assert workspaces.parse_mountinfo(text) == (("/data", "xfs"),)


@pytest.mark.parametrize("fstype", list(workspaces.ALLOWED_FILESYSTEMS))
def test_an_allowlisted_filesystem_is_accepted(env, mountinfo, fstype, tmp_path):
    path = str(tmp_path / "disk" / "Vault")
    info = mountinfo(mount_line(str(tmp_path / "disk"), fstype))
    os.makedirs(path)
    assert workspaces.check_filesystem(path, mountinfo_path=info) == fstype


@pytest.mark.parametrize("fstype", ["cifs", "nfs4", "fuse.sshfs", "9p",
                                    "virtiofs", "overlay", "tmpfs", "ramfs"])
def test_a_rejected_filesystem_names_the_detected_type(env, mountinfo, fstype,
                                                       tmp_path):
    path = str(tmp_path / "disk" / "Vault")
    info = mountinfo(mount_line(str(tmp_path / "disk"), fstype))
    os.makedirs(path)
    with pytest.raises(workspaces.WorkspaceError) as caught:
        workspaces.check_filesystem(path, mountinfo_path=info)
    assert fstype in str(caught.value)


def test_the_longest_matching_mount_point_wins(env, mountinfo, tmp_path):
    disk = tmp_path / "disk"
    inner = disk / "inner"
    inner.mkdir(parents=True)
    info = mountinfo(mount_line(str(disk), "ext4"), mount_line(str(inner), "tmpfs"))
    assert workspaces.filesystem_type(str(disk), mountinfo_path=info) == "ext4"
    assert workspaces.filesystem_type(str(inner), mountinfo_path=info) == "tmpfs"


def test_a_later_mount_on_the_same_point_shadows_an_earlier_one(env, mountinfo,
                                                                tmp_path):
    disk = tmp_path / "disk"
    disk.mkdir()
    info = mountinfo(mount_line(str(disk), "ext4"), mount_line(str(disk), "tmpfs"))
    assert workspaces.filesystem_type(str(disk), mountinfo_path=info) == "tmpfs"


def test_a_mount_point_with_spaces_and_unicode_classifies(env, mountinfo, tmp_path):
    disk = tmp_path / "My Disk ñ"
    target = disk / "Vault"
    target.mkdir(parents=True)
    info = mountinfo(mount_line(str(disk), "bcachefs"))
    assert workspaces.check_filesystem(str(target), mountinfo_path=info) == "bcachefs"


def test_a_nonexistent_root_is_classified_by_its_nearest_existing_parent(
        env, mountinfo, tmp_path):
    disk = tmp_path / "disk"
    disk.mkdir()
    info = mountinfo(mount_line(str(disk), "zfs"))
    missing = str(disk / "not" / "created" / "yet")
    assert workspaces.nearest_existing(missing) == str(disk)
    assert workspaces.check_filesystem(missing, mountinfo_path=info) == "zfs"


def test_an_unclassifiable_path_is_refused_not_assumed_local(tmp_path):
    empty = tmp_path / "empty-mountinfo"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.filesystem_type(str(tmp_path), mountinfo_path=str(empty))


def test_an_unreadable_mountinfo_is_an_error(tmp_path):
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.filesystem_type("/", mountinfo_path=str(tmp_path / "absent"))


def test_check_local_state_combines_the_symlink_and_filesystem_rules(
        env, mountinfo, tmp_path):
    info = mountinfo(mount_line(str(tmp_path), "ext4"))
    assert workspaces.check_local_state(env.local, mountinfo_path=info) == "ext4"
    os.makedirs(os.path.dirname(env.local), exist_ok=True)
    os.symlink(str(tmp_path), env.local)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.check_local_state(env.local, mountinfo_path=info)


# ------------------------------------------------------------- validation --

def test_validate_fields_normalizes_and_generates_an_id(env):
    result = workspaces.validate_fields("  Vault  ", "Documents//Vault/",
                                        env.local + "/")
    assert result.name == "Vault"
    assert result.remote == "Documents/Vault"
    assert result.local == env.local
    assert result.enabled is True
    assert workspaces.validate_id(result.id) == result.id


def test_generated_ids_are_sixteen_lowercase_hex_characters():
    identifiers = {workspaces.new_id() for _ in range(50)}
    assert len(identifiers) == 50
    for identifier in identifiers:
        assert workspaces.validate_id(identifier) == identifier


@pytest.mark.parametrize("name", ["", "   ", "x" * 81, "a\nb", "a\x00b", "a\tb",
                                  7, None])
def test_a_bad_display_name_is_refused(env, name):
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.validate_fields(name, "Documents/Vault", env.local)


def test_names_need_not_be_unique(env):
    first = workspaces.validate_fields("Vault", "Documents/One", env.local)
    workspaces.validate_fields("Vault", "Documents/Two", env.other,
                               existing=[first])


def test_a_duplicate_id_is_refused(env):
    first = workspace(env)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.validate_fields("Other", "Documents/Other", env.other,
                                   workspace_id=first.id, existing=[first])


@pytest.mark.parametrize("candidate_local", [
    "{local}", "{local}/inside", "{parent}",
])
def test_overlapping_local_roots_are_refused(env, candidate_local):
    first = workspace(env)
    target = candidate_local.format(local=env.local,
                                    parent=os.path.dirname(env.local))
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.validate_fields("Other", "Documents/Other", target,
                                   existing=[first])


@pytest.mark.parametrize("candidate_remote", [
    "Documents/Vault", "documents/vault", "DOCUMENTS/VAULT/Sub",
    "Documents", "documents/Vault/deeper/still",
])
def test_overlapping_remote_roots_are_refused_case_insensitively(
        env, candidate_remote):
    first = workspace(env)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.validate_fields("Other", candidate_remote, env.other,
                                   existing=[first])


def test_unicode_equivalent_remote_roots_collide(env):
    first = workspace(env, remote="Notes/N\u0303andu")
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.validate_fields("Other", "Notes/ñANDU", env.other,
                                   existing=[first])


def test_a_neighbouring_remote_root_is_not_an_overlap(env):
    first = workspace(env, remote="Documents/Vault")
    result = workspaces.validate_fields("Other", "Documents/Vaults", env.other,
                                        existing=[first])
    assert result.remote == "Documents/Vaults"


def test_a_neighbouring_local_root_is_not_an_overlap(env):
    first = workspace(env)
    result = workspaces.validate_fields("Other", "Documents/Other",
                                        env.local + "-two", existing=[first])
    assert result.local == env.local + "-two"


def test_validation_touches_no_filesystem(env, monkeypatch):
    """The GUI runs this on every keystroke, so it must not stat anything."""
    def forbidden(*args, **kwargs):
        raise AssertionError("validate_fields must not touch the filesystem")

    for name in ("stat", "lstat", "listdir", "makedirs", "open", "mkdir"):
        monkeypatch.setattr(os, name, forbidden)
    assert workspaces.validate_fields("Vault", "Documents/Vault", env.local)


# ------------------------------------------------------------- the document --

def test_parse_accepts_the_documented_shape(env):
    document = {
        "version": 1,
        "workspaces": [{
            "id": "9f14c0a7b3e25d68",
            "name": "Vault",
            "remote": "Documents/Vault",
            "local": env.local,
            "enabled": True,
        }],
    }
    config = workspaces.parse(document)
    assert config.workspaces == (workspace(env),)
    assert config.as_document() == document


def test_a_missing_configuration_means_no_workspaces(env):
    assert workspaces.load().workspaces == ()


@pytest.mark.parametrize("payload", [
    "not json at all",
    "[]",
    '{"version": 2, "workspaces": []}',
    '{"version": "1", "workspaces": []}',
    '{"version": true, "workspaces": []}',
    '{"workspaces": []}',
    '{"version": 1}',
    '{"version": 1, "workspaces": {}}',
    '{"version": 1, "workspaces": [], "extra": 1}',
    '{"version": 1, "workspaces": ["not an object"]}',
])
def test_a_malformed_document_fails_closed(env, payload):
    workspaces.ensure_config_dir()
    with open(workspaces.config_path(), "w", encoding="utf-8") as handle:
        handle.write(payload)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.load()
    # The unreadable file is reported, never rewritten or replaced.
    with open(workspaces.config_path(), encoding="utf-8") as handle:
        assert handle.read() == payload


def test_an_entry_with_an_unknown_or_missing_field_is_rejected(env):
    good = workspace(env).as_document()
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.parse({"version": 1, "workspaces": [dict(good, colour="red")]})
    for field in ("id", "name", "remote", "local", "enabled"):
        broken = {k: v for k, v in good.items() if k != field}
        with pytest.raises(workspaces.WorkspaceError):
            workspaces.parse({"version": 1, "workspaces": [broken]})


@pytest.mark.parametrize("field,value", [
    ("id", "nope"), ("id", 7), ("name", ""), ("name", "a\nb"),
    ("remote", "../escape"), ("remote", "/Documents"), ("remote", ""),
    ("local", "relative"), ("local", "/"), ("enabled", "true"), ("enabled", 1),
])
def test_a_bad_field_value_is_rejected(env, field, value):
    entry = dict(workspace(env).as_document(), **{field: value})
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.parse({"version": 1, "workspaces": [entry]})


def test_duplicate_and_overlapping_entries_are_rejected_at_load(env):
    first = workspace(env).as_document()
    collisions = [
        dict(first),                                            # duplicate id
        dict(first, id="0" * 16, remote="Documents/Other"),     # duplicate local
        dict(first, id="0" * 16, local=env.other,
             remote="DOCUMENTS/vault/sub"),                     # remote overlap
        dict(first, id="0" * 16, remote="Documents/Other",
             local=os.path.join(env.local, "inner")),           # local overlap
    ]
    for second in collisions:
        with pytest.raises(workspaces.WorkspaceError):
            workspaces.parse({"version": 1, "workspaces": [first, second]})


def test_more_than_the_maximum_number_of_workspaces_is_rejected(env):
    entries = [dict(workspace(env).as_document(),
                    id=f"{index:016x}",
                    remote=f"Documents/Vault{index}",
                    local=f"{env.local}-{index}")
               for index in range(workspaces.MAX_WORKSPACES + 1)]
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.parse({"version": 1, "workspaces": entries})


def test_an_oversized_configuration_is_rejected(env, monkeypatch):
    workspaces.save(workspaces.WorkspaceConfig((workspace(env),)))
    monkeypatch.setattr(workspaces, "MAX_CONFIG_BYTES", 8)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.load()


def test_a_symlinked_configuration_file_is_not_followed(env, tmp_path):
    workspaces.ensure_config_dir()
    victim = tmp_path / "victim.json"
    victim.write_text("do not touch", encoding="utf-8")
    os.symlink(str(victim), workspaces.config_path())
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.load()
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.save(workspaces.WorkspaceConfig((workspace(env),)))
    assert victim.read_text(encoding="utf-8") == "do not touch"


def test_a_symlinked_config_directory_is_refused(env, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.makedirs(env.config, exist_ok=True)
    os.symlink(str(elsewhere), workspaces.config_dir())
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.save(workspaces.WorkspaceConfig((workspace(env),)))
    assert list(elsewhere.iterdir()) == []


def test_a_non_regular_destination_is_refused(env):
    directory = workspaces.ensure_config_dir()
    os.mkdir(os.path.join(directory, workspaces.CONFIG_NAME))
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.save(workspaces.WorkspaceConfig((workspace(env),)))
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.load()


# ---------------------------------------------------------------- writing --

def test_save_creates_a_0700_directory_and_a_0600_file(env):
    assert workspaces.save(
        workspaces.WorkspaceConfig((workspace(env),))) == workspaces.SAVED
    assert mode_of(workspaces.config_dir()) == 0o700
    assert mode_of(workspaces.config_path()) == 0o600


def test_a_round_trip_preserves_order_and_every_field(env):
    config = workspaces.WorkspaceConfig((
        workspace(env, id="ff" * 8, name="Second", remote="Documents/Two",
                  local=env.other, enabled=False),
        workspace(env),
    ))
    workspaces.save(config)
    assert workspaces.load() == config
    with open(workspaces.config_path(), encoding="utf-8") as handle:
        assert json.load(handle)["workspaces"][0]["name"] == "Second"


def test_an_unchanged_save_skips_the_write_but_tightens_the_mode(env):
    config = workspaces.WorkspaceConfig((workspace(env),))
    workspaces.save(config)
    path = workspaces.config_path()
    os.chmod(path, 0o644)
    before = os.stat(path).st_mtime_ns

    assert workspaces.save(config) == workspaces.UNCHANGED
    assert os.stat(path).st_mtime_ns == before
    assert mode_of(path) == 0o600


def test_save_leaves_no_temporary_file_behind(env):
    workspaces.save(workspaces.WorkspaceConfig((workspace(env),)))
    workspaces.save(workspaces.WorkspaceConfig(
        (workspace(env), workspace(env, id="ff" * 8, remote="Documents/Two",
                                   local=env.other))))
    assert sorted(os.listdir(workspaces.config_dir())) == ["workspaces.json"]


def test_a_failed_replacement_keeps_the_previous_file_and_cleans_up(env,
                                                                    monkeypatch):
    original = workspaces.WorkspaceConfig((workspace(env),))
    workspaces.save(original)

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    # A nested context, so undoing this patch cannot also undo the fixture's
    # injected environment and send the assertions below at the real ~/.config.
    with monkeypatch.context() as failing:
        failing.setattr(os, "replace", boom)
        with pytest.raises(workspaces.WorkspaceError):
            workspaces.save(workspaces.WorkspaceConfig(
                (workspace(env, name="Renamed"),)))
    assert workspaces.load() == original
    assert sorted(os.listdir(workspaces.config_dir())) == ["workspaces.json"]


def test_saving_preserves_unrelated_files_in_the_directory(env):
    directory = workspaces.ensure_config_dir()
    neighbour = os.path.join(directory, "something-else.json")
    with open(neighbour, "w", encoding="utf-8") as handle:
        handle.write('{"kept": true}')
    workspaces.save(workspaces.WorkspaceConfig((workspace(env),)))
    with open(neighbour, encoding="utf-8") as handle:
        assert handle.read() == '{"kept": true}'


def test_save_refuses_a_configuration_it_would_refuse_to_read(env):
    colliding = workspaces.WorkspaceConfig(
        (workspace(env), workspace(env, id="ff" * 8)))
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.save(colliding)
    assert not os.path.exists(workspaces.config_path())


# ------------------------------------------------------------ edit helpers --

def test_scheduling_order_is_by_id_and_skips_paused_workspaces(env):
    config = workspaces.WorkspaceConfig((
        workspace(env, id="ff" * 8, remote="Documents/Two", local=env.other),
        workspace(env, id="00" * 8, remote="Documents/Three",
                  local=env.local + "-three"),
        workspace(env, enabled=False),
    ))
    assert [w.id for w in config.scheduled()] == ["00" * 8, "ff" * 8]


def test_adding_removing_and_pausing_a_workspace(env):
    first = workspace(env)
    config = workspaces.with_workspace(workspaces.WorkspaceConfig(), first)
    assert config.get(first.id) == first

    paused = workspaces.with_enabled(config, first.id, False)
    assert paused.workspaces[0].enabled is False
    assert paused.scheduled() == ()

    assert workspaces.without_workspace(paused, first.id).workspaces == ()


def test_adding_a_colliding_workspace_is_refused(env):
    config = workspaces.with_workspace(workspaces.WorkspaceConfig(), workspace(env))
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.with_workspace(config, workspace(env, id="ff" * 8))


def test_editing_an_unknown_workspace_is_refused(env):
    config = workspaces.WorkspaceConfig()
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.without_workspace(config, "9f14c0a7b3e25d68")
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.with_enabled(config, "9f14c0a7b3e25d68", False)
