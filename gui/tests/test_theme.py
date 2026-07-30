"""Pure checks for the palette-aware GUI theme."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import health, theme


def test_both_schemes_cover_all_theme_tokens():
    for scheme in (theme.LIGHT, theme.DARK):
        assert set(theme.SEVERITY_COLORS[scheme]) == {health.GREEN, health.YELLOW, health.RED}
        assert set(theme.PROVISION_COLORS[scheme]) == {
            "ready", "work", "wait", "blocked", "credential", "pending"}
        assert set(theme.BANNER_STYLES[scheme]) == {"starting", "shutdown", "off", "error"}
        assert set(theme.PROTOCOL_STYLES[scheme]) == {"skewed", "incompatible"}


def test_the_tray_covers_its_two_non_severity_states_in_both_schemes():
    for scheme in (theme.LIGHT, theme.DARK):
        assert set(theme.TRAY_COLORS[scheme]) == {"starting", "off"}
    # The boot disc must stay a distinct blue rather than drifting towards the
    # yellow fault colour (D29): more blue than red in both schemes.
    for scheme in (theme.LIGHT, theme.DARK):
        starting = theme.tray_color(scheme, "starting")
        red, blue = int(starting[1:3], 16), int(starting[5:7], 16)
        assert blue > red


def test_dark_severity_colours_are_lighter_than_light():
    for severity in (health.GREEN, health.YELLOW, health.RED):
        assert theme.lightness(theme.severity_color(theme.DARK, severity)) > \
            theme.lightness(theme.severity_color(theme.LIGHT, severity))


def test_muted_and_dim_are_strictly_between_window_and_text():
    for scheme in (theme.LIGHT, theme.DARK):
        low, high = sorted((theme.WINDOW_LIGHTNESS[scheme], theme.TEXT_LIGHTNESS[scheme]))
        for color in (theme.muted_color(scheme), theme.dim_color(scheme)):
            assert low < theme.lightness(color) < high


def test_scheme_for_uses_light_on_a_tie():
    assert theme.scheme_for(20, 21) == theme.DARK
    assert theme.scheme_for(20, 20) == theme.LIGHT
    assert theme.scheme_for(21, 20) == theme.LIGHT


def test_lightness_matches_known_hsl_values():
    assert theme.lightness("#000000") == 0
    assert theme.lightness("#ffffff") == 255
    assert theme.lightness("#ff0000") == 128
    assert theme.lightness("#808080") == 128


def test_styles_have_complete_css_fragments():
    for styles in (theme.BANNER_STYLES, theme.PROTOCOL_STYLES):
        for scheme_styles in styles.values():
            for style in scheme_styles.values():
                pairs = [part.strip() for part in style.split(";") if part.strip()]
                parsed = dict(part.split(": ", 1) for part in pairs)
                assert all(key and value for key, value in parsed.items())
                assert {"background", "color"} <= set(parsed)
