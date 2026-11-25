"""Fact-key normalisation, value equality, body normalisation, IDs and timestamps (spec 2.2)."""
from __future__ import annotations

import random
import unittest

from recall_v2.core import normalise, timefmt
from recall_v2.core.errors import RecallError
from recall_v2.core.model import encode_ulid


class NormTests(unittest.TestCase):
    def test_nfkc_casefold_whitespace_trim(self):
        cases = {
            "Service X": "service x",
            "  Service\t\n  X  ": "service x",
            "ＳＥＲＶＩＣＥ": "service",          # fullwidth -> NFKC
            "Straße": "strasse",                   # casefold
            "ﬁle": "file",                         # ligature -> NFKC
            "a  b": "a b",               # Unicode spaces collapse
            "x　　y": "x y",
            "Ⅻ": "xii",                            # roman numeral compatibility form
            "\u00df\u0301": "s\u015b",              # casefold output recomposed (idempotence)
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalise.norm(raw), expected)

    def test_rejects_control_format_empty_and_oversize(self):
        for bad in ("", "   ", "\t\n", "a\u0000b", "a\u001fb", "a​b", "a‮b", "a\u0007"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(RecallError) as raised:
                    normalise.norm(bad)
                self.assertEqual(raised.exception.code, "invalid_input")
        self.assertEqual(len(normalise.norm("a" * 256)), 256)
        with self.assertRaises(RecallError):
            normalise.norm("a" * 257)
        with self.assertRaises(RecallError):
            normalise.norm("é" * 129)  # 258 bytes after normalising
        with self.assertRaises(RecallError):
            normalise.norm(None)

    def test_norm_is_idempotent_on_random_text(self):
        rng = random.Random(20261125)
        alphabet = "aAßẞﬁＡｚ \t 　éé́Ωω½Ⅻİıǅ"
        for _ in range(2000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 20)))
            try:
                once = normalise.norm(text)
            except RecallError:
                continue
            self.assertEqual(normalise.norm(once), once, repr(text))

    def test_fact_key(self):
        key = normalise.fact_key("  Service-X ", "OWNER")
        self.assertEqual(key, "service-x\u001fowner")
        self.assertEqual(normalise.split_fact_key(key), ("service-x", "owner"))
        self.assertEqual(normalise.fact_key("service x", "owner"), normalise.fact_key("SERVICE   X", "Owner"))
        self.assertNotEqual(normalise.fact_key("a b", "c"), normalise.fact_key("a", "b c"))

    def test_value_equality_is_canonical_nfc_and_case_sensitive(self):
        self.assertTrue(normalise.values_equal({"b": 1, "a": "é"}, {"a": "é", "b": 1}))
        self.assertFalse(normalise.values_equal("Ann", "ann"))
        self.assertEqual(normalise.canonical_value({"z": [1, "é"], "a": None}), '{"a":null,"z":[1,"é"]}')
        with self.assertRaises(RecallError):
            normalise.canonical_value(float("nan"))

    def test_body_normalisation(self):
        self.assertEqual(normalise.normalise_body("  a  \r\nb \rc  \n\n"), "a\nb\nc")
        self.assertEqual(normalise.body_hash("x \r\n"), normalise.body_hash("x"))

    def test_identifier_rules(self):
        normalise.check_id("alpha.1_x-y", normalise.PRINCIPAL_ID, "principal")
        for bad in ("-a", "", "a" * 65, "a b", "a:b"):
            with self.assertRaises(RecallError):
                normalise.check_id(bad, normalise.PRINCIPAL_ID, "principal")
        normalise.check_id("task:migrate-db", normalise.ITEM_KEY, "key")
        ulid = encode_ulid(1_700_000_000_000, bytes(range(10)))
        self.assertEqual(len(ulid), 26)
        self.assertIsNotNone(normalise.ULID.fullmatch(ulid))


class TimeTests(unittest.TestCase):
    def test_round_trip(self):
        rng = random.Random(7)
        for _ in range(500):
            micros = rng.randint(0, 4_000_000_000_000_000)
            text = timefmt.format_rfc3339(micros)
            self.assertRegex(text, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
            self.assertEqual(timefmt.parse_rfc3339(text), micros)

    def test_offsets_convert_to_utc(self):
        self.assertEqual(timefmt.parse_rfc3339("2026-11-25T10:30:00.000000+01:00"),
                         timefmt.parse_rfc3339("2026-11-25T09:30:00.000000Z"))
        self.assertEqual(timefmt.format_rfc3339(timefmt.parse_rfc3339("2026-11-25T09:30:00-02:30")),
                         "2026-11-25T12:00:00.000000Z")
        self.assertEqual(timefmt.parse_rfc3339("1970-01-01T00:00:00.000001Z"), 1)

    def test_rejects_naive_and_seven_digit_input(self):
        for bad in ("2026-11-25T09:30:00.000000", "2026-11-25T09:30:00.0000000Z", "2026-11-25 09:30:00Z",
                    "2026-13-01T00:00:00Z", "2026-11-25T24:00:00Z", "2026-11-25T09:30:60Z", "", "yesterday"):
            with self.subTest(bad=bad):
                with self.assertRaises(RecallError) as raised:
                    timefmt.parse_rfc3339(bad)
                self.assertEqual(raised.exception.code, "invalid_input")


if __name__ == "__main__":
    unittest.main()
