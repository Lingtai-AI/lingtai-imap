"""Regression tests for the IMAP search DSL (issue #2).

A bare natural-language query like ``IAU video`` used to fall through
every recognised key:value branch and silently compile to ``[b"ALL"]``,
returning every message in the folder. Bare tokens should compile to
TEXT criteria so the server actually searches for them.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lingtai_imap.account import IMAPAccount  # noqa: E402


class BuildSearchCriteriaTests(unittest.TestCase):
    def _build(self, query: str) -> list[bytes]:
        return IMAPAccount._build_search_criteria(query)

    def test_known_keys_still_compile_unchanged(self):
        self.assertEqual(
            self._build("from:alice@example.com"),
            [b"FROM", b"alice@example.com"],
        )
        self.assertEqual(
            self._build("subject:hello unseen"),
            [b"SUBJECT", b"hello", b"UNSEEN"],
        )

    def test_bare_token_is_text_search_not_all(self):
        criteria = self._build("IAU")
        self.assertNotEqual(
            criteria, [b"ALL"],
            "bare token must not silently fall back to ALL",
        )
        self.assertIn(b"TEXT", criteria)
        self.assertIn(b"IAU", criteria)

    def test_multiple_bare_tokens_are_anded(self):
        criteria = self._build("IAU video")
        self.assertNotEqual(criteria, [b"ALL"])
        # Each bare token contributes one TEXT clause; IMAP AND-s adjacent
        # criteria implicitly.
        self.assertEqual(criteria.count(b"TEXT"), 2)
        self.assertIn(b"IAU", criteria)
        self.assertIn(b"video", criteria)

    def test_mixed_keys_and_bare_tokens(self):
        criteria = self._build("from:bob video unseen")
        self.assertIn(b"FROM", criteria)
        self.assertIn(b"bob", criteria)
        self.assertIn(b"TEXT", criteria)
        self.assertIn(b"video", criteria)
        self.assertIn(b"UNSEEN", criteria)

    def test_quoted_bare_phrase_is_one_text_clause(self):
        # A bare quoted phrase like `"IAU video"` should be one TEXT clause.
        criteria = self._build('"IAU video"')
        self.assertEqual(criteria.count(b"TEXT"), 1)
        self.assertIn(b"IAU video", criteria)

    def test_empty_query_still_returns_all(self):
        # Truly empty input is the one legitimate ALL case — preserves
        # the existing "show me everything" affordance.
        self.assertEqual(self._build(""), [b"ALL"])
        self.assertEqual(self._build("   "), [b"ALL"])


if __name__ == "__main__":
    unittest.main()
