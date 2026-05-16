# Murano integrations

Drop-in configuration snippets so agent frameworks can use Murano as their persistent memory layer via the Model Context Protocol (MCP).

The Murano MCP server exposes three tools today:

- `search_kb(query, k=10)` — vector search over your vault; returns the top-K Markdown chunks with `[[file#heading]]` citation keys. No LLM call. Cheap, fast.
- `ask_kb(query, k=6, max_tokens=1024, temperature=0.2)` — full RAG. Retrieves chunks, asks the configured Venice chat model to answer grounded only in those chunks, with inline `[[file#heading]]` citations.
- `capture_url(url, tags=[])` — fetches a web page with `trafilatura`, writes a Markdown file with YAML frontmatter into `<vault>/web-captures/YYYY-MM-DD-<slug>.md`, and immediately indexes it so subsequent `search_kb` / `ask_kb` calls can cite it.

More tools land in later phases: `list_themes` and `get_chunk` (Phase 5).

## Prerequisites (one-time)

```bash
murano init               # creates ~/murano/vault/ and ~/.murano/
murano config set-key     # paste your Venice API key (OS keychain)
murano index              # embed your vault
murano ping               # verify Venice connectivity
```

Find the absolute path of the `murano` binary your install produced — most users will see something like `/Users/<you>/projects/murano/.venv/bin/murano`. Substitute it for `/ABSOLUTE/PATH/TO/.venv/bin/murano` everywhere below.

## Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent on your OS and merge in the contents of [`claude-desktop/mcp-config.json`](./claude-desktop/mcp-config.json):

```json
{
  "mcpServers": {
    "murano": {
      "command": "/ABSOLUTE/PATH/TO/.venv/bin/murano",
      "args": ["mcp"],
      "env": {
        "MURANO_VAULT": "/ABSOLUTE/PATH/TO/your/vault",
        "MURANO_DATA": "/ABSOLUTE/PATH/TO/.murano"
      }
    }
  }
}
```

Restart Claude Desktop. In a new chat you should see `search_kb` and `ask_kb` listed under the tools picker. Ask something only your vault knows.

The `env` block is optional — Murano falls back to `~/murano/vault/` and `~/.murano/` by default. Override only if you keep your vault elsewhere.

## Cursor

Cursor's MCP settings live at `~/.cursor/mcp.json` (or the project-scoped `.cursor/mcp.json`). Merge in [`cursor/mcp-config.json`](./cursor/mcp-config.json). Reload the window. The tools will be available to the agent in Composer / Chat.

## Other MCP-aware hosts

Every MCP host supports the same `{ "mcpServers": { "<name>": { "command": ..., "args": [...] } } }` shape. The config files in this directory are valid for any of them — Hermes Agent, OpenClaw, Codex CLI, custom hosts, etc. Skill files for Hermes and OpenClaw land in Phase 6.5.

## Verifying without a host

```bash
# Start the server manually and send a list_tools request via the MCP CLI inspector:
npx @modelcontextprotocol/inspector murano mcp
```

The inspector opens a local UI that lets you exercise `search_kb` and `ask_kb` against your vault without wiring it into a host.

## Troubleshooting

- **Tool calls return `ERROR: No index found at …`** — run `murano index` first.
- **Tool calls return `ERROR: No Venice API key found in the OS keychain`** — the host process can't see your keychain. Run `murano config set-key` in a regular terminal first; the Murano MCP process inherits keychain access via `keyring`.
- **Host says the server crashed on startup** — try running `murano mcp` manually in a terminal. The server prints `[murano-mcp] ready on stdio` to stderr; if it errors out, the message will tell you why.
- **Logs vs protocol** — MCP uses stdout for protocol messages, so all human-readable Murano logs go to stderr. Don't be alarmed if `2>/dev/null` makes the server look silent.
