# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [0.1.0] — 2026-04-20

Initial release. Extracted from `EtroxTaran/ai-portal`. Full Phase 3 implementation
complete (562 tests, coverage 90.80%). See PR [#2](https://github.com/EtroxTaran/ai-review-pipeline/pull/2)
and Issue [#1](https://github.com/EtroxTaran/ai-review-pipeline/issues/1).

### Added

**Stage 5 — AC-Validation**
- `stages/ac_validation.py` — Acceptance-Criteria coverage validator (Codex primary + Claude second-opinion)
- `issue_parser.py` — Gherkin `Given/When/Then` AC parser + `Closes #N` resolver

**Discord Notifications**
- `discord_notify.py` — ops-n8n webhook bridge; sticky message support, `@here` mentions,
  disagreement alerts, per-stage result posting

**Pipeline Modules (16 extracted from ai-portal + 1 new)**
- `common.py` — Shared constants, HTTP helpers, commit status posting (95 tests, cov 96%)
- `scoring.py` — Score normalization and confidence weighting (14 tests, cov 87%)
- `issue_context.py` — GitHub Issue context fetcher (25 tests, cov 85%)
- `metrics.py` — Per-stage metrics collection (12 tests, cov 91%)
- `metrics_summary.py` — Aggregate metrics reporting (27 tests, cov 95%)
- `preflight.py` — CLI OAuth presence check (22 tests, cov 95%)
- `consensus.py` — Confidence-weighted consensus aggregator; avg ≥ 8 success, 5–7 soft,
  < 5 failure; Fail-Closed on missing stage (59 tests, cov 96%)
- `nachfrage.py` — Soft-consensus human-ACK handler (13 tests, cov 94%)
- `fix_loop.py` — Iterative multi-pass fix loop (25 tests, cov 82%)
- `auto_fix.py` — Single-pass auto-fix agent; max 10 files, rollback on failure (37 tests, cov 92%)
- `stages/code_review.py` — Stage 1 runner: Codex (gpt-5) (18 tests, cov 87%)
- `stages/cursor_review.py` — Stage 1b runner: Cursor Agent (composer-2) (15 tests, cov 94%)
- `stages/security_review.py` — Stage 2 runner: Gemini 2.5 Pro + semgrep (21 tests, cov 97%)
- `stages/design_review.py` — Stage 3 runner: Claude Opus 4.7 (27 tests, cov 94%)
- `stages/stage.py` — Stage orchestrator: `StageConfig`, `build_arg_parser`, `run_stage`;
  stub auto-resolved in 4 runners (52 tests, cov 80%)
- `stages/ac_validation.py` — Stage 5: AC-Validation (new, not extracted) (28 tests, cov 96.81%)

**CLI**
- `cli.py` — Unified `ai-review` console script with subcommands:
  `stage`, `consensus`, `nachfrage`, `auto-fix`, `fix-loop`, `ac-validate`, `metrics`,
  `--version`, `--help` (24 tests, cov 83%)
- Shadow CLI flags (closes [#1](https://github.com/EtroxTaran/ai-review-pipeline/issues/1)):
  - `--status-context-prefix` — rewrites all stage commit-status contexts under a new prefix
    (enables parallel shadow-pipeline runs)
  - `--status-context` — overrides the consensus status context name
  - `--discord-channel` — overrides the target Discord channel ID per run
  - `--no-ping` — suppresses `@here` / role mentions in Discord notifications

**Workflow Templates (10 files)**
- `workflows/ai-code-review.yml` — Stage 1: Codex code review + fix-loop
- `workflows/ai-cursor-review.yml` — Stage 1b: Cursor second-opinion review
- `workflows/ai-design-review.yml` — Stage 3: Claude Opus 4.7 design review
- `workflows/ai-review-ac-validation.yml` — Stage 5: AC-Coverage validation (new)
- `workflows/ai-review-auto-escalate.yml` — Cron: stale Nachfrage escalation (30 min timeout)
- `workflows/ai-review-auto-fix.yml` — Manual: cross-stage auto-fix agent
- `workflows/ai-review-consensus.yml` — Consensus aggregation + Discord result
- `workflows/ai-review-nachfrage.yml` — Soft-consensus human-ACK handler
- `workflows/ai-review-scope-check.yml` — PR scope validation
- `workflows/ai-security-review.yml` — Stage 2: Gemini + semgrep security review

**gh Extension**
- `gh-extension/gh-ai-review` — GitHub CLI extension: `install`, `verify`, `uninstall`, `update`

**Dogfood Scaffolding**
- `.github/workflows/ci.yml` — Test, lint (ruff), YAML lint CI pipeline
- `.github/workflows/dogfood.yml` — Self-review via own pipeline (manual trigger)
- `.ai-review/config.yaml` — Self-config for dogfood (self-hosted r2d2 runner)
- `.github/PULL_REQUEST_TEMPLATE.md` — PR template with AC-linkage requirements
- `.github/ISSUE_TEMPLATE/` — Issue forms with Gherkin AC scaffolding

**Documentation**
- `docs/project-adoption.md` — Step-by-step adoption guide for new projects
- `docs/messaging-bridge.md` — ops-n8n Discord bridge setup
- `docs/discord-tailscale-funnel.md` — Tailscale Funnel ingress setup
- `docs/acceptance-criteria-style.md` — Gherkin AC authoring guide
- `workflows/README.md` — Consumer workflow template reference
- `schema/config.schema.yaml` — JSON Schema (draft-07) for `.ai-review/config.yaml`
- `schema/config.example.yaml` — Full annotated example config

### Changed

- **Discord replaces Telegram** — `consensus.py` and `stage.py` now use `discord_notify.py`
  instead of the legacy `telegram_alert.py`; all 10 workflow templates use
  `DISCORD_NOTIFICATION_WEBHOOK` (was `TELEGRAM_NOTIFICATION_WEBHOOK`)
- All 7 workflow templates migrated from `python -m ai_review_pipeline.stages.*` inline calls
  to the unified `ai-review` CLI (Wave 5 migration)
- Reviewer model defaults updated to current versions:
  Codex `gpt-5`, Cursor `composer-2`, Gemini `gemini-2.5-pro`, Claude `claude-opus-4-7`

### Removed

- `telegram_alert.py` — Phase-5 pre-emptive cleanup; Telegram channel retired, Discord is the
  sole notification target per Engineering Rules §12

### Fixed

- Stub auto-resolution in 4 Stage Runners (`code_review`, `cursor_review`, `security_review`,
  `design_review`) after `stage.py` orchestrator merge — runners now correctly delegate to
  `stage.py` instead of carrying inline stubs

---

[Unreleased]: https://github.com/EtroxTaran/ai-review-pipeline/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/EtroxTaran/ai-review-pipeline/releases/tag/v0.1.0
