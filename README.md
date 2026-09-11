# codex-pool

A standalone Python package: a CLI that manages a pool of Codex (ChatGPT
subscription) accounts and runs a local, loopback, OpenAI-Responses-API-
compatible server backed by that pool. An ordinary OpenAI SDK client points
`base_url` at the local server and never sees account selection — it needs no
session ID or Codex-specific detail to work. Toward the real Codex backend,
the server attaches its own stable per-conversation cache-affinity identity
to every request (upstream-only metadata, see `CONTRACT.md`); a caller may
optionally send its own `session_id`/`thread_id` request headers to anchor
that identity instead, but this is never required.

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="<local access key>")
response = client.responses.create(model="gpt-5-codex", input=[{"role": "user", "content": "Hello"}])
print(response.output_text)
```

## Status

This is an early standalone implementation, not a published package release.
Accounts can be imported from an explicit Codex OAuth auth-file or logged in
through the supported device-code flow. Quota reads require the Codex CLI on
`PATH` and use a temporary app-server process. Browser PKCE OAuth is not exposed
by the headless CLI.

Validation covers 180 isolated mocked tests and a two-turn native-request
parity check. An isolated live toy request through the production HTTP adapter
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

## Architecture

See `ANATOMY.md` for where code lives and `CONTRACT.md` for the two
scheduling rules, one-latest-baseline semantics, and the deliberately narrow
assistant text hash equivalence this proxy implements.

## License / attribution

This package is Apache-2.0 (see `LICENSE`). `src/codex_pool/auth_codex.py`
is adapted from LingTai kernel `src/lingtai/auth/codex.py` (also
Apache-2.0); see `NOTICE`.
