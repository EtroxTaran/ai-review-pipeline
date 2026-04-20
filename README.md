# ai-review-pipeline

Multi-stage AI review pipeline, extracted from `ai-portal` for use across all Nico+Sabine projects.

**Status:** Phase 3.1 — Package skeleton + Stage 5 (AC Validation) implemented TDD-first. Stages 1-4 extraction pending.

## Components

- `src/ai_review_pipeline/` — Python package
  - `issue_parser.py` — Gherkin AC + `Closes #N` resolver
  - `stages/ac_validation.py` — Stage 5 (Acceptance-Criteria Coverage)
- `schema/config.schema.yaml` — JSON Schema for `.ai-review/config.yaml`
- `gh-extension/gh-ai-review` — GitHub CLI extension (install/verify/uninstall/update)
- `workflows/` — Actions templates (pending extraction from ai-portal)
- `prompts/defaults/` — Overridable prompt templates (pending)

## Installation (once published)

```bash
pip install ai-review-pipeline
gh extension install EtroxTaran/gh-ai-review
gh ai-review install
```

## Development

```bash
pip install -e '.[dev]'
pytest --cov
```

## Context

Plan: `~/.claude/plans/reports-projects-ai-portal-docs-v2-40-a-iridescent-flask.md` — Phase 3.

## Phase 3.1 Status

- [x] Package skeleton (pyproject, src layout)
- [x] `issue_parser.py` — 18 tests green
- [x] `stages/ac_validation.py` — 10 tests green
- [x] `schema/config.schema.yaml` + example
- [x] `gh-ai-review` extension skeleton
- [ ] Extract `common.py`, `consensus.py`, `scoring.py`, `fix_loop.py` (Phase 3.2)
- [ ] Extract individual stage runners (`code_review.py`, `security_review.py`, `design_review.py`) (Phase 3.3)
- [ ] Port `telegram_alert.py` → `discord_notify.py` (Phase 3.4)
- [ ] Extract + parameterize workflow templates (Phase 3.5)
- [ ] Dogfood: pipeline reviewt sich selbst (Phase 3.6)

## License

MIT
