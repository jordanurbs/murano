# Murano

> Private, local-first personal knowledge base. Chat with your Markdown vault. Powered by [Venice](https://venice.ai).

**Status:** Phase 1 — skeleton + Venice plumbing.

Murano is a clean-room rebuild of the "memory tree" concept. You drop Markdown files into a vault (Obsidian-compatible), Murano chunks, embeds, and indexes them, then lets you chat with your knowledge through a CLI, a local web UI on port 3000, or an MCP server that any agent framework (Claude Desktop, Cursor, Hermes, OpenClaw, Codex CLI) can plug into.

No backend service. No telemetry. The only outbound call is to `api.venice.ai`.

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
- [ ] **Phase 3.5** — MCP server (`mcp`)
- [ ] **Phase 4** — Web capture (`capture`)
- [ ] **Phase 5** — Hierarchical summary tree
- [ ] **Phase 6** — Web UI + REST API (`serve` on port 3000)
- [ ] **Phase 6.5** — Reference skill files (Hermes, OpenClaw)
- [ ] **Phase 7** — QoL (token tracker, backup, local-embedding fallback)

## License

MIT. See [`LICENSE`](./LICENSE).

## Clean-room note

Murano is a clean-room rebuild of the OpenHuman "memory tree" concept based on its public documentation and standard RAG / hierarchical-summarization patterns (RAPTOR et al.). No source code from `tinyhumansai/openhuman` was copied. The names "OpenHuman" and "Tiny Humans" are not used in this project.
