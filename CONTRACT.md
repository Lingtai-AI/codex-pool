# codex-pool behavioral contract

## Scheduling: two rules only

1. **Full-prefix match → affinity.** If a request's `input` exactly extends
   (as a full item-by-item prefix) the current recorded baseline for a chain,
   under the same model-effective configuration, and the account that produced
   that baseline is still enabled, authenticated, and not known exhausted, the
   request is routed back to that account.
2. **No match → weighted load balance.** Otherwise the request goes to an
   unbiased weighted-random pick among enabled, authenticated, non-exhausted
   pool accounts.

There is no caller/session identifier and no client-visible concept of which
account served a request: routing itself reads only request content
(`input`/config-effective fields) and never any caller-supplied identity —
see "Upstream cache-affinity identity" below for the narrow, routing-blind
exception. Quota data that is unavailable or unknown never excludes an
account; only an explicit exhausted observation does.

## One current record per chain

Each affinity chain has at most one current record (content digest, length,
configuration digest, and account reference) at any time. This is not a
persisted transcript:

- A successful, complete turn replaces that one record.
- A failed, partial, or cancelled turn never calls `commit()`, so the previous
  record remains untouched. Success is never inferred from a match alone, and
  no output is fabricated to justify a commit.
- If concurrent requests extend the same current record, whichever commit
  writes last becomes the current record. Outputs are not merged or retained
  as additional alternatives.
- A later request built on already-replaced content simply misses and falls
  through to weighted load balance. It is not specially labeled or routed.
- Requests that match nothing receive ordinary fresh chain slots and are not
  retained as related history.

In short: an ordinary atomic dictionary update (`dict[chain_id] = new_record`)
under one lock, with no compare-and-swap or version table. Streaming responses
hold that lock only during the final commit.

## Input and upstream request shape

A plain string `input` is losslessly normalized to one user message before
routing. Array input is preserved. `input` and the client's `stream` preference
are transport concerns and are not part of affinity configuration. The known
model-effective fields used for configuration matching are `model`,
`instructions`, `tools`, `tool_choice`, `parallel_tool_calls`, `reasoning`,
`text`, `include`, `service_tier`, `temperature`, `top_p`, `max_output_tokens`,
and `truncation`.

### Affinity hash equivalence

Affinity hashes preserve the ordered full input prefix and all unknown fields;
they are not hashes of a broadly normalized or truncated summary. One narrow
representation equivalence is supported for ordinary assistant text: an input
item exactly shaped as `{"role": "assistant", "content": "<text>"}` hashes like
an output item exactly shaped as a `type: "message"`, `role: "assistant"`
message with one `type: "output_text"` part whose `text` is `<text>` and whose
`annotations` is absent or `[]`. The output message may additionally carry only
its known generated `id` (a non-empty string) and `status: "completed"`; those
known envelope fields are ignored for this equivalence. Changed text or role,
unknown fields, non-empty annotations, refusal/multimodal content, encrypted
reasoning, and tool-call names/IDs/arguments do not match. The wire request and
response payloads are never rewritten by this hashing-only canonicalization.

`store=False` and `background=False` are accepted as ordinary stateless and
synchronous values. The proxy always sends `store=False` to the Codex backend
and does not send `background`. Truthy `store` or `background`, plus
`previous_response_id` and `conversation`, are rejected clearly rather than
silently discarded. Other request fields are forwarded unchanged; unsupported
provider behavior remains an upstream error.

The proxy always adds `reasoning.encrypted_content` to the effective `include`
list (preserving supported caller-supplied `include` values and their order) before
both config hashing and the upstream call, matching what a native Codex
session requests on every turn. Config hashing and the forwarded body always
see this same effective `include` list, so this default cannot desync
affinity matching from what is actually sent. Unsupported non-null `include`
types (other than string or array) are rejected rather than silently discarded.

### Upstream cache-affinity identity

Independent of scheduling, the proxy resolves one stable per-conversation
identity per request and sends it upstream as the literal, underscored
`session_id` and `thread_id` HTTP headers plus the body `prompt_cache_key`
field (all three byte-identical), matching native Codex REST cache-affinity
behavior.

Precedence requires an explicit anchor: a body `prompt_cache_key` only wins,
and identity is only promoted to the `session_id`/`thread_id` headers, when
the caller *also* sends an explicit `session_id` or `thread_id` request
header. An ordinary Responses body `prompt_cache_key` alone (for example a
generic SDK's shared/model-global cache key) is not proof of per-caller
identity and is never promoted on its own — doing so could collapse unrelated
callers who happen to share one key onto the same upstream cache slot. Given
an anchor header, precedence is: explicit body `prompt_cache_key` first, else
`session_id`, else `thread_id`.

Ordinary callers that send no anchor header get the proxy's own per-chain id
instead — reused on prefix continuation, fresh on no-match — substituted for
*all three* upstream identity fields, including replacing any caller-supplied
body `prompt_cache_key`. This headerless substitution is an explicit
owner-layer adaptation for ordinary SDK clients: native bare construction
keeps a body-only key without identity headers. Here, no-match requests get
distinct identities even when their body-only cache keys are equal; actual
content-prefix continuations reuse the existing chain identity.

This identity is read-only input to the upstream request; it is never used
for and never overrides the two scheduling rules above, and no new required
header is introduced (both headers remain optional).

## Eligibility and authentication

An account is eligible only if it is enabled, has a valid auth file (or can
refresh one), and has no persisted observation proving quota exhaustion. A
quota read with no reliable numeric value clears the observation to unknown,
which remains eligible. Refreshing a token does not change account identity.

## Errors and state

- Unsupported request fields are rejected with a clear OpenAI-shaped error.
- There is no automatic cross-account retry on upstream failure: a failed turn
  returns an error to its caller.
- A response with no observable output is returned only when the upstream gave a
  known completed status; it does not advance affinity. Real streamed
  `response.output_item.done` items are preserved even if a terminal trailer's
  `response.output` is empty.
- No response/session transcript is persisted. Chain records live only in
  bounded process memory and are lost on restart.
- Account/pool settings and the small quota-exhaustion observation live in the
  package's own `pool.json`; auth credentials remain in the explicitly
  referenced auth file and are never included in status or errors.

See `ANATOMY.md` for the implementation map.
