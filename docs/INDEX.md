# Architecture Decision Records

ADRs are the source of truth for why things are the way they are in this
project. Each ADR is a design-spec narrative: the problem, why the obvious
answers don't work, and the system built instead.

## Rules

- Everything merged is `Accepted`. No statuses, no dates; git history knows.
- Never reference other design/plan documents. ADRs may reference each other
  and code (paths at the bottom of the file).
- One ADR is proposed, reviewed, merged; only then the next one starts.

## Template

```markdown
# ADR-NNNN: <Title>

## The problem

## What we tried first, and why it wasn't enough

## What we built instead

## What this enables

## What it cost us

## Code

- `parlance/...` — what implements it
- `tests/...` — what proves it
```

## Index

| #    | Title |
|------|-------|
| 0001 | [SQLite as source of truth; agents mutate it, decks are projections](0001-store-centric-architecture.md) |
| 0002 | [Chat-sim cards and the grading split: self-grade on mobile, existing add-ons on desktop, LLM in CLI](0002-chat-cards-and-grading.md) |
| 0003 | [Deck lifecycle: stable GUIDs update in place; retirement is explicit](0003-deck-lifecycle.md) |
| 0004 | [Model strategy: Gemini API direct, one key, tool arguments as the only structure](0004-model-strategy.md) |
| 0005 | [The agent: tool-argument schemas as the validation boundary, restart-loop driver, pair-profile init](0005-agent-design.md) |
| 0006 | [Practice subsystem: voice drills with persistent learner memory](0006-practice-design.md) |
| 0007 | [Personal wordlist from chat exports: word-bag ingestion behind a public-frequency gate](0007-personal-wordlist-ingestion.md) |
| 0008 | [The personal deck: a mode flag over the shared pipeline, not a second pipeline](0008-personal-deck-mode.md) |
| 0009 | [Anki review ingestion: desktop Anki as a read-only relay](0009-anki-review-ingestion.md) |
| 0010 | [Listen cards: ear training gated on review history](0010-listen-cards.md) |
