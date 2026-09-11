# CLI contract

The installed commands are exactly `subspool` and `subspool-cli`.

Human commands:

```text
subspool                         # open Codex TUI
subspool --help
subspool --version
subspool modules [list]
subspool codex account import ID --path AUTH.json [--weight N]
subspool codex account list
subspool codex account login ID --device
subspool codex account enable ID
subspool codex account disable ID
subspool codex account weight ID N
subspool codex status
subspool codex quota
subspool codex serve [--listen 127.0.0.1:8765] [--api-key KEY]
```

The account import operation references an existing auth file; it does not
copy credentials. Device login remains human-only. There are no logout,
remove, service, provider-discovery, or plugin commands. `--listen` remains
the baseline loopback grammar and remote binds are rejected.

The Agent command is machine-only. It never invokes the TUI or a prompt:

```text
subspool-cli [--help|--version]
subspool-cli modules [list]
subspool-cli codex account list
subspool-cli codex account import ID --path AUTH.json [--weight N]
subspool-cli codex account enable|disable ID
subspool-cli codex account weight ID N
subspool-cli codex status
subspool-cli codex quota
```

Agent `codex account login`, `codex serve`, `tui`, unsupported commands, and
bad syntax return a structured error. Every ordinary completion is one UTF-8
JSON object and newline:

```json
{"schema_version":1,"command":"codex.quota","ok":true,"data":{},"error":null}
```

Failures use `ok:false` and an error object with `code`, `message`, and
`details`. Exit codes are `0` success, `2` syntax/unsupported, `3`
quota/auth/network unavailable or incomplete, `4` local state/configuration,
`5` unexpected internal failure, and `130` handled interruption. A successful
quota check may report exhausted accounts with exit `0`; a failed required
refresh exits `3` and includes the readable partial shared snapshot.

`account list` is metadata-only: it reports account ID, enabled/weight, local
auth presence/state, never quota percentages or a quota
derived ready flag. `codex quota` always requests a current pool-wide refresh
and returns module, resolved root, generated time, committed revision,
refresh outcome, account rows, current quota, explicitly historical
`last_success`, freshness/age, eligibility, and exclusion reason.

The TUI enters `CHECKING` before current values are shown. `u` forces a shared
refresh, `r` reloads metadata and performs the same refresh, and a one-second
inspection loop observes shared state and requests the 30-second background
target. Historical samples are labeled stale/last-success and are never
rendered as current green values.

Account references are any non-empty path-safe string: `/`, `\`, `.`, and `..`
are rejected, with no additional ASCII or length restriction. Agent parsing
consumes the complete token list; unknown, duplicate, or trailing options are
syntax errors and all help paths are state-free JSON responses.
