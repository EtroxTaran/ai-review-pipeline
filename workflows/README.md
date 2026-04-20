# AI-Review-Pipeline — Workflow Templates

Dieses Verzeichnis enthält die 10 parametrisierten GitHub-Actions-Workflow-Templates
der `ai-review-pipeline`. Sie sind **keine** `.github/workflows/`-Files — `gh ai-review install`
kopiert sie in das Ziel-Repo. Alle Templates sind generisch: kein hardcoded Repo-Name,
kein hardcoded Branch.

---

## Workflow-Übersicht

| Datei | Stage | Trigger | Produziert | Runner |
|---|---|---|---|---|
| `ai-code-review.yml` | Stage 1 | `pull_request` (opened/sync/reopen/ready) | Commit-Status `ai-review/code`, Sticky-PR-Comment, optional Fix-Commits `[ai-fix] code:` | self-hosted |
| `ai-cursor-review.yml` | Stage 1b | `pull_request` (opened/sync/reopen/ready) | Commit-Status `ai-review/code-cursor`, Second-Opinion-Comment (non-blocking by default) | self-hosted |
| `ai-security-review.yml` | Stage 2 | `pull_request` (opened/sync/reopen/ready) | Commit-Status `ai-review/security`, semgrep-Baseline-Report im PR-Comment | self-hosted |
| `ai-design-review.yml` | Stage 3 | `pull_request` (opened/sync/reopen/ready) | Commit-Status `ai-review/design`, DESIGN.md-Konformitäts-Kommentar | self-hosted |
| `ai-review-ac-validation.yml` | Stage 5 | `pull_request` (opened/sync/reopen/**edited**) | Commit-Status `ai-review/ac-validation`, AC-Coverage-Report im PR-Comment | self-hosted |
| `ai-review-consensus.yml` | Aggregator | `pull_request` + `check_suite.completed` | Commit-Status `ai-review/consensus` (Required-Check), Discord-Nachricht mit Gesamt-Score | self-hosted |
| `ai-review-scope-check.yml` | Gate | `pull_request` (opened/sync/reopen/edited) | Commit-Status `ai-review/scope-check` (informational, nicht required) | ubuntu-latest |
| `ai-review-nachfrage.yml` | Command-Handler | `issue_comment` (PR-Kommentar mit `/ai-review`) | Verarbeitet approve/retry/security-waiver/ac-waiver-Commands, triggert Downstream-Workflows | ubuntu-latest |
| `ai-review-auto-fix.yml` | Auto-Fix | `workflow_dispatch` (pr_number, stage, sha) | Fix-Commits `[ai-fix] <stage>:` auf PR-Branch, Post-Fix-Validation via `AI_REVIEW_POSTFIX_CHECK` | self-hosted |
| `ai-review-auto-escalate.yml` | Escalation-Cron | `schedule` (alle 5 min) | Setzt stale soft-Consensus-PRs von `pending` auf `failure`, Discord-Alert | ubuntu-latest |

---

## Erforderliche Secrets (Consumer-Repo)

| Secret | Workflow(s) | Pflicht | Beschreibung |
|---|---|---|---|
| `GITHUB_TOKEN` | Alle | Ja (automatisch) | Standard Actions-Token. Scopes: `statuses:write`, `pull-requests:write`, `contents:write` (für Fix-Commits). |
| `DISCORD_BOT_TOKEN` | consensus, auto-escalate, nachfrage | Ja | Discord-Bot-Token aus `~/.openclaw/.env`. Selber Token für alle Projekte. |
| `DISCORD_CHANNEL_ID` | consensus, auto-escalate | Ja | Projektspezifische Discord-Channel-ID (aus Discord Developer Mode). |
| `ANTHROPIC_API_KEY` | design, ac-validation | Situativ | Nur wenn der r2d2-Runner **keinen** lokalen `~/.claude`-OAuth-Store hat. |

> Auf r2d2 mit vollständigem OAuth-Store für alle vier CLIs (`claude`, `codex`, `cursor`, `gemini`)
> werden keine API-Key-Secrets im Repo benötigt. `DISCORD_BOT_TOKEN` und `DISCORD_CHANNEL_ID`
> müssen in jedem Consumer-Repo gesetzt sein.

---

## Erforderliche Repository-Variablen (Consumer-Repo vars)

| Variable | Default | Beschreibung |
|---|---|---|
| `AI_REVIEW_POSTFIX_CHECK` | `''` (no-op) | Shell-Kommando das nach einem Auto-Fix-Commit ausgeführt wird. Beispiel: `pnpm typecheck && pnpm test --changed`. Leer = kein Post-Fix-Check. |

Setzen via GitHub-UI (*Settings → Secrets and variables → Actions → Variables*) oder:
```bash
gh variable set AI_REVIEW_POSTFIX_CHECK --body "pnpm typecheck && pnpm test --changed"
```

---

## Runner-Anforderungen

Alle Stages außer `scope-check` und `auto-escalate` laufen auf dem Self-Hosted-Runner
mit Labels `[self-hosted, r2d2, ai-review]`.

Der Runner muss folgendes vorhalten:

| Komponente | Verwendung |
|---|---|
| `codex` CLI mit OAuth | Stage 1 (gpt-5) + Stage 5 judge_model |
| `cursor` CLI mit OAuth | Stage 1b (composer-2) |
| `gemini` CLI mit OAuth | Stage 2 (gemini-2.5-pro) + semgrep |
| `claude` CLI mit OAuth | Stage 3 + Stage 5 second_opinion_model (claude-opus-4-7) |
| `semgrep` | Stage 2 Security-Baseline |
| `python3 ≥ 3.11` | Alle self-hosted Stages |
| `scripts/ai-review-preflight.sh` | Im Consumer-Repo — prüft OAuth-Presence pro Stage |

`scripts/ai-review-preflight.sh <stage>` muss im Consumer-Repo existieren und Exit-Code 0
zurückgeben wenn der jeweilige CLI-OAuth-Store für die Stage vorhanden ist. Fehlt das Script,
schlägt die Stage mit einem klaren Fehler an.

Stages `ai-review-scope-check.yml` und `ai-review-auto-escalate.yml` laufen auf
`ubuntu-latest` — sie brauchen nur `gh` CLI und Python-Standard-Bibliothek.

---

## GitHub-Permissions pro Job

| Workflow | `contents` | `pull-requests` | `statuses` | `checks` | `actions` |
|---|---|---|---|---|---|
| `ai-code-review.yml` | write (Fix-Commits) | write (Comments) | write | write | — |
| `ai-cursor-review.yml` | read | write | write | write | — |
| `ai-security-review.yml` | read | write | write | write | — |
| `ai-design-review.yml` | read | write | write | write | — |
| `ai-review-ac-validation.yml` | read | write | write | — | — |
| `ai-review-consensus.yml` | read | read | write | read | read |
| `ai-review-scope-check.yml` | read | write | write | — | — |
| `ai-review-nachfrage.yml` | read | write | write | — | write |
| `ai-review-auto-fix.yml` | write | write | write | write | — |
| `ai-review-auto-escalate.yml` | read | read | write | — | — |

> `pull_request_target` ist in **keinem** Workflow verwendet. Alle Trigger sind `pull_request`
> (oder `workflow_dispatch`/`schedule`/`check_suite`/`issue_comment`). Das ist konform mit
> Security-Guardrail Rule 10 aus CLAUDE.md (`pull_request_target` verboten wegen Injection-Risiko).

---

## Customization via `.ai-review/config.yaml`

Alle Stage-Timeouts, Modell-Overrides und Blocking-Verhalten sind in der
Pro-Projekt-Config `.ai-review/config.yaml` konfigurierbar. Die Workflows lesen diese
Config via `python -m ai_review_pipeline.stages.<stage>`, die die Config aus dem
Checkout-Verzeichnis lädt.

Relevante Config-Felder:

```yaml
reviewers:
  codex: gpt-5            # Override für Stage 1 + Stage 5 judge
  cursor: composer-2      # Override für Stage 1b
  gemini: gemini-2.5-pro  # Override für Stage 2
  claude: claude-opus-4-7 # Override für Stage 3 + Stage 5 second_opinion

stages:
  code_review:
    enabled: true         # false = Workflow-Run überspringt Stage (pending → skipped)
    blocking: true        # false = Stage trägt nicht zur Consensus-Berechnung bei
    timeout_seconds: 600  # Hard-Wall für den Stage-Python-Prozess
  cursor_review:
    enabled: true
    blocking: false       # Default: non-blocking (informational)
  security:
    enabled: true
    blocking: true
  design:
    enabled: true
    blocking: false       # Default: non-blocking
  ac_validation:
    enabled: true
    blocking: true
    judge_model: gpt-5
    second_opinion_model: claude-opus-4-7
    min_coverage: 1.0     # 1.0 = 100% AC-Abdeckung Pflicht

consensus:
  success_threshold: 8    # avg_score >= 8 → success
  soft_threshold: 5       # 5 <= avg_score < 8 → nachfrage (soft)
  fail_closed_on_missing_stage: true
```

Das vollständige JSON-Schema steht in `schema/config.schema.yaml`. Abweichungen erzeugen
einen Schema-Validation-Fehler beim nächsten Pipeline-Run.

---

## Branch-Protection: Required-Check setzen

Der einzige Required-Check ist `ai-review/consensus`. Alle anderen Stage-Statuses
sind Inputs für den Consensus-Aggregator.

```bash
# Alternativ über GitHub-UI: Settings → Branches → Branch protection rules
gh api repos/:owner/:repo/branches/main/protection \
  --method PUT \
  --input - <<'EOF'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["ai-review/consensus"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
EOF
```

---

## Hinweise zum Stage-5-Interim-Modus

`cli.py`-Einstiegspunkt (`python -m ai_review_pipeline.cli ac-validation`) ist noch
nicht implementiert (Phase 3.5b pending). Der Workflow `ai-review-ac-validation.yml`
nutzt interim einen Python-Inline-Block mit direktem `validate_ac_coverage`-Aufruf.
Nach cli.py-Implementierung vereinfacht sich der Run-Step auf:

```yaml
run: |
  python -m ai_review_pipeline.cli ac-validation \
    --pr "$PR_NUMBER" \
    --sha "$PR_HEAD_SHA" \
    --target-url "$TARGET_URL"
```

---

## Onboarding eines neuen Projekts

Vollständiges Step-by-Step-Runbook: [docs/project-adoption.md](../docs/project-adoption.md).

Kurzform:
```bash
pip install ai-review-pipeline
gh extension install EtroxTaran/gh-ai-review
cd /path/to/your-project
gh ai-review install    # kopiert Templates + legt .ai-review/config.yaml an
gh ai-review verify     # prüft PAT-Scopes + Runner-Registration
```
