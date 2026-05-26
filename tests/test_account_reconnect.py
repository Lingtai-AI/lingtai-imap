"""Regression tests for stale tool-call IMAP connection handling (issue #2).

Covers:
  * ``_ensure_connected()`` discards a cached client whose ``noop()`` raises
    and returns a fresh client instead.
  * A transient socket/SSL/IMAP-abort error on a tool-call op (search,
    fetch_envelopes) is handled by reconnecting and retrying once before
    surfacing the result.
  * Persistent failures still propagate after one retry — no infinite loop.
"""
from __future__ import annotations

import imaplib
import socket
import ssl
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure src/ on path for ``python -m unittest discover`` (pytest uses conftest).
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lingtai_imap.account import IMAPAccount  # noqa: E402


class _FakeIMAPClient:
    """Minimal stand-in for imapclient.IMAPClient.

    Records calls and can be programmed to raise on specific methods to
    simulate a stale socket.
    """

    def __init__(self, host=None, port=None, ssl=True):
        self.host = host
        self.port = port
        self.ssl = ssl
        self.logged_in = False
        self.logged_out = False
        self.calls: list[tuple[str, tuple, dict]] = []
        # Programmable side-effects keyed by method name → list of
        # exceptions (popped FIFO). Empty/missing → success.
        self.raises: dict[str, list] = {}
        self._capabilities = (b"IMAP4REV1", b"IDLE", b"MOVE", b"UIDPLUS")
        self._folders = [((b"\\HasNoChildren",), b"/", "INBOX")]
        self._search_result: list[int] = [1, 2, 3]

    def _maybe_raise(self, method: str) -> None:
        queue = self.raises.get(method)
        if queue:
            exc = queue.pop(0)
            raise exc

    def _record(self, method: str, *args, **kwargs) -> None:
        self.calls.append((method, args, kwargs))

    def login(self, user, password):
        self._record("login", user, password)
        self._maybe_raise("login")
        self.logged_in = True

    def logout(self):
        self._record("logout")
        self.logged_out = True

    def capabilities(self):
        self._record("capabilities")
        self._maybe_raise("capabilities")
        return self._capabilities

    def list_folders(self):
        self._record("list_folders")
        self._maybe_raise("list_folders")
        return self._folders

    def select_folder(self, folder, readonly=False):
        self._record("select_folder", folder, readonly=readonly)
        self._maybe_raise("select_folder")

    def search(self, criteria):
        self._record("search", criteria)
        self._maybe_raise("search")
        return list(self._search_result)

    def fetch(self, uids, parts):
        self._record("fetch", uids, parts)
        self._maybe_raise("fetch")
        return {}

    def noop(self):
        self._record("noop")
        self._maybe_raise("noop")


class _ClientFactory:
    """Manages the IMAPClient patch so tests can swap behavior mid-run."""

    def __init__(self, prepare=None):
        self.created: list[_FakeIMAPClient] = []
        self.prepare = prepare or (lambda client, idx: None)
        self._patcher = patch(
            "lingtai_imap.account.IMAPClient", side_effect=self._build,
        )

    def __enter__(self):
        self._patcher.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._patcher.stop()

    def _build(self, host, port=None, ssl=True):
        client = _FakeIMAPClient(host, port=port, ssl=ssl)
        self.prepare(client, len(self.created))
        self.created.append(client)
        return client


def _make_account() -> IMAPAccount:
    return IMAPAccount(
        email_address="user@example.com",
        email_password="secret",
        imap_host="imap.example.com",
    )


class StaleConnectionTests(unittest.TestCase):
    def test_ensure_connected_discards_dead_client_and_reconnects(self):
        """A cached client whose NOOP raises must be discarded and replaced."""
        with _ClientFactory() as factory:
            acct = _make_account()
            acct.connect()
            self.assertEqual(len(factory.created), 1)
            first = factory.created[0]

            # Simulate the socket dying server-side: next NOOP raises.
            first.raises["noop"] = [
                ssl.SSLError(
                    "EOF occurred in violation of protocol (_ssl.c:2501)",
                ),
            ]

            client = acct._ensure_connected()

            self.assertIsNot(client, first, "expected a brand-new IMAPClient")
            self.assertEqual(
                len(factory.created), 2,
                "expected exactly one reconnect",
            )
            self.assertIs(client, factory.created[1])
            self.assertTrue(factory.created[1].logged_in)

    def test_search_retries_once_on_transient_ssl_error(self):
        """search() must reconnect+retry once when a transient SSL error fires."""
        with _ClientFactory() as factory:
            acct = _make_account()
            acct.connect()
            first = factory.created[0]
            # The first attempt's search() will raise the canonical Gmail
            # error.
            first.raises["search"] = [
                ssl.SSLError(
                    "EOF occurred in violation of protocol (_ssl.c:2501)",
                ),
            ]
            # If the retry path probes NOOP on the dead client, fail it
            # too so the code is forced to reconnect.
            first.raises["noop"] = [socket.error("broken pipe")]

            uids = acct.search("INBOX", "from:alice@example.com")

            self.assertEqual(uids, ["1", "2", "3"])
            self.assertEqual(
                len(factory.created), 2,
                f"expected one reconnect, got {len(factory.created)}",
            )
            second = factory.created[1]
            search_calls = [c for c in second.calls if c[0] == "search"]
            self.assertEqual(len(search_calls), 1)

    def test_search_does_not_retry_indefinitely(self):
        """Persistent failure surfaces after a bounded number of attempts."""

        def prepare(client, idx):
            # Every client raises SSLError on search and on NOOP, so any
            # liveness probe also fails.
            client.raises["search"] = [ssl.SSLError(f"EOF #{idx}")]
            client.raises["noop"] = [socket.error("broken pipe")]

        with _ClientFactory(prepare=prepare) as factory:
            acct = _make_account()
            acct.connect()
            with self.assertRaises((ssl.SSLError, OSError)):
                acct.search("INBOX", "from:alice@example.com")
            # No infinite loop: each call attempts at most one liveness
            # reconnect plus one retry-after-error reconnect, so the
            # total number of IMAPClient constructions is small and
            # bounded — never more than 3 for a single tool action.
            self.assertLessEqual(
                len(factory.created), 3,
                f"too many reconnects: {len(factory.created)}",
            )

    def test_fetch_envelopes_retries_once_on_imap_abort(self):
        """fetch_envelopes() must recover from imaplib.IMAP4.abort."""
        with _ClientFactory() as factory:
            acct = _make_account()
            acct.connect()
            first = factory.created[0]
            first.raises["select_folder"] = [imaplib.IMAP4.abort("BYE")]
            first.raises["noop"] = [socket.error("broken pipe")]

            envelopes = acct.fetch_envelopes("INBOX", n=5)

            # Fake fetch returns {} → empty envelope list, but the call
            # itself must have succeeded after reconnect.
            self.assertEqual(envelopes, [])
            self.assertEqual(len(factory.created), 2)


if __name__ == "__main__":
    unittest.main()
