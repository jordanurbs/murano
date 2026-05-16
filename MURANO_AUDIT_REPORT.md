# Murano Audit Report

## Real bugs

- **Vault path traversal via prefix check**: `api` and `ui` file-open/read endpoints validate with `str(candidate).startswith(str(vault_root))`, which is bypassable with sibling-prefix paths (e.g. `../vault2/secret.md`). A real request can read outside-vault files.
  - `src/murano/api/routes.py:198-201` (`/api/v1/open`)
  - `src/murano/api/routes.py:432-435` (`/api/v1/vault/file`)
  - `src/murano/ui/routes.py:87-90` (`/file`)

- **Indexer can ingest files outside vault via symlinks, then stores absolute paths**: traversal guard is missing during file walk; `_relpath` falls back to absolute path when `relative_to` fails, so out-of-vault content can be indexed/cited.
  - `src/murano/index/indexer.py:61-68` (walk/selection)
  - `src/murano/index/indexer.py:71-75` (`_relpath` fallback)

- **Venice API key can be sent to arbitrary hosts**: `MURANO_VENICE_BASE_URL` is accepted without host allowlisting, then used for both OpenAI client traffic and raw `/models` calls with `Authorization: Bearer <key>`. This violates the stated “key only to api.venice.ai” policy.
  - `src/murano/config.py:114-117`
  - `src/murano/venice.py:59`
  - `src/murano/venice.py:77-80`

## Design concerns

- **Security tests are too permissive and can pass for the wrong reason**: traversal tests accept `404` as success, which masks the prefix-bypass bug when target files don’t exist.
  - `tests/test_api.py:351-354`
  - `tests/test_api.py:383-385`

- **Policy/docs conflict on outbound host rules**: docs claim only outbound call is `api.venice.ai`, while code/docs also support arbitrary OpenAI-compatible endpoints; this weakens the threat model and operator expectations.
  - `README.md:9`
  - `README.md:54`
  - `src/murano/config.py:17-19`
  - `src/murano/api/server.py:63-65`

- **Integration instructions may break in PATH-restricted hosts**: Hermes/OpenClaw skill files use `command: murano` (not absolute path), while setup text mostly emphasizes absolute binary paths for JSON snippets. This can fail when host processes don’t inherit shell PATH.
  - `integrations/hermes/murano-skill.md:15-16`
  - `integrations/openclaw/murano-skill.yaml:27-28`
  - `integrations/README.md:22`
  - `integrations/README.md:57`

## What's good

- **Core retrieval/answer logic is actually centralized** (minimal transport drift risk): CLI, HTTP, and MCP all route through `chat.retriever` / `chat.answer` instead of duplicating RAG logic.
  - `src/murano/chat/retriever.py`
  - `src/murano/chat/answer.py`
  - `src/murano/api/routes.py:241`
  - `src/murano/mcp/server.py:319`

- **Keychain handling is disciplined**: key storage/read/delete is isolated in config and never printed/logged directly by command paths.
  - `src/murano/config.py:144-157`
  - `src/murano/cli.py:164-167`

- **Capture input validation is sensible at baseline**: requires absolute `http(s)` URLs and rejects obviously invalid schemes.
  - `src/murano/capture/web.py:204-208`

- **Dependency license auditing is non-trivial and useful**: supports mixed license-expression handling (OR clauses) rather than naive substring checks.
  - `src/murano/licenses.py:77-100`

- **Quality baseline currently green**: install/test/license commands succeeded as expected (`117 passed`, `murano licenses` reports no copyleft).
