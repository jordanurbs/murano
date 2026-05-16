---
name: murano
title: Murano knowledge base
version: 0.1.0
description: |
  Query the user's private Markdown vault via Murano (a local-first knowledge
  base backed by Venice). Murano runs entirely on the user's machine; the only
  outbound network call it makes is to api.venice.ai.
author: Murano contributors
license: MIT
transports:
  http:
    base_url: http://localhost:3000/api/v1
  mcp:
    command: murano
    args: [mcp]
tags: [memory, rag, knowledge-base, local-first]
---

# Murano knowledge base skill

This skill lets an agent search and ask questions over the user's personal
Markdown vault. Murano returns answers grounded in the user's notes with
inline Obsidian-style citations: `[[file#heading]]`.

## When to use this skill

Reach for Murano whenever the user asks something that might be answered by
their own notes, captures, or any Markdown they've placed in
`~/murano/vault/`. Examples:

- "What do I have written about X?"
- "Summarize my notes on Y."
- "Did I write down the recipe for Z?"
- "What were my main interests last quarter?" (use `list_themes` first)
- "Save this article into my knowledge base." (use `capture_url`)

## Prerequisites

The user must have done a one-time setup:

```bash
murano init
murano config set-key       # paste their Venice API key into the OS keychain
murano index                # embed their vault
murano tree rebuild         # build the summary tree (recommended)
murano serve --restart      # HTTP API on http://localhost:3000
# OR
murano mcp                  # MCP server over stdio
```

If `murano serve` is running, the HTTP API at `http://localhost:3000/api/v1`
is available. If the user is wiring you to the MCP server, use the `mcp`
transport instead.

## Tools

### `search_kb(query, k=10)`

Vector search over the vault. Cheap, fast, no LLM call. Returns the top-K
Markdown chunks with their file path, heading path, and citation key.

- **When to use:** you want raw passages so you can synthesize an answer
  yourself, or you want to show the user a list of relevant notes.
- **HTTP:** `POST /api/v1/search` with `{"query": "...", "k": 10}`.
- **MCP:** `search_kb` tool.

### `ask_kb(query, k=6, max_tokens=1024, temperature=0.2)`

Full RAG: retrieves chunks, optionally injects summary themes as context,
asks the configured Venice chat model to answer grounded only in those
chunks, with inline `[[file#heading]]` citations.

- **When to use:** the user asked a natural-language question and wants a
  cited answer in prose. This is your default.
- **HTTP:** `POST /api/v1/ask` (Server-Sent Events stream — events
  `retrieval`, `delta`, `done`).
- **MCP:** `ask_kb` tool (synchronous from MCP's perspective; the streaming
  happens internally and a complete answer is returned).

### `capture_url(url, tags=[])`

Fetch a web page with `trafilatura`, write a Markdown file with YAML
frontmatter into `<vault>/web-captures/YYYY-MM-DD-<slug>.md`, and index it
immediately so subsequent `search_kb` / `ask_kb` calls can cite it.

- **When to use:** the user shares a URL and wants it remembered.
- **HTTP:** `POST /api/v1/capture` with `{"url": "...", "tags": [...]}`.
- **MCP:** `capture_url` tool.

### `list_themes(level=1)`

Walk Murano's hierarchical summary tree at a given level. Returns each
cluster's title + 3-5 sentence summary + member count.

- **When to use:** orient yourself before deciding what to ask. A typical
  pattern is to call `list_themes(level=2)` for a high-level overview, then
  `ask_kb` once you know which themes the user's question maps to.
- **HTTP:** `GET /api/v1/themes?level=1`.
- **MCP:** `list_themes` tool.

### `get_chunk(chunk_id)`

Fetch one chunk by id. `chunk_id` has the form `<file-relpath>::<ord>`.

- **When to use:** follow-up after a citation; the user wants to see the
  full passage behind a particular `[[file#heading]]` reference.
- **HTTP:** `GET /api/v1/chunks/{chunk_id}`.
- **MCP:** `get_chunk` tool.

## Conventions

- **Cite every claim.** When you relay an `ask_kb` answer, preserve the
  inline `[[file#heading]]` citations exactly as Murano produced them.
  Never invent or alter citation keys.
- **Themes are context, not sources.** `list_themes` results give you
  orientation; do NOT cite a theme node. Themes have ids like `L1::0`,
  while chunk ids look like `cooking/risotto.md::2`.
- **Respect the vault.** Don't write files outside the vault. The only
  way the skill adds material to the vault is via `capture_url`.

## Failure modes

- "No index found at ~/.murano/chunks.db" → the user hasn't run
  `murano index` yet. Tell them, don't retry blindly.
- "No Venice API key found in the OS keychain" → tell the user to run
  `murano config set-key`.
- An empty Sources footer on an `ask_kb` answer means retrieval found
  nothing matching. Don't fabricate; tell the user.

## Example

User: "What rice should I use for mushroom risotto?"

You should:

1. Call `ask_kb({"query": "what rice should I use for mushroom risotto?"})`.
2. Relay the answer verbatim, keeping the `[[file#heading]]` citations.
3. If the user clicks a citation in a UI that supports it, that opens
   the source file in their editor — no extra action needed from you.
