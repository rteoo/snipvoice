"""Per-OS palette/font resolution for the manager GUI.

The load-bearing guarantee is the first test class: on Windows every token must
resolve to the deliberate Fluent palette, while the macOS safety seam remains
platform-native.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import ui_theme


# Win Design System tokens are copied here to catch accidental drift.
FLUENT_WINDOWS_COLORS = {
    "surface": "#F3F3F3",
    "surface_alt": "#FFFFFF",
    "surface_alt_active": "#DEDEDE",
    "surface_hover": "#EAEAEA",
    "card": "#FFFFFF",
    "field": "#FFFFFF",
    "field_hover": "#EAEAEA",
    "control": "#EAEAEA",
    "control_active": "#DEDEDE",
    "control_border": "#767676",
    "text": "#1A1A1A",
    "text_strong": "#1A1A1A",
    "text_muted": "#5C5C5C",
    "text_on_accent": "#FFFFFF",
    "border": "#D6D6D6",
    "divider": "#D6D6D6",
    "accent": "#005FB8",
    "accent_active": "#004A91",
    "danger": "#A4262C",
    "danger_active": "#8D1F24",
    "focus_ring": "#005FB8",
    "link": "#005FB8",
    "warning": "#7A4D00",
    "success": "#0F6B36",
    "select_bg": "#DCEEFF",
    "select_fg": "#1A1A1A",
    "text_native": "#1A1A1A",
    "tab_unselected_fg": "#5C5C5C",
}

DARK_WINDOWS_COLORS = {
    "surface": "#202020",
    "surface_alt": "#2B2B2B",
    "card": "#2B2B2B",
    "field": "#333333",
    "control": "#333333",
    "control_border": "#A0A0A0",
    "text": "#F5F5F5",
    "text_strong": "#F5F5F5",
    "text_muted": "#C4C4C4",
    "border": "#494949",
    "accent": "#60CDFF",
    "accent_active": "#A1E2FF",
}


class WindowsPaletteTests(unittest.TestCase):
    """Windows must keep the intentional Fluent palette stable."""

    def test_every_fluent_token_keeps_its_declared_literal(self):
        colors = ui_theme.palette("windows")
        for token, expected in FLUENT_WINDOWS_COLORS.items():
            self.assertEqual(colors[token], expected, token)

    def test_windows_fonts_are_segoe_ui_at_the_original_sizes(self):
        theme = ui_theme.build_theme("windows", system="windows")
        self.assertEqual(theme.font(9), ("Segoe UI", 9))
        self.assertEqual(theme.font(12, "bold"), ("Segoe UI", 12, "bold"))
        self.assertEqual(theme.emoji_font(12), ("Segoe UI Emoji", 12))
        self.assertEqual(theme.mono_font(10, "bold"), ("Consolas", 10, "bold"))
        self.assertEqual(theme.symbol_family, "Segoe UI Symbol")

    def test_windows_ignores_a_system_default_size(self):
        # Windows is the reference scale; probing must not shift it.
        theme = ui_theme.build_theme("windows", system="windows", default_size=13)
        self.assertEqual(theme.size_delta, 0)
        self.assertEqual(theme.font(9), ("Segoe UI", 9))

    def test_windows_prefers_vista(self):
        self.assertEqual(ui_theme.ttk_theme_preference("windows")[0], "vista")

    def test_windows_dark_palette_is_opaque_and_high_contrast(self):
        colors = ui_theme.palette("dark", system="windows")
        for token, expected in DARK_WINDOWS_COLORS.items():
            self.assertEqual(colors[token], expected, token)
        self.assertEqual(colors["text_native"], colors["text"])

    def test_windows_dark_mode_uses_clam_so_colors_are_honored(self):
        self.assertEqual(
            ui_theme.ttk_theme_preference("windows", dark=True)[0], "clam"
        )


class LinuxPaletteTests(unittest.TestCase):
    def test_windows_palette_uses_literal_native_colors_on_linux(self):
        # ``SystemButtonText`` is a Win32 Tk color name. Linux may still use
        # the Windows/Fluent appearance preference, but X11 Tk needs the
        # literal fallback for widgets that receive an explicit foreground.
        theme = ui_theme.build_theme("windows", system="linux")
        self.assertEqual(theme.text_native, theme.text)
        for options in (
            theme.checkbutton_colors(theme.surface),
            theme.toolbar_button_colors(theme.card),
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

    def test_mac_dark_keeps_dynamic_surfaces_over_the_opaque_base(self):
        dark = ui_theme.palette("dark", system="darwin")
        self.assertEqual(dark["surface"], "systemWindowBackgroundColor")
        self.assertEqual(dark["card"], "systemTextBackgroundColor")
        self.assertEqual(dark["text"], "systemTextColor")
        self.assertNotEqual(dark["surface_alt"], ui_theme.palette("light")["surface_alt"])

    def test_mac_fonts_use_the_system_families(self):
        theme = ui_theme.build_theme("dark", system="darwin")
        self.assertEqual(theme.family, ".AppleSystemUIFont")
        self.assertEqual(theme.emoji_family, "Apple Color Emoji")
        self.assertEqual(theme.mono_family, "Menlo")
        self.assertNotIn("Segoe", theme.symbol_family)

    def test_mac_sizes_shift_onto_the_platform_scale(self):
        # A 9 pt Windows label is body text; it must land on the platform's
        # own default size rather than two points under every native control.
        theme = ui_theme.build_theme("light", system="darwin", default_size=13)
        self.assertEqual(theme.size_delta, 4)
        self.assertEqual(theme.font(9), (".AppleSystemUIFont", 13))
        self.assertEqual(theme.font(12, "bold"), (".AppleSystemUIFont", 16, "bold"))

    def test_an_unreadable_default_size_leaves_the_scale_alone(self):
        for probe in (None, 0):
            theme = ui_theme.build_theme("light", system="darwin", default_size=probe)
            self.assertEqual(theme.size_delta, 0)

    def test_mac_prefers_aqua(self):
        self.assertEqual(ui_theme.ttk_theme_preference("darwin")[0], "aqua")


class AppearanceDetectionTests(unittest.TestCase):

    def test_luminance_classification(self):
        self.assertEqual(ui_theme.appearance_kind(0.0), "dark")
        self.assertEqual(ui_theme.appearance_kind(0.12), "dark")
        self.assertEqual(ui_theme.appearance_kind(0.93), "light")
        self.assertEqual(ui_theme.appearance_kind(1.0), "light")

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

    def test_invalid_persisted_preference_falls_back_to_system(self):
        for value in (None, "", "sepia", 1):
            self.assertEqual(ui_theme.normalize_preference(value), "system")


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
        self.assertEqual(theme.surface, DARK_WINDOWS_COLORS["surface"])
        self.assertEqual(theme.card, DARK_WINDOWS_COLORS["card"])
        self.assertNotEqual(theme.surface, "systemWindowBackgroundColor")


class WidgetOptionTests(unittest.TestCase):

    def test_widget_helpers_are_inert_off_macos(self):
        # Win32's own defaults for these widgets are already correct; pinning
        # them would swap the user's selection color for the app's blue.
        for system in ("windows", "linux"):
            theme = ui_theme.build_theme("windows", system=system)
            self.assertEqual(theme.entry_colors(), {})
            self.assertEqual(theme.text_colors(), {})
            self.assertEqual(theme.listbox_colors(), {})
            self.assertEqual(theme.checkbutton_colors("#FFFFFF")["bg"], "#FFFFFF")
            self.assertEqual(theme.button_colors()["bg"], "#EAEAEA")

    def test_disabled_checkboxes_keep_readable_secondary_text(self):
        theme = ui_theme.build_theme("light", system="windows")
        self.assertEqual(
            theme.checkbutton_colors(theme.card)["disabledforeground"],
            theme.text_muted,
        )

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
            ui_theme.build_theme("windows", system="linux").text_native, "#1A1A1A"
        )

    def test_windows_keeps_its_button_widths_and_window_size(self):
        theme = ui_theme.build_theme("windows", system="windows")
        self.assertEqual(theme.button_width(12), 12)
        self.assertEqual(theme.manager_window_size, ("1120x820", 1040, 700))
        self.assertFalse(theme.stacked_toolbar_status)

    def test_fluent_spacing_and_tree_density_are_stable(self):
        theme = ui_theme.build_theme("windows", system="windows")
        self.assertEqual(
            (theme.space_xs, theme.space_sm, theme.space_md,
             theme.space_lg, theme.space_xl),
            (4, 8, 12, 16, 24),
        )
        self.assertEqual(theme.tree_row_height, 30)

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

    def test_settings_cards_use_the_shared_surface_and_quiet_border(self):
        theme = ui_theme.build_theme("windows", system="windows")
        self.assertEqual(
            theme.card_options(),
            {
                "bg": theme.card,
                "highlightbackground": theme.border,
                "highlightthickness": 1,
                "bd": 0,
            },
        )

    def test_macos_never_paints_a_natively_drawn_control(self):
        # Aqua ignores -background on buttons and checkboxes but honours
        # -foreground, so any color the app supplies can only turn the title
        # invisible against the bezel Aqua draws anyway (macOS 15 / Tk 9.0).
        theme = ui_theme.build_theme("dark", system="darwin")
        self.assertEqual(theme.button_colors(), {})
        self.assertEqual(theme.button_colors(accent=True), {})
        self.assertEqual(theme.checkbutton_colors("#222"), {})
        self.assertEqual(theme.toolbar_button_colors("#222"), {})
        self.assertEqual(theme.glyph_button_colors("#222"), {})

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
        self.assertEqual(theme.listbox_colors()["fg"], theme.text)
        # Win32 paints selectcolor behind the indicator in both states; an
        # accent fill made unchecked boxes look checked.
        self.assertEqual(theme.checkbutton_colors(theme.card)["selectcolor"], theme.field)

    def test_nav_buttons_mark_the_selected_section_off_macos(self):
        theme = ui_theme.build_theme("dark", system="windows")
        self.assertEqual(theme.nav_button_colors(theme.surface, selected=True)["fg"], theme.accent)
        self.assertEqual(theme.nav_button_colors(theme.surface)["fg"], theme.text_native)
        self.assertEqual(theme.nav_button_colors(theme.surface)["bg"], theme.surface)
        self.assertEqual(ui_theme.build_theme("dark", system="darwin").nav_button_colors("#222"), {})

    def test_dark_readonly_entries_and_lists_avoid_light_native_faces(self):
        theme = ui_theme.build_theme("dark", system="windows")
        self.assertEqual(theme.entry_colors()["readonlybackground"], theme.surface_alt)
        self.assertNotIn("readonlybackground", theme.text_colors())
        self.assertEqual(theme.listbox_colors()["highlightbackground"], theme.border)

    def test_entry_chrome_draws_a_flat_bordered_field_off_macos(self):
        for kind in ("windows", "dark"):
            theme = ui_theme.build_theme(kind, system="windows")
            chrome = theme.entry_chrome()
            self.assertEqual(chrome["relief"], "flat")
            self.assertEqual(chrome["highlightthickness"], 1)
            self.assertEqual(chrome["highlightbackground"], theme.border)
            self.assertEqual(chrome["highlightcolor"], theme.focus_ring)
        self.assertEqual(ui_theme.build_theme("dark", system="darwin").entry_chrome(), {})

    def test_text_colors_drop_the_options_tk_text_rejects(self):
        colors = ui_theme.build_theme("dark", system="darwin").text_colors()
        self.assertNotIn("disabledbackground", colors)
        self.assertNotIn("disabledforeground", colors)
        self.assertIn("inactiveselectbackground", colors)

    def test_button_colors_always_pair_a_foreground_with_a_background(self):
        for kind in ("windows", "light", "dark"):
            theme = ui_theme.build_theme(kind, system="windows")
            for accent in (False, True):
                colors = theme.button_colors(accent=accent)
                self.assertEqual(
                    set(colors),
                    {"bg", "fg", "activebackground", "activeforeground",
                     "highlightbackground"},
                )

    def test_fluent_button_chrome_has_consistent_geometry_and_focus(self):
        theme = ui_theme.build_theme("windows", system="windows")
        self.assertEqual(
            theme.button_chrome(),
            {
                "relief": "flat", "bd": 0, "padx": 12, "pady": 6,
                "highlightthickness": 1, "highlightcolor": theme.focus_ring,
                "cursor": "hand2",
            },
        )
        compact = theme.button_chrome(compact=True)
        self.assertEqual(compact["padx"], 8)
        self.assertEqual(compact["pady"], 4)

    def test_neutral_buttons_stand_out_on_cards_and_the_page(self):
        # Neutral buttons once shared the card's near-identical grey and its
        # quiet border, so Baixar/Remover read as bare text on model cards.
        def luminance(color):
            channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                      for c in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def contrast(a, b):
            high, low = sorted((luminance(a), luminance(b)), reverse=True)
            return (high + 0.05) / (low + 0.05)

        for kind in ("windows", "dark"):
            theme = ui_theme.build_theme(kind, system="windows")
            colors = theme.button_colors()
            for backdrop in (theme.card, theme.surface):
                self.assertGreaterEqual(
                    contrast(colors["highlightbackground"], backdrop), 1.5, (kind, backdrop))
                self.assertNotEqual(colors["bg"], backdrop)

    def test_filled_buttons_wear_no_grey_ring(self):
        theme = ui_theme.build_theme("dark", system="windows")
        self.assertEqual(theme.button_colors(accent=True)["highlightbackground"], theme.accent)
        self.assertEqual(theme.button_colors(danger=True)["highlightbackground"], theme.danger)

    def test_danger_button_uses_distinct_semantic_tokens(self):
        theme = ui_theme.build_theme("windows", system="windows")
        colors = theme.button_colors(danger=True)
        self.assertEqual(colors["bg"], theme.danger)
        self.assertEqual(colors["activebackground"], theme.danger_active)
        self.assertEqual(colors["fg"], theme.text_on_accent)

    def test_toolbar_buttons_use_the_editor_surface_and_hover_token(self):
        colors = ui_theme.build_theme("windows", system="windows").toolbar_button_colors("#FFFFFF")
        self.assertEqual(colors["bg"], "#FFFFFF")
        self.assertEqual(colors["activebackground"], "#EAEAEA")

    def test_accent_button_uses_the_fluent_windows_tokens(self):
        colors = ui_theme.build_theme("windows", system="windows").button_colors(accent=True)
        self.assertEqual(colors["bg"], "#005FB8")
        self.assertEqual(colors["fg"], "#FFFFFF")
        self.assertEqual(colors["activebackground"], "#004A91")

    def test_toolbar_frame_uses_the_editor_card_surface(self):
        for system in ("windows", "linux"):
            theme = ui_theme.build_theme("windows", system=system)
            self.assertEqual(theme.toolbar_frame_colors(), {"bg": theme.card})

    def test_toolbar_frame_keeps_the_card_surface_on_macos(self):
        theme = ui_theme.build_theme("dark", system="darwin")
        self.assertEqual(theme.toolbar_frame_colors(), {"bg": theme.card})

    def test_status_label_uses_the_body_face_and_muted_grey(self):
        for system in ("windows", "linux"):
            options = ui_theme.build_theme("windows", system=system).status_label_options()
            theme = ui_theme.build_theme("windows", system=system)
            self.assertEqual(options["font"], theme.font(8))
            self.assertEqual(options["fg"], theme.text_muted)

    def test_status_label_uses_the_body_face_and_muted_grey_on_macos(self):
        theme = ui_theme.build_theme("dark", system="darwin")
        options = theme.status_label_options()
        self.assertEqual(options["font"], theme.font(8))
        self.assertEqual(options["fg"], theme.text_muted)

    def test_unselected_tab_foreground_uses_the_fluent_neutral(self):
        for system in ("windows", "linux"):
            self.assertEqual(
                ui_theme.build_theme("windows", system=system).tab_unselected_fg,
                "#5C5C5C",
            )

    def test_unselected_tab_foreground_follows_the_appearance_on_macos(self):
        # The selected tab keeps `text`; the unselected one tracks the system
        # text color exactly as PR56 shipped it (via text_strong).
        for kind in ("light", "dark"):
            theme = ui_theme.build_theme(kind, system="darwin")
            self.assertEqual(theme.tab_unselected_fg, "systemTextColor")
            self.assertEqual(theme.tab_unselected_fg, theme.text_strong)


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

    def test_dark_windows_picks_clam(self):
        style = self.FakeStyle(("vista", "clam", "default"))
        theme = ui_theme.build_theme("dark", system="windows")
        self.assertEqual(
            ui_theme.apply_ttk_theme(style, "windows", resolved=theme), "clam"
        )

    def test_skips_a_theme_this_platform_lacks(self):
        # This is the actual bug: "vista" does not exist off Windows, and the
        # old bare try/except left whatever theme was already active.
        style = self.FakeStyle(("aqua", "clam", "default"))
        light = ui_theme.build_theme("windows", system="windows")
        self.assertEqual(ui_theme.apply_ttk_theme(style, "windows", resolved=light), "default")
        self.assertEqual(style.used, ["default"])

    def test_never_raises_when_ttk_misbehaves(self):
        class Broken:
            def theme_names(self):
                raise RuntimeError("no ttk")

        self.assertIsNone(ui_theme.apply_ttk_theme(Broken(), "windows"))

    def test_manager_styles_define_navigation_and_selection_states(self):
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

        style = Recorder()
        theme = ui_theme.build_theme("windows", system="windows")
        self.assertIs(ui_theme.configure_manager_styles(style, theme), style)
        self.assertEqual(
            style.configured["Manager.TNotebook.Tab"]["padding"], (18, 10)
        )
        self.assertEqual(
            style.mapped["Manager.TNotebook.Tab"]["padding"],
            [("selected", (18, 10))],
        )
        self.assertIn(
            ("selected", theme.accent),
            style.mapped["Manager.TNotebook.Tab"]["foreground"],
        )
        self.assertEqual(
            style.mapped["Manager.Treeview"]["background"],
            [("selected", theme.select_bg)],
        )
        # The sidebar shell's page container draws no tab strip.
        self.assertEqual(style.layouts["Pages.TNotebook.Tab"], [])

    def test_device_combobox_style_has_roomy_font_and_focus_states(self):
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

        style = Recorder()
        theme = ui_theme.build_theme("dark", system="windows")
        ui_theme.configure_manager_styles(style, theme)
        self.assertEqual(style.configured["Device.TCombobox"]["padding"], (10, 8))
        self.assertEqual(style.configured["Device.TCombobox"]["font"], theme.font(10))
        self.assertIn(("focus", theme.field),
                      style.mapped["Device.TCombobox"]["fieldbackground"])
        self.assertIn(("disabled", theme.text_muted),
                      style.mapped["Device.TCombobox"]["foreground"])
        self.assertIn(("focus", theme.focus_ring),
                      style.mapped["Device.TCombobox"]["bordercolor"])

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
        self.assertEqual(options["-font"], theme.font(10))
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
        self.assertEqual(len(calls), 1)
        command, subcommand, style, option, offset = calls[0]
        self.assertEqual((command, subcommand, style, option),
                         ("ttk::style", "configure", "system.Device.TCombobox", "-postoffset"))
        self.assertGreaterEqual(200 + offset[2], 360)

    def test_dark_manager_styles_replace_clam_light_defaults(self):
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

        style = Recorder()
        theme = ui_theme.build_theme("dark", system="windows")
        ui_theme.configure_manager_styles(style, theme)
        self.assertEqual(style.configured["Horizontal.TProgressbar"]["troughcolor"], theme.field)
        self.assertEqual(style.configured["Manager.Treeview"]["bordercolor"], theme.border)
        self.assertEqual(style.configured["Sash"]["background"], theme.surface)
        self.assertIn(("!active", theme.surface_alt),
                      style.mapped["Vertical.TScrollbar"]["background"])
        self.assertEqual(style.mapped["Manager.TNotebook.Tab"]["lightcolor"],
                         [("selected", theme.card)])


class GuiSourceTests(unittest.TestCase):
    """The GUI must go through the seam; a literal here is the bug returning."""

    SOURCE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "snipvoice.pyw"
    )

    def _source(self):
        with open(self.SOURCE, encoding="utf-8") as handle:
            return handle.read()

    def test_no_hardcoded_colors_left_in_the_gui(self):
        import re
        self.assertEqual(re.findall(r'"#[0-9A-Fa-f]{3,8}"', self._source()), [])

    def test_no_windows_only_font_families_left_in_the_gui(self):
        import re
        leaked = re.findall(r'"(Segoe[^"]*|Consolas|Arial|Helvetica)"', self._source())
        self.assertEqual(leaked, [])

    def test_buttons_and_checkboxes_never_take_a_color_directly(self):
        """Aqua draws these itself; their colors must come from the seam.

        A literal ``fg=`` here is invisible on macOS rather than merely
        off-palette: Aqua keeps its own light bezel whatever ``bg`` says, and
        then honours the foreground, so the label vanishes into the bezel.
        """
        import ast
        color_options = {
            "bg", "fg", "background", "foreground", "activebackground",
            "activeforeground", "selectcolor", "disabledforeground",
        }
        offenders = []
        for node in ast.walk(ast.parse(self._source())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and func.attr in ("Button", "Checkbutton")):
                continue
            for keyword in node.keywords:
                if keyword.arg in color_options:
                    offenders.append((node.lineno, func.attr, keyword.arg))
        self.assertEqual(offenders, [])

    def test_the_windows_only_ttk_theme_is_no_longer_forced(self):
        self.assertNotIn('theme_use("vista")', self._source())


if __name__ == "__main__":
    unittest.main()
