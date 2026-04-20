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

## Phase 3 Status

- [x] Phase 3.1 — Package skeleton + Stage 5 AC-Validation (28 tests, 96.81% cov)
- [x] Phase 3.3 — `common.py` extraction TDD (95 tests, 96% cov)
- [x] Phase 3.4 — `discord_notify.py` (33 tests, 98% cov, ops-n8n webhook)
- [x] Phase 3.5 — 10 workflow templates (9 ported + 1 new AC-validation)
- [x] Phase 3.3 Wave 2 — `scoring.py`, `issue_context.py`, `metrics.py`, `metrics_summary.py`, `preflight.py`
- [ ] Phase 3.2 — GitHub-Repo `EtroxTaran/ai-review-pipeline` anlegen + push (braucht User-Go)
- [ ] Phase 3.3 Wave 3 — `consensus.py`, `nachfrage.py`, `fix_loop.py`, `auto_fix.py`
- [ ] Phase 3.3 Wave 4 — Stage-Runner: `code_review.py`, `cursor_review.py`, `security_review.py`, `design_review.py`, `stage.py` (orchestrator)
- [ ] Phase 3.5b — `cli.py` für `ai-review` Console-Script (vereinfacht `ai-review-ac-validation.yml`)
- [ ] Phase 3.6 — Dogfooding: Pipeline reviewt sich selbst

**Current main:** 256/256 pytest green · Coverage 93.73% · stdlib + pyyaml + requests only.

## License

MIT
