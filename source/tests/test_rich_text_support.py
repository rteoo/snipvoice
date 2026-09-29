import re
import unittest

from rich_text_support import (
    build_html_fragment,
    build_rtf_document,
    extract_plain_text,
    get_clipboard_payload,
    is_rich_text_payload,
    normalize_rich_text_payload,
    normalize_style_spans,
)


# RTF encodes non-ASCII as a signed 16-bit \uN? escape. Any value outside
# [-32768, 32767] is undefined for an RTF reader, so this regex lets tests
# assert the invariant on real generated documents.
_RTF_UNICODE_ESCAPE = re.compile(r"\\u(-?\d+)\?")
_RTF_SIGNED_16_MIN = -32768
_RTF_SIGNED_16_MAX = 32767


def _rtf_unicode_values(rtf):
    return [int(match) for match in _RTF_UNICODE_ESCAPE.findall(rtf)]


class PayloadDetectionTests(unittest.TestCase):
    def test_is_rich_text_payload_accepts_minimal_valid_dict(self):
        self.assertTrue(is_rich_text_payload({"__kind__": "rich_text", "text": ""}))

    def test_is_rich_text_payload_ignores_extra_keys(self):
        value = {"__kind__": "rich_text", "text": "abc", "spans": [], "extra": 1}
        self.assertTrue(is_rich_text_payload(value))

    def test_is_rich_text_payload_rejects_non_dict_and_wrong_kind(self):
        self.assertFalse(is_rich_text_payload("abc"))
        self.assertFalse(is_rich_text_payload(None))
        self.assertFalse(is_rich_text_payload(["__kind__", "rich_text"]))
        self.assertFalse(is_rich_text_payload({"__kind__": "plain", "text": "abc"}))

    def test_is_rich_text_payload_requires_string_text(self):
        self.assertFalse(is_rich_text_payload({"__kind__": "rich_text"}))
        self.assertFalse(is_rich_text_payload({"__kind__": "rich_text", "text": 5}))
        self.assertFalse(is_rich_text_payload({"__kind__": "rich_text", "text": None}))

    def test_extract_plain_text_covers_every_value_kind(self):
        # Consolidates the previous single-case rich-payload test with the
        # documented fallbacks: rich dict -> text, None -> "", anything else
        # -> str(value).
        rich = {"__kind__": "rich_text", "text": "plain value", "spans": []}
        self.assertEqual("plain value", extract_plain_text(rich))
        self.assertEqual("", extract_plain_text(None))
        self.assertEqual("just text", extract_plain_text("just text"))
        self.assertEqual("5", extract_plain_text(5))
        # A dict that is not a rich payload is stringified, not unwrapped.
        self.assertEqual("{'a': 1}", extract_plain_text({"a": 1}))


class SpanNormalizationTests(unittest.TestCase):
    def test_drops_zero_length_negative_and_inverted_spans(self):
        spans = [
            {"tag": "bold", "start": 2, "end": 2},   # zero length
            {"tag": "bold", "start": -1, "end": 3},  # negative start
            {"tag": "bold", "start": 4, "end": 1},   # start > end
        ]
        self.assertEqual([], normalize_style_spans(spans, 10))

    def test_drops_unknown_tag(self):
        self.assertEqual([], normalize_style_spans([{"tag": "blink", "start": 0, "end": 3}], 10))

    def test_drops_non_integer_bounds(self):
        spans = [
            {"tag": "bold", "start": 1.5, "end": 3},
            {"tag": "bold", "start": "0", "end": 3},
            {"tag": "bold", "start": None, "end": 3},
            {"tag": "bold", "start": 0, "end": None},
        ]
        self.assertEqual([], normalize_style_spans(spans, 10))

    def test_skips_non_dict_and_none_entries(self):
        spans = [None, "bold", 42, ["bold", 0, 3], {"tag": "bold", "start": 0, "end": 3}]
        self.assertEqual(
            [{"tag": "bold", "start": 0, "end": 3}],
            normalize_style_spans(spans, 10),
        )

    def test_none_spans_yields_empty(self):
        self.assertEqual([], normalize_style_spans(None, 10))

    def test_clips_end_to_text_length_and_drops_start_beyond_text(self):
        spans = [
            {"tag": "bold", "start": 0, "end": 100},   # clipped to text_length
            {"tag": "italic", "start": 5, "end": 8},   # start == text_length -> dropped
            {"tag": "code", "start": 8, "end": 12},    # start > text_length -> dropped
        ]
        self.assertEqual(
            [{"tag": "bold", "start": 0, "end": 5}],
            normalize_style_spans(spans, 5),
        )

    def test_all_spans_dropped_on_empty_text(self):
        spans = [{"tag": "bold", "start": 0, "end": 4}]
        self.assertEqual([], normalize_style_spans(spans, 0))

    def test_no_clipping_when_text_length_is_none(self):
        spans = [{"tag": "bold", "start": 0, "end": 999}]
        self.assertEqual(
            [{"tag": "bold", "start": 0, "end": 999}],
            normalize_style_spans(spans, None),
        )

    def test_output_is_sorted_by_start_then_end_then_tag(self):
        spans = [
            {"tag": "italic", "start": 5, "end": 9},
            {"tag": "bold", "start": 0, "end": 3},
            {"tag": "underline", "start": 0, "end": 3},
            {"tag": "code", "start": 0, "end": 2},
        ]
        result = normalize_style_spans(spans, 20)
        self.assertEqual(
            [(0, 2, "code"), (0, 3, "bold"), (0, 3, "underline"), (5, 9, "italic")],
            [(item["start"], item["end"], item["tag"]) for item in result],
        )

    def test_only_tag_start_end_keys_are_kept(self):
        span = {"tag": "bold", "start": 0, "end": 3, "color": "red", "extra": True}
        self.assertEqual(
            [{"tag": "bold", "start": 0, "end": 3}],
            normalize_style_spans([span], 10),
        )


class HtmlGenerationTests(unittest.TestCase):
    def test_empty_text_yields_empty_div(self):
        self.assertEqual("<div></div>", build_html_fragment("", []))

    def test_plain_text_is_wrapped_without_tags(self):
        self.assertEqual("<div>hello</div>", build_html_fragment("hello", []))

    def test_html_special_characters_are_escaped(self):
        fragment = build_html_fragment("<a> & \"x\" 'y'", [])
        self.assertIn("&lt;a&gt;", fragment)
        self.assertIn("&amp;", fragment)
        self.assertIn("&quot;x&quot;", fragment)
        self.assertIn("&#x27;y&#x27;", fragment)
        self.assertNotIn("<a>", fragment)

    def test_escaping_happens_before_style_wrapping(self):
        fragment = build_html_fragment("<b>", [{"tag": "bold", "start": 0, "end": 3}])
        self.assertEqual("<div><strong>&lt;b&gt;</strong></div>", fragment)

    def test_newline_becomes_break_tag(self):
        self.assertEqual("<div>a<br>b</div>", build_html_fragment("a\nb", []))

    def test_style_nesting_order_is_stable(self):
        # bold+italic always nests <strong><em>...; the fixed order guarantees a
        # deterministic, well-formed tag stack.
        fragment = build_html_fragment("x", [{"tag": "bold", "start": 0, "end": 1}, {"tag": "italic", "start": 0, "end": 1}])
        self.assertEqual("<div><strong><em>x</em></strong></div>", fragment)

    def test_nested_span_within_wider_span(self):
        fragment = build_html_fragment(
            "abcdef",
            [{"tag": "bold", "start": 0, "end": 6}, {"tag": "italic", "start": 2, "end": 4}],
        )
        self.assertEqual(
            "<div><strong>ab</strong><strong><em>cd</em></strong><strong>ef</strong></div>",
            fragment,
        )

    def test_overlapping_same_tag_spans_merge_into_one_run(self):
        fragment = build_html_fragment(
            "abcdef",
            [{"tag": "bold", "start": 0, "end": 3}, {"tag": "bold", "start": 2, "end": 6}],
        )
        self.assertEqual("<div><strong>abcdef</strong></div>", fragment)
        self.assertEqual(1, fragment.count("<strong>"))

    def test_astral_span_uses_codepoint_offsets(self):
        # A span over an astral emoji works because Python string offsets are
        # code points, not UTF-16 units; the emoji is length 1.
        fragment = build_html_fragment("a\U0001F600b", [{"tag": "bold", "start": 1, "end": 2}])
        self.assertEqual("<div>a<strong>\U0001F600</strong>b</div>", fragment)

    def test_combining_characters_are_preserved(self):
        # "e" + combining acute accent: two code points, styled independently.
        fragment = build_html_fragment("é", [{"tag": "bold", "start": 0, "end": 2}])
        self.assertEqual("<div><strong>é</strong></div>", fragment)


class RtfGenerationTests(unittest.TestCase):
    def test_document_has_header_font_table_and_closes(self):
        rtf = build_rtf_document("hello", [])
        self.assertTrue(rtf.startswith(r"{\rtf1\ansi\deff0"))
        self.assertIn(r"{\fonttbl{\f0 Arial;}{\f1 Consolas;}}", rtf)
        self.assertTrue(rtf.endswith("}"))

    def test_backslash_and_braces_are_escaped(self):
        rtf = build_rtf_document("a\\b{c}d", [])
        self.assertIn(r"a\\b\{c\}d", rtf)

    def test_newline_becomes_par(self):
        rtf = build_rtf_document("a\nb", [])
        self.assertIn(r"\par ", rtf)
        self.assertIn("\n", rtf)

    def test_bmp_non_ascii_uses_unsigned_escape(self):
        rtf = build_rtf_document("é", [])  # e-acute, codepoint 233
        self.assertIn(r"\u233?", rtf)

    def test_high_bmp_codepoint_uses_negative_signed_escape(self):
        # U+F900 (63744) must wrap to the negative signed-16 form -1792, not
        # the raw unsigned value which an RTF reader would misread.
        rtf = build_rtf_document("豈", [])
        self.assertIn(r"\u-1792?", rtf)

    def test_style_control_words_present_for_each_bit(self):
        rtf = build_rtf_document(
            "abcde",
            [
                {"tag": "bold", "start": 0, "end": 1},
                {"tag": "italic", "start": 1, "end": 2},
                {"tag": "underline", "start": 2, "end": 3},
                {"tag": "code", "start": 3, "end": 4},
                {"tag": "strike", "start": 4, "end": 5},
            ],
        )
        for control in (r"\b ", r"\i ", r"\ul ", r"\f1 ", r"\strike "):
            self.assertIn(control, rtf)

    def test_astral_codepoint_emits_in_range_surrogate_pair_escapes(self):
        # REGRESSION: a single subtraction of 65536 left astral chars such as
        # U+1F600 (128512) encoded as 62976 -- above the signed-16 maximum
        # 32767, which RTF readers misrender. The correct encoding is the
        # UTF-16 surrogate pair as two in-range \uN? escapes.
        rtf = build_rtf_document("\U0001F600", [])
        values = _rtf_unicode_values(rtf)
        # U+1F600 -> UTF-16 D83D DE00 -> signed units -10179, -8704.
        self.assertEqual([-10179, -8704], values)
        for value in values:
            self.assertGreaterEqual(value, _RTF_SIGNED_16_MIN)
            self.assertLessEqual(
                value,
                _RTF_SIGNED_16_MAX,
                msg=f"astral char produced out-of-range RTF unicode escape {value}",
            )


class BuildPayloadTests(unittest.TestCase):
    def test_builds_html_and_rtf_with_expected_nesting(self):
        payload = normalize_rich_text_payload(
            {
                "__kind__": "rich_text",
                "text": "hello world",
                "spans": [
                    {"tag": "bold", "start": 0, "end": 5},
                    {"tag": "code", "start": 6, "end": 11},
                    {"tag": "strike", "start": 6, "end": 11},
                ],
            }
        )
        self.assertTrue(is_rich_text_payload(payload))
        self.assertEqual("hello world", payload["text"])
        self.assertIn("<strong>hello</strong>", payload["html"])
        self.assertIn("<s><code>world</code></s>", payload["html"])
        self.assertIn(r"\b ", payload["rtf"])
        self.assertIn(r"\f1 ", payload["rtf"])
        self.assertIn(r"\strike ", payload["rtf"])


class NormalizePayloadTests(unittest.TestCase):
    def test_backfills_missing_formats(self):
        payload = normalize_rich_text_payload(
            {"__kind__": "rich_text", "text": "abc", "spans": [{"tag": "italic", "start": 0, "end": 3}]}
        )
        self.assertIn("html", payload)
        self.assertIn("rtf", payload)
        self.assertIn("<em>abc</em>", payload["html"])

    def test_empty_html_is_rebuilt_from_spans(self):
        payload = normalize_rich_text_payload(
            {
                "__kind__": "rich_text",
                "text": "abc",
                "spans": [{"tag": "bold", "start": 0, "end": 3}],
                "html": "",
                "rtf": "",
            }
        )
        self.assertIn("<strong>abc</strong>", payload["html"])
        self.assertIn(r"\b ", payload["rtf"])

    def test_existing_html_is_trusted_and_not_recomputed(self):
        # normalize only backfills; it never re-derives a present html/rtf. A
        # consumer that edits text must rebuild html/rtf itself, not normalize.
        payload = normalize_rich_text_payload(
            {
                "__kind__": "rich_text",
                "text": "abc",
                "spans": [{"tag": "bold", "start": 0, "end": 3}],
                "html": "<div>STALE</div>",
                "rtf": "{stale}",
            }
        )
        self.assertEqual("<div>STALE</div>", payload["html"])
        self.assertEqual("{stale}", payload["rtf"])

    def test_normalizes_stored_spans(self):
        payload = normalize_rich_text_payload(
            {
                "__kind__": "rich_text",
                "text": "abc",
                "spans": [{"tag": "bold", "start": 0, "end": 999}, {"tag": "blink", "start": 0, "end": 1}],
            }
        )
        self.assertEqual([{"tag": "bold", "start": 0, "end": 3}], payload["spans"])

    def test_drops_extra_keys(self):
        payload = normalize_rich_text_payload(
            {"__kind__": "rich_text", "text": "abc", "spans": [], "note": "extra"}
        )
        self.assertNotIn("note", payload)
        self.assertEqual({"__kind__", "text", "spans", "html", "rtf"}, set(payload))

    def test_non_rich_value_is_returned_unchanged(self):
        self.assertEqual("plain", normalize_rich_text_payload("plain"))
        self.assertIsNone(normalize_rich_text_payload(None))
        # __kind__ present but text missing -> not a rich payload -> passthrough.
        broken = {"__kind__": "rich_text", "spans": []}
        self.assertIs(broken, normalize_rich_text_payload(broken))


class ClipboardPayloadTests(unittest.TestCase):
    def test_plain_text_yields_text_only(self):
        self.assertEqual({"text": "just text"}, get_clipboard_payload("just text"))

    def test_none_yields_empty_text(self):
        self.assertEqual({"text": ""}, get_clipboard_payload(None))

    def test_rich_payload_includes_html_and_rtf(self):
        payload = get_clipboard_payload(
            {
                "__kind__": "rich_text",
                "text": "abc",
                "spans": [{"tag": "underline", "start": 0, "end": 3}, {"tag": "strike", "start": 0, "end": 3}],
            }
        )
        self.assertEqual("abc", payload["text"])
        self.assertIn("<u><s>abc</s></u>", payload["html"])
        self.assertIn("rtf", payload)

    def test_missing_formats_are_backfilled_for_clipboard(self):
        payload = get_clipboard_payload(
            {"__kind__": "rich_text", "text": "abc", "spans": [{"tag": "bold", "start": 0, "end": 3}]}
        )
        self.assertIn("<strong>abc</strong>", payload["html"])
        self.assertIn(r"\b ", payload["rtf"])


class MalformedInputTests(unittest.TestCase):
    def test_string_spans_are_ignored_gracefully(self):
        # A string is iterable; each char is a non-dict and is skipped.
        payload = normalize_rich_text_payload(
            {"__kind__": "rich_text", "text": "abc", "spans": "not-a-list"}
        )
        self.assertEqual([], payload["spans"])

    def test_dict_spans_are_ignored_gracefully(self):
        payload = normalize_rich_text_payload(
            {"__kind__": "rich_text", "text": "abc", "spans": {"tag": "bold"}}
        )
        self.assertEqual([], payload["spans"])

    def test_non_iterable_spans_are_sanitized_instead_of_crashing(self):
        # REGRESSION: a truthy non-iterable spans value (e.g. an integer from a
        # corrupt/hand-edited snippet) used to raise TypeError on the live
        # paste path via get_clipboard_payload. It must degrade to "no spans"
        # like every other malformed spans shape (None, str, dict, junk list).
        payload = get_clipboard_payload(
            {"__kind__": "rich_text", "text": "abc", "spans": 5}
        )
        self.assertEqual("abc", payload["text"])


if __name__ == "__main__":
    unittest.main()
