# Codex CLI contract

Human operations are selected by `subspool codex`. Agent operations are
selected by `subspool-cli codex` and are wrapped by the root JSON envelope.

```text
account import ID --path AUTH.json [--weight N]
account list
account login ID --device       # human only
account enable ID
account disable ID
account weight ID N
status
quota
serve --listen 127.0.0.1:8765  # human only
```

Account list is metadata-only and never reports quota readiness. Account IDs
are any non-empty path-safe strings; `/`, `\`, `.`, and `..` are rejected,
with no additional ASCII or length restriction. Existing IDs remain readable.
Auth paths are explicit references; credentials
are not copied into a generic home. There is no logout/remove command in this
release.

`quota` always requests the shared bounded pool-wide refresh, whether or not a
proxy is running. Its data includes `module`, resolved `codex_root`,
`generated_at`, committed `snapshot_revision`, refresh outcome, account rows,
current sample, historical `last_success`, timestamps, freshness/age,
eligibility, exclusion reason, and sanitized error. Missing/failed/checking or
stale samples are not current values. A failed required refresh returns exit
3 while retaining the readable partial snapshot in Agent `data`.

The WHAM adapter makes one direct request per account, retains the baseline
nullable secondary-window parsing, calculates remaining as `100 - used` only
for valid finite percentages, and never fabricates zero for unknown. Provider
bodies, auth headers, tokens, device codes, and credential paths never enter
machine output or errors.

Agent `login` and `serve` return stable `prompt_required`/`unsupported_command`
errors; they never prompt, open a browser, bind a port, or write human text.
Agent parser/usage errors likewise produce one JSON object plus newline. Exit
codes are 0 success, 2 syntax/unsupported, 3 unavailable/incomplete quota or
auth/network work, 4 local state/configuration, 5 unexpected internal failure,
and 130 interruption.
