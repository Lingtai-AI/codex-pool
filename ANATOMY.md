---
related_files:
  - README.md
  - CONTRACT.md
  - CLI_CONTRACT.md
  - pyproject.toml
  - .github/workflows/build.yml
  - .github/workflows/publish.yml
  - src/subs_pool/cli.py
  - src/subs_pool/agent_cli.py
  - src/subs_pool/registry.py
  - src/subs_pool/tui.py
  - src/subs_pool/modules/codex/tui.py
  - src/subs_pool/modules/codex/accounts.py
  - src/subs_pool/modules/codex/quota.py
  - src/subs_pool/modules/codex/quota_store.py
  - src/subs_pool/modules/codex/quota_refresh.py
  - src/subs_pool/modules/codex/routing.py
  - src/subs_pool/modules/codex/server.py
maintenance: Keep this map repo-relative and factual.
---

# subs-pool anatomy

The package has one explicit built-in module, Codex. There is no entry-point
discovery, provider framework, downloaded code, daemon, or generic inference
router.

The generic shell consists of `cli.py` (human `subspool` dispatch),
`agent_cli.py` (machine-only `subspool-cli` JSON envelopes), `registry.py`
(the static Codex mapping), `home.py` (the `SUBS_POOL_HOME` root), and `tui.py`
(Textual lifecycle and presentation). Codex local status, quota, and account
actions are owned by `modules/codex/tui.py` and run through its in-process
adapter; `cli_client.py` remains for the cancellable human device-login stream
and injected frontend tests.

The Codex module owns account configuration, auth and device login, WHAM
parsing, the versioned sidecar and refresh coordinator, strict quota-gated
routing, affinity, Responses/SSE behavior, and the foreground proxy.
`quota_store.py` is the only persisted quota authority. `quota_refresh.py`
coordinates one pool-wide bounded refresh across processes.

The selected Codex root is either explicit `CODEX_POOL_HOME` or
`<SUBS_POOL_HOME>/codex`, with `~/.subs-pool` as the generic default. Its
direct layout is:

```text
<codex-root>/pool.json
<codex-root>/quota-v1.json
<codex-root>/state.lock
<codex-root>/quota-v1.refresh.lock
<codex-root>/auth/...
```

The old `codex_pool` import package and `codex-pool` console alias are not
built. Provider wire identity strings containing `codex-pool` remain in the
Codex upstream adapter where baseline protocol tests require them.

`build.yml` runs tests, builds wheel/sdist, smoke-tests both installed
commands, and uploads artifacts only. `publish.yml` is the release publisher
tuple with `id-token: write` and deliberately no GitHub job environment.
