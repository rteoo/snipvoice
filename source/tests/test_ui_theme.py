"""Per-OS palette/font resolution for the manager GUI.

The load-bearing guarantee is the first test class: on Windows every token must
resolve to the Teô Design System 2.7.1 Windows family, while the macOS safety
seam remains platform-native.
"""

import ast
from datetime import datetime
import os
import re
import sys
import types
import tkinter as tk
from tkinter import font as tkfont
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import ui_theme


# DS 2.7.1 Windows-family tokens are copied here to catch accidental drift.
WINDOWS_LIGHT_COLORS = {
    "surface": "#f6f5f4",
    "surface_alt": "#e9e8e7",
    "surface_alt_active": "#e8e6e3",
    "surface_hover": "#e8e6e3",
    "card": "#ffffff",
    "field": "#ffffff",
    "field_hover": "#e8e6e3",
    "control": "#ffffff",
    "control_active": "#e8e6e3",
    "control_border": "#7b8088",
    "field_border": "#7b8088",
    "text": "#202630",
    "text_strong": "#202630",
    "text_muted": "#606060",
    "text_on_accent": "#15100A",
    "disabled_fg": "#9B9DA2",
    "border": "#d8d8d8",
    "divider": "#d8d8d8",
    "accent": "#E6A05A",
    "accent_active": "#BE7836",
    "accent_mark": "#A0520A",
    "danger": "#9B2C2C",
    "danger_active": "#9B2C2C",
    "focus_ring": "#263C56",
    "link": "#A0520A",
    "warning": "#7A4A00",
    "success": "#16643B",
    "info": "#263C56",
    "danger_bg": "#FCE8E8",
    "warning_bg": "#FFF1D6",
    "success_bg": "#E6F6EC",
    "info_bg": "#E8EEF5",
    "chart_primary": "#A0520A",
    "chart_secondary": "#263C56",
    "chart_neutral": "#7B8492",
    "select_bg": "#e8e6e3",
    "select_fg": "#202630",
    "text_native": "#202630",
    "tab_unselected_fg": "#606060",
}

WINDOWS_DARK_COLORS = {
    "surface": "#232323",
    "surface_alt": "#292929",
    "surface_alt_active": "#484848",
    "surface_hover": "#484848",
    "card": "#1c1c1e",
    "field": "#1c1c1e",
    "field_hover": "#484848",
    "control": "#1c1c1e",
    "control_active": "#484848",
    "control_border": "#999ca3",
    "field_border": "#999ca3",
    "text": "#F2EFE9",
    "text_strong": "#F2EFE9",
    "text_muted": "#bcbcbc",
    "text_on_accent": "#15100A",
    "disabled_fg": "#7C7B79",
    "border": "#454545",
    "divider": "#454545",
    "accent": "#E6A05A",
    "accent_active": "#CE8845",
    "accent_mark": "#E6A05A",
    "danger": "#FF8A85",
    "danger_active": "#FF8A85",
    "focus_ring": "#F0C293",
    "link": "#EDB47C",
    "warning": "#FFD27A",
    "success": "#73E0A1",
    "info": "#ADC1DC",
    "danger_bg": "#351718",
    "warning_bg": "#342A17",
    "success_bg": "#13271D",
    "info_bg": "#202E40",
    "chart_primary": "#E6A05A",
    "chart_secondary": "#8CA8CF",
    "chart_neutral": "#788697",
    "select_bg": "#484848",
    "select_fg": "#F2EFE9",
    "text_native": "#F2EFE9",
    "tab_unselected_fg": "#bcbcbc",
}

OVERLAY_COLORS = {
    "overlay_bg": "#1c1c1e",
    "overlay_border": "#454545",
    "overlay_text": "#F2EFE9",
    "overlay_muted": "#bcbcbc",
    "overlay_live": "#E6A05A",
    "overlay_busy": "#8CA8CF",
    "overlay_ok": "#73E0A1",
    "overlay_trouble": "#FF8A85",
}

VARIABLE_FACES = frozenset({
    "Segoe UI", "Segoe UI Semibold", "Segoe UI Variable Text",
    "Segoe UI Variable Text Semibold", "Segoe UI Variable Display",
    "Segoe UI Variable Display Semib", "Cascadia Mono", "Consolas",
})


def luminance(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class WindowsPaletteTests(unittest.TestCase):
    """Windows must keep the DS 2.7.1 Windows-family palette stable."""

    def test_every_light_token_keeps_its_declared_literal(self):
        colors = ui_theme.palette("windows")
        for token, expected in {**WINDOWS_LIGHT_COLORS, **OVERLAY_COLORS}.items():
            self.assertEqual(colors[token], expected, token)

    def test_windows_dark_palette_keeps_its_declared_literal(self):
        colors = ui_theme.palette("dark", system="windows")
        for token, expected in {**WINDOWS_DARK_COLORS, **OVERLAY_COLORS}.items():
            self.assertEqual(colors[token], expected, token)
        self.assertEqual(colors["text_native"], colors["text"])

    def test_the_hud_is_dark_in_both_appearances(self):
        for kind, system in (("windows", "windows"), ("dark", "windows"), ("light", "darwin"),
                             ("dark", "darwin")):
            colors = ui_theme.palette(kind, system=system)
            for token, expected in OVERLAY_COLORS.items():
                self.assertEqual(colors[token], expected, (kind, system, token))

    def test_disabled_text_is_text_blended_45_percent_over_the_control(self):
        # Tk has no opacity, so the DS's 0.45 disabled opacity is a fixed blend.
        for kind in ("windows", "dark"):
            colors = ui_theme.palette(kind, system="windows")
            text, control = ui_theme.rgb(colors["text"]), ui_theme.rgb(colors["control"])
            blended = [round((0.45 * t + 0.55 * c) * 255) for t, c in zip(text, control)]
            actual = [round(channel * 255) for channel in ui_theme.rgb(colors["disabled_fg"])]
            for expected, value in zip(blended, actual):
                self.assertLessEqual(abs(expected - value), 1, (kind, blended, actual))

    def test_windows_prefers_clam_in_both_appearances(self):
        # Vista paints its own faces, selection and meters over the palette.
        self.assertEqual(ui_theme.ttk_theme_preference("windows"), ("clam", "default"))
        self.assertEqual(ui_theme.ttk_theme_preference("darwin")[0], "aqua")


class ContrastTests(unittest.TestCase):
    """WCAG 2.2 AA over both Windows-family palettes: 4.5:1 text, 3:1 marks."""

    def _themes(self):
        return [ui_theme.palette(kind, system="windows") for kind in ("windows", "dark")]

    def test_text_and_muted_text_read_on_every_surface(self):
        for colors in self._themes():
            for surface in ("surface_alt", "surface", "card", "select_bg", "control_active"):
                for role in ("text", "text_muted", "link"):
                    self.assertGreaterEqual(
                        contrast(colors[role], colors[surface]), 4.5, (role, surface, colors[surface]))

    def test_semantic_text_reads_on_its_tone_and_on_the_content_surfaces(self):
        for colors in self._themes():
            for tone in ("danger", "warning", "success", "info"):
                for surface in (f"{tone}_bg", "card", "surface"):
                    self.assertGreaterEqual(
                        contrast(colors[tone], colors[surface]), 4.5, (tone, surface))

    def test_control_boundaries_focus_and_marks_hold_3_to_1(self):
        for colors in self._themes():
            for role in ("field_border", "control_border", "focus_ring", "accent_mark",
                         "chart_primary", "chart_secondary", "chart_neutral"):
                for surface in ("surface_alt", "surface", "card"):
                    self.assertGreaterEqual(
                        contrast(colors[role], colors[surface]), 3.0, (role, surface))
            # The nav marker and focus ring also sit on the selection plane.
            for role in ("accent_mark", "focus_ring"):
                self.assertGreaterEqual(contrast(colors[role], colors["select_bg"]), 3.0, role)

    def test_list_state_words_read_on_rows_and_selected_rows_use_the_selection_text(self):
        # A toned row draws its tag color on the content plane; a selected row
        # draws select_fg (the style map wins over tags on Tk 8.6 and 9), which
        # matters for dark danger: 4.02:1 on the selection plane.
        for colors in self._themes():
            for role in ("danger", "warning", "info", "text_muted"):
                self.assertGreaterEqual(contrast(colors[role], colors["card"]), 4.5, role)
            self.assertGreaterEqual(contrast(colors["select_fg"], colors["select_bg"]), 4.5)

    def test_info_bar_text_and_edges_read_on_their_tone(self):
        for colors in self._themes():
            for tone in ui_theme.INFO_BAR_TONES:
                fill = colors[f"{tone}_bg"]
                for role in ("text", "link"):
                    self.assertGreaterEqual(contrast(colors[role], fill), 4.5, (tone, role))
                # The tone fill is close to the page, so the tone edge draws the bar.
                for surface in ("surface", "card"):
                    self.assertGreaterEqual(contrast(colors[tone], colors[surface]), 3.0, (tone, surface))
                self.assertGreaterEqual(contrast(colors["control_border"], fill), 3.0, tone)

    def test_primary_label_reads_on_copper(self):
        for colors in self._themes():
            for fill in ("accent", "accent_active"):
                self.assertGreaterEqual(contrast(colors["text_on_accent"], colors[fill]), 4.5, fill)

    def test_a_pressed_primary_darkens(self):
        # Win32 shows a button's active colors only while it is pressed, so
        # accent_active is the DS pressed role, never the lighter hover.
        for colors in self._themes():
            self.assertLess(luminance(colors["accent_active"]), luminance(colors["accent"]))

    def test_hud_text_and_tones_read_on_the_fixed_dark_surface(self):
        for role in ("overlay_text", "overlay_muted"):
            self.assertGreaterEqual(contrast(OVERLAY_COLORS[role], OVERLAY_COLORS["overlay_bg"]), 4.5)
        for tone in ("live", "busy", "ok", "trouble", "muted"):
            self.assertGreaterEqual(
                contrast(OVERLAY_COLORS[f"overlay_{tone}"], OVERLAY_COLORS["overlay_bg"]), 3.0, tone)


class WindowsFontTests(unittest.TestCase):

    def test_legacy_sizes_map_onto_the_pixel_type_ramp(self):
        theme = ui_theme.build_theme("windows", system="windows")
        self.assertEqual(theme.font(8), ("Segoe UI", -12))
        self.assertEqual(theme.font(9), ("Segoe UI", -14))
        self.assertEqual(theme.font(10), ("Segoe UI", -14))
        self.assertEqual(theme.font(), theme.body())
        self.assertEqual(theme.font(9, "bold"), ("Segoe UI Semibold", -14))
        # Headings and titles are semibold whether or not the caller asked.
        self.assertEqual(theme.font(11), ("Segoe UI Semibold", -18))
        self.assertEqual(theme.font(12, "bold"), ("Segoe UI Semibold", -18))
        self.assertEqual(theme.font(14, "bold"), ("Segoe UI Semibold", -28))

    def test_roles_name_the_ramp(self):
        theme = ui_theme.build_theme("dark", system="windows")
        self.assertEqual(theme.caption(), theme.font(8))
        self.assertEqual(theme.body(), theme.font(9))
        self.assertEqual(theme.body_strong(), theme.font(9, "bold"))
        self.assertEqual(theme.heading(), ("Segoe UI Semibold", -18))
        self.assertEqual(theme.title(), ("Segoe UI Semibold", -28))
        self.assertEqual(theme.mono(), ("Consolas", -14))

    def test_an_unmapped_windows_size_fails_loudly(self):
        theme = ui_theme.build_theme("windows", system="windows")
        with self.assertRaises(ValueError):
            theme.font(13)

    def test_segoe_ui_variable_uses_its_optical_faces_when_tk_lists_them(self):
        theme = ui_theme.build_theme("dark", system="windows", families=VARIABLE_FACES)
        self.assertEqual(theme.caption(), ("Segoe UI Variable Text", -12))
        self.assertEqual(theme.body(), ("Segoe UI Variable Text", -14))
        self.assertEqual(theme.body_strong(), ("Segoe UI Variable Text Semibold", -14))
        # GDI enumerates the Display semibold face under a 31-character name.
        self.assertEqual(theme.heading(), ("Segoe UI Variable Display Semib", -18))
        self.assertEqual(theme.title(), ("Segoe UI Variable Display Semib", -28))
        self.assertEqual(theme.mono(), ("Cascadia Mono", -14))
        # The bare family name is not a GDI face; Tk would substitute Arial.
        faces = ui_theme.font_faces("windows", VARIABLE_FACES)
        self.assertNotIn("Segoe UI Variable", faces.values())

    def test_a_host_without_semibold_faces_synthesizes_bold(self):
        theme = ui_theme.build_theme("dark", system="windows", families={"Segoe UI"})
        self.assertEqual(theme.body_strong(), ("Segoe UI", -14, "bold"))
        self.assertEqual(theme.title(), ("Segoe UI", -28, "bold"))
        self.assertEqual(theme.mono(), ("Consolas", -14))

    def test_pixel_sizes_follow_tk_scaling(self):
        # 144 DPI: Tk reports 2.0 pixels per point, so the ramp scales by 1.5.
        theme = ui_theme.build_theme("dark", system="windows", px_scale=1.5)
        self.assertEqual(theme.body(), ("Segoe UI", -21))
        self.assertEqual(theme.title(), ("Segoe UI Semibold", -42))

    def test_windows_ignores_a_system_default_size(self):
        theme = ui_theme.build_theme("windows", system="windows", default_size=13)
        self.assertEqual(theme.size_delta, 0)
        self.assertEqual(theme.font(9), ("Segoe UI", -14))

    def test_emoji_and_symbol_faces_are_unchanged(self):
        theme = ui_theme.build_theme("windows", system="windows")
        self.assertEqual(theme.emoji_font(12), ("Segoe UI Emoji", 12))
        self.assertEqual(theme.mono_font(10, "bold"), ("Consolas", -14, "bold"))
        self.assertEqual(theme.symbol_family, "Segoe UI Symbol")


class LinuxPaletteTests(unittest.TestCase):
    def test_windows_palette_uses_literal_native_colors_on_linux(self):
        # ``SystemButtonText`` is a Win32 Tk color name. Linux may still use
        # the Windows appearance preference, but X11 Tk needs the literal
        # fallback for widgets that receive an explicit foreground.
        theme = ui_theme.build_theme("windows", system="linux")
        self.assertEqual(theme.text_native, theme.text)
        for options in (
            theme.checkbutton_colors(theme.surface),
            theme.nav_button_colors(theme.surface),
            theme.glyph_button_colors(theme.card),
            theme.button_colors(),
        ):
            self.assertNotIn("SystemButtonText", options.values())


class MacPaletteTests(unittest.TestCase):

    def test_backgrounds_and_text_use_aqua_dynamic_colors(self):
        # Only these follow a live appearance switch; hardcoding them is the
        # bug this module exists to fix.
        for kind in ("light", "dark"):
            colors = ui_theme.palette(kind)
            self.assertEqual(colors["surface"], "systemWindowBackgroundColor")
            self.assertEqual(colors["card"], "systemTextBackgroundColor")
            self.assertEqual(colors["field"], "systemTextBackgroundColor")
            self.assertEqual(colors["text"], "systemTextColor")
            self.assertEqual(colors["text_strong"], "systemTextColor")

    def test_ink_and_copper_maps_only_accent_link_and_focus(self):
        light, dark = ui_theme.palette("light"), ui_theme.palette("dark")
        self.assertEqual((light["accent"], light["link"], light["focus_ring"]),
                         ("#E6A05A", "#A0520A", "#263C56"))
        self.assertEqual((dark["accent"], dark["link"], dark["focus_ring"]),
                         ("#E6A05A", "#EDB47C", "#F0C293"))
        # Aqua's own grays stay in dark system mode.
        self.assertEqual(dark["surface_alt"], "#2C2C2E")
        self.assertEqual(dark["text_muted"], "#98989D")

    def test_alpha_carrying_system_colors_are_never_used(self):
        # Tk drops the alpha channel and hands back pure white, which is
        # invisible on the dark surface -- these must stay fixed grays.
        broken = {
            "systemSecondaryLabelColor",
            "systemSeparatorColor",
            "systemPlaceholderTextColor",
            "systemDisabledControlTextColor",
        }
        for kind in ("light", "dark"):
            for token, value in ui_theme.palette(kind).items():
                self.assertNotIn(value, broken, f"{kind}/{token}")

    def test_dark_mode_replaces_the_light_greys_and_dim_accents(self):
        light = ui_theme.palette("light")
        dark = ui_theme.palette("dark")
        for token in ("surface_alt", "border", "divider", "text_muted",
                      "link", "warning", "success"):
            self.assertNotEqual(dark[token], light[token], token)

    def test_mac_fonts_use_the_system_families(self):
        theme = ui_theme.build_theme("dark", system="darwin")
        self.assertEqual(theme.family, ".AppleSystemUIFont")
        self.assertEqual(theme.emoji_family, "Apple Color Emoji")
        self.assertEqual(theme.mono_family, "Menlo")
        self.assertNotIn("Segoe", theme.symbol_family)

    def test_mac_sizes_shift_onto_the_platform_scale(self):
        # A 9 pt label is body text; it must land on the platform's own
        # default size rather than two points under every native control.
        theme = ui_theme.build_theme("light", system="darwin", default_size=13)
        self.assertEqual(theme.size_delta, 4)
        self.assertEqual(theme.font(9), (".AppleSystemUIFont", 13))
        self.assertEqual(theme.font(12, "bold"), (".AppleSystemUIFont", 16, "bold"))
        self.assertEqual(theme.body_strong(), (".AppleSystemUIFont", 13, "bold"))

    def test_an_unreadable_default_size_leaves_the_scale_alone(self):
        for probe in (None, 0):
            theme = ui_theme.build_theme("light", system="darwin", default_size=probe)
            self.assertEqual(theme.size_delta, 0)


class AppearanceDetectionTests(unittest.TestCase):

    def test_probe_failure_falls_back_to_light(self):
        class Broken:
            def winfo_rgb(self, _name):
                raise RuntimeError("no such color")

        self.assertEqual(ui_theme._probe_kind(Broken(), "darwin"), "light")

    def test_probe_reads_the_window_background(self):
        class Fake:
            def __init__(self, rgb):
                self.rgb = rgb
                self.asked = None

            def winfo_rgb(self, name):
                self.asked = name
                return self.rgb

        dark = Fake((7710, 7710, 7710))
        self.assertEqual(ui_theme._probe_kind(dark, "darwin"), "dark")
        self.assertEqual(dark.asked, "systemWindowBackgroundColor")
        self.assertEqual(
            ui_theme._probe_kind(Fake((60652, 60652, 60652)), "darwin"), "light"
        )

    def test_windows_never_probes(self):
        class Exploding:
            def winfo_rgb(self, _name):
                raise AssertionError("Windows must not query Aqua colors")

        with mock.patch("ui_theme._windows_apps_use_light_theme", return_value=True):
            self.assertEqual(ui_theme._probe_kind(Exploding(), "windows"), "windows")

    def test_windows_system_dark_is_detected_without_querying_tk(self):
        class Exploding:
            def winfo_rgb(self, _name):
                raise AssertionError("Windows must not query Aqua colors")

        with mock.patch("ui_theme._windows_apps_use_light_theme", return_value=False):
            self.assertEqual(ui_theme._probe_kind(Exploding(), "windows"), "dark")

    def test_unset_or_invalid_preference_defaults_to_dark(self):
        for value in (None, "", "sepia", 1):
            self.assertEqual(ui_theme.normalize_preference(value), "dark")

    def test_an_explicit_stored_preference_is_kept(self):
        for value in ("system", "light", "dark"):
            self.assertEqual(ui_theme.normalize_preference(value), value)


class ThemeCacheTests(unittest.TestCase):

    def setUp(self):
        ui_theme.reset()
        self.addCleanup(ui_theme.reset)

    def test_theme_resolves_without_a_widget(self):
        theme = ui_theme.theme()
        self.assertEqual(theme.system, ui_theme.current_os())
        self.assertIn(theme.kind, ("windows", "light", "dark"))

    def test_bind_replaces_the_cached_theme(self):
        first = ui_theme.bind(None, system="windows")
        self.assertIs(ui_theme.theme(), first)
        second = ui_theme.bind(None, system="darwin")
        self.assertIsNot(second, first)
        self.assertIs(ui_theme.theme(), second)

    def test_widgetless_bind_never_claims_dark(self):
        # Guessing dark and being wrong is the unreadable case; light is the
        # historical behavior.
        self.assertEqual(ui_theme.bind(None, system="darwin").kind, "light")

    def test_explicit_windows_dark_preference_wins_over_system(self):
        with mock.patch("ui_theme._windows_apps_use_light_theme", return_value=True):
            theme = ui_theme.bind(None, system="windows", preference="dark")
        self.assertEqual(theme.kind, "dark")
        self.assertEqual(theme.preference, "dark")

    def test_explicit_mac_dark_uses_fixed_surfaces_instead_of_aqua_system_colors(self):
        theme = ui_theme.bind(None, system="darwin", preference="dark")
        self.assertEqual(theme.kind, "dark")
        self.assertEqual(theme.preference, "dark")
        self.assertEqual(theme.surface, WINDOWS_DARK_COLORS["surface"])
        self.assertEqual(theme.card, WINDOWS_DARK_COLORS["card"])
        self.assertNotEqual(theme.surface, "systemWindowBackgroundColor")


class WidgetOptionTests(unittest.TestCase):
    def test_prepare_dialog_has_no_modal_or_focus_side_effects(self):
        owner, window, icon = mock.Mock(), mock.Mock(), mock.Mock()
        owner.winfo_toplevel.return_value = owner
        window.winfo_ismapped.return_value = True
        ui = ui_theme.build_theme("dark", system="windows")
        with mock.patch.object(ui_theme, "apply_window_chrome") as chrome:
            self.assertIs(ui_theme.prepare_dialog(window, owner, resolved=ui, set_icon=icon), window)
        chrome.assert_called_once_with(window, ui)
        icon.assert_called_once_with(window)
        window.transient.assert_called_once_with(owner)
        for name in ("grab_set", "wait_window", "wait_visibility", "focus_set", "focus_force", "withdraw", "deiconify"):
            getattr(window, name).assert_not_called()

    def test_unmapped_windows_dialog_applies_chrome_once_on_own_map_and_keeps_other_bindings(self):
        class Registry:
            def __init__(self):
                self.commands, self.bindings = {}, {}
            def createcommand(self, name, callback):
                self.commands[name] = callback
            def deletecommand(self, name):
                del self.commands[name]
            def call(self, *args):
                if len(args) == 1 and isinstance(args[0], tuple):
                    args = args[0]
                if args[0] != "bind":
                    raise AssertionError(args)
                key = args[1:3]
                if len(args) == 3:
                    return self.bindings.get(key, "")
                script = args[3]
                self.bindings[key] = self.bindings.get(key, "") + script[1:] if script.startswith("+") else script
                return ""
        class Window(tk.Misc):
            def __init__(self):
                self.tk, self._tclCommands, self.master, self._w = Registry(), [], None, ".dialog"
            def configure(self, **kwargs):
                pass
            def transient(self, parent):
                pass
            def winfo_ismapped(self):
                return False
        for destroy_before_map in (False, True):
            with self.subTest(destroy_before_map=destroy_before_map):
                window, owner = Window(), mock.Mock()
                owner.winfo_toplevel.return_value = owner
                other = window.bind("<Map>", lambda _event: None, add="+")
                old_script = window.tk.bindings[(window._w, "<Map>")]
                ui = ui_theme.build_theme("dark", system="windows")
                with mock.patch.object(ui_theme, "apply_window_chrome") as chrome:
                    ui_theme.prepare_dialog(window, owner, resolved=ui)
                    chrome.assert_not_called()
                    self.assertIn(other, window.tk.commands)
                    own = next(name for name in window.tk.commands if name != other)
                    if not destroy_before_map:
                        callback = window.tk.commands[own]
                        callback(types.SimpleNamespace(widget=object()))
                        chrome.assert_not_called()
                        callback(types.SimpleNamespace(widget=window))
                        callback(types.SimpleNamespace(widget=window))
                        chrome.assert_called_once_with(window, ui)
                        self.assertEqual(window.tk.bindings[(window._w, "<Map>")], old_script)
                        self.assertIn(other, window.tk.commands)
                        self.assertNotIn(own, window.tk.commands)
                    tk.Misc.destroy(window)
                    self.assertEqual(window.tk.commands, {})
                    self.assertIsNone(window._tclCommands)
                    if destroy_before_map:
                        chrome.assert_not_called()

    def test_nonwindows_unmapped_dialog_keeps_immediate_inert_chrome_behavior(self):
        window, owner = mock.Mock(), mock.Mock()
        ui = ui_theme.build_theme("dark", system="linux")
        with mock.patch.object(ui_theme, "apply_window_chrome") as chrome:
            ui_theme.prepare_dialog(window, owner, resolved=ui)
        chrome.assert_called_once_with(window, ui)
        window.bind.assert_not_called()

    def test_elision_fits_the_actual_font_width_and_keeps_short_names(self):
        font = mock.Mock()
        font.measure.side_effect = lambda value: len(value) * 9
        self.assertEqual(ui_theme.ellipsize("USB", font, 100), "USB")
        fitted = ui_theme.ellipsize("Synthetic long device name", font, 90)
        self.assertTrue(fitted.endswith("…"))
        self.assertLessEqual(font.measure(fitted), 90)

    def test_system_reprobe_does_not_change_a_fixed_preference_or_cached_theme(self):
        ui = ui_theme.build_theme("dark", system="windows", preference="dark")
        before = ui_theme._current
        with mock.patch.object(ui_theme, "_probe_kind", return_value="windows") as probe:
            self.assertFalse(ui_theme.system_appearance_changed(mock.Mock(), ui))
            probe.assert_not_called()
            ui.preference = "system"
            self.assertTrue(ui_theme.system_appearance_changed(mock.Mock(), ui))
        self.assertIs(ui_theme._current, before)


    def test_widget_helpers_are_inert_off_macos(self):
        # Win32's own defaults for these widgets are already correct in the
        # light appearance, including the system text selection color.
        for system in ("windows", "linux"):
            theme = ui_theme.build_theme("windows", system=system)
            self.assertEqual(theme.entry_colors(), {})
            self.assertEqual(theme.text_colors(), {})
            self.assertEqual(theme.listbox_colors(), {})
            self.assertEqual(theme.checkbutton_colors("#FFFFFF")["bg"], "#FFFFFF")
            self.assertEqual(theme.button_colors()["bg"], theme.control)

    def test_disabled_checkboxes_dim_to_the_disabled_text(self):
        theme = ui_theme.build_theme("light", system="windows")
        self.assertEqual(
            theme.checkbutton_colors(theme.card)["disabledforeground"], theme.disabled_fg)

    def test_added_foregrounds_resolve_to_each_platform_default(self):
        # `text_native` is for widgets the pre-change GUI left uncolored, so it
        # has to *be* the platform default rather than the app's near-black.
        self.assertEqual(
            ui_theme.build_theme("windows", system="windows").text_native,
            "SystemButtonText",
        )
        self.assertEqual(
            ui_theme.build_theme("dark", system="darwin").text_native,
            "systemTextColor",
        )
        # X11 has neither name.
        self.assertEqual(
            ui_theme.build_theme("windows", system="linux").text_native, "#202630"
        )

    def test_windows_keeps_its_button_widths_and_window_size(self):
        theme = ui_theme.build_theme("windows", system="windows")
        self.assertEqual(theme.button_width(12), 12)
        self.assertEqual(theme.manager_window_size, ("1120x820", 1040, 700))
        self.assertFalse(theme.stacked_toolbar_status)

    def test_macos_sizes_buttons_to_their_text_and_widens_the_window(self):
        # Aqua's bezel has a minimum width the flat Win32 button does not, so
        # the tuned character widths overflow their pane and clip the last
        # button in the row.
        theme = ui_theme.build_theme("dark", system="darwin")
        self.assertEqual(theme.button_width(12), 0)
        geometry, min_width, _ = theme.manager_window_size
        self.assertEqual(geometry, "1160x840")
        self.assertGreater(min_width, 820)
        self.assertTrue(theme.stacked_toolbar_status)

    def test_macos_never_paints_a_natively_drawn_control(self):
        # Aqua ignores -background on buttons and checkboxes but honours
        # -foreground, so any color the app supplies can only turn the title
        # invisible against the bezel Aqua draws anyway (macOS 15 / Tk 9.0).
        theme = ui_theme.build_theme("dark", system="darwin")
        for variant in ui_theme.BUTTON_VARIANTS:
            self.assertEqual(theme.button_colors(variant), {})
        self.assertEqual(theme.checkbutton_colors("#222"), {})
        self.assertEqual(theme.glyph_button_colors("#222"), {})
        self.assertEqual(theme.button_chrome(), {})

    def test_entry_colors_pin_every_channel_aqua_would_theme(self):
        theme = ui_theme.build_theme("dark", system="darwin")
        colors = theme.entry_colors()
        for option in ("bg", "fg", "insertbackground", "selectbackground",
                       "selectforeground"):
            self.assertIn(option, colors)
        self.assertEqual(colors["bg"], theme.field)
        self.assertEqual(colors["fg"], theme.text)

    def test_windows_dark_paints_entries_lists_and_checkbox_selection(self):
        theme = ui_theme.build_theme("dark", system="windows")
        self.assertEqual(theme.entry_colors()["bg"], theme.field)
        self.assertEqual(theme.entry_colors()["disabledforeground"], theme.disabled_fg)
        self.assertEqual(theme.listbox_colors()["fg"], theme.text)
        # Win32 paints selectcolor behind the indicator in both states; an
        # accent fill made unchecked boxes look checked.
        self.assertEqual(theme.checkbutton_colors(theme.card)["selectcolor"], theme.field)

    def test_nav_selection_is_a_neutral_plane_with_primary_text(self):
        theme = ui_theme.build_theme("dark", system="windows")
        selected = theme.nav_button_colors(theme.surface_alt, selected=True)
        idle = theme.nav_button_colors(theme.surface_alt)
        self.assertEqual(selected["bg"], theme.select_bg)
        self.assertEqual(selected["highlightbackground"], theme.select_bg)
        self.assertEqual(selected["fg"], theme.text_native)
        self.assertNotIn(theme.accent, selected.values())
        self.assertEqual(idle["bg"], theme.surface_alt)
        self.assertEqual(idle["fg"], theme.text_native)
        self.assertEqual(ui_theme.build_theme("dark", system="darwin").nav_button_colors("#222"), {})

    def test_dark_readonly_entries_and_lists_avoid_light_native_faces(self):
        theme = ui_theme.build_theme("dark", system="windows")
        self.assertEqual(theme.entry_colors()["readonlybackground"], theme.surface_alt)
        self.assertNotIn("readonlybackground", theme.text_colors())

    def test_list_rings_come_from_the_chrome_off_macos_and_stay_on_macos(self):
        # Off macOS entry_chrome owns the ring; both splat into one call.
        for kind in ("windows", "dark"):
            theme = ui_theme.build_theme(kind, system="windows")
            self.assertEqual(set(theme.listbox_colors()) & set(theme.entry_chrome()), set())
            self.assertEqual(set(theme.text_colors()) & set(theme.entry_chrome()), set())
        mac = ui_theme.build_theme("dark", system="darwin")
        self.assertEqual(mac.listbox_colors()["highlightbackground"], mac.field_border)
        self.assertEqual(mac.listbox_colors()["highlightcolor"], mac.focus_ring)

    def test_entry_chrome_draws_a_visible_compact_field_off_macos(self):
        for kind in ("windows", "dark"):
            theme = ui_theme.build_theme(kind, system="windows")
            chrome = theme.entry_chrome()
            self.assertEqual(chrome["relief"], "flat")
            self.assertEqual(chrome["highlightthickness"], 1)
            self.assertEqual(chrome["bd"], 5)
            self.assertEqual(chrome["highlightbackground"], theme.field_border)
            self.assertEqual(chrome["highlightcolor"], theme.focus_ring)
            # A read-only pane is content: the decorative line, same geometry.
            pane = theme.entry_chrome(readonly=True)
            self.assertEqual(pane, {**chrome, "highlightbackground": theme.border})
        self.assertEqual(ui_theme.build_theme("dark", system="darwin").entry_chrome(), {})
        self.assertEqual(ui_theme.build_theme("dark", system="darwin").entry_chrome(readonly=True), {})

    def test_text_colors_drop_the_options_tk_text_rejects(self):
        colors = ui_theme.build_theme("dark", system="darwin").text_colors()
        self.assertNotIn("disabledbackground", colors)
        self.assertNotIn("disabledforeground", colors)
        self.assertIn("inactiveselectbackground", colors)

    def test_button_colors_cover_every_state_option(self):
        expected = {"bg", "fg", "activebackground", "activeforeground",
                    "disabledforeground", "highlightbackground"}
        for kind in ("windows", "light", "dark"):
            theme = ui_theme.build_theme(kind, system="windows")
            for variant in ui_theme.BUTTON_VARIANTS:
                for disabled in (False, True):
                    colors = theme.button_colors(variant, surface=theme.card, disabled=disabled)
                    self.assertEqual(set(colors), expected, (kind, variant, disabled))
                    # Colors and chrome combine into one call (the Menubutton).
                    self.assertEqual(set(colors) & set(theme.button_chrome()), set())
            self.assertEqual(theme.button_chrome()["highlightcolor"], theme.focus_ring)

    def test_neutral_buttons_keep_a_visible_boundary_on_cards_and_the_page(self):
        # A neutral button shares the content fill, so its edge carries it.
        for kind in ("windows", "dark"):
            theme = ui_theme.build_theme(kind, system="windows")
            ring = theme.button_colors()["highlightbackground"]
            for backdrop in (theme.card, theme.surface, theme.surface_alt):
                self.assertGreaterEqual(contrast(ring, backdrop), 3.0, (kind, backdrop))

    def test_filled_and_quiet_buttons_blend_their_idle_ring_into_the_surface(self):
        theme = ui_theme.build_theme("dark", system="windows")
        for variant in ("primary", "quiet"):
            colors = theme.button_colors(variant, surface=theme.card)
            self.assertEqual(colors["highlightbackground"], theme.card, variant)
        self.assertEqual(theme.button_colors("quiet", surface=theme.card)["bg"], theme.card)
        self.assertEqual(theme.button_colors("quiet", surface=theme.card)["fg"], theme.link)

    def test_primary_button_uses_copper_with_its_dark_label(self):
        for kind, pressed in (("dark", "#CE8845"), ("windows", "#BE7836")):
            colors = ui_theme.build_theme(kind, system="windows").button_colors(
                "primary", surface="#ffffff")
            self.assertEqual(colors["bg"], "#E6A05A")
            self.assertEqual(colors["fg"], "#15100A")
            self.assertEqual(colors["activebackground"], pressed)

    def test_danger_button_uses_the_danger_pair(self):
        theme = ui_theme.build_theme("windows", system="windows")
        colors = theme.button_colors("danger", surface=theme.card)
        self.assertEqual(colors["bg"], theme.danger_bg)
        self.assertEqual(colors["fg"], theme.danger)
        self.assertEqual(colors["highlightbackground"], theme.danger)
        self.assertNotIn(theme.accent, colors.values())

    def test_disabled_buttons_show_no_copper_and_no_danger_fill(self):
        for kind in ("windows", "dark"):
            theme = ui_theme.build_theme(kind, system="windows")
            loud = {theme.accent, theme.accent_active, theme.danger_bg, theme.danger}
            for variant in ui_theme.BUTTON_VARIANTS:
                colors = theme.button_colors(variant, surface=theme.card, disabled=True)
                self.assertEqual(set(colors.values()) & loud, set(), (kind, variant))
                self.assertEqual(colors["fg"], theme.disabled_fg)

    def test_button_variants_are_validated(self):
        theme = ui_theme.build_theme("dark", system="windows")
        with self.assertRaises(ValueError):
            theme.button_colors("accent")
        with self.assertRaises(ValueError):
            theme.button_colors("primary")  # a filled ring needs its surface

    def test_compact_buttons_reach_the_32px_density(self):
        # Win32 adds a fixed 5px around the text: 20px line + 2*3 + 2*1 + 5.
        theme = ui_theme.build_theme("dark", system="windows")
        chrome = theme.button_chrome(compact=True)
        self.assertEqual((chrome["pady"], chrome["highlightthickness"]), (3, 1))
        # A ringless navigation button keeps the same footprint.
        flush = theme.button_chrome(compact=True, ring=False)
        self.assertEqual((flush["padx"], flush["pady"], flush["highlightthickness"]), (9, 4, 0))
        self.assertEqual(theme.tree_row_height, 32)

    def test_unselected_tab_foreground_follows_the_appearance_on_macos(self):
        # The selected tab keeps `text`; the unselected one tracks the system
        # text color exactly as PR56 shipped it (via text_strong).
        for kind in ("light", "dark"):
            theme = ui_theme.build_theme(kind, system="darwin")
            self.assertEqual(theme.tab_unselected_fg, "systemTextColor")
            self.assertEqual(theme.tab_unselected_fg, theme.text_strong)

    def test_rgb_reads_hex_and_rejects_names(self):
        self.assertEqual(ui_theme.rgb("#FF0000"), (1.0, 0.0, 0.0))
        with self.assertRaises(ValueError):
            ui_theme.rgb("systemTextColor")


class Recorder:
    def __init__(self):
        self.configured = {}
        self.mapped = {}
        self.layouts = {}

    def configure(self, name, **options):
        self.configured[name] = options

    def map(self, name, **options):
        self.mapped[name] = options

    def layout(self, name, layout):
        self.layouts[name] = layout


class TtkThemeSelectionTests(unittest.TestCase):

    class FakeStyle:
        def __init__(self, available, current="default"):
            self.available = available
            self.current = current
            self.used = []

        def theme_names(self):
            return self.available

        def theme_use(self, name=None):
            if name is None:
                return self.current
            if name not in self.available:
                raise RuntimeError("no such theme")
            self.used.append(name)
            self.current = name
            return name

    def test_picks_the_first_available_preference(self):
        style = self.FakeStyle(("aqua", "clam", "default"))
        self.assertEqual(ui_theme.apply_ttk_theme(style, "darwin"), "aqua")
        self.assertEqual(style.used, ["aqua"])

    def test_windows_picks_clam_in_both_appearances(self):
        for kind in ("windows", "dark"):
            style = self.FakeStyle(("vista", "clam", "default"))
            theme = ui_theme.build_theme(kind, system="windows")
            self.assertEqual(ui_theme.apply_ttk_theme(style, "windows", resolved=theme), "clam")

    def test_skips_a_theme_this_platform_lacks(self):
        # The old bare try/except left whatever theme was already active.
        style = self.FakeStyle(("winnative", "default"))
        light = ui_theme.build_theme("windows", system="windows")
        self.assertEqual(ui_theme.apply_ttk_theme(style, "windows", resolved=light), "default")
        self.assertEqual(style.used, ["default"])

    def test_never_raises_when_ttk_misbehaves(self):
        class Broken:
            def theme_names(self):
                raise RuntimeError("no ttk")

        self.assertIsNone(ui_theme.apply_ttk_theme(Broken(), "windows"))

    def test_manager_styles_define_navigation_and_selection_states(self):
        style = Recorder()
        theme = ui_theme.build_theme("windows", system="windows")
        self.assertIs(ui_theme.configure_manager_styles(style, theme), style)
        self.assertEqual(style.configured["Manager.TNotebook.Tab"]["padding"], (18, 10))
        self.assertEqual(style.mapped["Manager.TNotebook.Tab"]["padding"], [("selected", (18, 10))])
        # Selected tabs read as primary text on the content plane, not copper.
        self.assertIn(("selected", theme.text_strong),
                      style.mapped["Manager.TNotebook.Tab"]["foreground"])
        for name in ("Manager.Treeview", "Meeting.Treeview"):
            self.assertEqual(style.mapped[name]["background"], [("selected", theme.select_bg)])
            self.assertEqual(style.configured[name]["rowheight"], 32)
            self.assertEqual(style.configured[name]["font"], theme.body())
            self.assertEqual(style.configured[f"{name}.Heading"]["font"], theme.body_strong())
        # The sidebar shell's page container draws no tab strip.
        self.assertEqual(style.layouts["Pages.TNotebook.Tab"], [])

    def test_list_headings_sit_on_content_and_never_match_the_selection(self):
        # In Light the old navigation-step heading (#e9e8e7) was 1.02:1 against
        # the selection plane, so a selected first row merged into it.
        for kind in ("windows", "dark"):
            style = Recorder()
            theme = ui_theme.build_theme(kind, system="windows")
            ui_theme.configure_manager_styles(style, theme)
            for name in ("Manager.Treeview", "Meeting.Treeview"):
                heading = style.configured[f"{name}.Heading"]
                self.assertEqual(heading["background"], theme.card, (kind, name))
                self.assertNotEqual(heading["background"], theme.select_bg, (kind, name))
                # Clam's raised border: only the dark edge (bottom, right) shows.
                self.assertEqual((heading["relief"], heading["bordercolor"], heading["lightcolor"],
                                  heading["darkcolor"]),
                                 ("raised", theme.card, theme.card, theme.border), (kind, name))

    def test_lists_fields_and_sliders_draw_a_focus_ring(self):
        style = Recorder()
        theme = ui_theme.build_theme("dark", system="windows")
        ui_theme.configure_manager_styles(style, theme)
        for name in ("TCombobox", "Manager.Treeview", "Meeting.Treeview", "Horizontal.TScale"):
            for edge in ("bordercolor", "lightcolor", "darkcolor"):
                self.assertIn(("focus", theme.focus_ring), style.mapped[name][edge], (name, edge))
        # At rest the list edge is invisible and the field edge is the boundary.
        self.assertEqual(style.configured["Manager.Treeview"]["bordercolor"], theme.card)
        self.assertEqual(style.configured["TCombobox"]["bordercolor"], theme.field_border)

    def test_comboboxes_share_one_compact_contract(self):
        style = Recorder()
        theme = ui_theme.build_theme("dark", system="windows")
        ui_theme.configure_manager_styles(style, theme)
        self.assertEqual(style.configured["TCombobox"]["padding"], (8, 5))
        self.assertEqual(style.configured["Device.TCombobox"], {"font": theme.body()})
        mapped = style.mapped["TCombobox"]
        self.assertIn(("disabled", theme.disabled_fg), mapped["foreground"])
        self.assertIn(("readonly", "focus", theme.select_bg), mapped["selectbackground"])
        self.assertNotIn("Device.TCombobox", style.mapped)

    def test_meters_and_progress_use_the_chart_roles(self):
        style = Recorder()
        theme = ui_theme.build_theme("dark", system="windows")
        ui_theme.configure_manager_styles(style, theme)
        self.assertEqual(style.configured["Horizontal.TProgressbar"]["background"], theme.chart_primary)
        self.assertEqual(style.configured["Secondary.Horizontal.TProgressbar"]["background"],
                         theme.chart_secondary)
        self.assertEqual(style.configured["Horizontal.TProgressbar"]["troughcolor"], theme.field)

    def test_combobox_popdown_styles_its_actual_listbox(self):
        class FakeTk:
            def __init__(self):
                self.calls = []

            def call(self, *args):
                self.calls.append(args)
                return ".device.popdown"

        class FakeCombo:
            def __init__(self):
                self.tk = FakeTk()

            def __str__(self):
                return ".device"

        combo = FakeCombo()
        theme = ui_theme.build_theme("dark", system="windows")
        self.assertEqual(
            ui_theme.configure_combobox_popdown(combo, theme),
            ".device.popdown.f.l",
        )
        self.assertEqual(combo.tk.calls[0],
                         ("ttk::combobox::PopdownWindow", ".device"))
        configure = combo.tk.calls[1]
        self.assertEqual(configure[0], ".device.popdown.f.l")
        options = dict(zip(configure[2::2], configure[3::2]))
        self.assertEqual(options["-background"], theme.field)
        self.assertEqual(options["-foreground"], theme.text)
        self.assertEqual(options["-selectbackground"], theme.select_bg)
        self.assertEqual(options["-selectforeground"], theme.select_fg)
        self.assertEqual(options["-font"], theme.body())
        self.assertEqual(options["-relief"], "flat")
        self.assertEqual(options["-highlightthickness"], 0)
        self.assertEqual(options["-selectborderwidth"], 0)
        self.assertEqual(options["-activestyle"], "none")

    def test_combobox_popdown_leaves_macos_native(self):
        class FakeTk:
            def call(self, *args):
                raise AssertionError("Aqua must own the combobox popdown")

        class FakeCombo:
            tk = FakeTk()

            def __str__(self):
                return ".device"

        theme = ui_theme.build_theme("dark", system="darwin")
        self.assertIsNone(ui_theme.configure_combobox_popdown(FakeCombo(), theme))

    def test_macos_popdown_fit_widens_only_through_the_post_offset(self):
        calls = []

        class FakeTk:
            def call(self, *args):
                calls.append(args)
                if args == ("ttk::combobox::PopdownWindow", ".device"):
                    return ".device.popdown"
                if args[:2] == ("winfo", "exists"):
                    return args[2] == ".device.popdown.f.l"
                if args == (".device.popdown.f.l", "cget", "-font"):
                    return "TkTextFont"
                if args[:3] != ("ttk::style", "configure", "system.Device.TCombobox"):
                    raise AssertionError(args)

            getboolean = staticmethod(bool)

        class FakeWindow:
            def winfo_width(self):
                return 900

            def winfo_rootx(self):
                return 0

        class FakeCombo:
            tk = FakeTk()

            def __str__(self):
                return ".device"

            def cget(self, option):
                return {"values": ("A long endpoint name",), "style": "system.Device.TCombobox"}[option]

            def winfo_toplevel(self):
                return FakeWindow()

            def winfo_width(self):
                return 200

            def winfo_rootx(self):
                return 100

        theme = ui_theme.build_theme("dark", system="darwin")
        with mock.patch.object(ui_theme.tkfont, "Font") as font:
            font.return_value.measure.return_value = 360
            self.assertIsNone(
                ui_theme.configure_combobox_popdown(FakeCombo(), theme, fit_values=True))
        self.assertIn((".device.popdown.f.l", "cget", "-font"), calls)
        command, subcommand, style, option, offset = calls[-1]
        self.assertEqual((command, subcommand, style, option),
                         ("ttk::style", "configure", "system.Device.TCombobox", "-postoffset"))
        self.assertGreaterEqual(200 + offset[2], 360)

    def test_aqua_font_feature_detection_reads_listbox_and_menu_without_native_mutation(self):
        theme = ui_theme.build_theme("dark", system="darwin")
        for child in (".f.l", ".menu"):
            with self.subTest(child=child):
                calls = []
                native = ".device.popdown" + child
                def call(*args):
                    calls.append(args)
                    if args == ("ttk::combobox::PopdownWindow", ".device"):
                        return ".device.popdown"
                    if args[:2] == ("winfo", "exists"):
                        return args[2] == native
                    if args == (native, "cget", "-font"):
                        return "SyntheticNativeFont"
                    raise AssertionError(args)
                combo = mock.Mock()
                combo.__str__ = mock.Mock(return_value=".device")
                combo.tk.call.side_effect = call
                combo.tk.getboolean.side_effect = bool
                with mock.patch.object(ui_theme.tkfont, "Font") as font:
                    self.assertIs(ui_theme.combobox_font(combo, theme), font.return_value)
                font.assert_called_once_with(root=combo, font="SyntheticNativeFont")
                self.assertFalse(any("configure" in args for args in calls))

    def test_dark_manager_styles_replace_clam_light_defaults(self):
        style = Recorder()
        theme = ui_theme.build_theme("dark", system="windows")
        ui_theme.configure_manager_styles(style, theme)
        self.assertEqual(style.configured["Sash"]["background"], theme.surface)
        self.assertIn(("!active", theme.field_border),
                      style.mapped["Vertical.TScrollbar"]["background"])
        self.assertEqual(style.mapped["Manager.TNotebook.Tab"]["lightcolor"],
                         [("selected", theme.card)])


def _tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise unittest.SkipTest(f"Tk initialization unavailable: {exc}") from exc
    root.withdraw()
    return root


@unittest.skipUnless(sys.platform == "win32", "the painted controls are verified on Windows")
class ThemedControlTests(unittest.TestCase):
    """The button factory and focus ring on a real (withdrawn) Tk root."""

    @classmethod
    def setUpClass(cls):
        cls.root = _tk_root()
        families = frozenset(tkfont.families(cls.root))
        cls.theme = ui_theme.build_theme("dark", system="windows", families=families)
        cls.light = ui_theme.build_theme("windows", system="windows", families=families)

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        # Tk delivers generated focus events only to mapped windows; this one
        # is mapped off-screen and fully transparent.
        self.host = tk.Toplevel(self.root)
        self.host.attributes("-alpha", 0.0)
        self.host.geometry("320x120+-4000+-4000")
        self.addCleanup(self.host.destroy)
        self.card = tk.Frame(self.host, bg=self.theme.card)
        self.card.pack(fill="both", expand=True)

    def _button(self, variant="neutral", **options):
        return ui_theme.button(self.card, "Iniciar gravação", lambda: None, variant,
                               resolved=self.theme, **options)

    def test_disabling_a_primary_drops_its_copper_and_enabling_restores_it(self):
        button = self._button("primary")
        self.assertEqual(button.cget("bg"), self.theme.accent)
        self.assertEqual(button.cget("highlightbackground"), self.theme.card)
        button.configure(state="disabled")
        self.assertEqual(button.cget("bg"), self.theme.control)
        self.assertEqual(button.cget("highlightcolor"), self.theme.border)
        self.assertEqual(button.cget("disabledforeground"), self.theme.disabled_fg)
        button["state"] = "normal"
        self.assertEqual(button.cget("bg"), self.theme.accent)
        button.config(state=tk.DISABLED, text="Aguarde")
        self.assertEqual(button.cget("bg"), self.theme.control)
        self.assertEqual(button.cget("text"), "Aguarde")

    def test_a_ttk_parent_lends_its_style_background(self):
        from tkinter import ttk
        style = ttk.Style(self.root)
        style.configure("Probe.TFrame", background=self.theme.surface)
        frame = ttk.Frame(self.root, style="Probe.TFrame")
        self.addCleanup(frame.destroy)
        button = ui_theme.button(frame, "Salvar", lambda: None, "primary", resolved=self.theme)
        self.assertEqual(button.cget("highlightbackground"), self.theme.surface)

    def test_unknown_variants_fail_before_building_a_widget(self):
        with self.assertRaises(ValueError):
            self._button("accent")

    def test_focus_thickens_the_ring_without_resizing_the_button(self):
        button = self._button()
        button.pack()
        self.root.update()
        rest = (button.winfo_reqwidth(), button.winfo_reqheight())
        self.assertGreaterEqual(rest[1], 32)
        # Win32 paints a push button's ring as its default ring, in
        # -highlightcolor: the control boundary at rest, the focus ring on focus.
        self.assertEqual(str(button.cget("default")), "active")
        self.assertEqual(button.cget("highlightcolor"), self.theme.field_border)
        button.event_generate("<FocusIn>")
        self.assertEqual(int(button.cget("highlightthickness")), 2)
        self.assertEqual(button.cget("highlightcolor"), self.theme.focus_ring)
        self.assertEqual((button.winfo_reqwidth(), button.winfo_reqheight()), rest)
        button.event_generate("<FocusIn>")  # repeated focus events are idempotent
        self.assertEqual(int(button.cget("pady")), 2)
        button.event_generate("<FocusOut>")
        self.assertEqual(int(button.cget("highlightthickness")), 1)
        self.assertEqual(button.cget("highlightcolor"), self.theme.field_border)
        self.assertEqual((button.winfo_reqwidth(), button.winfo_reqheight()), rest)

    def test_a_ringless_button_gains_the_ring_only_on_focus(self):
        button = tk.Button(self.card, text="Biblioteca", font=self.theme.body(),
                           **self.theme.nav_button_colors(self.theme.surface_alt),
                           **self.theme.button_chrome(compact=True, ring=False))
        ui_theme.focus_ring(button, self.theme)
        button.pack()
        self.root.update()
        rest = button.winfo_reqheight()
        button.event_generate("<FocusIn>")
        self.assertEqual(int(button.cget("highlightthickness")), 2)
        self.assertEqual(button.cget("highlightcolor"), self.theme.focus_ring)
        self.assertEqual(button.winfo_reqheight(), rest)
        button.event_generate("<FocusOut>")
        self.assertEqual(int(button.cget("highlightthickness")), 0)

    def test_entries_keep_their_32px_height_through_focus(self):
        entry = tk.Entry(self.card, font=self.theme.body(), **self.theme.entry_colors(),
                         **self.theme.entry_chrome())
        ui_theme.focus_ring(entry, self.theme, shrink=("bd",))
        entry.pack()
        self.root.update()
        rest = entry.winfo_reqheight()
        self.assertGreaterEqual(rest, 32)
        entry.event_generate("<FocusIn>")
        self.assertEqual(int(entry.cget("highlightthickness")), 2)
        self.assertEqual(entry.winfo_reqheight(), rest)


    def test_text_fields_and_lists_draw_the_field_edge_and_keep_their_size_on_focus(self):
        # Native sunken chrome showed no ring at all and, in Light, lost its
        # right and bottom edges to Win32's white bevel on a white card.
        for theme in (self.theme, self.light):
            for factory, colors in ((tk.Text, theme.text_colors()),
                                    (tk.Listbox, theme.listbox_colors())):
                with self.subTest(kind=theme.kind, widget=factory.__name__):
                    widget = factory(self.card, height=2, width=20, font=theme.body(),
                                     **colors, **theme.entry_chrome())
                    ui_theme.focus_ring(widget, theme, shrink=("bd",))
                    widget.pack()
                    self.root.update()
                    self.assertEqual(str(widget.cget("relief")), "flat")
                    self.assertEqual(widget.cget("highlightbackground"), theme.field_border)
                    self.assertEqual(widget.cget("highlightcolor"), theme.focus_ring)
                    self.assertEqual(int(widget.cget("highlightthickness")), 1)
                    rest = (widget.winfo_reqwidth(), widget.winfo_reqheight())
                    widget.event_generate("<FocusIn>")
                    self.assertEqual(int(widget.cget("highlightthickness")), 2)
                    self.assertEqual((widget.winfo_reqwidth(), widget.winfo_reqheight()), rest)
                    widget.event_generate("<FocusOut>")
                    self.assertEqual(int(widget.cget("highlightthickness")), 1)
                    self.assertEqual((widget.winfo_reqwidth(), widget.winfo_reqheight()), rest)
                    widget.destroy()

    def _descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self._descendants(child)

    def test_info_bar_pairs_tone_glyph_title_body_action_and_details(self):
        for theme in (self.theme, self.light):
            with self.subTest(kind=theme.kind):
                acted, copied, outcome = [], [], [False]
                bar = ui_theme.info_bar(
                    self.card, theme, "danger", "A transcrição falhou",
                    "O áudio foi preservado e pode ser transcrito de novo.",
                    action=("Transcrever novamente", lambda: acted.append(True)),
                    details="Detalhes técnicos: runtime unavailable",
                    copy=lambda text, done: (copied.append(text), done(outcome[0])),
                )
                self.addCleanup(bar.destroy)
                for sibling in self.card.pack_slaves():
                    if sibling is not bar:
                        sibling.destroy()
                bar.pack(fill="x")
                self.root.update()
                self.assertEqual(bar.cget("bg"), theme.danger_bg)
                self.assertEqual(bar.cget("highlightbackground"), theme.danger)
                labels = {w.cget("text"): w for w in self._descendants(bar) if isinstance(w, tk.Label)}
                self.assertEqual(labels["A transcrição falhou"].cget("fg"), theme.text)
                self.assertIn("O áudio foi preservado e pode ser transcrito de novo.", labels)
                glyph = next(w for text, w in labels.items() if len(text) == 1)
                self.assertEqual(glyph.cget("fg"), theme.danger)
                buttons = {w.cget("text"): w for w in self._descendants(bar) if isinstance(w, tk.Button)}
                buttons["Transcrever novamente"].invoke()
                self.assertEqual(acted, [True])
                texts = [w for w in self._descendants(bar) if isinstance(w, tk.Text)]
                self.assertEqual(len(texts), 1)
                self.assertFalse(texts[0].winfo_ismapped())
                buttons["Detalhes ▾"].invoke()
                self.root.update()
                self.assertTrue(texts[0].winfo_ismapped())
                self.assertEqual(texts[0].get("1.0", "end-1c"), "Detalhes técnicos: runtime unavailable")
                self.assertEqual(str(texts[0].cget("state")), "disabled")
                self.assertEqual(buttons["Detalhes ▾"].cget("text"), "Detalhes ▴")
                copy = next(w for w in self._descendants(bar)
                            if isinstance(w, tk.Button) and w.cget("text") == "Copiar")
                # The bar never touches the clipboard itself; the caller's copy does,
                # and a failure is said instead of shown as a copy.
                copy.invoke()
                self.assertEqual(copied, ["Detalhes técnicos: runtime unavailable"])
                self.assertEqual(copy.cget("text"), "Copiar")
                labels = [w.cget("text") for w in self._descendants(bar) if isinstance(w, tk.Label)]
                self.assertIn("Selecione o texto e pressione Ctrl+C", labels)
                outcome[0] = True
                copy.invoke()
                self.assertEqual(copy.cget("text"), "Copiado")
                labels = [w.cget("text") for w in self._descendants(bar) if isinstance(w, tk.Label)]
                self.assertNotIn("Selecione o texto e pressione Ctrl+C", labels)
                buttons["Detalhes ▾"].invoke()
                self.root.update()
                self.assertFalse(texts[0].winfo_ismapped())

    def test_info_bar_details_without_a_copy_callable_offer_no_copy_button(self):
        bar = ui_theme.info_bar(self.card, self.theme, "danger", "Título", "Corpo.", details="Texto técnico")
        self.addCleanup(bar.destroy)
        buttons = [w.cget("text") for w in self._descendants(bar) if isinstance(w, tk.Button)]
        self.assertEqual(buttons, ["Detalhes ▾"])

    def test_info_bar_without_action_or_details_has_no_button_row(self):
        bar = ui_theme.info_bar(self.card, self.theme, "warning", "Título", "Corpo.")
        self.addCleanup(bar.destroy)
        self.assertEqual(bar.cget("bg"), self.theme.warning_bg)
        self.assertEqual([w for w in self._descendants(bar) if isinstance(w, (tk.Button, tk.Text))], [])

    def test_info_bar_rejects_an_unknown_tone(self):
        with self.assertRaises(ValueError):
            ui_theme.info_bar(self.card, self.theme, "error", "Título", "Corpo.")

    def test_info_bar_writes_the_severity_and_local_time_and_wraps_its_title(self):
        moment = datetime(2026, 10, 20, 14, 2)
        for tone, word in (("danger", "Erro"), ("warning", "Aviso"), ("info", "Informação"),
                           ("success", "Concluído")):
            with self.subTest(tone=tone):
                closed = []
                title = "Sem áudio do sistema há 1 min 05 s e um título longo o bastante para quebrar"
                bar = ui_theme.info_bar(self.card, self.theme, tone, title, "Corpo.",
                                        when=moment, dismiss=lambda: closed.append(True))
                self.addCleanup(bar.destroy)
                bar.pack(fill="x")
                self.root.update()
                labels = {w.cget("text"): w for w in self._descendants(bar) if isinstance(w, tk.Label)}
                stamp = labels[f"{word} às 14:02"]
                title_label = labels[title]
                self.assertIs(title_label, bar.title_label)
                # The stamp sits at the right of the title row, inside the bar ...
                self.assertGreaterEqual(stamp.winfo_rootx(),
                                        title_label.winfo_rootx() + title_label.winfo_width())
                self.assertLessEqual(stamp.winfo_rootx() + stamp.winfo_width(),
                                     bar.winfo_rootx() + bar.winfo_width())
                # ... and the title wraps to make room instead of pushing it out.
                line = tkfont.Font(root=self.root, font=title_label.cget("font")).metrics("linespace")
                self.assertGreater(title_label.winfo_height(), line)
                buttons = {w.cget("text"): w for w in self._descendants(bar) if isinstance(w, tk.Button)}
                buttons["Fechar"].invoke()
                self.assertEqual(closed, [True])
                bar.destroy()

    def test_info_bar_without_a_body_shows_only_its_title(self):
        bar = ui_theme.info_bar(self.card, self.theme, "warning", "Só o título", "")
        self.addCleanup(bar.destroy)
        labels = [w.cget("text") for w in self._descendants(bar) if isinstance(w, tk.Label)]
        self.assertEqual([text for text in labels if len(text) > 1], ["Só o título"])

    def test_message_bar_takes_no_room_until_a_page_has_something_to_say(self):
        content = tk.Frame(self.card, height=40, bg=self.theme.card)
        content.pack(fill="x")
        bar = ui_theme.MessageBar(self.card, self.theme)
        with self.assertRaises(ValueError):
            bar.receipt("Antes de attach()")
        bar.attach(side="top", fill="x", before=content)
        self.root.update()
        self.assertEqual((bar.winfo_manager(), bar.kind), ("", None))
        moment = datetime(2026, 10, 20, 14, 2)
        bar.problem("danger", "Não foi possível carregar a biblioteca", "Tente de novo.",
                    action=("Tentar novamente", lambda: None), details="OSError: locked", when=moment)
        self.root.update()
        self.assertEqual((bar.winfo_manager(), bar.kind), ("pack", "problem"))
        self.assertLess(bar.winfo_rooty(), content.winfo_rooty())
        texts = [w.cget("text") for w in self._descendants(bar) if isinstance(w, (tk.Label, tk.Button))]
        for expected in ("Não foi possível carregar a biblioteca", "Erro às 14:02", "Tentar novamente",
                         "Detalhes ▾"):
            self.assertIn(expected, texts)
        # One message at a time: a routine outcome is a quiet line, not a second bar.
        bar.receipt("Título salvo.", moment)
        self.root.update()
        self.assertEqual((bar.kind, len(bar.winfo_children())), ("receipt", 1))
        line = bar.winfo_children()[0]
        self.assertEqual((line.cget("text"), line.cget("fg")), ("Título salvo · 14:02", self.theme.text_muted))
        bar.retitle("Ignorado: não há problema à vista")
        self.assertEqual(line.cget("text"), "Título salvo · 14:02")
        bar.problem("warning", "Primeiro", "Corpo.")
        bar.retitle("Segundo")
        self.assertEqual(bar.winfo_children()[0].title_label.cget("text"), "Segundo")
        bar.clear()
        self.root.update()
        self.assertEqual((bar.winfo_manager(), bar.kind, bar.winfo_children()), ("", None, []))

    def test_message_bar_can_sit_in_a_grid_cell(self):
        host = tk.Frame(self.card, bg=self.theme.card)
        host.pack(fill="both", expand=True)
        host.columnconfigure(0, weight=1)
        bar = ui_theme.MessageBar(host, self.theme)
        bar.attach(grid=True, row=2, column=0, sticky="ew")
        bar.receipt("Pronto.")
        self.root.update()
        self.assertEqual((bar.winfo_manager(), bar.grid_info()["row"]), ("grid", 2))
        bar.clear()
        self.root.update()
        self.assertEqual(bar.winfo_manager(), "")

    def test_status_chip_writes_the_state_and_colors_only_its_dot(self):
        chip = ui_theme.StatusChip(self.card, self.theme, bg=self.theme.card, font=self.theme.heading())
        chip.pack(fill="x")
        chip.set("Gravando · 00:45:54", "accent_mark")
        self.root.update()
        dot = next(w for w in chip.winfo_children() if isinstance(w, tk.Canvas))
        shape = dot.find_all()[0]
        self.assertEqual(chip.label.cget("text"), "Gravando · 00:45:54")
        self.assertEqual(dot.itemcget(shape, "fill"), self.theme.accent_mark)
        self.assertEqual(chip.label.cget("fg"), self.theme.text_strong)
        chip.set("Pronto · 00:00:00", "success")
        self.assertEqual(dot.itemcget(shape, "fill"), self.theme.success)
        self.assertEqual(chip.label.cget("fg"), self.theme.text_strong)

    def test_a_menubutton_shares_the_button_row_height(self):
        # The Ditado footer puts the primary and its "more" menu in one row.
        button = self._button("primary")
        more = tk.Menubutton(self.card, text="Mais opções ▾", font=self.theme.body(),
                             **self.theme.button_colors(), **self.theme.button_chrome())
        button.pack(side="left")
        more.pack(side="left")
        self.root.update()
        self.assertEqual(more.winfo_reqheight(), button.winfo_reqheight())


class GuiSourceTests(unittest.TestCase):
    """The GUI must go through the seam; a literal here is the bug returning."""

    SOURCES = ("snipvoice.pyw", "meeting_gui.py", "meeting_chat.py", "meeting_waveform.py",
               "voice_indicator.py")

    def test_secondary_windows_use_prepare_dialog_with_explicit_primary_and_hud_exemptions(self):
        exemptions = {("snipvoice.pyw", "_show_manager_window"), ("meeting_gui.py", "__init__"),
                      ("voice_indicator.py", "_build")}
        found = set()
        root = os.path.join(os.path.dirname(__file__), "..")
        for name in ("snipvoice.pyw", "meeting_gui.py", "voice_indicator.py", "ui_theme.py"):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                source = handle.read()
            lines, stack = source.splitlines(), []
            case = self
            class Scan(ast.NodeVisitor):
                def visit_FunctionDef(self, node):
                    stack.append(node.name)
                    self.generic_visit(node)
                    stack.pop()
                def visit_Call(self, node):
                    if isinstance(node.func, ast.Attribute) and node.func.attr == "Toplevel":
                        key = (name, stack[-1])
                        if key in exemptions:
                            found.add(key)
                        else:
                            case.assertIn("prepare_dialog(", "\n".join(lines[node.lineno:node.lineno + 3]), key)
                    self.generic_visit(node)
            Scan().visit(ast.parse(source))
        self.assertEqual(found, exemptions)
        for name in ("meeting_gui.py", "snipvoice.pyw"):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                self.assertNotIn("simpledialog", handle.read())

    def _sources(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        for name in self.SOURCES:
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                yield name, handle.read()

    def test_no_hardcoded_colors_left_in_the_gui(self):
        for name, source in self._sources():
            self.assertEqual(re.findall(r'"#[0-9A-Fa-f]{3,8}"', source), [], name)

    def test_no_windows_only_font_families_left_in_the_gui(self):
        for name, source in self._sources():
            leaked = re.findall(r'"(Segoe[^"]*|Consolas|Cascadia[^"]*|Arial|Helvetica)"', source)
            self.assertEqual(leaked, [], name)

    def test_every_text_field_and_list_declares_its_chrome(self):
        """A bare ``tk.Text``/``tk.Listbox`` falls back to Win32's sunken bevel."""
        offenders = []
        for name, source in self._sources():
            for node in ast.walk(ast.parse(source)):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("Text", "Listbox")):
                    continue
                themed = any(
                    keyword.arg is None and isinstance(keyword.value, ast.Call)
                    and isinstance(keyword.value.func, ast.Attribute)
                    and keyword.value.func.attr == "entry_chrome"
                    for keyword in node.keywords)
                explicit = any(keyword.arg == "relief" for keyword in node.keywords)
                if not (themed or explicit):
                    offenders.append((name, node.lineno, node.func.attr))
        self.assertEqual(offenders, [])

    def test_buttons_and_checkboxes_never_take_a_color_directly(self):
        """Aqua draws these itself; their colors must come from the seam.

        A literal ``fg=`` here is invisible on macOS rather than merely
        off-palette: Aqua keeps its own light bezel whatever ``bg`` says, and
        then honours the foreground, so the label vanishes into the bezel.
        """
        color_options = {
            "bg", "fg", "background", "foreground", "activebackground",
            "activeforeground", "selectcolor", "disabledforeground",
        }
        offenders = []
        for name, source in self._sources():
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute)
                        and func.attr in ("Button", "Checkbutton", "Radiobutton", "Menubutton")):
                    continue
                for keyword in node.keywords:
                    if keyword.arg in color_options:
                        offenders.append((name, node.lineno, func.attr, keyword.arg))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
