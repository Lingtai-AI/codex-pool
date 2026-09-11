---
related_files:
  - README.md
  - CONTRACT.md
  - CLI_CONTRACT.md
  - pyproject.toml
  - src/codex_pool/__init__.py
  - src/codex_pool/__main__.py
  - src/codex_pool/accounts.py
  - src/codex_pool/auth_codex.py
  - src/codex_pool/chain.py
  - src/codex_pool/cli.py
  - src/codex_pool/cli_client.py
  - src/codex_pool/device_login.py
  - src/codex_pool/errors.py
  - src/codex_pool/hashing.py
  - src/codex_pool/home.py
  - src/codex_pool/quota.py
  - src/codex_pool/routing.py
  - src/codex_pool/server.py
  - src/codex_pool/sse.py
  - src/codex_pool/tui.py
  - src/codex_pool/upstream.py
maintenance: |
  Keep related_files repo-relative and real. Update this map when ownership or
  composition changes; CONTRACT.md owns behavior, not this structural map.
---

# codex-pool Anatomy

Where the code lives and how it is composed. See `CONTRACT.md` for the
behavioral promises (two scheduling rules, one-current-baseline semantics,
error/redaction behavior, and the local CLI/TUI boundary).

## Components

- `src/codex_pool/home.py` — `data_home()` / `pool_state_path()`: resolves
  the `CODEX_POOL_HOME`-overridable data root every other component reads and
  writes under.
- `src/codex_pool/accounts.py` — `Account`, `AccountStore`: owns pool state.
  Persists `<home>/pool.json` (ref → auth path, enabled/weight, and the
  bounded quota-exhaustion observation), atomic write + `FileLock`.
  `Account.to_status_dict()` is the only account view exposed to CLI/TUI/API;
  it never contains token contents or the internal quota marker.
- `src/codex_pool/auth_codex.py` — `CodexTokenManager`, `CodexAuthError`,
  `is_token_expired_error`, `is_usage_limit_reached_error`. Adapted from the
  LingTai kernel `src/lingtai/auth/codex.py` (Apache-2.0; see `NOTICE`). Reads
  and refreshes one Codex OAuth token file by explicit account path.
- `src/codex_pool/device_login.py` — headless device-code OAuth flow adapted
  from the shipping LingTai TUI OAuth source. It is dependency-injected for
  mocked HTTP tests, writes the resulting token bundle atomically, and emits
  only the pinned authorization/completed JSONL events.
- `src/codex_pool/quota.py` — Codex CLI `app-server` JSON-RPC quota reader,
  adapted from the LingTai kernel quota source. Uses an owned temporary
  `CODEX_HOME`, never mutates the account auth file, and reports unknown data
  as `None` rather than zero.
- `src/codex_pool/hashing.py` — `canonical_json`, `item_hash`,
  `rolling_hashes`, `extend_hash`, `config_hash`: order-preserving content
  hashing with the narrow plain-assistant equivalence in `CONTRACT.md`, used
  only as a routing index.
- `src/codex_pool/chain.py` — `ChainStore`, `Baseline`, `MatchResult`,
  `new_session_id`, `DEFAULT_MAX_SESSIONS`: bounded in-memory
  one-current-record-per-chain table (latest N by commit order, default
  10000). Issues the pool-owned time+random session id for each new chain,
  never reusing a retained id. `commit()` replaces the current record under
  one lock; a new chain's first commit never overwrites a retained id.
- `src/codex_pool/routing.py` — `select_account`, `weighted_choice`,
  `eligible_refs`: full-prefix affinity and weighted load balancing over
  authenticated, enabled, non-exhausted accounts.
- `src/codex_pool/sse.py` — `SSEDecoder`, `encode_event`: incremental UTF-8,
  CR/LF-aware SSE parsing/encoding shared by server and HTTP upstream.
- `src/codex_pool/upstream.py` — `Upstream` protocol and the one production
  `CodexHTTPUpstream` adapter. Its base URL is fixed by construction; tests
  inject an `httpx` transport and do not contact the live endpoint. Builds the
  native-parity wire headers (honest `codex-pool` originator/User-Agent, no
  beta header, `Accept: application/json`, and the `session_id`/`thread_id`/
  turn-metadata headers for the identity `server.py` resolves) — it never
  derives that identity itself.
- `src/codex_pool/errors.py` — local typed errors mapped to OpenAI-shaped
  response bodies.
- `src/codex_pool/server.py` — `create_app()`: Starlette app exposing
  `POST /v1/responses` and `GET /health`. Wires bearer auth, request
  normalization/validation, routing, upstream driving, streaming versus
  aggregated response shaping, and commit-on-complete success. Also resolves
  the native-parity `reasoning.encrypted_content` include default (applied
  before config hashing) and forwards the routed chain id to `upstream.py` as
  the sole upstream session identity; caller `prompt_cache_key` and
  `session_id`/`thread_id` headers never override it. Effective include
  participates in config-based affinity.
- `src/codex_pool/cli_client.py` — thin async subprocess wrapper for the
  frozen CLI JSON/JSONL contract. Owns child cleanup and never provider/auth
  logic.
- `src/codex_pool/tui.py` — thin Textual frontend. Renders CLI facts and sends
  all actions through `CLIClient`; it does not read account/auth/provider
  state directly.
- `src/codex_pool/cli.py` — command composition root: account import/list/
  device login, pool mutations, status, quota, serve (validates
  `CODEX_POOL_MAX_SESSIONS` at startup), and TUI dispatch. See
  `CLI_CONTRACT.md` for the stable machine surface.

## Connections

- `cli.py` → `accounts.py` for pool mutations; `device_login.py` for login;
  `quota.py` for quota reads/observations; `auth_codex.py` for status;
  `server.py` + `upstream.py` + `chain.py` for `serve`; `tui.py` for TUI
  dispatch.
- `tui.py` → `cli_client.py` only. `CLIClient` invokes the local CLI through
  `[sys.executable, "-m", "codex_pool", ...]` and parses its stable output.
- `server.py` → `accounts.py` (account records), `routing.py` (selection),
  `auth_codex.py` (access token/account id), `upstream.py` (injected
  production/test adapter), `chain.py` (success commit), and `sse.py` (client
  stream encoding).
- `routing.py` → `accounts.py`, `auth_codex.py`, and `chain.py`.
- `chain.py` → `hashing.py` only.
- `upstream.py` → `sse.py` and `errors.py`.

## Composition

Flat, single-package layout (`src/codex_pool/`). `cli.py` is the operation
composition root: it constructs a concrete `AccountStore`, `ChainStore`, and
`CodexHTTPUpstream` for `serve`. `server.py` takes all three as constructor
arguments, which lets tests inject a fake upstream.

## State

- Persistent package state: `<CODEX_POOL_HOME>/pool.json` stores non-secret
  account refs, explicit auth-file paths, enabled/weight, and the small
  quota-exhaustion observation. The account's referenced auth file is owned
  by its writer; `auth_codex.py` refreshes it in place.
- Ephemeral service state: `ChainStore._records` is process-local memory
  bounded by `CODEX_POOL_MAX_SESSIONS` and is lost on restart. No
  response/session transcript is stored.
- Quota reads use a process-owned temporary native Codex home that is cleaned
  after each read; it is not the package's persistent state.

## Notes

- The Textual frontend is mandatory package functionality but remains thin and
  CLI-driven. It does not implement account, auth, quota, or HTTP behavior.
- Device-code login is the supported headless mode. Browser PKCE OAuth needs a
  real local callback/browser UX and is reported as intentionally unsupported
  by `CLI_CONTRACT.md`; no endpoint is invented here.
- Live OAuth, live Codex requests, and installation/publishing acceptance are
  not performed by this file-edit worker. Mocked tests are authored for the
  parent to run.
