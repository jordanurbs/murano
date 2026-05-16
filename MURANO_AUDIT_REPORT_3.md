# Murano audit — GPT-5.5 — 2026-05-16

## 0. Bottom line

Murano is close to v1-worthy: the core CLI/RAG path, HTTP pages, keychain gating, MCP handlers, and prior audit fixes held up under the baseline test suite and an isolated live Venice smoke. I would still fix the backup/export symlink leak before tagging v1, and I would strongly consider fixing the RSS retry starvation and vault-browser symlink crash in the same pass because they are small, local changes with clear regression tests.

## 1. Must-fix bugs

1. `src/murano/backup.py:50` walks the vault with `vault_root.rglob("*")`, accepts `child.is_file()` at `src/murano/backup.py:53`, and then writes that path directly into export/backup zips at `src/murano/backup.py:70` and `src/murano/backup.py:98`. `Path.is_file()` and `ZipFile.write()` follow symlinks, so a Markdown symlink inside the vault can cause `murano export` / `murano backup` to include the contents of a file outside the vault. I reproduced this with `vault/linked.md -> ../outside-secret.md`; the zip contained `vault/linked.md` with `OUTSIDE SECRET`. Suggested fix: reuse the resolved-containment policy from `src/murano/index/indexer.py:49`, or call `relpath_in_vault()` on each resolved candidate and skip anything that escapes; add export and backup regression tests mirroring `tests/test_security.py:111`.

## 2. Should-fix design concerns

1. `src/murano/capture/feed.py:117` slices feed entries to `[:limit]` before checking whether entries were already seen or whether a capture failed, and `src/murano/capture/feed.py:152` to `src/murano/capture/feed.py:158` records failed captures only in the returned report, not in feed state. A single permanently broken top entry can be retried forever and can starve later entries when `--limit` is small. I hit this live with `https://hnrss.org/frontpage --limit 1`: both runs retried the same entry and failed with the same SSL error. Suggested fix: treat `limit` as "new captures to attempt" rather than "entries to scan", or persist failed entry IDs with a retry/backoff policy so one bad item does not block the feed.

2. `src/murano/api/routes.py:428` to `src/murano/api/routes.py:439` builds `/api/v1/vault/tree` by recursing over `Path.iterdir()` and then calling `child.resolve().relative_to(vault)` without handling symlinks that point outside the vault. A symlinked directory inside the vault currently produces a 500 from the unauthenticated vault browser; I reproduced this with `vault/linkdir -> ../outside` containing `secret.md`. Suggested fix: resolve each candidate before descending, skip anything whose resolved path is outside the resolved vault root, and add a test alongside `tests/test_api.py:424`.

3. `src/murano/ui/routes.py:77` passes `api_key_present=bool(get_api_key())` into the settings page, and `src/murano/ui/templates/settings.html:27` to `src/murano/ui/templates/settings.html:32` renders only "stored in OS keychain" vs "not set". That is misleading when `MURANO_VENICE_BASE_URL` points at a custom endpoint with `MURANO_API_KEY`: `/api/v1/health` correctly reports `api_key_source` and `venice_base_url` at `src/murano/api/routes.py:100`, but the actual UX still tells the operator to run `murano config set-key`. Suggested fix: reuse `_effective_api_key_source()` or the health response shape in the settings template and show `keychain`, `env`, or `none`.

4. `src/murano/ui/static/app.js:150` to `src/murano/ui/static/app.js:165` parses citations independently per SSE delta. If the model streams `[[file#heading]]` split across chunks, the UI renders plain text instead of a clickable citation even though the final `done` event can still list it. Suggested fix: keep a small carry buffer for partial `[[...]]` tokens, or render citations from the accumulated answer after `done`.

5. `src/murano/vault/chunker.py:109` to `src/murano/vault/chunker.py:128` advances `cursor` with `len(line)`, and `src/murano/vault/chunker.py:220` persists that value as `byte_offset`. For Unicode before a section, this is a character offset, not a byte offset; I reproduced `éééé\n# H\nbody` producing offset `5` for content whose UTF-8 byte offset is `13`. Suggested fix: track offsets over `line.encode("utf-8")` lengths, or rename the DB/API field if character offsets are the intended contract.

6. `src/murano/capture/feed.py:111` parses a user-supplied feed URL, but `src/murano/capture/feed.py:141` to `src/murano/capture/feed.py:153` then fetches each entry link advertised by that feed through `capture_url()`. That behavior is probably intended for RSS, but the README phrasing at `README.md:9` to `README.md:11` says the exception is the URL the user passes to `capture-feed`, not arbitrary entry URLs chosen by the feed publisher. Suggested fix: document the network boundary as "the feed URL and the http(s) entry links it advertises"; optionally reject private/link-local entry hosts unless the operator opts in.

7. `src/murano/tree/build.py:144` to `src/murano/tree/build.py:180` performs all cluster summarization and summary embedding before `src/murano/tree/build.py:316` to `src/murano/tree/build.py:326` replaces the tree DB. This is recoverable because the old tree remains until the final atomic rebuild, but a hung LLM call loses all progress for that rebuild. Suggested fix: set explicit OpenAI/httpx timeouts if not already inherited from the SDK defaults, and consider checkpointing per-level summaries only if real vaults make rebuilds long enough to justify the extra state.

## 3. Nice-to-have polish

1. `src/murano/capture/web.py:150` to `src/murano/capture/web.py:162` fetches full response text with no content-length guard. A malicious or accidental huge HTML response can make `capture` consume memory before trafilatura rejects or extracts it. Suggested fix: use an `httpx` streaming response and enforce a conservative byte cap for v1.

2. `src/murano/api/routes.py:309` to `src/murano/api/routes.py:310` sends `type(e).__name__` and the raw exception string to SSE clients for unexpected errors. That is helpful locally, but it can expose absolute paths and implementation details in a LAN-exposed server. Suggested fix: log details server-side and return a shorter generic message for the last-resort path.

3. `src/murano/api/scheduler.py:193` to `src/murano/api/scheduler.py:196` and `src/murano/api/scheduler.py:210` to `src/murano/api/scheduler.py:211` silently ignore failures while killing port holders. That is acceptable for best-effort `--restart`, but a debug log would make "why did restart not free port 3000?" easier to diagnose.

4. `tests/test_chunker.py:76` to `tests/test_chunker.py:90` covers oversized sections, but there is no regression test for byte offsets with multibyte text. Add the `éééé\n# H\nbody` case above once the offset contract is fixed.

## 4. Things you got right

- The prior path traversal fix is centralized in `src/murano/security.py:29`, and the HTTP/UI read/open paths now use it at `src/murano/api/routes.py:215`, `src/murano/api/routes.py:449`, and `src/murano/ui/routes.py:89`.
- The keychain key gating fix held: custom hosts use `MURANO_API_KEY` or a no-auth placeholder at `src/murano/venice.py:65` to `src/murano/venice.py:84`, and tests cover the non-canonical host path at `tests/test_security.py:172`.
- Retrieval is still shared instead of transport-forked: HTTP uses `Retriever.open()` / `stream_answer()` at `src/murano/api/routes.py:116` and `src/murano/api/routes.py:257`, MCP uses the same core at `src/murano/mcp/server.py:246` and `src/murano/mcp/server.py:317`, and the CLI uses `stream_answer()` at `src/murano/cli.py:443`.
- SSE framing is not vulnerable to simple answer-text CR/LF injection because `_sse()` JSON-serializes dict payloads at `src/murano/api/routes.py:241` to `src/murano/api/routes.py:244`; newlines in model text become JSON escapes inside a single `data:` line.
- `get_chunk()` uses a parameterized SQLite lookup at `src/murano/tree/retrieve.py:88` to `src/murano/tree/retrieve.py:94`, so crafted chunk IDs do not become SQL injection.
- The live isolated smoke worked: `murano ping`, `murano index`, `murano ask`, `murano usage`, and `murano backup` all succeeded against a temp vault, and the backup contained no DB entries.
- The baseline is green: `uv run pytest -q` reported `145 passed`, `uv run ruff check src/ tests/` passed, and `uv run murano licenses` reported `All clear — 87 packages, none flagged as copyleft.`

## 5. Plan-vs-reality gap

1. ✓ `murano init && murano config set-key && murano index && murano serve` gets to a working UI on `localhost:3000`. Evidence: `src/murano/cli.py:73`, `src/murano/cli.py:109`, `src/murano/cli.py:270`, and `src/murano/cli.py:639` implement the path; I ran an isolated live smoke with an already-stored key, then started `murano serve --restart --port 3000 --no-schedule --no-watch` and got 200s from `/`, `/browse`, and `/settings`.

2. ⚠️ File changes in the vault reflect in answers within seconds. Evidence: `src/murano/vault/watcher.py:62` to `src/murano/vault/watcher.py:74` maps watchfiles events to scoped `index_vault()` calls, and `tests/test_watcher.py:20` verifies event-to-subpath mapping. I did not run a live watcher + Venice reindex timing test.

3. ✓ `murano capture <url>` ingests web articles. Evidence: `src/murano/cli.py:517` to `src/murano/cli.py:572` and `src/murano/capture/web.py:199` to `src/murano/capture/web.py:259` implement it; `tests/test_capture.py:155` verifies writing frontmatter/body. I also ran `capture-feed https://xkcd.com/rss.xml --limit 1` twice in an isolated vault and saw the first run capture an article and the second skip it as already seen.

4. ⚠️ Hierarchical summary tree rebuilds nightly. Evidence: `src/murano/api/scheduler.py:129` to `src/murano/api/scheduler.py:140` registers the cron job and `src/murano/api/server.py:44` to `src/murano/api/server.py:57` starts/stops it in lifespan; `tests/test_tree.py:383` exercises the build path with mocked Venice. I did not leave a server running until 03:00 to observe the scheduled job.

5. ⚠️ `murano mcp` works as an MCP server in Claude Desktop and Cursor. Evidence: `src/murano/mcp/server.py:58` to `src/murano/mcp/server.py:209` registers the five tools, `tests/test_mcp.py:164` verifies the tool list, and `integrations/claude-desktop/mcp-config.json:1` plus `integrations/cursor/mcp-config.json:1` provide configs. I did not run the interactive MCP inspector or launch Claude/Cursor as MCP hosts.

6. ✓ Reference skill files for Hermes and OpenClaw are present. Evidence: `integrations/hermes/murano-skill.md:1` and `integrations/openclaw/murano-skill.yaml:1`.

7. ⚠️ No outbound network calls except to `api.venice.ai`. Evidence: direct calls are concentrated in `src/murano/venice.py:128`, `src/murano/capture/web.py:153`, and `src/murano/capture/feed.py:111`; the designed exceptions are documented at `README.md:9` to `README.md:11`. The remaining gap is RSS entry links: `capture-feed` fetches URLs supplied by the feed content, not only the literal feed URL typed by the user.

8. ✓ The Venice API key never leaves the OS keychain except in `Authorization:` headers to `api.venice.ai`. Evidence: keychain access is isolated at `src/murano/config.py:144` to `src/murano/config.py:157`, non-canonical hosts avoid `get_api_key()` at `src/murano/venice.py:65` to `src/murano/venice.py:84`, and `tests/test_security.py:172` asserts the keychain key is not even read for custom hosts.

9. ✓ No GPL/AGPL deps. Evidence: `src/murano/licenses.py` is exercised by `tests/test_phase7.py:217`, and `uv run murano licenses` reported `All clear — 87 packages, none flagged as copyleft.`

## 6. What I didn't get to

- I did not run `npx @modelcontextprotocol/inspector murano mcp`; MCP coverage is from unit tests and config inspection.
- I did not perform the 50-concurrent-client SSE load test. The code holds one synchronous Venice stream per request at `src/murano/api/routes.py:247` to `src/murano/api/routes.py:335`, but I did not measure slow-client behavior.
- I did not empirically find the chunk count where `murano tree rebuild` exceeds 10 minutes. The code reads all chunk embeddings into memory at `src/murano/tree/build.py:69` to `src/murano/tree/build.py:97`; a 4096-dim float64 matrix is roughly 32 KiB per chunk before Python/list overhead, so large vaults will hit memory pressure, but I did not run a destructive large-vault benchmark.
- I did not do a full backup restore followed by `murano ask` against the restored vault. I did verify an isolated live backup zip omitted `chunks.db` and `summary_tree.db`, and the symlink leak above shows the backup file walker still needs a security regression test.
- I did not test the actual macOS "click citation opens editor" path through a browser. Static review found the split-delta citation issue, and `/api/v1/open` itself uses `safe_vault_path()` before `subprocess.run()`.

## 7. Regressions from prior audits

Empty. I did not find the prior sibling-prefix traversal, keychain-key exfiltration to custom hosts, feed set-order nondeterminism, MCP coercion, htmx CDN dependency, or duplicated capture-and-index policy still exploitable in the current `HEAD`.
