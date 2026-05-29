# lingtai-imap

LingTai IMAP MCP server — real email via IMAP/SMTP, exposed as MCP tools, with multi-account support and a cross-agent relay bridge.

This is the canonical setup, configuration, and troubleshooting doc for the `lingtai-imap` MCP. It is fetched by LingTai agents (or anyone else) when they need to install or configure this server.

> **MCP / LICC contract spec:** see the `lingtai-anatomy` skill, `reference/mcp-protocol.md`, for the canonical specification of the catalog → registry → activation chain, environment-variable injection, and the LICC v1 inbox callback protocol. The reference client implementation is `src/lingtai_imap/licc.py` in this repo (vendored verbatim into all first-party LingTai MCP repos — copy it if you're writing your own).

## Tools

One omnibus MCP tool: `imap(action=...)`. Actions: `send`, `check`, `read`, `reply`, `search`, `delete`, `move`, `flag`, `folders`, `contacts`, `add_contact`, `remove_contact`, `edit_contact`, `accounts`. Email IDs are compound keys: `account:folder:uid`.


## LingTai profile resources

`lingtai-imap` is a normal MCP server, but it also publishes a small
LingTai-specific profile through ordinary MCP resources. This keeps the addon
self-contained: configuration docs, troubleshooting, pointer-skill text, and
runtime status live with the MCP package instead of being copied into LingTai's
TUI or bundled skills.

Resources:

| URI | MIME type | Purpose |
|---|---|---|
| `lingtai://manifest` | `application/vnd.lingtai.mcp-profile+json` | Machine-readable LingTai profile: server metadata, resource index, ownership boundaries, agent entrypoints, and safe runtime status. |
| `lingtai://skills/imap` | `text/markdown; profile=lingtai-skill` | Thin routing/pointer skill. Existing LingTai addon skills can point here rather than duplicating platform details. |
| `lingtai://docs/configuration` | `text/markdown` | Authoritative config schema and setup notes. |
| `lingtai://docs/troubleshooting` | `text/markdown` | Common startup, delivery, and SMTP failure diagnostics. |
| `lingtai://status` | `application/json` | Current account/listener state with passwords and raw config omitted. |

Boundary:

- `/mcp` is the human-facing LingTai TUI/control-panel surface for viewing MCP
  configuration, status, resources, and onboarding.
- Agents should use MCP tools/resources/prompts directly. For IMAP, that means
  using the `imap(action=...)` tool for mail operations and reading the
  `lingtai://...` resources for documentation/status.
- Existing LingTai addon skills should remain as thin progressive-disclosure
  pointers toward this MCP, not as the authoritative copy of changing provider
  details.

## Inbound mail (LICC)

Inbound IMAP messages flow into the host agent's inbox via the LingTai Inbox Callback Contract. The server reads two env vars that the LingTai kernel injects automatically when spawning the MCP:

- `LINGTAI_AGENT_DIR` — host agent's working directory.
- `LINGTAI_MCP_NAME` — this MCP's registry name (typically `imap`).

Each new email is delivered as a LICC event with:
- `from`, `subject` — straight from the email headers.
- `body` — a ~300 char preview (use `imap(action="read", email_id=...)` to fetch the full message).
- `metadata.email_id` — compound key for downstream tool calls.
- `metadata.account` — which configured account received it.

The server-side stub listener has been removed; real IMAP IDLE polling runs on every account.

## Cross-agent relay bridge

Other agents on the same host can route outbound mail through this server by writing message files into `<agent_working_dir>/imap_bridge/inbox/<sender>/<msg-id>/message.json`. The bridge picks them up and sends via this MCP's IMAP/SMTP. Useful for networks where only one agent owns the email account.

## Install

```bash
# Into the LingTai agent's venv (typically ~/.lingtai-tui/runtime/venv/)
pip install git+https://github.com/Lingtai-AI/lingtai-imap.git
```

After install, `python -m lingtai_imap` (or the `lingtai-imap` script) starts the MCP server over stdio.

## Configure

The server reads its IMAP/SMTP credentials from a JSON file pointed at by the `LINGTAI_IMAP_CONFIG` environment variable. Recommended path: `.secrets/imap.json` inside the agent's working directory. Plaintext only — this MCP does not support `*_env` indirection.

### Config schema

```json
{
  "accounts": [
    {
      "email_address": "agent@example.com",
      "email_password": "16-char-app-password",
      "imap_host": "imap.gmail.com",
      "imap_port": 993,
      "smtp_host": "smtp.gmail.com",
      "smtp_port": 587,
      "allowed_senders": ["someone@example.com"],
      "poll_interval": 30
    }
  ]
}
```

- `accounts` is an array — append more entries to add accounts.
- `email_password` should be an **app password** (not your account password). For Gmail: enable 2FA, then create one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). For Outlook: account.microsoft.com/security → App passwords.
- `imap_host` / `imap_port` default to `imap.gmail.com` / `993`. Override for non-Gmail providers.
- `smtp_host` / `smtp_port` default to `smtp.gmail.com` / `587`.
- `allowed_senders` — optional allow-list. When set, IMAP messages from other senders are silently ignored by the listener.
- `poll_interval` — seconds between IDLE reconciliation slices (default 30).

### Activation in LingTai

Inside `init.json`, add `imap` to your `addons:` list (which auto-registers via the kernel catalog) and add an entry under `mcp:` to activate:

```json
{
  "addons": ["imap"],
  "mcp": {
    "imap": {
      "type": "stdio",
      "command": "/path/to/your/python",
      "args": ["-m", "lingtai_imap"],
      "env": {
        "LINGTAI_IMAP_CONFIG": ".secrets/imap.json"
      }
    }
  }
}
```

Then run `system(action="refresh")` from the agent. The MCP subprocess starts, the IMAP IDLE listener begins polling, the bridge directory is created, and the omnibus `imap` tool becomes available.

## Troubleshooting

- **`LINGTAI_IMAP_CONFIG env var not set`** — your `init.json` `mcp.imap.env` entry is missing the `LINGTAI_IMAP_CONFIG` key.
- **`IMAP config not found`** — the path resolves but no file exists. Relative paths are resolved against `LINGTAI_AGENT_DIR`.
- **`Login failed` / `AUTHENTICATIONFAILED`** — usually a wrong password or a regular password instead of an app password. Re-create the app password.
- **`gaierror: nodename nor servname provided`** — `imap_host` is wrong or DNS resolution failed.
- **`SSL: CERTIFICATE_VERIFY_FAILED`** — corporate certificate stores. Workaround under discussion; for now, run from a venv with up-to-date `certifi`.
- **`MCP server failed to start`** — usually the `command` path in `init.json` doesn't have `lingtai_imap` installed. Confirm with `<command> -m lingtai_imap --help` from a shell.
- **Tool calls return `IMAP manager not initialized`** — server boot failed. Check stderr for the underlying exception and fix the config / env, then `system(action="refresh")`.
- **Listener says `tool_connected: false, listening: false`** — call `imap(action="accounts")` to see per-account status. The listener auto-reconnects with backoff; transient failures recover on their own.

## License

MIT.


## Onboarding resources

- `lingtai://onboarding/imap` — IMAP/SMTP setup workflow, provider caveats, and verification checklist.
- `lingtai://onboarding/html-template` — static secret-free HTML checklist template for local browser presentation.
