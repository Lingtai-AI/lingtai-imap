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
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import Server
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
    "Setup, config schema, and troubleshooting: "
    "https://github.com/Lingtai-AI/lingtai-imap"
)


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
