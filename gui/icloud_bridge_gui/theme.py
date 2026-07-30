"""Palette-aware GUI colours, kept Qt-free so the pure suite can test them."""

from __future__ import annotations

from . import health

LIGHT = "light"
DARK = "dark"

WINDOW_LIGHTNESS = {LIGHT: 246, DARK: 32}
TEXT_LIGHTNESS = {LIGHT: 46, DARK: 222}

SEVERITY_COLORS = {
    LIGHT: {health.GREEN: "#2e9e4f", health.YELLOW: "#d99b1a", health.RED: "#c8402c"},
    DARK: {health.GREEN: "#57d68d", health.YELLOW: "#f6c75e", health.RED: "#ff8c82"},
}
PROVISION_COLORS = {
    LIGHT: {"ready": "#2e9e4f", "work": "#d99b1a", "wait": "#1a5fb4",
            "blocked": "#c8402c", "credential": "#6a3fa0", "pending": "#8b8e91"},
    DARK: {"ready": "#57d68d", "work": "#f6c75e", "wait": "#78aeed",
           "blocked": "#ff8c82", "credential": "#c4a7e7", "pending": "#b6b8bb"},
}
BANNER_STYLES = {
    LIGHT: {"starting": "background: #e7f0fb; color: #1a3a63;",
            "shutdown": "background: #e7f0fb; color: #1a3a63;",
            "off": "background: #ececec; color: #3a3a3a;",
            "error": "background: #fbecea; color: #c8402c;"},
    DARK: {"starting": "background: #21354d; color: #d5e7ff;",
           "shutdown": "background: #21354d; color: #d5e7ff;",
           "off": "background: #303236; color: #d7d9dd;",
           "error": "background: #4a2829; color: #ffaaa2;"},
}
PROTOCOL_STYLES = {
    LIGHT: {"skewed": "background: #fdf5e2; color: #6b4e00;",
            "incompatible": "background: #fbecea; color: #c8402c;"},
    DARK: {"skewed": "background: #4b3a17; color: #ffe39a;",
           "incompatible": "background: #4a2829; color: #ffaaa2;"},
}
_LINK_COLORS = {LIGHT: "#1a5fb4", DARK: "#78aeed"}
_MUTED_COLORS = {LIGHT: "#697277", DARK: "#aeb2b5"}
_DIM_COLORS = {LIGHT: "#697277", DARK: "#aeb2b5"}
_INSTRUCTION_STYLES = {
    LIGHT: "background: #eef4fb; color: #1a5fb4;",
    DARK: "background: #21354d; color: #78aeed;",
}


def scheme_for(window_lightness: int, text_lightness: int) -> str:
    """Choose DARK when the window is darker than text; a tie is LIGHT."""
    return DARK if window_lightness < text_lightness else LIGHT


def severity_color(scheme: str, severity: str) -> str:
    return SEVERITY_COLORS[scheme][severity]


def provision_color(scheme: str, kind: str) -> str:
    return PROVISION_COLORS[scheme][kind]


def link_color(scheme: str) -> str:
    return _LINK_COLORS[scheme]


def muted_color(scheme: str) -> str:
    return _MUTED_COLORS[scheme]


def dim_color(scheme: str) -> str:
    return _DIM_COLORS[scheme]


def instruction_style(scheme: str) -> str:
    return _INSTRUCTION_STYLES[scheme]


def banner_style(scheme: str, kind: str) -> str:
    return BANNER_STYLES[scheme][kind]


def protocol_style(scheme: str, state: str) -> str:
    return PROTOCOL_STYLES[scheme][state]


def lightness(color: str) -> int:
    """Return QColor-compatible HSL lightness for a ``#rrggbb`` colour.

    ``QColor`` rounds the half-sum up, so this does too: the two numbers are
    compared against each other in :func:`scheme_for`, and an off-by-one there
    would decide the scheme for a palette that sits exactly on the boundary.
    """
    red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    return (max(red, green, blue) + min(red, green, blue) + 1) // 2
