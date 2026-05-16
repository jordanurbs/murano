# Murano

> Private, local-first personal knowledge base. Chat with your Markdown vault. Powered by [Venice](https://venice.ai).

**Status:** v1 feature-complete — all 7 plan phases shipped.

Murano is a clean-room rebuild of the "memory tree" concept. You drop Markdown files into a vault (Obsidian-compatible), Murano chunks, embeds, and indexes them, then lets you chat with your knowledge through a CLI, a local web UI on port 3000, or an MCP server that any agent framework (Claude Desktop, Cursor, Hermes, OpenClaw, Codex CLI) can plug into.

No backend service. No telemetry. By default the only outbound call is to `api.venice.ai`. Two narrowly-scoped exceptions exist by design:
- `murano capture <url>` / `capture-feed <url>` fetches user-supplied URLs.
- `MURANO_VENICE_BASE_URL` lets advanced users point at any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio). When that's set, Murano **never** sends the keychain Venice API key — you must provide `MURANO_API_KEY` for the local endpoint, or leave it unset for no-auth servers.

## Install (dev)

Requires Python 3.11+ and [`uv`](https://github.com/astral-sh/uv).

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

## Quickstart

```bash
murano init                    # create ~/murano/vault/ and ~/.murano/
murano config set-key          # paste your Venice API key (stored in OS keychain)
murano ping                    # validate connectivity and resolve models

# Drop some .md files into ~/murano/vault/ and:
murano index                   # embed them
murano serve --restart         # http://localhost:3000 — chat UI + REST API + nightly tree rebuild
```

You should see:

```
Venice OK, chat=qwen-3-6-plus, embed=text-embedding-qwen3-8b
  embed: 4096 dims, max 32768 tokens
```

## Roadmap

See [`MURANO_PLAN.md`](./MURANO_PLAN.md) for the full plan and phase breakdown.

- [x] **Phase 1** — Skeleton + Venice plumbing (`init`, `config set-key`, `ping`)
- [x] **Phase 2** — Vault → chunks → embeddings (`index`, `reindex`, `watch`, `search`)
- [x] **Phase 3** — Flat RAG (`ask` with streaming + Obsidian-style citations)
- [x] **Phase 3.5** — MCP server (`mcp` with `search_kb` + `ask_kb` tools; configs in [`integrations/`](./integrations/))
- [x] **Phase 4** — Web capture (`capture <url>` + `capture_url` MCP tool, auto-indexed)
- [x] **Phase 5** — Hierarchical summary tree (`tree rebuild/show`, hybrid retrieval, `list_themes` + `get_chunk` MCP tools)
- [x] **Phase 6** — Web UI + REST API (`serve` on port 3000, SSE-streamed chat, vault browser, settings, nightly tree rebuild + background watcher)
- [x] **Phase 6.5** — Reference skill files (Hermes, OpenClaw, Codex CLI) in [`integrations/`](./integrations/)
- [x] **Phase 7** — QoL (`usage`, `export`, `backup`, `licenses`, `capture-feed`)

> Local-embedding fallback (`sentence-transformers`) was scoped to Phase 7 but deferred. The embedding call sites would need to be refactored to go through a provider interface, and `sentence-transformers` pulls in PyTorch (~700 MB). Users who really want offline embeddings can swap in any OpenAI-compatible local server (Ollama, LM Studio) and point `MURANO_VENICE_BASE_URL` at it.

## License

MIT. See [`LICENSE`](./LICENSE).

## Clean-room note

Murano is a clean-room rebuild of the OpenHuman "memory tree" concept based on its public documentation and standard RAG / hierarchical-summarization patterns (RAPTOR et al.). No source code from `tinyhumansai/openhuman` was copied. The names "OpenHuman" and "Tiny Humans" are not used in this project.
