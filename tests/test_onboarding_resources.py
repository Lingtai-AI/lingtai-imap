"""Tests for MCP-owned IMAP onboarding resources."""
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
            ]
        }


class OnboardingResourceTests(unittest.TestCase):
    def test_manifest_indexes_onboarding_resources(self):
        manifest = server.lingtai_profile(_FakeManager())
        resources = manifest["interfaces"]["agent_entrypoints"]["resources"]

        self.assertIn("lingtai://onboarding/imap", resources)
        self.assertIn("lingtai://onboarding/html-template", resources)
        self.assertEqual(
            manifest["interfaces"]["agent_entrypoints"]["onboarding"],
            "lingtai://onboarding/imap",
        )
        self.assertEqual(
            manifest["interfaces"]["agent_entrypoints"]["onboarding_html_template"],
            "lingtai://onboarding/html-template",
        )

        resource_uris = {entry["uri"] for entry in manifest["resources"]}
        self.assertIn("lingtai://onboarding/imap", resource_uris)
        self.assertIn("lingtai://onboarding/html-template", resource_uris)

    def test_resources_include_secret_free_onboarding_docs(self):
        resources = server.lingtai_resources(_FakeManager())

        self.assertIn("lingtai://onboarding/imap", resources)
        self.assertIn("lingtai://onboarding/html-template", resources)

        markdown_mime, markdown = resources["lingtai://onboarding/imap"]
        html_mime, html = resources["lingtai://onboarding/html-template"]

        self.assertEqual(markdown_mime, server.MARKDOWN_MIME)
        self.assertEqual(html_mime, "text/html")
        self.assertIn("IMAP/SMTP", markdown)
        self.assertIn("Verification checklist", markdown)
        self.assertIn("{{EMAIL_ADDRESS}}", html)
        self.assertIn("Secret safety", html)

        combined = markdown + html + json.dumps(server.lingtai_profile(_FakeManager()))
        self.assertNotIn("hunter2", combined.lower())
        self.assertNotIn("16-char-app-password", combined)
        self.assertNotIn("EAAG", combined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
