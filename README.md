# ai-review-pipeline

Multi-stage AI code review pipeline for GitHub Actions — extracted from `ai-portal` for reuse
across all projects. Confidence-weighted consensus from 5 AI reviewers (Codex, Cursor, Gemini,
Claude, AC-Judge), Discord notifications via ops-n8n, and a unified `ai-review` CLI.

[![CI](https://github.com/EtroxTaran/ai-review-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/EtroxTaran/ai-review-pipeline/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-90.80%25-brightgreen)](https://github.com/EtroxTaran/ai-review-pipeline)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Features

- **5-Stage review pipeline** — Code (Codex), Code-Cursor (Cursor), Security (Gemini + semgrep),
  Design (Claude Opus 4.7), AC-Validation (Codex primary + Claude second-opinion)
- **Gherkin Acceptance-Criteria** — `Given/When/Then` blocks parsed from linked GitHub Issues;
  1:1 AC↔Test coverage enforced, Fail-Closed on missing issue
- **Discord Bridge** — Consensus results, disagreement alerts, and sticky pipeline reports
  delivered to Nathan-Ops Guild via ops-n8n webhook
- **10 Workflow Templates** — Drop-in GitHub Actions YAML for consumer repos (9 extracted stages
  + 1 new AC-validation)
- **`gh ai-review` Extension** — `install`, `verify`, `uninstall`, `update` commands for
  bootstrapping a new repo in one step
- **Unified `ai-review` CLI** — `stage`, `consensus`, `auto-fix`, `fix-loop`, `ac-validate`,
  `metrics` subcommands; shadow CLI flags for multi-pipeline parallelism
  (`--status-context-prefix`, `--status-context`, `--discord-channel`, `--no-ping`)
- **Confidence-weighted Consensus** — avg ≥ 8 = success, 5–7 = soft (Nachfrage), < 5 = failure;
  Fail-Closed on missing stage

---

## Module Inventory

### Top-Level (`src/ai_review_pipeline/`)

| Module | Description |
|---|---|
| `common.py` | Shared constants, HTTP helpers, status posting |
| `issue_parser.py` | Gherkin AC + `Closes #N` resolver |
| `scoring.py` | Score normalization + weighting |
| `issue_context.py` | GitHub Issue context fetcher |
| `metrics.py` | Per-stage metrics collection |
| `metrics_summary.py` | Aggregate metrics reporting |
| `preflight.py` | CLI OAuth presence check |
| `consensus.py` | Confidence-weighted consensus aggregator |
| `nachfrage.py` | Soft-consensus human-ACK handler |
| `fix_loop.py` | Iterative multi-pass fix loop |
| `auto_fix.py` | Single-pass auto-fix agent |
| `discord_notify.py` | Discord notification bridge (ops-n8n webhook) |
| `cli.py` | Unified `ai-review` console entry point |

### Stage Runners (`src/ai_review_pipeline/stages/`)

| Module | Stage | Reviewer |
|---|---|---|
| `code_review.py` | Stage 1 | Codex (gpt-5) |
| `cursor_review.py` | Stage 1b | Cursor Agent (composer-2) |
| `security_review.py` | Stage 2 | Gemini 2.5 Pro + semgrep |
| `design_review.py` | Stage 3 | Claude Opus 4.7 |
| `ac_validation.py` | Stage 5 | Codex primary + Claude second-opinion |
| `stage.py` | Orchestrator | `StageConfig`, `build_arg_parser`, `run_stage` |

**Total: 17 extracted modules + 1 new Stage 5 = 18 modules. 562 tests, coverage 90.80%.**

---

## Installation

Until the package is published to PyPI, install directly from GitHub:

```bash
pip install git+https://github.com/EtroxTaran/ai-review-pipeline.git
```

Install the `gh` extension for one-step repo bootstrap:

```bash
gh extension install EtroxTaran/gh-ai-review
```

---

## Quickstart

### 1. Bootstrap a new repo

```bash
# Copies workflow templates + creates .ai-review/config.yaml scaffold
gh ai-review install
```

### 2. Configure the pipeline

Edit `.ai-review/config.yaml` in your repo:

```yaml
version: "1.0"

reviewers:
  codex: gpt-5
  cursor: composer-2
  gemini: gemini-2.5-pro
  claude: claude-opus-4-7

stages:
  code_review:
    enabled: true
    blocking: true
  security:
    enabled: true
    blocking: true
  design:
    enabled: false        # opt-in for UI repos
  ac_validation:
    enabled: true
    blocking: true
    min_coverage: 1.0     # 100% AC-Coverage enforced

consensus:
  success_threshold: 8    # avg >= 8 = success
  soft_threshold: 5       # 5-7 = soft Nachfrage
  fail_closed_on_missing_stage: true

notifications:
  target: discord
  discord:
    channel_id: "YOUR_DISCORD_CHANNEL_ID"
```

### 3. Set secrets

```bash
gh secret set DISCORD_NOTIFICATION_WEBHOOK
```

The `GITHUB_TOKEN` is provided automatically. CLI OAuth credentials (Codex, Claude, Cursor, Gemini)
are read from the self-hosted r2d2 runner's host filesystem — no API keys in the repo.

---

## CLI Reference

```
usage: ai-review [-h] [--version] <subcommand> ...

Unified console script for the ai-review-pipeline.

Subcommands:
  stage <name>      Run a specific stage: code-review | cursor-review | security | design | ac-validation
  consensus         Aggregate + post consensus (requires --sha)
  nachfrage         Process nachfrage/waiver commands [TODO: not yet implemented]
  auto-fix          Single-pass auto-fix (requires --pr --reason)
  fix-loop          Iterative fix-loop (requires --stage --pr-number --summary --worktree --base-branch --branch)
  ac-validate       Stage-5 AC-Validation inline (no LLM judge in CLI mode)
  metrics           Metrics summary (optional: --since --path --json)

Exit codes: 0=success, 1=failure/findings, 2=error/not-implemented
```

### Stage subcommand

```bash
ai-review stage code-review --pr 42 --max-iterations 2
ai-review stage cursor-review --pr 42 --skip-fix-loop
ai-review stage security --pr 42
ai-review stage design --pr 42 --skip-fix-loop
ai-review stage ac-validation   # delegates to ac-validate
```

Stage flags (forwarded to each stage runner):

| Flag | Description |
|---|---|
| `--pr <N>` | PR number (required) |
| `--max-iterations <N>` | Fix-loop iterations (default 2) |
| `--skip-fix-loop` | Skip auto-fix loop, review-only |
| `--skip-preflight` | Skip CLI OAuth check |
| `--status-context-prefix <prefix>` | Override status context prefix (e.g. `ai-review-v2`) |

### Consensus subcommand

```bash
ai-review consensus --sha <commit-sha> --pr 42
ai-review consensus --sha <commit-sha> --pr 42 \
  --status-context-prefix ai-review-v2 \
  --discord-channel 1234567890 \
  --no-ping
```

Consensus flags (PR#2, closes #1):

| Flag | Description |
|---|---|
| `--sha <sha>` | Commit SHA to aggregate (required) |
| `--pr <N>` | PR number for Disagreement-Alert |
| `--target-url <url>` | Optional status target URL |
| `--status-context <ctx>` | Override consensus status context name |
| `--status-context-prefix <prefix>` | Filter + rewrite all stage contexts under this prefix |
| `--discord-channel <id>` | Override Discord channel ID for this run |
| `--no-ping` | Suppress `@here` / role mention in Discord notifications |

### AC-Validate subcommand

```bash
ai-review ac-validate \
  --pr-body-file pr_body.txt \
  --linked-issues-file linked_issues.json \
  --changed-files "src/foo.py,tests/test_foo.py" \
  --diff-file pr_diff.txt
```

### Metrics subcommand

```bash
ai-review metrics
ai-review metrics --since 2026-04-01 --path /path/to/repo --json
```

---

## Architecture

```
Consumer Repo (any GitHub project)
        |
        | pull_request event
        v
  GitHub Actions (.github/workflows/)
   ├─ ai-code-review.yml        → Stage 1  (Codex)
   ├─ ai-cursor-review.yml      → Stage 1b (Cursor)
   ├─ ai-security-review.yml    → Stage 2  (Gemini + semgrep)
   ├─ ai-design-review.yml      → Stage 3  (Claude Opus 4.7)
   ├─ ai-review-ac-validation.yml → Stage 5 (Codex + Claude judge)
   ├─ ai-review-consensus.yml   → Consensus aggregation
   ├─ ai-review-nachfrage.yml   → Soft-consensus human-ACK
   ├─ ai-review-auto-fix.yml    → workflow_dispatch: auto-fix agent
   ├─ ai-review-auto-escalate.yml → cron: stale nachfrage escalation
   └─ ai-review-scope-check.yml → PR scope validation
        |
        | ai-review CLI (pip package)
        v
  ai_review_pipeline Python package
   ├─ stages/code_review.py     runs on self-hosted r2d2 runner
   ├─ stages/cursor_review.py   (OAuth credentials on host FS)
   ├─ stages/security_review.py
   ├─ stages/design_review.py
   ├─ stages/ac_validation.py
   └─ stages/stage.py  (orchestrator)
        |
        | consensus.py aggregates all stage commit statuses
        v
  Consensus Result (avg score + state)
        |
        | discord_notify.py
        v
  ops-n8n webhook → Discord "Nathan Ops" Guild
  (channel per project, sticky messages, @here mentions)
```

---

## Documentation

- [`docs/project-adoption.md`](docs/project-adoption.md) — Step-by-step guide for adopting the pipeline in a new project
- [`docs/messaging-bridge.md`](docs/messaging-bridge.md) — ops-n8n Discord bridge setup and webhook configuration
- [`docs/discord-tailscale-funnel.md`](docs/discord-tailscale-funnel.md) — Tailscale Funnel setup for Discord webhook ingress
- [`docs/acceptance-criteria-style.md`](docs/acceptance-criteria-style.md) — Gherkin AC authoring guide (Given/When/Then)
- [`workflows/README.md`](workflows/README.md) — Consumer workflow template reference

---

## Development

```bash
# Clone and set up
git clone https://github.com/EtroxTaran/ai-review-pipeline.git
cd ai-review-pipeline

# Install with dev dependencies
pip install -e '.[dev]'

# Run tests with coverage
pytest --cov=ai_review_pipeline --cov-report=term

# Coverage gate (must stay ≥ 80%)
pytest --cov=ai_review_pipeline --cov-fail-under=80
```

### CI Gates (this repo)

- **Test & Coverage** — pytest, cov ≥ 80% (blocking)
- **Lint** — ruff check (non-blocking, warnings only)
- **YAML Lint** — yamllint on `workflows/*.yml` (non-blocking)

### Schema Validation

Config files are validated against [`schema/config.schema.yaml`](schema/config.schema.yaml).
See [`schema/config.example.yaml`](schema/config.example.yaml) for a full annotated example.

---

## License

MIT — see [LICENSE](LICENSE).
