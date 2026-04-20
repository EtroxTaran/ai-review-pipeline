# AI-Review-Pipeline — Workflow Templates

Diese Verzeichnis enthält die 10 parametrisierten GitHub-Actions-Workflow-Templates
der `ai-review-pipeline`. Sie sind keine `.github/workflows/`-Files — `gh-ai-review install`
kopiert sie in das Ziel-Repo.

---

## Workflow-Übersicht

| Datei | Stage | Zweck | Runner |
|---|---|---|---|
| `ai-code-review.yml` | Stage 1 | Codex (gpt-5) — funktionale Korrektheit, Fix-Loop | self-hosted |
| `ai-cursor-review.yml` | Stage 1b | Cursor (composer-2) — Second Opinion, review-only | self-hosted |
| `ai-security-review.yml` | Stage 2 | Gemini 2.5 Pro + semgrep-Baseline, review-only | self-hosted |
| `ai-design-review.yml` | Stage 3 | Claude Opus 4.7 — DESIGN.md-Konformität, review-only | self-hosted |
| `ai-review-ac-validation.yml` | Stage 5 | AC-Coverage gegen Gherkin-IssueACs (Codex primary, Claude second-opinion) | self-hosted |
| `ai-review-consensus.yml` | Aggregator | Pollt alle Stage-Statuses → `ai-review/consensus` Required-Status | self-hosted |
| `ai-review-scope-check.yml` | Gate | Regex-Gate: PR-Body muss Issue-Ref ODER Summary-Template haben | ubuntu-latest |
| `ai-review-nachfrage.yml` | Command-Handler | `/ai-review approve/retry/security-waiver/ac-waiver` PR-Commands | ubuntu-latest |
| `ai-review-auto-fix.yml` | Auto-Fix | Single-Pass Claude-Fix via workflow_dispatch (Discord-Button oder /retry) | self-hosted |
| `ai-review-auto-escalate.yml` | Escalation-Cron | 5-min-Cron: stale soft-Consensus-PRs → failure eskalieren | ubuntu-latest |

---

## Erforderliche Secrets (Consumer-Repo)

| Secret | Pflicht | Beschreibung |
|---|---|---|
| `GITHUB_TOKEN` | Ja (automatisch) | Actions-Standard. Benötigt `statuses:write`, `pull-requests:write`, `contents:write` |
| `DISCORD_NOTIFICATION_WEBHOOK` | Empfohlen | Discord-Webhook-URL für Escalation-Alerts. Leer = kein Alert, kein Crash |
| `ANTHROPIC_API_KEY` | Situativ | Nur wenn der Consumer-Repo **kein** self-hosted-r2d2-Runner mit OAuth-Store nutzt |

> **Hinweis:** Auf dem globalen r2d2-Runner liegen die CLI-OAuth-Credentials
> (Claude, Codex, Cursor, Gemini) lokal im Filesystem (`~/.claude`, `~/.codex` etc.).
> Consumer-Repos auf diesem Runner benötigen keine API-Key-Secrets.

---

## Erforderliche ENV-Variablen (Consumer-Repo vars)

| Variable | Default | Beschreibung |
|---|---|---|
| `AI_REVIEW_POSTFIX_CHECK` | `''` (no-op) | Shell-Kommando für post-fix Validierung in `ai-review-auto-fix.yml`. Beispiel: `pnpm typecheck && pnpm test --changed` |

---

## Runner-Anforderungen

Alle self-hosted-Stages benötigen einen Runner mit Labels `[self-hosted, r2d2, ai-review]`.
Auf diesem Runner müssen vorhanden sein:

- `codex` CLI (OAuth-Login für gpt-5)
- `cursor` CLI (OAuth-Login für composer-2)
- `gemini` CLI (OAuth-Login für gemini-2.5-pro)
- `claude` CLI (OAuth-Login für claude-opus-4-7)
- `semgrep` (für Stage 2 Security-Baseline)
- Python 3.11+
- `scripts/ai-review-preflight.sh` im Consumer-Repo (prüft CLI-OAuth-Presence)

---

## Voraussetzungen im Consumer-Repo

1. `scripts/ai-review-preflight.sh <stage>` muss existieren und 0 zurückgeben wenn der CLI-OAuth für `<stage>` vorhanden ist.
2. Für `ai-review/consensus` als Required-Status: Branch-Protection-Rule im Consumer-Repo setzen.
3. Issues müssen Gherkin-AC-Blocks enthalten (```gherkin ... ```), damit Stage 5 Coverage validieren kann.

---

## Reviewer-Modelle (Stand 2026-04)

Per CLAUDE.md Rule 11 — immer aktuell halten:

- Codex: `gpt-5`
- Cursor: `composer-2`
- Gemini: `gemini-2.5-pro`
- Claude: `claude-opus-4-7`

---

## Stage-5 TODO

`cli.py`-Einstiegspunkt (`python -m ai_review_pipeline.cli ac-validation`) ist noch nicht
implementiert. Der Workflow nutzt interim einen Python-Inline-Block mit direktem
`validate_ac_coverage`-Aufruf. Nach cli.py-Implementierung vereinfacht sich der Run-Step auf:

```yaml
run: |
  python -m ai_review_pipeline.cli ac-validation \
    --pr "$PR_NUMBER" \
    --sha "$PR_HEAD_SHA" \
    --target-url "$TARGET_URL"
```
