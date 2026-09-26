import ast
import os
import string
import sys
import unittest

SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SOURCE_DIR)

import i18n
from i18n_en_us import EN_US


def _placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}


def _source_files():
    for name in sorted(os.listdir(SOURCE_DIR)):
        if name.endswith((".py", ".pyw")) and name != "i18n_en_us.py":
            yield os.path.join(SOURCE_DIR, name)


def _marked_calls():
    """Yield (file, line, name, first argument node, keyword names) for tr()/N_() calls."""
    for path in _source_files():
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name not in {"tr", "N_"} or not node.args:
                continue
            yield (os.path.basename(path), node.lineno, name, node.args[0],
                   {k.arg for k in node.keywords})


def _literal(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class TranslateTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    def test_default_language_returns_source_text(self):
        self.assertEqual(i18n.current_language(), "pt-BR")
        self.assertEqual(i18n.tr("Geral"), "Geral")

    def test_english_uses_catalog_and_formats_values(self):
        i18n.set_language("en-US")
        self.assertEqual(i18n.tr("Geral"), "General")
        self.assertEqual(i18n.tr("Idioma"), "Language")

    def test_unknown_language_falls_back_to_default(self):
        self.assertEqual(i18n.set_language("fr-FR"), "pt-BR")
        self.assertEqual(i18n.set_language(None), "pt-BR")

    def test_literal_braces_survive_without_values(self):
        self.assertEqual(i18n.tr('{"a": 1}'), '{"a": 1}')


class CatalogCoverageTests(unittest.TestCase):
    def test_every_marked_literal_has_an_english_entry(self):
        problems = []
        for filename, line, name, arg, keywords in _marked_calls():
            where = f"{filename}:{line}"
            text = _literal(arg)
            if text is None:
                # tr(variable) translates text marked elsewhere with N_().
                if name == "N_":
                    problems.append(f"{where}: N_() needs a string literal")
                continue
            if text not in EN_US:
                problems.append(f"{where}: missing en-US entry for {text!r}")
            elif keywords and _placeholders(text) != keywords:
                problems.append(f"{where}: placeholders do not match keywords")
        self.assertEqual(problems, [])

    def test_catalog_has_no_stale_entries(self):
        used = {_literal(arg) for _, _, _, arg, _ in _marked_calls()}
        self.assertEqual(sorted(set(EN_US) - used), [])

    def test_translations_keep_placeholders_and_format_specs(self):
        def fields(text):
            return sorted(
                (name, spec, conversion or "")
                for _, name, spec, conversion in string.Formatter().parse(text)
                if name is not None
            )

        mismatched = [key for key, value in EN_US.items() if fields(key) != fields(value)]
        self.assertEqual(mismatched, [])

    def test_translations_are_not_blank(self):
        self.assertEqual([key for key, value in EN_US.items() if not value.strip()], [])


if __name__ == "__main__":
    unittest.main()
