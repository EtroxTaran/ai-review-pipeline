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
- [x] Phase 3.3 Wave 3 — `consensus.py`, `nachfrage.py`, `fix_loop.py`, `auto_fix.py` (+ `telegram_alert.py` als Phase-5-Legacy-Shim)
- [x] Phase 3.3 Wave 4a — Stage-Runner: `code_review.py`, `cursor_review.py`, `security_review.py`, `design_review.py`
- [x] Phase 3.3 Wave 4b — `stage.py` Orchestrator (StageConfig + build_arg_parser + run_stage, 52 Tests)
- [ ] Phase 3.2 — GitHub-Repo `EtroxTaran/ai-review-pipeline` anlegen + push (braucht User-Go)
- [ ] Phase 3.5b — `cli.py` für `ai-review` Console-Script (vereinfacht `ai-review-ac-validation.yml`)
- [ ] Phase 3.6 — Dogfooding: Pipeline reviewt sich selbst
- [ ] Phase 5 Legacy-Cleanup — `telegram_alert.py` entfernen, `consensus.py` auf `discord_notify` umstellen

**Current main:** 523/523 pytest green · Coverage 90.47% · stdlib + pyyaml + requests only.

## Module-Inventar (17 extrahiert + 1 neu Stage 5)

Top-level: `common`, `issue_parser`, `scoring`, `issue_context`, `metrics`, `metrics_summary`, `preflight`, `consensus`, `nachfrage`, `fix_loop`, `auto_fix`, `discord_notify`, `telegram_alert` (deprecated).
Stages: `ac_validation` (neu), `code_review`, `cursor_review`, `security_review`, `design_review`, `stage` (Orchestrator).

## License

MIT
