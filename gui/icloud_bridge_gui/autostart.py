"""Qt-free control of the XDG autostart entry (v2 plan D29).

The tray's checkable **Start when the computer starts** action toggles the
autostart `.desktop` file that ``install-gui.sh`` drops in
``~/.config/autostart/``.  Toggling flips ``Hidden=`` (and keeps
``X-GNOME-Autostart-enabled=`` in step) rather than deleting the file, so the
installer's absolute, install-time-expanded ``Exec`` path survives.  If the file
is missing it is recreated pointing at the installed launcher.

No Qt here so the read/toggle logic is unit-testable against a tmpdir home.
Paths key off ``home`` exactly as ``install-gui.sh`` does (``$HOME/.config`` and
``$HOME/.local``), not ``XDG_CONFIG_HOME``, so the two agree.
"""

from __future__ import annotations

import os
import tempfile

FILENAME = "icloud-bridge-tray.desktop"


def _home(home: str | None) -> str:
    return home if home is not None else os.path.expanduser("~")


def autostart_path(*, home: str | None = None) -> str:
    return os.path.join(_home(home), ".config", "autostart", FILENAME)


def launcher_path(*, home: str | None = None) -> str:
    return os.path.join(_home(home), ".local", "bin", "icloud-bridge-gui")


def _icon_path(home: str) -> str:
    return os.path.join(home, ".local", "share", "icloud-bridge-gui",
                        "icloud_bridge_gui", "icons", "icloud-green.svg")


def _value(text: str, key: str) -> str | None:
    """The last value for a case-sensitive `.desktop` key, or ``None``."""
    found: str | None = None
    prefix = key + "="
    for line in text.splitlines():
        if line.startswith(prefix):
            found = line[len(prefix):]
    return found


def is_enabled(*, home: str | None = None) -> bool:
    """True when autostart is active.

    Absent file, ``Hidden=true``, or ``X-GNOME-Autostart-enabled=false`` all mean
    disabled (unchecked); anything else is enabled.
    """
    path = autostart_path(home=home)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return False
    hidden = _value(text, "Hidden")
    if hidden is not None and hidden.strip().lower() == "true":
        return False
    gnome = _value(text, "X-GNOME-Autostart-enabled")
    if gnome is not None and gnome.strip().lower() == "false":
        return False
    return True


def _template(home: str, enabled: bool) -> str:
    hidden = "false" if enabled else "true"
    gnome = "true" if enabled else "false"
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=iCloud bridge (tray)\n"
        "Comment=Keeps the iCloud bridge tray icon running\n"
        f"Exec={launcher_path(home=home)} --minimized\n"
        f"Icon={_icon_path(home)}\n"
        "Terminal=false\n"
        "Categories=Utility;System;\n"
        "NoDisplay=true\n"
        f"Hidden={hidden}\n"
        f"X-GNOME-Autostart-enabled={gnome}\n"
    )


def _apply_flags(lines: list[str], enabled: bool) -> list[str]:
    """Rewrite only the two flag keys, preserving Exec/Icon and everything else."""
    hidden = "false" if enabled else "true"
    gnome = "true" if enabled else "false"
    out: list[str] = []
    inserted = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Hidden=") or stripped.startswith("X-GNOME-Autostart-enabled="):
            continue
        out.append(line)
        if not inserted and stripped == "[Desktop Entry]":
            out.append(f"Hidden={hidden}\n")
            out.append(f"X-GNOME-Autostart-enabled={gnome}\n")
            inserted = True
    if not inserted:
        out = ["[Desktop Entry]\n",
               f"Hidden={hidden}\n",
               f"X-GNOME-Autostart-enabled={gnome}\n"] + out
    return out


def _write_atomic(path: str, text: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory,
        prefix="." + FILENAME + ".", suffix=".tmp", delete=False)
    temp_name = handle.name
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except OSError:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def set_enabled(enabled: bool, *, home: str | None = None) -> None:
    """Enable or disable autostart, keeping (or recreating) the entry file."""
    resolved = _home(home)
    path = autostart_path(home=resolved)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        _write_atomic(path, _template(resolved, enabled))
        return
    _write_atomic(path, "".join(_apply_flags(lines, enabled)))
