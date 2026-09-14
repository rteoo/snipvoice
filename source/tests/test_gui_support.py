import unittest
from unittest import mock

from gui_support import (
    filter_static_snippets,
    focus_modal_input,
    iter_filtered_mapping_items,
    snippet_row_values,
)
import gui_support


class GuiSupportTests(unittest.TestCase):
    def test_modal_input_reveals_and_focuses_only_after_activation(self):
        events = []
        dialog = mock.Mock()
        initial_widget = mock.Mock()
        submit = mock.Mock(side_effect=lambda callback: callback(object()))
        dialog.attributes.side_effect = (
            lambda name, value: events.append((name, value))
        )
        dialog.deiconify.side_effect = lambda: events.append("deiconify")
        dialog.wait_visibility.side_effect = lambda: events.append("visible")
        dialog.lift.side_effect = lambda: events.append("lift")
        initial_widget.focus_force.side_effect = lambda: events.append("focus")
        cancel_activation = mock.Mock()
        cancel_key_focus = mock.Mock()

        with mock.patch.object(
            gui_support.platform_support,
            "activate_application_when_ready",
            return_value=cancel_activation,
        ) as activate, mock.patch.object(
            gui_support.platform_support,
            "focus_tk_window_when_ready",
            return_value=cancel_key_focus,
        ) as focus_native, mock.patch.object(
            gui_support.platform_support, "IS_MAC", True
        ):
            cancel = focus_modal_input(dialog, initial_widget, submit)
            self.assertEqual(
                [("-alpha", 0.0), "deiconify", "visible", "lift"],
                events,
            )
            focus_native.assert_not_called()
            activate.call_args.args[0]()
            focus_native.call_args.args[2]()

        self.assertEqual(
            [
                ("-alpha", 0.0),
                "deiconify",
                "visible",
                "lift",
                ("-alpha", 1.0),
                "lift",
                "focus",
            ],
            events,
        )
        cancel()
        cancel_activation.assert_called_once_with()
        cancel_key_focus.assert_called_once_with()

    def test_cancelled_modal_drops_a_queued_reveal(self):
        dialog = mock.Mock()
        initial_widget = mock.Mock()
        queued = []
        cancel_native = mock.Mock()

        with mock.patch.object(
            gui_support.platform_support,
            "activate_application_when_ready",
            side_effect=lambda on_active, _on_failed: on_active() or cancel_native,
        ), mock.patch.object(
            gui_support.platform_support,
            "focus_tk_window_when_ready",
        ), mock.patch.object(gui_support.platform_support, "IS_MAC", True):
            cancel = focus_modal_input(
                dialog,
                initial_widget,
                lambda callback: queued.append(callback),
            )

        cancel()
        queued[0](object())
        initial_widget.focus_force.assert_not_called()
        dialog.attributes.assert_called_once_with("-alpha", 0.0)
        cancel_native.assert_called_once_with()

    def test_modal_without_an_input_focuses_the_dialog_itself(self):
        dialog = mock.Mock()

        with mock.patch.object(
            gui_support.platform_support,
            "activate_application_when_ready",
            side_effect=lambda on_active, _on_failed: on_active() or (lambda: None),
        ), mock.patch.object(
            gui_support.platform_support,
            "focus_tk_window_when_ready",
            side_effect=lambda _dialog, _target, on_key, _on_failed: (
                on_key(),
                (lambda: None),
            )[-1],
        ):
            cancel = focus_modal_input(
                dialog,
                None,
                lambda callback: callback(object()),
            )

        dialog.focus_force.assert_called_once_with()
        cancel()

    def test_native_key_failure_destroys_hidden_dialog_and_raises(self):
        dialog = mock.Mock()
        initial_widget = mock.Mock()

        with mock.patch.object(
            gui_support.platform_support,
            "activate_application_when_ready",
            side_effect=lambda on_active, _on_failed: on_active() or (lambda: None),
        ), mock.patch.object(
            gui_support.platform_support,
            "focus_tk_window_when_ready",
            side_effect=lambda _dialog, _target, _on_key, on_failed: (
                on_failed("native key failed"),
                (lambda: None),
            )[-1],
        ):
            cancel = focus_modal_input(
                dialog,
                initial_widget,
                lambda callback: callback(object()),
            )

        dialog.destroy.assert_called_once_with()
        with self.assertRaisesRegex(RuntimeError, "native key failed"):
            cancel()

    def test_filter_static_snippets_without_query_keeps_only_persisted_static_entries(self):
        snippets = {
            "xname": "Alex",
            "_cpf_numbers": {"fulano": "123"},
            "xdyn": lambda: "value",
        }

        self.assertEqual({"xname": "Alex"}, filter_static_snippets(snippets, ""))

    def test_filter_static_snippets_matches_key_and_value(self):
        snippets = {
            "xname": "Alex",
            "xemail": "contato@example.com",
        }

        self.assertEqual({"xemail": "contato@example.com"}, filter_static_snippets(snippets, "example"))
        self.assertEqual({"xname": "Alex"}, filter_static_snippets(snippets, "name"))

    def test_filter_static_snippets_matches_rich_text_plain_value(self):
        snippets = {
            "xsig": {
                "__kind__": "rich_text",
                "text": "Assinatura principal",
                "spans": [],
            }
        }

        self.assertEqual({"xsig": snippets["xsig"]}, filter_static_snippets(snippets, "assinatura"))

    def test_iter_filtered_mapping_items_ignores_prefix_metadata(self):
        mapping = {
            "__prefix__": "clw",
            "gtw": "gateway",
            "api": "api server",
        }

        self.assertEqual(["api", "gtw"], iter_filtered_mapping_items(mapping, ""))
        self.assertEqual(["gtw"], iter_filtered_mapping_items(mapping, "gate"))


class FilterEdgeCaseTests(unittest.TestCase):
    """Adversarial queries: whitespace, unicode/accents, regex metacharacters."""

    def test_whitespace_only_query_behaves_like_an_empty_query(self):
        snippets = {"xname": "Alex", "xemail": "a@b.com"}
        self.assertEqual(snippets, filter_static_snippets(snippets, "   \t "))

    def test_query_is_a_literal_substring_not_a_regex(self):
        # A blind re.search would treat "(net)" as a group and ".*" as match-all;
        # the filter must compare literally.
        snippets = {"xa": "Total (net) due", "xb": "plain text"}
        self.assertEqual({"xa": snippets["xa"]}, filter_static_snippets(snippets, "(net)"))
        self.assertEqual({}, filter_static_snippets(snippets, ".*"))

    def test_regex_metacharacter_query_does_not_raise(self):
        # An unbalanced bracket would blow up re.compile; str.__contains__ is safe.
        snippets = {"xa": "array[0] value"}
        self.assertEqual({"xa": snippets["xa"]}, filter_static_snippets(snippets, "["))

    def test_matching_is_case_insensitive_but_accent_sensitive(self):
        snippets = {"xinsc": "Inscrição estadual", "xother": "Cadastro"}
        # Case folds: an all-caps accented query still matches.
        self.assertEqual({"xinsc": snippets["xinsc"]}, filter_static_snippets(snippets, "INSCRIÇÃO"))
        # Accents are not folded: the de-accented spelling does not match.
        self.assertEqual({}, filter_static_snippets(snippets, "inscricao"))

    def test_unicode_key_match_is_case_insensitive(self):
        snippets = {"xcafé": "espresso"}
        self.assertEqual(snippets, filter_static_snippets(snippets, "CAFÉ"))

    def test_mapping_filter_on_non_dict_returns_empty(self):
        self.assertEqual([], iter_filtered_mapping_items(None, ""))
        self.assertEqual([], iter_filtered_mapping_items("not a mapping", "x"))

    def test_mapping_filter_matches_rich_text_plain_value(self):
        mapping = {
            "__prefix__": "c",
            "a": {"__kind__": "rich_text", "text": "Contrato assinado", "spans": []},
            "b": "outro",
        }
        self.assertEqual(["a"], iter_filtered_mapping_items(mapping, "assinado"))

    def test_mapping_filter_query_is_literal_and_accent_sensitive(self):
        mapping = {"__prefix__": "c", "opcao": "Opção válida", "outro": "texto"}
        self.assertEqual(["opcao"], iter_filtered_mapping_items(mapping, "OPÇÃO"))
        self.assertEqual([], iter_filtered_mapping_items(mapping, "opcao válida"))


class SnippetRowValuesTests(unittest.TestCase):
    def test_plain_snippet_has_no_markers(self):
        self.assertEqual(("xname", "Alex", ""), snippet_row_values("xname", "Alex"))

    def test_newlines_and_runs_of_space_collapse(self):
        _, preview, _ = snippet_row_values("xsig", "linha um\n\nlinha  dois\tfim")

        self.assertEqual("linha um linha dois fim", preview)

    def test_long_preview_is_truncated_with_ellipsis(self):
        _, preview, _ = snippet_row_values("xlong", "a" * 200, preview_chars=10)

        self.assertEqual(10, len(preview))
        self.assertTrue(preview.endswith("…"))

    def test_rich_text_payload_is_marked(self):
        value = {"__kind__": "rich_text", "text": "Assinatura", "spans": []}

        self.assertEqual(("xsig", "Assinatura", "RT"), snippet_row_values("xsig", value))

    def test_variable_bearing_snippet_is_marked(self):
        _, _, markers = snippet_row_values("xhello", "Olá %%nome%%, tudo bem?")

        self.assertEqual("%%", markers)

    def test_rich_text_with_variables_gets_both_markers(self):
        value = {"__kind__": "rich_text", "text": "Olá %%nome%%", "spans": []}

        self.assertEqual("RT %%", snippet_row_values("xboth", value)[2])

    def test_value_is_not_mutated(self):
        value = {"__kind__": "rich_text", "text": "Assinatura", "spans": []}
        snapshot = dict(value)

        snippet_row_values("xsig", value)

        self.assertEqual(snapshot, value)


if __name__ == "__main__":
    unittest.main()
