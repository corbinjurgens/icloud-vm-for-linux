"""Backup/restore of the selective-sync choices (v2 plan D36).

No Qt, no mount, no docker: the whole module is local-disk work against an
injected base directory, so the atomicity, permission and revision-monotonicity
rules are all checkable here.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import backup  # noqa: E402


@pytest.fixture
def base(tmp_path):
    return str(tmp_path / "state")


def fixed_clock():
    return datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def saved_document(base):
    with open(backup.backup_path(base), encoding="utf-8") as handle:
        return json.load(handle)


def mode_of(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# ------------------------------------------------------------------- paths --

def test_state_base_defaults_when_xdg_is_unset_or_relative():
    home = os.path.expanduser("~")
    default = os.path.join(home, ".local", "state")
    assert backup.state_base({}) == default
    assert backup.state_base({"XDG_STATE_HOME": ""}) == default
    # A relative XDG_STATE_HOME is invalid per the spec; do not honour it.
    assert backup.state_base({"XDG_STATE_HOME": "relative/path"}) == default
    assert backup.state_base({"XDG_STATE_HOME": "/srv/state"}) == "/srv/state"


def test_backup_path_is_under_the_app_directory(base):
    assert backup.backup_path(base) == os.path.join(
        base, "icloud-bridge-gui", "exclusions-backup.json")


# ------------------------------------------------------------------ saving --

def test_first_save_writes_the_documented_shape(base):
    assert backup.save(["Docs/Big"], 7, backup.SOURCE_READ,
                       base=base, now=fixed_clock) == backup.SAVED
    assert saved_document(base) == {
        "version": 1,
        "savedAt": "2026-07-26T12:00:00Z",
        "source": "read",
        "revision": 7,
        "exclusions": ["Docs/Big"],
    }


def test_save_creates_the_directory_0700_and_the_file_0600(base):
    backup.save([], 0, backup.SOURCE_APPLY, base=base, now=fixed_clock)
    assert mode_of(backup.app_dir(base)) == 0o700
    assert mode_of(backup.backup_path(base)) == 0o600


def test_save_is_atomic_and_leaves_no_temp_file(base):
    backup.save(["Docs"], 1, backup.SOURCE_READ, base=base, now=fixed_clock)
    backup.save(["Docs", "Other"], 2, backup.SOURCE_READ, base=base, now=fixed_clock)
    leftovers = sorted(os.listdir(backup.app_dir(base)))
    assert leftovers == ["exclusions-backup.json"]


def test_an_empty_list_is_a_valid_backup(base):
    """"Include everything" is a real choice, not an absent one."""
    assert backup.save([], 4, backup.SOURCE_APPLY, base=base, now=fixed_clock) == backup.SAVED
    assert backup.load(base).exclusions == ()
    assert backup.load(base).revision == 4


def test_case_insensitive_duplicates_collapse_before_saving(base):
    backup.save(["Docs", "docs", "DOCS/Big"], 3, backup.SOURCE_READ,
                base=base, now=fixed_clock)
    assert backup.load(base).exclusions == ("Docs",)


def test_an_invalid_path_is_refused(base):
    with pytest.raises(backup.BackupError):
        backup.save(["../escape"], 1, backup.SOURCE_READ, base=base, now=fixed_clock)
    assert not os.path.exists(backup.backup_path(base))


@pytest.mark.parametrize("revision", [-1, "3", True, None, 1.5])
def test_a_bad_revision_is_refused(base, revision):
    with pytest.raises(backup.BackupError):
        backup.save(["Docs"], revision, backup.SOURCE_READ, base=base, now=fixed_clock)


def test_an_unknown_source_is_refused(base):
    with pytest.raises(backup.BackupError):
        backup.save(["Docs"], 1, "guess", base=base, now=fixed_clock)


# ------------------------------------------------- the replacement rules ----

def test_identical_content_and_revision_skips_the_write_but_tightens_the_mode(base):
    backup.save(["Docs"], 5, backup.SOURCE_READ, base=base, now=fixed_clock)
    path = backup.backup_path(base)
    os.chmod(path, 0o644)
    before = os.stat(path).st_mtime_ns

    assert backup.save(["Docs"], 5, backup.SOURCE_READ,
                       base=base, now=fixed_clock) == backup.UNCHANGED
    assert os.stat(path).st_mtime_ns == before      # steady state does not churn
    assert mode_of(path) == 0o600                   # but a loose mode is fixed


def test_a_read_at_a_higher_revision_replaces_the_backup(base):
    backup.save(["Docs"], 5, backup.SOURCE_READ, base=base, now=fixed_clock)
    assert backup.save(["Docs", "Other"], 6, backup.SOURCE_READ,
                       base=base, now=fixed_clock) == backup.SAVED
    assert backup.load(base).exclusions == ("Docs", "Other")


def test_a_lower_revision_read_cannot_destroy_the_saved_copy(base):
    """The rebuilt-VM case: a fresh revision-0 empty config must not win."""
    backup.save(["Docs/Big", "Photos"], 12, backup.SOURCE_READ, base=base, now=fixed_clock)
    assert backup.save([], 0, backup.SOURCE_READ,
                       base=base, now=fixed_clock) == backup.KEPT_NEWER
    kept = backup.load(base)
    assert kept.revision == 12
    assert kept.exclusions == ("Docs/Big", "Photos")


def test_the_same_revision_with_different_content_is_a_reported_conflict(base):
    backup.save(["Docs"], 5, backup.SOURCE_READ, base=base, now=fixed_clock)
    assert backup.save(["Other"], 5, backup.SOURCE_READ,
                       base=base, now=fixed_clock) == backup.CONFLICT
    assert backup.load(base).exclusions == ("Docs",)


def test_an_explicit_apply_may_replace_a_higher_revision_backup(base):
    """Apply's revision is newly incremented, so it is the operator's intent."""
    backup.save(["Docs/Big"], 12, backup.SOURCE_READ, base=base, now=fixed_clock)
    assert backup.save([], 13, backup.SOURCE_APPLY,
                       base=base, now=fixed_clock) == backup.SAVED
    assert backup.load(base).exclusions == ()
    assert backup.load(base).source == backup.SOURCE_APPLY


# ------------------------------------------------------ hostile filesystem --

def test_a_symlinked_app_directory_is_refused(tmp_path):
    base = tmp_path / "state"
    base.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (base / "icloud-bridge-gui").symlink_to(target)
    with pytest.raises(backup.BackupError):
        backup.save(["Docs"], 1, backup.SOURCE_READ, base=str(base), now=fixed_clock)


def test_a_non_directory_in_the_way_is_refused(tmp_path):
    base = tmp_path / "state"
    base.mkdir()
    (base / "icloud-bridge-gui").write_text("not a directory", encoding="utf-8")
    with pytest.raises(backup.BackupError):
        backup.save(["Docs"], 1, backup.SOURCE_READ, base=str(base), now=fixed_clock)


def test_a_symlinked_backup_file_is_not_followed(base, tmp_path):
    backup.ensure_app_dir(base)
    victim = tmp_path / "victim.json"
    victim.write_text("do not touch", encoding="utf-8")
    os.symlink(str(victim), backup.backup_path(base))
    with pytest.raises(backup.BackupError):
        backup.save(["Docs"], 1, backup.SOURCE_READ, base=base, now=fixed_clock)
    with pytest.raises(backup.BackupError):
        backup.load(base)
    assert victim.read_text(encoding="utf-8") == "do not touch"


def test_a_non_regular_destination_is_refused(base):
    directory = backup.ensure_app_dir(base)
    os.mkdir(os.path.join(directory, backup.BACKUP_NAME))
    with pytest.raises(backup.BackupError):
        backup.save(["Docs"], 1, backup.SOURCE_READ, base=base, now=fixed_clock)


# ----------------------------------------------------------------- loading --

def test_a_missing_backup_is_an_error_not_an_empty_list(base):
    with pytest.raises(backup.BackupError):
        backup.load(base)


@pytest.mark.parametrize("payload", [
    "not json",
    json.dumps([1, 2, 3]),
    json.dumps({"version": 2, "revision": 1, "exclusions": []}),
    json.dumps({"revision": 1, "exclusions": []}),
    json.dumps({"version": 1, "revision": -1, "exclusions": []}),
    json.dumps({"version": 1, "revision": "1", "exclusions": []}),
    json.dumps({"version": 1, "revision": True, "exclusions": []}),
    json.dumps({"version": 1, "revision": 1}),
    json.dumps({"version": 1, "revision": 1, "exclusions": "Docs"}),
    json.dumps({"version": 1, "revision": 1, "exclusions": [7]}),
    json.dumps({"version": 1, "revision": 1, "exclusions": ["../escape"]}),
    json.dumps({"version": 1, "revision": 1, "exclusions": ["/rooted"]}),
])
def test_a_malformed_backup_is_rejected(base, payload):
    backup.ensure_app_dir(base)
    with open(backup.backup_path(base), "w", encoding="utf-8") as handle:
        handle.write(payload)
    with pytest.raises(backup.BackupError):
        backup.load(base)


def test_an_oversized_backup_is_rejected(base, monkeypatch):
    backup.ensure_app_dir(base)
    with open(backup.backup_path(base), "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "revision": 1, "exclusions": ["x" * 500]}, handle)
    monkeypatch.setattr(backup, "MAX_BACKUP_BYTES", 32)
    with pytest.raises(backup.BackupError):
        backup.load(base)


def test_a_stale_but_valid_backup_loads(base):
    """Age is not a defect: stale content is exactly what a restore is for."""
    backup.save(["Docs/Big"], 2, backup.SOURCE_READ, base=base, now=fixed_clock)
    # Something else moves the world on; the backup is untouched and still good.
    loaded = backup.load(base)
    assert loaded.revision == 2
    assert loaded.exclusions == ("Docs/Big",)


def test_a_round_trip_through_the_document_preserves_everything(base):
    backup.save(["Docs/Big", "Photos"], 9, backup.SOURCE_APPLY, base=base, now=fixed_clock)
    loaded = backup.load(base)
    assert backup.parse(loaded.as_document()) == loaded


# ---------------------------------------------------------------- previews --

def test_preview_lists_additions_and_removals_case_insensitively():
    saved = backup.Backup(revision=4, exclusions=("Docs/Big", "Photos"))
    result = backup.preview(saved, ["photos", "Other"])
    assert result.additions == ("Docs/Big",)
    assert result.removals == ("Other",)
    assert result.changes_anything


def test_preview_of_an_identical_selection_changes_nothing():
    saved = backup.Backup(revision=4, exclusions=("Docs/Big",))
    result = backup.preview(saved, ["docs/big"])
    assert result.additions == ()
    assert result.removals == ()
    assert not result.changes_anything


def test_preview_of_an_empty_backup_removes_everything_currently_excluded():
    saved = backup.Backup(revision=4, exclusions=())
    result = backup.preview(saved, ["Docs"])
    assert result.additions == ()
    assert result.removals == ("Docs",)
