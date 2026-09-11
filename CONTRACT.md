# subs-pool contract

Version 0.1.0 ships one static built-in module: `codex`. The generic core owns
package identity, the two executable entrypoints, root dispatch, the static
registry, the JSON envelope, and the TUI lifecycle. It does not interpret
credentials, quota, provider protocols, routing, affinity, or Responses
conversation state.

`subspool` is the human interface: no arguments opens the Codex TUI;
`subspool codex ...` runs human Codex operations. `subspool-cli` is the Agent
interface: every ordinary completion writes exactly one JSON object and never
prompts, launches a browser, or opens the TUI.

Codex owns account/auth state, WHAM parsing, quota freshness, the sidecar,
refresh coordination, routing, chain/session affinity, upstream wire
behavior, and the foreground local Responses server. The Codex server keeps
the baseline hash/chain/session/no-replay/commit behavior. Quota is a gate
before upstream dispatch, not a retry or failover mechanism.

The root precedence is:

```text
generic_root = explicit SUBS_POOL_HOME, otherwise ~/.subs-pool
codex_root   = explicit CODEX_POOL_HOME, otherwise generic_root/codex
```

Both variables are path selections, not migration controls. Leading `~` is
expanded and relative values are resolved absolutely. Empty values are
configuration errors. No legacy root is scanned, copied, merged, or used as a
fallback. The existing Codex root keeps `pool.json` at its direct root.

```text
<codex_root>/pool.json
<codex_root>/quota-v1.json
<codex_root>/state.lock
<codex_root>/quota-v1.refresh.lock
```

`quota-v1.json` is separate, versioned, compact, atomic, and token-free. A
missing file means unchecked. Invalid or unsupported sidecars are preserved and
reported as state errors. Only a fresh successful sample for the current
account epoch, enabled/authenticated account, and non-exhausted known quota is
eligible. Unknown values, failed/checking states, and stale successes are not
current capacity.

Refreshes use the existing `filelock` dependency. The refresh lock elects one
pool-wide owner; the state lock is short and is never held during network
work. Account work is bounded and at most two requests run in one wave. TUI,
Agent CLI, human quota, and the foreground proxy use the same coordinator.

The proxy remains foreground-only, loopback-only, and defaults to
`--listen 127.0.0.1:8765`. It performs startup/periodic/JIT quota checks but
does not install a service or restart a generation on failure.

No provider/plugin discovery, service bootstrap, database, persistent
affinity, billing/history collector, logout/remove lifecycle, or extra account
provider is part of this release.
