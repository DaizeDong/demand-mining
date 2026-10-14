# Contributing

demand-mining follows the Skill Repo Spec v1 and [PHILOSOPHY.md](PHILOSOPHY.md).
Changes must preserve the current runtime and privacy contracts.

## Ground rules

- Redact supported personal data before model processing or persistence. Keep real observations,
  credentials and operational history in the PRIVATE companion. Credential backup may
  use the selected Mode A or Mode B policy in [CONFIG.md](CONFIG.md#secrets-mode-b-e6).
- Keep model interpretation separate from deterministic scoring, evidence admission and delivery
  state. The shipped Discord tap and daemon own demand collection; external research uses the
  interfaces and support limits in [the delegation reference](skills/demand-mining/reference/delegation.md).
- Use only synthetic examples. Never copy real conversation into documentation, changelog entries
  or commit messages.

## Workflow

1. Implement the change and update the affected entry documents and references together.
2. Run the applicable pinned Guards and Style checks used by CI. Documentation changes use
   `python style/tools/doc_contract.py --root . --profile skill --stage accepted`.
3. Preserve `.githooks/` forwarding to pinned Guards. Restore missing submodules with
   `git submodule update --init --recursive`; do not vendor or bypass scanners.
4. For a release, align plugin metadata, README badges, ROADMAP and CHANGELOG. Documentation-only
   maintenance does not require a version bump. Preserve released history and use Unreleased for
   current changes.
