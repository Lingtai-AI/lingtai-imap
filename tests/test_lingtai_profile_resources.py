"""Tests for LingTai-owned MCP profile resources."""
from __future__ import annotations

import json
import unittest

from lingtai_imap import server


class _FakeManager:
    def handle(self, args: dict) -> dict:
        assert args == {"action": "accounts"}
        return {
            "accounts": [
                {
                    "address": "agent@example.com",
                    "tool_connected": True,
                    "listener_connected": True,
                    "listening": True,
                }
            ],
            "tcp_alias": "/tmp/bridge",
        }


class LingTaiProfileResourceTests(unittest.TestCase):
    def test_manifest_declares_pointer_skill_and_human_mcp_boundary(self):
        manifest = server.lingtai_profile(_FakeManager())

        self.assertEqual(manifest["schema_version"], "1.0")
        self.assertEqual(manifest["server"]["name"], "imap")
        self.assertEqual(manifest["interfaces"]["human_frontend"], "/mcp")
        self.assertIn("imap", manifest["interfaces"]["agent_entrypoints"]["tools"])
        self.assertIn(
            "lingtai://skills/imap",
            manifest["interfaces"]["agent_entrypoints"]["resources"],
        )
        self.assertIn(
            "thin addon skills that point agents toward this MCP",
            manifest["philosophy"]["lingtai_owns"],
        )

    def test_resources_include_profile_docs_status_and_no_passwords(self):
        resources = server.lingtai_resources(_FakeManager())

        self.assertIn("lingtai://manifest", resources)
        self.assertIn("lingtai://skills/imap", resources)
        self.assertIn("lingtai://docs/configuration", resources)
        self.assertIn("lingtai://docs/troubleshooting", resources)
        self.assertIn("lingtai://status", resources)

        manifest_mime, manifest_text = resources["lingtai://manifest"]
        self.assertEqual(manifest_mime, server.LINGTAI_PROFILE_MIME)
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["status"]["accounts"][0]["address"], "agent@example.com")

        all_text = "\n".join(content for _mime, content in resources.values())
        # The resources may document field names and placeholder credentials,
        # but runtime status must not dump concrete secret-looking values.
        self.assertNotIn("email_password\": \"16-char-app-password", all_text)
        self.assertNotIn("hunter2", all_text.lower())

    def test_status_resource_reports_degraded_without_manager(self):
        _mime, text = server.lingtai_resources(None)["lingtai://status"]
        status = json.loads(text)

        self.assertEqual(status["status"], "degraded")
        self.assertFalse(status["manager_initialized"])
        self.assertEqual(status["accounts"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
