# codex-pool CLI ↔ frontend contract (frozen)

This is the exact command/JSON surface driven by the Textual frontend. It is
not a new SDK. Invocation is `[sys.executable, "-m", "codex_pool", ...]` or
the installed `codex-pool` entry point. Machine-facing commands accept
`--json`; JSON output is one object (or one JSON-Lines stream for device login)
and is never mixed with human-readable output on the same stream.

## Global JSON error shape

An ordinary `--json` command failure exits nonzero, leaves stdout empty, and
writes exactly one line to stderr:

```json
{"error":"<human-readable, nonsecret message>"}
```

Argparse-level errors honor `--json` as well. Credential/token values,
provider response bodies, and auth-file contents never appear in output or
errors.

## Account and pool commands

- `accounts import REF --path PATH [--weight N] --json` imports an explicit,
  user-provided local auth-file path and prints one account status object.
- `accounts list --json` prints `{"accounts":[<status>...]}`.
- `accounts login REF --device --json` runs device-code OAuth in this CLI
  process and streams JSONL events. `--weight N` may set the weight of a new
  ref and is ignored when re-login updates an existing ref.
- `pool enable REF --json`, `pool disable REF --json`, and
  `pool weight REF N --json` print one account status object.

The shared status object is exactly:

```json
{"ref":"example","enabled":true,"weight":1,"auth_present":true,"quota":"unknown"}
```

It never carries live quota readings or auth contents. Re-login preserves the
existing ref's enabled/weight state and resets any stale internal quota
observation.

## Device-code login

Only `--device` login is implemented. Omitting it is a nonzero JSON error that
names the unsupported browser OAuth gap. The flow uses the real device-code
endpoints/client id/poll rules adapted from the shipping LingTai TUI source;
there is no invented fallback:

1. `{"event":"authorization_required","verification_uri":"https://auth.openai.com/codex/device","user_code":"ABCD-1234","expires_in":900,"interval":5}`
2. `{"event":"completed","account":<status>}` on success.

The CLI flushes each event. A failure exits nonzero with one nonsecret JSON
error on stderr, even if event 1 was already printed. No access token, refresh
token, id token, code verifier, or raw provider response is emitted.
Polling remains inside this process, never in the TUI.

The browser PKCE flow in the source (`local HTTP listener`, system browser,
and callback on a local port) is intentionally unsupported in this one-shot
headless CLI. The command reports that gap instead of silently inventing a
flow.

## Status and quota

- `status --json` prints `{"accounts":[<status with authenticated>...],"eligible_count":N}`.
  `eligible_count` counts enabled, authenticated accounts that are not known
  exhausted. Unknown quota remains eligible; no liveness claim is included.
- `quota --json` reads each imported account through a throwaway `codex
  app-server` JSON-RPC process and prints:

```json
{"accounts":[{"ref":"example","quota":{
  "primary_used_percent":30,
  "secondary_used_percent":null,
  "primary_reset_at":null,
  "secondary_reset_at":null,
  "observed_at":"2026-09-10T04:55:00+00:00"
}}]}
```

`primary_used_percent` is the source-verified
`rateLimits.primary.usedPercent` field. The symmetric secondary field is
parsed defensively when present but is not source-verified. Reset timestamps
are always null because no field name exists in the available source. Per-
account failures keep all numeric values null (never zero), add a short
nonsecret `error` reason, and do not fail other accounts or imply exhaustion.
`observed_at` is always a real UTC ISO-8601 timestamp.

A reading proving primary or secondary usage is 100% is stored as the
package's small quota observation and makes that account ineligible until a
later read proves it non-exhausted or unknown. Unknown/failure clears the
observation and remains eligible.

## Local server and TUI

- `serve --listen 127.0.0.1:8765 [--api-key KEY | $CODEX_POOL_API_KEY] --json`
  runs uvicorn in the foreground. The host must be `127.0.0.1`, `localhost`,
  or `::1`; remote listening is not supported. Port must be an integer in
  `1..65535`. A missing key is a nonzero JSON error, and the key is never
  echoed.
- `tui` and no subcommand dispatch to `codex_pool.tui.run_tui()`.
  The Textual frontend is a thin CLI subprocess client: it renders returned
  facts and sends import/login/pool/quota actions through the frozen surface.
  It never reads auth/token files or performs provider calls.
