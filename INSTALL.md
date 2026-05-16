# Installing Murano

End-to-end install + first-run guide. Following this top to bottom takes about
**5 minutes** and ends with a working chat UI on `http://localhost:3000` plus
the MCP server wired into Claude Desktop or Cursor (optional).

If anything goes off the rails, jump to [Troubleshooting](#troubleshooting).

---

## 0. Prerequisites

You need:

- **Python 3.11+** (`python --version` should print 3.11 or newer).
- **[`uv`](https://github.com/astral-sh/uv)** — install via
  `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS/Linux or
  `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` on Windows.
- **A Venice API key.** Sign up at [venice.ai](https://venice.ai), grab a key
  from your account settings. The key never leaves your OS keychain.
- **macOS or Linux** for the smoothest path. Windows works for the CLI/HTTP
  surface; some niceties (`xdg-open`, `lsof`-based `--restart`) degrade.

You do **not** need:

- Docker, Postgres, Redis, or any backend.
- A frontend toolchain (no `npm`, no build step).
- An OpenAI key — Murano talks to Venice via the OpenAI-compatible SDK.

---

## 1. Get the code

```bash
git clone https://github.com/aicaptains/murano.git
cd murano
```

Or grab the release tarball once a v1 tag is cut.

---

## 2. Install in a virtual environment

```bash
uv venv                 # creates ./.venv with the right Python
source .venv/bin/activate
uv pip install -e .     # editable install — lets you `git pull` and rerun
```

Expected output (last line):

```
Installed N packages in <seconds>
```

Sanity check:

```bash
murano --version
# murano 0.1.0
```

---

## 3. Set up paths + the Venice API key

```bash
murano init
```

What this does: creates `~/murano/vault/` (where your Markdown files go) and
`~/.murano/` (the derived index + config + logs). Idempotent — safe to rerun.

Expected output:

```
                       Murano paths
┌────────┬──────────────────────────────────────────┬─────────┐
│ Label  │ Path                                     │ Status  │
├────────┼──────────────────────────────────────────┼─────────┤
│ vault  │ /Users/you/murano/vault                  │ created │
│ data   │ /Users/you/.murano                       │ created │
│ logs   │ /Users/you/.murano/logs                  │ created │
│ config │ /Users/you/.murano/config.toml           │ written │
└────────┴──────────────────────────────────────────┴─────────┘
```

Then store your Venice key in the OS keychain:

```bash
murano config set-key
# Venice API key: <paste key, input is hidden>
# Stored Venice API key in OS keychain (service=murano, username=venice-api-key).
```

The key is now in macOS Keychain / GNOME Keyring / Windows Credential Manager.
No file Murano writes ever contains it.

Verify connectivity:

```bash
murano ping
# Venice OK, chat=qwen-3-6-plus, embed=text-embedding-qwen3-8b
#   embed: 4096 dims, max 32768 tokens
```

If you see `Venice OK ...`, you're good. If not, jump to
[Troubleshooting](#troubleshooting).

---

## 4. Add some Markdown and index it

Drop any `.md` files into `~/murano/vault/`. Two ways:

**Quick test files (verifies the pipeline end-to-end):**

```bash
mkdir -p ~/murano/vault/cooking
cat > ~/murano/vault/cooking/risotto.md <<'EOF'
# Mushroom Risotto

A weeknight risotto using cremini mushrooms and arborio rice.

## Ingredients

- 1.5 cups arborio rice
- 6 cups warm vegetable stock
- 1 lb cremini mushrooms, sliced
- 1 shallot, minced
- 1/2 cup dry white wine
- 1/2 cup grated parmesan
- 3 tbsp butter

## Method

Saute the mushrooms in olive oil over high heat until golden. Set aside.
Toast the rice for 60 seconds. Deglaze with wine. Add stock one ladle at
a time, stirring constantly, until each ladle is absorbed before adding
the next. About 20 minutes total. Stir in the mushrooms, parmesan, and
final knob of butter. Rest off heat for 2 minutes before serving.
EOF
```

**Or capture a real article off the web:**

```bash
murano capture "https://en.wikipedia.org/wiki/Risotto"
# Captured into web-captures/2026-MM-DD-risotto-wikipedia.md
# Indexed N chunks
```

Either way, run the index (idempotent — only embeds new/changed files):

```bash
murano index
```

Expected output table includes `chunks inserted > 0`. If it says `0 files
seen`, your vault is empty — drop files in and rerun.

---

## 5. Ask your knowledge base something

```bash
murano ask "what kind of rice should I use for risotto?"
```

You'll see a retrieval line, then the answer streams word-by-word, then a
**Sources** footer with `✓` next to each chunk the model actually cited.
Citations are Obsidian-style `[[file#heading]]`.

Try a few questions to feel it out:

```bash
murano ask "summarize my notes" -k 6
murano ask "what's in the vault about italian cooking?" --show-context
murano ask "how do I deglaze?" --max-tokens 200
```

---

## 6. Build the summary tree (optional but recommended)

The "memory tree" is what makes thematic queries work well. It clusters
chunks, summarizes each cluster, and feeds those summaries to the LLM as
context (without ever citing them — citations stay anchored to real chunks).

```bash
murano tree rebuild
```

This is a one-shot LLM-heavy operation: with N chunks, it makes roughly
`sqrt(N)` Venice chat calls per level for 2–3 levels. A 100-chunk vault
takes about 5 minutes; the nightly scheduler picks this up automatically
once you run `murano serve`.

Verify:

```bash
murano tree show
# Shows your L1 + L2 themes with titles and 3-5 sentence summaries.
```

---

## 7. Start the web UI

```bash
murano serve --restart
# Starting Murano on http://127.0.0.1:3000
#   schedule=on  watch=on  reload=off
# INFO:     Uvicorn running on http://127.0.0.1:3000
```

Open `http://localhost:3000` in your browser. You'll see:

- **Chat** (`/`) — type a question, watch the answer stream with clickable
  citations, see the Sources footer.
- **Browse** (`/browse`) — vault file tree on the left, file viewer on the
  right, URL capture form at the top.
- **Settings** (`/settings`) — paths, models, key source, index stats,
  tree status. Buttons for "Test connection," "Re-index vault,"
  "Rebuild tree."
- **API** (`/docs`) — auto-generated Swagger UI for every endpoint.

**Click a citation** in any streamed answer — it should open the source `.md`
file in your default editor (macOS `open` / Linux `xdg-open` / Windows
`os.startfile`).

The watcher is running, so any `.md` file you drop into the vault becomes
searchable within ~5 seconds without restarting.

Stop with `Ctrl-C`. To restart cleanly without killing other processes on
port 3000, use `--restart` again (uses `lsof` to find and kill only the
prior `murano serve`).

---

## 8. (Optional) Wire into Claude Desktop / Cursor / Codex CLI

The MCP server exposes 5 tools (`search_kb`, `ask_kb`, `capture_url`,
`list_themes`, `get_chunk`) to any agent framework that speaks MCP.

Find the absolute path of your installed binary:

```bash
which murano
# /Users/you/projects/murano/.venv/bin/murano
```

**Claude Desktop:** edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) and merge in:

```json
{
  "mcpServers": {
    "murano": {
      "command": "/Users/you/projects/murano/.venv/bin/murano",
      "args": ["mcp"]
    }
  }
}
```

Restart Claude Desktop. In a new chat, the tools picker should list
`search_kb`, `ask_kb`, `capture_url`, `list_themes`, `get_chunk`.

**Cursor:** same JSON shape in `~/.cursor/mcp.json` (or project-scoped
`.cursor/mcp.json`). Reload the window.

**Codex CLI:** see `integrations/codex-cli/mcp-config.json`.

**Hermes Agent / OpenClaw:** drop-in skill files in
`integrations/hermes/murano-skill.md` and
`integrations/openclaw/murano-skill.yaml`. Replace `/ABSOLUTE/PATH/TO/...`
with `which murano` output.

Full per-host instructions and troubleshooting in
[`integrations/README.md`](./integrations/README.md).

---

## 9. (Optional) Bind to the LAN for multi-device use

By default Murano binds `127.0.0.1` only. If you want other devices on
your network to reach the UI, set an API token first:

```bash
murano serve --restart --host 0.0.0.0 --api-token "$(openssl rand -hex 16)"
# Killed N process(es) on port 3000.
# Starting Murano on http://0.0.0.0:3000
# NOTE: Murano is binding to a non-loopback address (0.0.0.0). Mutating
# endpoints require the X-Murano-Token header (token is set). Read
# endpoints (/health, /search, /chunks, /themes) are open.
```

Without `--api-token`, you'll get a big red warning instead and every
mutating endpoint is wide open to anyone on the network. The token gets
auto-injected into the bundled UI's HTML so the browser interface still
works; external clients need to send `X-Murano-Token: <token>` on every
POST/PUT/PATCH/DELETE under `/api/`.

---

## 10. Verification checklist

Before tagging v1, run through these. Should all pass.

```bash
# 1. All tests green.
pytest -q
# expect: "171 passed"

# 2. Lint clean.
ruff check src/ tests/
# expect: "All checks passed!"

# 3. No copyleft deps.
murano licenses
# expect: "All clear — N packages, none flagged as copyleft."

# 4. Ping resolves to real Venice models.
murano ping
# expect: "Venice OK, chat=qwen-3-6-plus, embed=text-embedding-qwen3-8b"

# 5. End-to-end: drop a file, index it, ask about it within 30s.
echo "# Frog\n\nFrogs are amphibians." > ~/murano/vault/frog.md
murano index && murano ask "what is a frog?" -k 1 --max-tokens 50
# expect: an answer citing [[frog#Frog]]

# 6. Backup excludes everything sensitive.
murano backup --out /tmp/murano-v1-check.zip
unzip -l /tmp/murano-v1-check.zip | grep -E "(chunks.db|summary_tree.db|api[_-]key)" || echo "✓ no DBs or keys in zip"

# 7. SSRF guard blocks loopback captures. (`murano capture` exits non-zero
#    on refusal — that's expected and good; we look at the message.)
out=$(murano capture "http://127.0.0.1:1/test" 2>&1 || true)
echo "$out" | grep -qi "refusing\|non-public" && echo "✓ SSRF guard active" || echo "✗ SSRF guard MISSING — output was: $out"

# 8. Web UI loads.
murano serve --restart &
sleep 2
curl -s http://127.0.0.1:3000/api/v1/health | head -c 200
echo ""
# expect: JSON with status:"ok", chunk_count:>0, api_key_source:"keychain"
pkill -f "murano serve" 2>/dev/null
```

If all 8 check out, you're ready to tag v1.

---

## Troubleshooting

### `murano ping` says "No Venice API key found in the OS keychain"

Run `murano config set-key` and paste your key when prompted. The key
goes into the OS keychain via the `keyring` library — verify with
`security find-generic-password -s murano` on macOS.

### `murano ping` says "Failed to reach Venice /v1/models"

Network is unreachable or your key is invalid. Check:
- `curl -H "Authorization: Bearer <your-key>" https://api.venice.ai/api/v1/models | head`
- Are you behind a proxy that requires HTTPS_PROXY?
- Did you accidentally set `MURANO_VENICE_BASE_URL` to something broken?
  `murano config show` will tell you what URL it's using.

### `murano index` reports `0 files seen`

Your vault is empty or in the wrong place. Run `murano config show` to
see `vault_root`. Drop `.md` files there.

### `murano ask` says "No index found at ~/.murano/chunks.db"

Run `murano index` first.

### Embedding dim mismatch after changing the embed model

If you change `MURANO_EMBED_MODEL` to a model with different dimensions
(e.g. switching from `text-embedding-qwen3-8b` (4096-dim) to
`text-embedding-bge-m3` (1024-dim)), the existing `chunks.db` is invalid.
Murano detects this and auto-wipes on the next `murano index`, but you
must re-run it:

```bash
murano reindex   # force a full rebuild
murano tree rebuild   # tree was built against the old embeddings
```

### `murano serve --restart` says port 3000 is still in use

`lsof -ti :3000` should report whatever's holding it; kill that PID
manually. The `--restart` flag only kills processes Murano can see via
`lsof`; on stripped-down containers without `lsof`, you'll need to
free the port yourself.

### Browser shows "could not open file for [[file#heading]]" when clicking a citation

The citation file resolution depends on your OS launcher. macOS uses
`open`, Linux uses `xdg-open`, Windows uses `os.startfile`. If your
default Markdown editor isn't registered for `.md`, the launcher
fails. Set a default Markdown editor (e.g. assign Obsidian to handle
`.md` files in System Settings → Default Apps).

### Tests fail with `ModuleNotFoundError: No module named 'murano'`

You're not in the venv. Run `source .venv/bin/activate`.

### Tests fail with `RuntimeError: ... cannot import name '...'`

The `-e` editable install missed something. Re-run `uv pip install -e .`.

### Backup or export zip is empty

Either your vault is empty, or all your `.md` files are inside a hidden
directory (starting with `.`). Hidden dirs are skipped by design.

### `murano mcp` works in a terminal but Claude Desktop / Cursor don't see the tools

Likely the host process can't find the `murano` binary on its inherited
`PATH`. The MCP config must use the **absolute path** from `which murano`,
not the bare command name. Restart the host fully after editing the
config; some hosts cache the manifest.

### "WARNING: Murano is binding to a non-loopback address"

You passed `--host 0.0.0.0` (or similar non-loopback). All endpoints are
unauthenticated by default. Either remove `--host`, or add
`--api-token "$(openssl rand -hex 16)"`. See section 9.

### Where do logs live?

`~/.murano/logs/scheduler.log` (nightly tree rebuild + watcher thread)
and `~/.murano/logs/usage.jsonl` (every Venice call's token counts).
`murano usage` summarizes the latter.

---

## Uninstalling

```bash
# Backup first if you want to keep anything.
murano backup --out ~/murano-final-backup.zip

# Remove the keychain entry.
murano config unset-key

# Remove the venv + derived index.
rm -rf .venv ~/.murano

# Optional: remove your vault too.
# rm -rf ~/murano
```

That's everything. No global state to clean up — no system services, no
daemons, no `~/.config/` orphans.
