# Auditing Murano

This file is meant to be handed to a fresh LLM (or human) to get a thorough,
structured audit of the Murano codebase. It contains two ready-to-paste
prompts (deep and short), notes on how to pick the right auditor, and a
short summary of what has already been audited and fixed so the new
reviewer doesn't re-trip on closed ground.

---

## How to use this file

1. **Pick the prompt.** Use the **deep prompt** for a sit-down review with
   shell access (Cursor/Claude Code in agent mode, Codex CLI, a GitHub
   Actions bot, etc.). Use the **short prompt** for a chat-only LLM (raw
   ChatGPT, Claude in a web UI) where you'll paste a handful of files
   alongside the prompt.
2. **Hand it the repo.** The auditor needs read access to the whole tree.
   For agentic tools, that's automatic. For chat-only models, paste
   `MURANO_PLAN.md`, the deep-prompt section, and 4–6 modules you most
   want examined.
3. **Read the deliverable carefully.** The prompt forces a structured
   report. If you get vague vibes back, the audit wasn't thorough; try a
   different model or paste more files.
4. **Drop the resulting `MURANO_AUDIT_REPORT_N.md` in the repo root** and
   say something like *"here's the next one"* to whoever (or whatever) is
   maintaining the code. Past reports live in the repo as
   `MURANO_AUDIT_REPORT*.md`.

## What's already been audited (don't re-litigate)

Two prior third-party audits have been closed. Skim these before starting
so your time goes to fresh territory:

| Round | Report | Fix commit | Highlights |
|---|---|---|---|
| 1 | `MURANO_AUDIT_REPORT.md` | `ce7425b` | Sibling-prefix path traversal on `/api/v1/{open,vault/file}` + `/file`; indexer silently storing absolute paths for out-of-vault files; keychain Venice key leaking to arbitrary hosts via `MURANO_VENICE_BASE_URL`. |
| 2 | (committed as part of `3933d2a`) | `3933d2a` | Feed dedup non-determinism (set ordering); MCP arg coercion silently clamped bad types; aggressive `pkill -f`; unpkg.com supply-chain dep in the UI; misleading "only outbound api.venice.ai" claims in several docstrings; missing api_key_source reporting; duplicated capture-then-index policy across CLI/HTTP/MCP. |

If you're confident you've found one of those bugs *still present* in the
current `HEAD`, that's actually a great finding — call it out as a
regression. Otherwise, look elsewhere.

## Tips for picking an auditor

- The two strongest audits so far caught **real exploits** (sibling-prefix
  traversal, set-iteration non-determinism) and **process-level issues**
  (tests that pass for the wrong reason, docstrings that lied about
  network behavior). When you pick a model, prioritize ones that score
  well on "actually runs the code" rather than "reads it and guesses."
- Send the same prompt to two different models and diff the deliverables.
  Overlap = high confidence; non-overlap = each model's blind spots.
- Don't trust a clean report. If a model returns "no findings" in section
  1 of the deliverable, the audit was almost certainly shallow; rerun
  with more pointed scope.

---

## The deep prompt

Copy everything below the line into the auditor's chat. Works for any
tool with shell + filesystem access.

---

```markdown
You are doing a deep audit of **Murano**, a private local-first personal
knowledge base. The project is MIT-licensed, ~7k lines of Python, and was
built in roughly a dozen commits across a single session. The author wants
to know what they missed before tagging v1.

# Context

- **Stack:** Python 3.11+, FastAPI + htmx web UI on `localhost:3000`,
  SQLite + sqlite-vec for vectors, `mcp` Python SDK for the MCP server,
  trafilatura for web capture, numpy for from-scratch k-means, feedparser
  for RSS, apscheduler for nightly rebuilds.
- **External services:** By default only `api.venice.ai` (chat + embeddings)
  and whatever URL the user passes to `capture`/`capture-feed`. The env
  var `MURANO_VENICE_BASE_URL` lets advanced users point at any OpenAI-
  compatible endpoint; in that case the keychain Venice key is NOT sent
  and `MURANO_API_KEY` is read from env instead.
- **Where the key lives:** OS keychain via `keyring`. Never in any file
  Murano writes. Backups defensively assert no DB or key in the zip.
- **Acceptance criteria:** see `MURANO_PLAN.md` §14. The author claims all
  9 are met; verify each.

# Setup (works on macOS or Linux; Windows has known caveats)

```bash
git status                       # confirm clean checkout
uv venv && source .venv/bin/activate
uv pip install -e .
pytest -q                        # expect ~145/145 passing as baseline
ruff check src/ tests/
murano licenses                  # expect "All clear — N packages, none flagged"
murano --help                    # inventory all subcommands
```

If you have a Venice API key, also run the live smoke (consumes some tokens):

```bash
murano init
murano config set-key
murano ping
mkdir -p ~/murano/vault
echo "# Test\n\nMurano is a private knowledge base." > ~/murano/vault/test.md
murano index
murano ask "what is Murano?" -k 2 --max-tokens 100
murano usage
murano backup --out /tmp/audit-backup.zip
unzip -l /tmp/audit-backup.zip   # confirm: no chunks.db, no API key
```

# Already-closed findings (don't waste time)

Read `MURANO_AUDIT_REPORT.md` and the commit messages for `ce7425b` and
`3933d2a` before starting. The path-traversal, key-exfil, feed-dedup,
MCP-arg-coercion, htmx-supply-chain, and "documentation lies about
network" classes of bug are already addressed. If you believe one is
still exploitable, that's a regression and worth a top-of-report mention.

# Audit dimensions

For each dimension, find at least one concrete issue OR explicitly state
"no findings." Don't pad with vague concerns.

## 1. Security & privacy (fresh angles)

- Audit ergonomics: are the LAN warning, key-source reporting, and
  capture URL validation visible enough in the actual UX? Imagine a
  hurried operator copy-pasting from a tutorial.
- TOCTOU: between `safe_vault_path()` validation and the subsequent
  `read_text` / `subprocess.run(["open", ...])`, can an attacker swap
  the resolved target via a symlink race?
- The vault watcher and the HTTP server share a process. Could a race
  let `/api/v1/ask` read a half-written capture file? Could a malicious
  vault file (e.g. a YAML frontmatter bomb, a giant chunk that OOMs the
  embedder, or unicode that breaks the chunker) DoS the index loop?
- SSE injection: `/api/v1/ask` streams JSON-in-`data:` fields. Could the
  *answer text* (which the model can fully control given a malicious
  prompt) inject CR/LF that confuses a naive SSE consumer?
- `/api/v1/open` runs `subprocess.run(["open", str(candidate)], check=True)`
  on macOS. Has the candidate been thoroughly sanitized? What about
  paths containing arguments-that-look-like-flags (`-version` etc.)?
- The MCP server runs in the user's account and inherits the same
  filesystem privileges. An LLM client could request `get_chunk(id)`
  with a crafted id. Is the id sanitized before hitting SQLite?
- `MURANO_VENICE_BASE_URL` accepts `http://` URLs. Even with the key-
  gating fix, is there a downside (e.g., the OpenAI SDK might still
  send some auth header)?

## 2. Correctness vs. plan

Read `MURANO_PLAN.md` §11 (Phases) and §14 (Acceptance). For each
acceptance criterion, find the test or shell command that proves it.

Pay special attention to:
- **Chunker** (`src/murano/vault/chunker.py`): does it actually produce
  ~512-token chunks with ~64 overlap? Write a test against a real long
  Markdown file (try a Wikipedia article) and check the distribution.
  Are pathological inputs (giant single paragraph, deeply nested code
  blocks, unicode-heavy text, files with only frontmatter) handled?
- **K-means** (`src/murano/tree/cluster.py`): is k-means++ initialization
  actually distance-weighted? What happens when all input vectors are
  identical? When `k == n`? When the embedding matrix has zero rows?
- **Hybrid retrieval prompt** (`src/murano/chat/answer.py`): could the
  model be tricked into citing a theme node? (Themes have ids like
  `L1::0`.) What stops the model from emitting `[[L1::0]]`?
- **Sources footer** uses `extract_citation_keys` regex. What citation
  variations does it miss? (`[[file]]`, `[[file#h1#h2]]`, escaped
  brackets, nested brackets?)
- **Tree rebuild** is sequential. What happens if the LLM call hangs
  mid-cluster? Is the partial state recoverable?

## 3. Code quality

- The shared retriever core lives in `src/murano/chat/`. Is it actually
  reused by all three transports (CLI / MCP / HTTP), or has drift crept
  back in? (The audit-2 fix introduced `capture_and_index`; check it
  hasn't been bypassed.)
- SQLite migration story: `init_for_model` rebuilds `vec_chunks` when
  dims change. Does it handle a partially-written DB after a crash?
  What about WAL files left behind by an interrupted indexer?
- The MCP server raises `RuntimeError` for tool errors. Run it via
  `npx @modelcontextprotocol/inspector murano mcp` if you can —
  exercise each tool with malformed inputs.
- Tests use `unittest.mock.patch` heavily. Find tests that patch at the
  wrong layer (e.g. patching `murano.chat.retriever.embed_one` when the
  actual call site uses a different attribute path).
- Check for swallowed exceptions. `except Exception: pass` or
  `except Exception as e: return None` are footguns; flag any.

## 4. UX

- Open `http://localhost:3000` after `murano serve --restart`. Try the
  chat, vault browser, settings page. Note layout breakage, dead
  buttons, confusing copy, accessibility issues.
- Click a streaming citation. Does it open the file in your default
  editor? Does it work for citations whose `file` part contains
  punctuation/spaces? (Captured filenames sometimes have weird slugs.)
- Try `murano capture-feed <url>` on a real RSS feed; verify
  idempotency on second run.
- After `Ctrl-C`-ing a `murano serve`, does `--restart` reliably
  recover? Are there any zombie processes or stuck WAL files?

## 5. Integration files

- `integrations/claude-desktop/`, `cursor/`, `codex-cli/` MCP configs
  all use `/ABSOLUTE/PATH/TO/...`. Verify the README explains how to
  resolve it, and that the failure mode for not substituting is clear.
- `integrations/hermes/murano-skill.md` and
  `integrations/openclaw/murano-skill.yaml`: the author admits these
  are reverse-engineered approximations. If you know either framework,
  flag schema mismatches.
- The skill files claim Murano "by default only contacts api.venice.ai"
  and list the two exceptions. Make sure the wording matches what the
  code actually does today.

## 6. Performance ceiling

The plan doesn't give load characteristics. Estimate empirically:
- At what chunk count does `murano tree rebuild` take more than 10 min?
- At what vault size does the watcher's per-file
  `index_vault(subpath=...)` start to feel slow?
- The full `chunks.db` is read into memory for tree-building. At what
  vault size does this OOM on a 16 GB machine?
- The SSE `/api/v1/ask` endpoint holds a Venice connection per request.
  How does it behave with 50 concurrent clients? With slow clients?

## 7. Backup integrity

The `murano backup` zip is supposed to be self-sufficient (the user can
unzip elsewhere and rebuild the index). Verify:
- Unzip into a fresh `~/murano-restored/vault/` + `~/.murano-restored/`.
- `MURANO_VAULT=...vault MURANO_DATA=...murano-restored murano index`
- `murano ask "..."` against the restored vault.
- The restored vault should produce identical answers to the original
  (modulo nondeterminism in the LLM).
- Verify the backup zip contains zero secrets via `unzip -p` + grep.

# Deliverable

A single Markdown file at the repo root named
`MURANO_AUDIT_REPORT_<N>.md` (where N is the next available number)
with **exactly** this structure:

```markdown
# Murano audit — <your name / model name> — <date>

## 0. Bottom line
<one paragraph: ship-worthy as v1, or not?>

## 1. Must-fix bugs
<numbered list, each item has: file:line, what's wrong, suggested fix>

## 2. Should-fix design concerns
<same format>

## 3. Nice-to-have polish
<same format>

## 4. Things you got right
<short list of decisions worth keeping>

## 5. Plan-vs-reality gap
<for each MURANO_PLAN.md §14 acceptance criterion: ✓ verified, ✗ broken,
 or ⚠️ "claims but not really". Cite the evidence either way.>

## 6. What I didn't get to
<honest list of things you skipped, with reasons>

## 7. Regressions from prior audits
<empty if none; otherwise: "I expected X to be fixed per MURANO_AUDIT_REPORT.md
 but found it still exploitable at file:line".>
```

Constraints:

- Be specific. "Error handling needs work" is useless. "`api/routes.py:298`
  catches `Exception` then re-raises as `RuntimeError` losing the original
  traceback — wrap with `raise … from e`" is useful.
- Cite file paths and line numbers for every claim.
- Run actual code, not just read it. If something needs a Venice key and
  you don't have one, say so explicitly in section 6.
- Don't suggest large rewrites. Murano is intentionally small; respect
  the architecture and the dependency budget unless something is broken.
- Length budget: aim for 200–500 lines of report. More than that, the
  signal-to-noise drops sharply.
```

---

## The short prompt

For chat-only LLMs (raw ChatGPT, Claude in a web UI). Paste this with
`MURANO_PLAN.md` and 4-6 modules attached.

---

```markdown
You are auditing **Murano**, a private local-first personal knowledge base
that chats with a Markdown vault via Venice's API. MIT-licensed, clean-room
rebuild of the OpenHuman "memory tree" concept. Two prior audits have been
closed (path traversal, key exfil, feed dedup, MCP coercion, htmx CDN dep,
documentation accuracy) — don't re-litigate.

**Read first (attached):** `MURANO_PLAN.md` and any modules I included.

**Then, in a single Markdown report:**

1. Skim every attached module and note any code that:
   - leaks the Venice API key outside the OS keychain
   - makes an outbound network call to a host other than `api.venice.ai`,
     a user-supplied capture URL, or the configured `MURANO_VENICE_BASE_URL`
   - is unsafe with path traversal, SSRF, race conditions, or shell injection
   - claims behavior in docstrings/README that the code doesn't deliver
   - duplicates logic that should live in `chat.retriever` / `chat.answer` /
     `capture.web.capture_and_index`
2. Skim any attached tests — flag any that pass for the wrong reason or
   that only exercise the happy path.
3. Skim any attached integration files — flag anything that would
   silently break if a user follows the instructions verbatim.

**Deliverable structure (no preamble, no apologies, just sections):**

- `## Real bugs` — must-fix issues with file:line references.
- `## Design concerns` — things that work but feel wrong; explain why.
- `## What's good` — non-trivial decisions you'd keep.

Be terse and specific. Reference file paths and line numbers. If you
have only the plan and no source, that's fine — flag it in your report
under "I didn't get to" rather than guessing.
```

---

## What to attach for chat-only LLMs

If your auditor can't run code, attach the following with the short
prompt for the best signal:

- `MURANO_PLAN.md` (always)
- `src/murano/security.py` (small, central, security-critical)
- `src/murano/api/routes.py` (largest attack surface)
- `src/murano/chat/answer.py` (the RAG core — prompt + streaming)
- `src/murano/tree/build.py` and `src/murano/tree/cluster.py` (the bespoke k-means)
- `src/murano/capture/feed.py` (where round 2 found a non-determinism bug)
- The latest `MURANO_AUDIT_REPORT*.md` so they don't repeat findings

That gives a strong chat-only auditor enough surface to find real things
without overwhelming the context window.

## Notes for the maintainer

- Run audits on a **clean checkout**. Local edits in flight muddy the
  trail and make it hard to tell what's a real finding vs. a half-done
  refactor.
- When fixing an audit, **commit the report itself** alongside the fix
  (round 1 did this; round 2 did not — round 1 is the model). It makes
  git history a forensic record of what was found and what was done.
- Audits compound. Each fresh auditor builds on the closed list, so the
  marginal value of round-3+ audits goes up, not down — the easy stuff
  is gone and what's left is the genuinely subtle.
