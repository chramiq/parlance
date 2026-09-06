# ADR-0004: Model strategy: LiteLLM gateway, one model, output-mode ladder, thinking off for JSON

## The problem

v1 scraped the Gemini web UI with session cookies (`gemini_webapi`) — free in
the worst way: cookies rot, and structured output was a regex hunting for a
JSON code fence. The replacement must (a) run against an OpenAI-compatible
LiteLLM gateway with credentials from `.env`, (b) stay provider-agnostic so
any future key is a config change, and (c) emit *reliable* structured data
from models whose tool-calling quality is uneven and whose "thinking" modes
leak reasoning into the answer.

## What we tried first, and why it wasn't enough

Asking nicely for JSON (v1). Batch of 20 words, "respond ONLY with valid
JSON", regex fallback. It mostly worked because the model was strong — and
failed structurally: no validation loop, no retry with the error fed back,
no schema the model is mechanically held to.

## What we built instead

**pydantic-ai v2 over `OpenAIChatModel`** — the chat-completions class, not
the Responses-API one, because LiteLLM gateways speak Chat Completions and
the Responses API is OpenAI-specific surface:

```python
model = OpenAIChatModel(
    settings.model,  # one name, from .env
    provider=OpenAIProvider(base_url=settings.llm_base_url, api_key=settings.llm_api_key),
)
agent = Agent(model, output_type=[FlashcardBatch, str], retries={"output": 3})
```

One model name, one key, one base URL — everything provider-ish lives in
`.env`, so swapping model or gateway never touches code.

**Output-mode ladder.** pydantic-ai supports three structured-output modes;
the choice per deployment is data (what the gateway's model actually honors),
verified by a probe before the first real run:

1. `NativeOutput` — JSON-schema response_format, strongest when the model
   offers it natively.
2. `ToolOutput` (the library default) — output as a tool call; works with
   virtually everything that has function calling through an
   OpenAI-compatible gateway.
3. `PromptedOutput` — schema injected into instructions + lenient parse +
   validation retry. The floor for models whose tool calling proves flaky
   through the proxy.

**Thinking stays off the JSON path.** Thinking-style models can leak
reasoning into content or refuse forced tool choice (documented
provider-side); `ModelSettings` turns thinking off for extraction/generation
steps. Semantic validation does the real correctness work regardless:
`@agent.output_validator` raising `ModelRetry` ("sentence must contain a form
of lemma X", "cloze must be derivable from the sentence") feeds the error
back until the batch validates — the mechanism v1 faked with tenacity.

**Offline by default in dev.** `TestModel` auto-generates schema-valid output
and `FunctionModel` scripts the tool loop, so the agent — batch generation,
curation tools, grading later — is testable with zero API keys. Probe scripts
hit the real endpoint exactly once per integration, when the endpoint's
behavior is pinned (per the probe-first rule in AGENTS.md).

Model fitness for this workload (translate/draft/grade text into JSON):
any modern chat model available on the gateway is comfortably sufficient;
the binding constraint is JSON adherence, which the ladder above plus output
retries absorbs. Prefer a model with native JSON-schema output if the gateway
offers one — the probe says which.

## What this enables

- The entire migration off scraped cookies is one `.env` edit; a different
  key or gateway later is the same edit.
- Weak-model breakage degrades a rung on the ladder instead of corrupting a
  deck.
- `result.usage` aggregates tokens per run, so `generate` prints a token bill
  per 1000 lemmas before it spends.

## What it cost us

- `PromptedOutput` mode exists but is deliberately unwired from happy-path
  defaults; if a model needs it, that is a config smell to investigate, not a
  silent fallback.
- Provider quirks (single-system-message merges, non-strict tool JSON) are
  handled by `OpenAIModelProfile` knobs only when proven by a probe — we do
  not pre-configure for gateway bugs we have not met.

## Probe findings (recorded 2026-09, against the real gateway)

Everything above was verified against the live `.env` gateway. Facts that
change the design:

- **Envelope quirk (gateway routing metadata).** The gateway annotates
  responses with `metadata: {"weight_version": "default", "weight_versions":
  [{"version": "default", "start": 0, "end": N}]}` — a list of objects where
  the strict OpenAI SDK demands `dict[str, str]`, so response validation
  *crashes* for glm-5/5.2/5.3, kimi-k3, qwen3.8-*, mistral-medium-3.5.
  Unaffected: qwen3.5, command-a, gemma4, gpt-oss-120b, deepseek*.
- **The fix is an 8-line httpx response hook** run before SDK parsing:
  strip the `metadata` key from non-streaming `application/json` bodies
  (SSE streams pass through untouched), passed via
  `OpenAIProvider(openai_client=AsyncOpenAI(http_client=AsyncClient(event_hooks={"response": [hook]})))`.
  With the hook, **every probed model passes all three output modes**
  (tool, native JSON-schema, prompted). ToolOutput stays the default.
- **Thinking leakage is model-family-specific.** deepseek-v4-flash writes
  its chain-of-thought into `content` on plain-text prompts (fine for tool
  calls — unusable for plain-text steps). glm, kimi, qwen3.8, mistral return
  reasoning separately as `reasoning_content` — zero leakage into content.
  Rule stands: thinking off for JSON, and the structured modes carry it.
- **Quality tease.** On a trivial translate-to-Czech task, glm-5.3, kimi-k3
  and mistral were correct; qwen3.8-flash-next produced a wrong POS gloss.
- **Default: `glm-5.3` via the metadata hook; fallback: `kimi-k3`.**
- **STT for free:** the gateway serves `whisper-large-v3` — the speech
  roadmap (practice --voice) costs no extra service.

## Code

- `parlance/config.py` — settings (endpoint, key, one model name) + the
  metadata-stripping httpx hook (lifted verbatim from the probe)
- `parlance/agent.py` — the single agent on the shared model factory
- `tests/` — TestModel/FunctionModel coverage of every agent mode
