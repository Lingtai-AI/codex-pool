# codex-pool

A standalone Python package: a CLI that manages a pool of Codex (ChatGPT
subscription) accounts and runs a local, loopback, OpenAI-Responses-API-
compatible server backed by that pool. An ordinary OpenAI SDK client points
`base_url` at the local server and never sees account selection — it needs no
session ID or Codex-specific detail to work. Toward the real Codex backend,
the server attaches its own stable per-conversation session identity to every
request (upstream-only metadata, see "Session identity and retention" below);
the pool is the only owner of that identity and ignores any caller-supplied
`prompt_cache_key` or `session_id`/`thread_id` headers.

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="<local access key>")
response = client.responses.create(model="gpt-5-codex", input=[{"role": "user", "content": "Hello"}])
print(response.output_text)
```

## Status

This is an early standalone implementation, not a published package release.
Accounts can be imported from an explicit Codex OAuth auth-file or logged in
through the supported device-code flow. Quota reads make one direct read-only
WHAM request with the existing account token; they do not refresh or write
auth, retry, start Codex, or copy credentials. Browser PKCE OAuth is not
exposed by the headless CLI.

Offline validation uses isolated mocked tests. The two-turn live
native-request parity check below predates the pool-owned session-identity
change; the new identity and retention behavior has not been live-validated.
An isolated live toy request through the production HTTP adapter
reported 0 cached tokens on the first turn and 7,168 of 7,416 input tokens on
its continuation; this is bounded provider evidence, not a quota/billing or
resident-agent migration claim. Live OAuth and native Windows/Linux terminal
use have not been validated. See `CONTRACT.md` and `CLI_CONTRACT.md` for supported behavior and
explicit exclusions; this is not a complete implementation of every Responses
API option.

## Install from source

Requires Python 3.11+ and uv. From a checkout of this repository:

```bash
uv venv .venv
uv pip install -e ".[test]" --python .venv/bin/python
.venv/bin/codex-pool tui
```

The example paths above are POSIX; on Windows use `.venv\Scripts\codex-pool.exe`.
The test extra is for development. There is no package-index release yet.
Activate the virtual environment before using the short `codex-pool` commands
below, or invoke the environment's executable explicitly.

## Terminal interface

Run `codex-pool` or `codex-pool tui` for the Textual interface. It is a frontend
to the same CLI: account import/device login, enable/disable, weights, status,
and quota. It does not implement a second auth or routing layer. Run
`codex-pool serve` separately for the local Responses server.

The accounts table shows independent primary and secondary **remaining**
meters, with actual window/reset/observation facts in the selected-account
detail. Press `u` to check quota for all accounts; press `r` to refresh account
metadata only. There is no automatic quota polling, retry, inferred window
name, or aggregate pool balance.

For device-code login, run `codex-pool accounts login personal --device` and
follow the displayed authorization URL/code. Do not share the code or auth-file
contents.

## CLI

See `CLI_CONTRACT.md` for the full, stable command/JSON contract. Quick
tour:

```bash
codex-pool accounts import personal --path ./fixtures/personal-auth.json
codex-pool accounts list --json
codex-pool pool weight personal 2
codex-pool pool disable personal
codex-pool status --json
CODEX_POOL_API_KEY="<choose-a-strong-local-key>" codex-pool serve --listen 127.0.0.1:8765
```

`CODEX_POOL_HOME` overrides the data-root (default `~/.codex-pool`); tests
always set it to an isolated temp directory.

## Running the server (user-managed)

You install, configure, launch, and supervise `codex-pool serve` yourself.
It is an ordinary foreground process; codex-pool ships no daemon, service
unit, autostart, or start/stop command. Clients, including LingTai, are
ordinary stateless Responses clients: they point `base_url` at the server and
do not install, start, stop, monitor, or restart it, and they do not supply or
track a pool session ID. Use your own terminal, tmux, or service manager if you
want it kept running.

Before starting any client:

1. Install from source (above); there is no package-index release.
2. Import or device-login at least one account, and check that
   `codex-pool status --json` reports `eligible_count` of at least 1.
3. Start the server on a loopback address with a local access key:
   `CODEX_POOL_API_KEY="<choose-a-strong-local-key>" codex-pool serve --listen 127.0.0.1:8765`
4. Wait until `GET http://127.0.0.1:8765/health` returns `{"status":"ok"}`,
   then configure clients with `base_url="http://127.0.0.1:8765/v1"` and the
   same key.

The TUI is a separate frontend and does not start or stop `serve`. If `serve`
exits, clients get connection errors until you restart it.

## Session identity and retention

The pool alone owns upstream session identity. For each request it first finds
the conversation's current record by content: the full input prefix under the
same effective config. That record's session ID is a stable label. It is sent
upstream as `prompt_cache_key`, `session_id`, and `thread_id`, and it keeps the
conversation's account affinity. A new conversation gets an ID from the
current time plus secure random bits, never from its content. Uniqueness is
probabilistic, not guaranteed by the clock. Continuations keep the existing
ID. Only a successful, completed response updates the record, and a failed or
partial one leaves it unchanged.

The server keeps the latest `CODEX_POOL_MAX_SESSIONS` session records (default
100000, a positive integer read once when `serve` starts). When that limit is
exceeded, the least recently updated session is evicted. Records live only in
memory: after eviction or a restart, that conversation's next request is
load-balanced and gets a new session ID. See `CONTRACT.md` for the exact
semantics.

## Architecture

See `ANATOMY.md` for where code lives and `CONTRACT.md` for the two
scheduling rules, one-latest-baseline semantics, and the deliberately narrow
assistant text hash equivalence this proxy implements.

## License / attribution

This package is Apache-2.0 (see `LICENSE`). `src/codex_pool/auth_codex.py`
is adapted from LingTai kernel `src/lingtai/auth/codex.py` (also
Apache-2.0); see `NOTICE`.
