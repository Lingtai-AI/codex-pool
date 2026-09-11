# subs-pool

`subs-pool` 0.1.0 is a small Codex subscription pool with two distinct
interfaces. `subspool` is the human TUI/foreground command surface;
`subspool-cli` is a JSON-only Agent surface. Codex is the only built-in
module. The package does not provide provider discovery, a service manager, a
database, billing/history collection, or automatic generation failover.

## Install

The target install once the project is published is:

```bash
uv tool install subs-pool
```

That command is a future target, not evidence that `subs-pool` is currently
available on PyPI. Until publication is independently verified, install from
a checkout:

```bash
git clone https://github.com/Lingtai-AI/subs-pool.git
cd subs-pool
uv venv .venv
uv pip install -e ".[test]" --python .venv/bin/python
.venv/bin/subspool --version
.venv/bin/subspool-cli --version
```

Python 3.11+ is required.

## Commands

```bash
subspool
subspool codex account import personal --path ./personal-auth.json
subspool codex account list
subspool codex account enable personal
subspool codex account weight personal 2
subspool codex status
subspool codex quota
subspool codex serve --listen 127.0.0.1:8765
```

The Agent equivalent emits exactly one JSON envelope and never prompts:

```bash
subspool-cli codex account list
subspool-cli codex quota
```

Agent login and server operations return stable unsupported/prompt-required
errors. Device login is available only from the human command:
`subspool codex account login personal --device`. Account import references an
existing auth file and never copies it.

## Roots and quota

The generic root is explicit `SUBS_POOL_HOME`, otherwise `~/.subs-pool`.
Codex uses explicit `CODEX_POOL_HOME` directly, otherwise
`<generic-root>/codex`. There is no old-home scan, migration, copy, merge, or
fallback. The selected Codex root contains:

```text
pool.json
quota-v1.json
state.lock
quota-v1.refresh.lock
```

`quota-v1.json` is the sole persisted quota authority. It is a versioned,
token-free atomic sidecar. A missing record is unchecked; failed, checking,
stale and unknown samples are not current capacity.
Only a fresh successful non-exhausted check for an enabled authenticated
account is eligible. The freshness limit is fixed at 60 seconds, with a
30-second background target. TUI, Agent CLI, human quota, and the foreground
proxy share one bounded `filelock` coordinator, without network work under
`state.lock`.

If the sidecar is malformed, preserve it for diagnosis, stop processes using
the selected root, and remove only the disposable `quota-v1.json` after making
a backup. Do not remove `pool.json` or credential files.

The baseline Codex `pool.json` location is retained. `quota_exhausted` in a
legacy file is ignored for routing and is not written as a second authority.
Stop old `codex-pool` processes before sharing an explicitly selected root;
old binaries do not participate in this locking/eligibility protocol.

## Foreground proxy

```bash
CODEX_POOL_API_KEY="choose-a-strong-local-key" \
  subspool codex serve --listen 127.0.0.1:8765
```

The proxy remains loopback-only and user-managed. It refreshes at startup,
periodically, and just before dispatch when needed. It returns a local
`quota_unavailable` error before upstream when no account passes the current
gate. It never replays a generation or silently moves a hard-bound
continuation.

The existing Codex upstream originator/user-agent strings are retained where
baseline protocol tests require them; they are provider wire facts, not
installed command names.

## Build and release checks

```bash
uv build
uv run --extra test pytest
```

`build.yml` tests, builds wheel/sdist, smoke-tests both installed entrypoints,
and uploads artifacts without publishing. `publish.yml` is the frozen release
publisher filename for `Lingtai-AI/subs-pool`; its publish job requests
`id-token: write` and intentionally has no GitHub job environment. It checks
the release tag/version before publishing via Trusted Publishing. No token is
stored in the repository.

See [CONTRACT.md](CONTRACT.md), [CLI_CONTRACT.md](CLI_CONTRACT.md), and
[ANATOMY.md](ANATOMY.md) for normative ownership and state details.
