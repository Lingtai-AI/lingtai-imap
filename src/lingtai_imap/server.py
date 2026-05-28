"""LingTai IMAP MCP server.

Exposes a single omnibus ``imap`` MCP tool that dispatches to the legacy
IMAPMailManager for all 14 actions (send/check/read/reply/search/delete/
move/flag/folders/contacts/add_contact/remove_contact/edit_contact/
accounts). Inbound IMAP events flow into the host agent's inbox via LICC.

Configuration:
    LINGTAI_IMAP_CONFIG  — path to a JSON config file (required).

Config schema (plaintext, no env-indirection):

    {
      "accounts": [
        {
          "email_address": "agent@example.com",
          "email_password": "16-char-app-password",
          "imap_host": "imap.gmail.com",      // optional, default Gmail
          "imap_port": 993,                    // optional
          "smtp_host": "smtp.gmail.com",       // optional
          "smtp_port": 587,                    // optional
          "allowed_senders": ["a@x.com"],      // optional allow-list
          "poll_interval": 30                  // optional, seconds
        }
      ]
    }

Env vars injected by the LingTai kernel for LICC:
    LINGTAI_AGENT_DIR — host agent's working directory.
    LINGTAI_MCP_NAME  — this MCP's registry name (typically "imap").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server

from ._migrate import migrate_legacy_state
from .bridge import FilesystemMailBridge
from .licc import push_inbox_event
from .manager import IMAPMailManager, SCHEMA, DESCRIPTION
from .service import IMAPMailService

log = logging.getLogger("lingtai_imap")


_SERVER_INSTRUCTIONS = (
    "lingtai-imap: real email via IMAP/SMTP with multi-account support. "
    "Configure via the LINGTAI_IMAP_CONFIG env var pointing at a JSON file. "
    "Inbound mail flows into the host agent's inbox via LICC. "
    "This server publishes LingTai profile resources; read lingtai://manifest "
    "to discover MCP-owned docs, routing hints, and status. "
    "Setup, config schema, and troubleshooting: "
    "https://github.com/Lingtai-AI/lingtai-imap"
)

LINGTAI_PROFILE_MIME = "application/vnd.lingtai.mcp-profile+json"
LINGTAI_SKILL_MIME = "text/markdown; profile=lingtai-skill"
JSON_MIME = "application/json"
MARKDOWN_MIME = "text/markdown"

_MANIFEST_URI = "lingtai://manifest"
_SKILL_URI = "lingtai://skills/imap"
_CONFIG_DOC_URI = "lingtai://docs/configuration"
_TROUBLESHOOTING_DOC_URI = "lingtai://docs/troubleshooting"
_STATUS_URI = "lingtai://status"


def _package_version() -> str:
    try:
        return version("lingtai-imap")
    except PackageNotFoundError:  # editable checkout without installation metadata
        return "0+local"


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _canonical_resource_uri(uri: object) -> str:
    return str(uri).rstrip("/")


def _safe_status_payload(manager: IMAPMailManager | None) -> dict[str, Any]:
    """Return runtime status without exposing passwords or config contents."""
    if manager is None:
        return {
            "status": "degraded",
            "manager_initialized": False,
            "accounts": [],
            "notes": [
                "IMAP manager did not initialize. Check server stderr and "
                "LINGTAI_IMAP_CONFIG; read lingtai://docs/troubleshooting.",
            ],
        }

    try:
        accounts = manager.handle({"action": "accounts"})
    except Exception as exc:  # pragma: no cover - defensive only
        return {
            "status": "error",
            "manager_initialized": True,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }

    return {
        "status": "ok",
        "manager_initialized": True,
        "accounts": accounts.get("accounts", []),
    }


def lingtai_profile(manager: IMAPMailManager | None = None) -> dict[str, Any]:
    """LingTai-specific MCP profile layered on ordinary MCP resources.

    This is a convention, not an MCP protocol extension. Generic MCP clients can
    ignore it; LingTai clients can use it for progressive disclosure.
    """
    return {
        "schema": "https://lingtai.ai/schemas/mcp-profile/v1",
        "schema_version": "1.0",
        "server": {
            "name": "imap",
            "package": "lingtai-imap",
            "version": _package_version(),
            "title": "LingTai IMAP",
            "summary": "Real email via IMAP/SMTP with multi-account support.",
            "homepage": "https://github.com/Lingtai-AI/lingtai-imap",
        },
        "philosophy": {
            "mcp_owns": [
                "configuration schema and credential expectations",
                "runtime status and diagnostics",
                "platform-specific setup and troubleshooting docs",
                "agent-facing tools/resources/prompts",
            ],
            "lingtai_owns": [
                "human-facing discovery and rendering in /mcp",
                "thin addon skills that point agents toward this MCP",
            ],
        },
        "interfaces": {
            "human_frontend": "/mcp",
            "agent_entrypoints": {
                "tools": ["imap"],
                "resources": [
                    _MANIFEST_URI,
                    _SKILL_URI,
                    _CONFIG_DOC_URI,
                    _TROUBLESHOOTING_DOC_URI,
                    _STATUS_URI,
                ],
            },
        },
        "resources": [
            {
                "uri": _MANIFEST_URI,
                "name": "lingtai-imap manifest",
                "mime_type": LINGTAI_PROFILE_MIME,
                "description": "Machine-readable LingTai profile for this MCP.",
            },
            {
                "uri": _SKILL_URI,
                "name": "imap pointer skill",
                "mime_type": LINGTAI_SKILL_MIME,
                "description": "Thin agent routing hint; authoritative detail remains in MCP resources.",
            },
            {
                "uri": _CONFIG_DOC_URI,
                "name": "configuration",
                "mime_type": MARKDOWN_MIME,
                "description": "Config schema, env vars, and ownership guidance.",
            },
            {
                "uri": _TROUBLESHOOTING_DOC_URI,
                "name": "troubleshooting",
                "mime_type": MARKDOWN_MIME,
                "description": "Common failures and diagnostic steps.",
            },
            {
                "uri": _STATUS_URI,
                "name": "runtime status",
                "mime_type": JSON_MIME,
                "description": "Current account/listener status with secrets omitted.",
            },
        ],
        "status": _safe_status_payload(manager),
    }


def _skill_markdown() -> str:
    return """---
name: imap
description: >
  Thin LingTai routing pointer for the lingtai-imap MCP. Use this when an
  agent needs real internet email via IMAP/SMTP, or needs to configure,
  diagnose, or understand the IMAP addon. Authoritative details live in this
  MCP's resources, not in a copied LingTai skill body.
version: 1.0.0
---

# IMAP MCP pointer

This capability is provided by the `lingtai-imap` MCP. The MCP itself owns the
changing platform details: configuration fields, credential expectations,
runtime status, diagnostics, and troubleshooting.

## Agent route

- Use the `imap(action=...)` tool for email operations.
- Read `lingtai://manifest` to discover this server's LingTai profile.
- Read `lingtai://docs/configuration` before configuring the addon.
- Read `lingtai://docs/troubleshooting` when the addon does not start or mail
  does not arrive.
- Read `lingtai://status` for current account/listener state.

## Human route

Use LingTai's human-facing `/mcp` frontend to inspect MCP configuration, status,
resources, and onboarding surfaces. Agents should not depend on `/mcp`; agents
should use MCP resources/tools/prompts directly.
"""


def _configuration_markdown() -> str:
    return """# lingtai-imap configuration

`lingtai-imap` is configured by the `LINGTAI_IMAP_CONFIG` environment variable.
The value points to a JSON file; relative paths are resolved under
`LINGTAI_AGENT_DIR` when LingTai launches the MCP.

## Canonical config shape

```json
{
  "accounts": [
    {
      "email_address": "agent@example.com",
      "email_password": "app-password-or-token",
      "imap_host": "imap.gmail.com",
      "imap_port": 993,
      "smtp_host": "smtp.gmail.com",
      "smtp_port": 587,
      "allowed_senders": ["trusted@example.com"],
      "poll_interval": 30
    }
  ]
}
```

A legacy single-account flat object with `email_address` is still accepted, but
new configs should use the `accounts` list.

## Field notes

- `email_address` and `email_password` are required for each account. Prefer an
  app password or provider token; do not store a primary account password unless
  the provider requires it.
- `imap_host`, `imap_port`, `smtp_host`, and `smtp_port` default to Gmail values
  when omitted. Set them explicitly for Outlook, custom domains, or other
  providers.
- `allowed_senders` is optional. Use it to restrict inbound LICC wake events to
  trusted addresses.
- `poll_interval` is optional and defaults to 30 seconds.

## Ownership

The MCP package owns this schema and its troubleshooting details. LingTai's
human-facing `/mcp` UI may render this resource, while agents should read this
resource directly when they need exact setup details.
"""


def _troubleshooting_markdown() -> str:
    return """# lingtai-imap troubleshooting

## Server starts but every tool call errors

Most often the server could not initialize the manager. Check:

1. `LINGTAI_IMAP_CONFIG` is set.
2. The referenced config file exists. Relative paths are resolved from
   `LINGTAI_AGENT_DIR`.
3. The JSON is valid and contains either `accounts: [...]` or the legacy
   single-account `email_address` shape.
4. Credentials are valid for both IMAP and SMTP. Many providers require app
   passwords or OAuth/app-specific tokens.

Read `lingtai://status`; if `manager_initialized` is `false`, fix config and
restart or refresh the host agent.

## Mail does not wake the host agent

1. Confirm the account appears in `lingtai://status`.
2. Confirm `listener_connected` and `listening` are true for that account.
3. Check whether `allowed_senders` excludes the sender.
4. Check the host agent's `.notification/mcp.imap.json` and logs for LICC
   delivery errors.

## Send/reply fails

1. Confirm SMTP host/port match the provider.
2. Confirm the account allows SMTP with the credential being used.
3. For replies, keep the compound email id shape `account:folder:uid`.

## Privacy and safety

`lingtai://status` intentionally omits passwords and raw config contents. Do not
copy config files into chat or issues without redacting secrets.
"""


def lingtai_resources(manager: IMAPMailManager | None = None) -> dict[str, tuple[str, str]]:
    """Return MCP-owned LingTai resources as uri -> (mime_type, content)."""
    return {
        _MANIFEST_URI: (LINGTAI_PROFILE_MIME, _json_dumps(lingtai_profile(manager))),
        _SKILL_URI: (LINGTAI_SKILL_MIME, _skill_markdown()),
        _CONFIG_DOC_URI: (MARKDOWN_MIME, _configuration_markdown()),
        _TROUBLESHOOTING_DOC_URI: (MARKDOWN_MIME, _troubleshooting_markdown()),
        _STATUS_URI: (JSON_MIME, _json_dumps(_safe_status_payload(manager))),
    }


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Read config from the path in LINGTAI_IMAP_CONFIG.

    Path is resolved relative to LINGTAI_AGENT_DIR (or cwd as fallback)
    if not absolute. Plaintext only — no *_env indirection.
    """
    config_path_raw = os.environ.get("LINGTAI_IMAP_CONFIG")
    if not config_path_raw:
        raise ValueError(
            "LINGTAI_IMAP_CONFIG env var not set — point it at your IMAP "
            "config JSON file"
        )
    config_path = Path(config_path_raw).expanduser()
    if not config_path.is_absolute():
        base = Path(os.environ.get("LINGTAI_AGENT_DIR", os.getcwd()))
        config_path = base / config_path
    if not config_path.is_file():
        raise FileNotFoundError(f"IMAP config not found: {config_path}")
    return json.loads(config_path.read_text(encoding="utf-8"))


def _accounts_from_config(cfg: dict) -> list[dict]:
    """Normalize config into the accounts list IMAPMailService expects.

    Accepts either the canonical ``{accounts: [...]}`` shape or a flat
    single-account dict for back-compat with very old configs.
    """
    if "accounts" in cfg:
        return list(cfg["accounts"])
    if "email_address" in cfg:
        return [{
            "email_address": cfg["email_address"],
            "email_password": cfg.get("email_password", ""),
            "imap_host": cfg.get("imap_host", "imap.gmail.com"),
            "imap_port": cfg.get("imap_port", 993),
            "smtp_host": cfg.get("smtp_host", "smtp.gmail.com"),
            "smtp_port": cfg.get("smtp_port", 587),
            "allowed_senders": cfg.get("allowed_senders"),
            "poll_interval": cfg.get("poll_interval", 30),
        }]
    raise ValueError(
        "config must contain either 'accounts' (list) or 'email_address' "
        "(single-account back-compat shape)"
    )


# ---------------------------------------------------------------------------
# Manager construction
# ---------------------------------------------------------------------------

def build_manager() -> tuple[IMAPMailManager, FilesystemMailBridge | None, Path]:
    """Construct the IMAP manager + bridge from env + config.

    Returns (manager, bridge, working_dir). ``bridge`` is None when the
    agent_dir env var is missing (e.g. running this MCP standalone for
    testing); that case still gives a functional manager but no
    cross-agent relay.
    """
    cfg = load_config()
    accounts = _accounts_from_config(cfg)

    agent_dir_raw = os.environ.get("LINGTAI_AGENT_DIR")
    working_dir = Path(agent_dir_raw) if agent_dir_raw else Path.cwd()
    working_dir.mkdir(parents=True, exist_ok=True)

    # One-shot legacy state cleanup (pre-rewrite _processed_uids files)
    state_dir = working_dir / "imap"
    if state_dir.is_dir():
        try:
            migrate_legacy_state(state_dir)
        except Exception as e:
            log.warning("legacy state migration failed: %s", e)

    bridge_dir = working_dir / "imap_bridge"
    bridge_dir.mkdir(parents=True, exist_ok=True)

    imap_svc = IMAPMailService(accounts=accounts, working_dir=working_dir)
    bridge = FilesystemMailBridge(bridge_dir=bridge_dir)

    def _on_inbound(event: dict) -> None:
        push_inbox_event(
            sender=event["from"],
            subject=event["subject"],
            body=event["body"],
            metadata=event.get("metadata"),
            wake=event.get("wake", True),
        )

    mgr = IMAPMailManager(
        service=imap_svc,
        working_dir=working_dir,
        tcp_alias=str(bridge_dir),
        on_inbound=_on_inbound,
    )
    mgr._bridge = bridge

    return mgr, bridge, working_dir


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

def build_server(manager: IMAPMailManager | None) -> Server:
    """Construct the MCP server. ``manager`` is None when eager start
    failed; in that case every tool call returns an error explaining why."""
    server: Server = Server("lingtai-imap", instructions=_SERVER_INSTRUCTIONS)

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        profile = lingtai_profile(manager)
        return [
            types.Resource(
                name=item["name"],
                uri=item["uri"],
                description=item["description"],
                mimeType=item["mime_type"],
            )
            for item in profile["resources"]
        ]

    @server.read_resource()
    async def _read_resource(uri: object) -> list[ReadResourceContents]:
        key = _canonical_resource_uri(uri)
        resources = lingtai_resources(manager)
        try:
            mime_type, content = resources[key]
        except KeyError as exc:
            raise ValueError(f"unknown resource: {uri!s}") from exc
        return [ReadResourceContents(content=content, mime_type=mime_type)]

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="imap",
                description=DESCRIPTION,
                inputSchema=SCHEMA,
            ),
        ]

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any],
    ) -> list[types.TextContent]:
        if name != "imap":
            raise ValueError(f"unknown tool: {name!r}")
        if manager is None:
            result = {
                "status": "error",
                "error": (
                    "IMAP manager not initialized — server boot failed. "
                    "Check stderr for the underlying exception (most often "
                    "missing LINGTAI_IMAP_CONFIG or invalid credentials)."
                ),
            }
        else:
            try:
                result = await asyncio.to_thread(manager.handle, arguments)
            except Exception as e:
                result = {
                    "status": "error",
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
        return [types.TextContent(
            type="text", text=json.dumps(result, ensure_ascii=False),
        )]

    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def serve() -> None:
    """Run the MCP server over stdio. Eagerly starts the manager so the
    IMAP IDLE listener is up before the host expects mail."""
    manager: IMAPMailManager | None = None
    try:
        manager, _bridge, _wd = build_manager()
        manager.start()
        log.info("IMAP listener + bridge running")
    except Exception as e:
        log.error(
            "eager start failed; tool calls will return errors until fixed: %s", e,
        )
        manager = None

    server = build_server(manager)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        if manager is not None:
            try:
                manager.stop()
            except Exception:
                pass
