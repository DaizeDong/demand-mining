# Contributing

demand-mining follows the **Skill Repo Spec v1** and the philosophy in `PHILOSOPHY.md`. Before any
change, ask the governing test: *does it fix the framing, or just patch a symptom?*

## Ground rules

- **Privacy is non-negotiable.** Anything that touches a user message must keep redact-on-ingest
  first. Never store raw conversation; never commit secrets (Mode B, `secrets/` is gitignored).
- **LLM proposes, the gate disposes.** Keep judgement in SKILL.md and rulings in `scripts/` pure
  functions. New scoring/dedup logic needs a deterministic test, not a prompt tweak.
- **Own the seam, delegate engines.** Do not add a 2nd Discord bot, a hotspot collector, or a
  competitor scraper here, delegate (auto-support / daily-hotspots / market-intel).

## Workflow

1. Add/adjust a test in `skills/demand-mining/tests/` first (eval-driven).
2. Implement the minimal change in `scripts/`.
3. `python -m pytest skills/demand-mining/tests/ -q` must stay green.
4. Run the acceptance gate: `check_conformance.py`, `budget_check.py`, `dedup_check.py`.
5. Keep the four version sources in lock-step (plugin.json · README badge · ROADMAP · CHANGELOG).

Every discovered mis-judgement / false-merge / PII-leak class should become a new adversarial
fixture in the matching T-suite, that is how the skill earns "tested-real".
