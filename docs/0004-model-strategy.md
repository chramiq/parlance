# ADR-0004: Model strategy: Gemini API direct, one key, tool arguments as the only structure

## The problem

Generation, audit, and the chat pre-pass all need one chat model that
(a) drives a reliable function-calling loop — the store tools are the
validation boundary (ADR-0005), so malformed structure must never reach the
store; (b) translates well for arbitrary language pairs, which is the
product's actual quality bar; and (c) asks for the smallest possible
credential surface, because every extra key, endpoint, and quota rule is
setup friction and a new failure mode.

## What we tried first, and why it wasn't enough

Routing through intermediate gateways to reach a model. A proxy adds a
base URL, a key, and a model namespace on top of the provider's own —
and every quirk at that boundary (nonstandard response envelopes, uneven
tool-calling quality per proxied model, reasoning tokens leaking into
content) becomes defensive code in the repo. Account-session clients
(borrowed cookies or CLI OAuth tokens) remove the API key entirely but
trade it for credential rot, rate opaqueness, and terms-of-service
exposure; neither supports the native function-calling contract the
agent depends on. Both approaches also leave translation quality to
whichever model the intermediary happens to serve.

## What we built instead

**One API key, one model name, direct to the provider.** The agent, audit,
and chat pre-pass all run on pydantic-ai's `GoogleModel` over
`GoogleProvider`, built by a single factory:

```python
model = GoogleModel(
    settings.gemini_model,
    provider=GoogleProvider(api_key=settings.gemini_api_key, retry_options=...),
)
```

`GEMINI_API_KEY` and `GEMINI_MODEL` are the whole LLM configuration; the
same key already serves the Live-API practice sessions, so the project's
entire credential surface is one key. Billing on the key's project unlocks
the paid tier — higher rate limits, Pro models, and prompts excluded from
training — while the monthly cloud credits that ship with the consumer
subscription absorb this workload's cost at these volumes.

**Transport retries at the provider.** Capacity spikes on flash-tier
models surface as transient 503s and quota bursts as 429s; both heal on
retry, so `HttpRetryOptions` (attempts with backoff on 408/429/5xx)
is configured once in the factory. Semantic failures stay the agent's own
affair: validation `ModelRetry`s feed errors back to the model, unchanged.

**No output-mode machinery.** Gemini's function calling is the strong
native path: agent output stays plain text, and every card, status change,
and profile enters the store as validated tool arguments exactly as
designed in ADR-0005. There is no JSON-mode ladder, no response-body
patching, no thinking-leak workaround — nothing between the schema and
the model.

**Offline by default in dev.** `TestModel` and `FunctionModel` cover every
agent mode with zero keys.

## What this enables

- Setup is two lines in `.env` next to the language pair; model upgrades
  (flash ↔ pro) are a one-value edit.
- Translation quality tracks a top multilingual model instead of a
  gateway's cheapest resident.
- `result.usage` still aggregates per run, and on a billed key the cost
  is real money — visible per run, cents per hundred cards.

## What it cost us

- Throughput depends on the key's tier: the degraded free tier (~20
  requests/day) is not viable for batch generation; a linked billing
  account is expected operation.
- Rate limits and quota behavior are Google's to change; the retry layer
  absorbs spikes, not policy shifts.

## Code

- `parlance/config.py` — `gemini_api_key`, `gemini_model` settings
- `parlance/agent.py` — the single agent + `build_model` factory
- `parlance/chats/translate.py` — pre-pass over the same factory
- `tests/` — TestModel/FunctionModel coverage of every agent mode
