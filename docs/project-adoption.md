# Projekt-Onboarding — ai-review-pipeline

Dieses Runbook beschreibt, wie ein neues Projekt die `ai-review-pipeline` aktiviert. Nach
dieser Anleitung laufen alle 5 Review-Stages auf jedem Pull-Request und der Required-Status
`ai-review/consensus` blockiert Merges bis die Pipeline grün ist.

---

## Voraussetzungen

Bevor du startest, müssen folgende Punkte erfüllt sein:

| Voraussetzung | Prüfkommando | Mindestversion |
|---|---|---|
| `gh` (GitHub CLI) | `gh --version` | 2.45 |
| `python3` | `python3 --version` | 3.11 |
| `yq` | `yq --version` | 4.x |
| r2d2-Runner registriert | `gh api repos/:owner/:repo/actions/runners` | Labels: `self-hosted, r2d2, ai-review` |
| Discord-Channel angelegt | — | Channel-ID aus Discord Developer Mode |
| ops-n8n läuft auf r2d2 | `curl -s http://localhost:5679/healthz` | HTTP 200 |

Der r2d2-Runner muss folgende CLIs mit aktiven OAuth-Sessions haben: `codex`, `cursor`,
`gemini`, `claude`, `semgrep`. Prüfe mit `gh ai-review verify` nach Installation (Schritt 3).

---

## Schritt-für-Schritt-Installation

### 1. Paket installieren

```bash
pip install ai-review-pipeline
```

Prüfe die Installation:

```bash
python3 -c "import ai_review_pipeline; print(ai_review_pipeline.__version__)"
```

### 2. gh-Extension installieren

```bash
gh extension install EtroxTaran/gh-ai-review
```

### 3. Workflow-Templates ins Projekt kopieren

Im Wurzelverzeichnis des Ziel-Repos ausführen:

```bash
cd /path/to/your-project
gh ai-review install
```

Was `install` tut:
- Kopiert alle 10 `ai-*.yml` Workflow-Templates nach `.github/workflows/`
- Legt `.ai-review/config.yaml` aus `schema/config.example.yaml` an (nur wenn noch nicht vorhanden)

Anschließend Installationsstatus prüfen:

```bash
gh ai-review verify
```

`verify` gibt Warnings aus wenn PAT-Scopes fehlen (`workflow` erforderlich) oder kein r2d2-Runner
am Repo registriert ist.

### 4. Konfiguration anpassen

Öffne `.ai-review/config.yaml` und passe mindestens folgende Felder an:

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
    timeout_seconds: 600
  cursor_review:
    enabled: true
    blocking: false        # Second-Opinion, non-blocking
  security:
    enabled: true
    blocking: true
  design:
    enabled: true
    blocking: false
  ac_validation:
    enabled: true
    blocking: true
    judge_model: gpt-5
    second_opinion_model: claude-opus-4-7
    min_coverage: 1.0      # 100 % AC-Abdeckung Pflicht

consensus:
  success_threshold: 8     # avg_score >= 8 = success
  soft_threshold: 5        # 5-7 = nachfrage
  fail_closed_on_missing_stage: true

notifications:
  target: discord
  discord:
    channel_id: "DEINE_CHANNEL_ID_HIER"   # <-- anpassen
    mention_role: "@here"
    sticky_message: true
```

Das vollständige Schema inklusive aller Optionen liegt in `schema/config.schema.yaml`.
Abweichungen führen beim nächsten Pipeline-Run zu einem Schema-Validation-Fehler (fail-closed).

### 5. Secrets setzen

```bash
# Discord-Bot-Token (gleicher Token für alle Projekte — liegt in ~/.openclaw/.env)
gh secret set DISCORD_BOT_TOKEN --body "$(grep DISCORD_BOT_TOKEN ~/.openclaw/.env | cut -d= -f2)"

# Discord-Channel-ID (projektspezifisch — aus Discord Developer Mode kopieren)
gh secret set DISCORD_CHANNEL_ID --body "1234567890123456789"
```

`ANTHROPIC_API_KEY` ist nur nötig wenn der Runner **keinen** lokalen OAuth-Store für `claude`
hat. Auf r2d2 mit `~/.claude`-OAuth-Session kann das entfallen.

Secrets-Übersicht:

| Secret | Pflicht | Beschreibung |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Ja | Aus `~/.openclaw/.env`. Wird für Messaging-Bridge gebraucht. |
| `DISCORD_CHANNEL_ID` | Ja | Per-Projekt. Aus Discord Developer Mode (Rechtsklick auf Channel). |
| `ANTHROPIC_API_KEY` | Nur ohne lokalen OAuth | Direkter API-Key-Fallback für Stage 4 + Stage 5. |
| `GITHUB_TOKEN` | Automatisch | Wird von GitHub Actions bereitgestellt. Benötigt `statuses:write`, `pull-requests:write`. |

### 6. Branch-Protection konfigurieren

Im GitHub-Repository unter *Settings → Branches → Branch protection rules*:

1. Neue Regel für `main` (oder deinen Default-Branch) anlegen.
2. "Require status checks to pass before merging" aktivieren.
3. Required-Check: `ai-review/consensus` hinzufügen.

Solange kein PR gelaufen ist, erscheint `ai-review/consensus` nicht in der Autocomplete-Liste.
Starte einen Test-PR und warte bis der Consensus-Job mindestens einmal gelaufen ist, dann
taucht der Check in der Branch-Protection auf.

### 7. Ersten Test-PR erstellen

```bash
git checkout -b chore/test-ai-review-pipeline
echo "# ai-review pipeline test" > .ai-review/.keep
git add .ai-review/.keep && git commit -m "chore: test ai-review pipeline activation"
gh pr create --title "test: ai-review pipeline activation" \
  --body "Closes #1

## Summary
- Aktiviert ai-review-pipeline via gh ai-review install
- Prüft alle Stages + Consensus-Status

## Test plan
- [ ] Alle 10 Workflow-Jobs erscheinen in Actions-Tab
- [ ] ai-review/consensus liefert Status (pending/success/failure)
- [ ] Discord-Channel empfängt Nachricht"
```

---

## Acceptance-Criteria-Convention

Damit Stage 5 (AC-Validation) arbeiten kann, müssen Issues Gherkin-Blöcke enthalten.
Die vollständige Schreibkonvention steht in [docs/acceptance-criteria-style.md](acceptance-criteria-style.md).

Kurzform: Jedes Issue braucht einen `## Acceptance Criteria`-Abschnitt mit einem
`gherkin`-Code-Block. PRs müssen `Closes #N` oder `Refs #N` im Body haben.
Fehlt beides, schlägt Stage 5 fail-closed an.

---

## Troubleshooting

### `gh ai-review verify` meldet fehlendes `r2d2`-Label

Der Runner ist nicht am Repo registriert. Im GitHub-Repository unter
*Settings → Actions → Runners* prüfen ob ein Runner mit Label `r2d2` erscheint.
Wenn nein: auf r2d2 `./config.sh --url <repo-url> --token <token> --labels self-hosted,r2d2,ai-review` ausführen.

### Stage hängt auf `pending` für mehr als 10 Minuten

Wahrscheinlich wartet der Job auf einen Runner. Prüfe:
```bash
gh api repos/:owner/:repo/actions/runs --jq '.workflow_runs[-1] | {status, conclusion}'
```
Wenn `status: queued` für >5 Minuten: Runner-Logs auf r2d2 prüfen (`journalctl -u actions.runner.*`).

### Stage 5 schlägt fehl mit "no AC found"

Das verknüpfte Issue hat keinen `gherkin`-Code-Block. Lege den Abschnitt
`## Acceptance Criteria` mit korrektem Fenced-Block an (siehe
[acceptance-criteria-style.md](acceptance-criteria-style.md)) und pushe einen leeren
Fix-Commit um die Stage neu zu triggern.

### Discord-Kanal empfängt keine Nachrichten

1. Prüfe ob `DISCORD_CHANNEL_ID` und `DISCORD_BOT_TOKEN` korrekt gesetzt sind:
   `gh secret list`.
2. Prüfe ob ops-n8n läuft: `curl -s http://localhost:5679/healthz` auf r2d2.
3. Prüfe n8n-Logs: `docker logs ops-n8n --tail 50`.
4. Prüfe ob der Bot dem Channel hinzugefügt wurde (Bot-Permissions in Discord-Server-Settings).

### `pip install ai-review-pipeline` schlägt fehl

Das Paket ist noch nicht auf PyPI veröffentlicht (Phase 3.2 pending). Installiere direkt
aus dem Repo:
```bash
pip install git+https://github.com/EtroxTaran/ai-review-pipeline.git
```

### Waiver für AC-Validation

Wenn Stage 5 fälschlicherweise schlägt (False Positive): PR-Kommentar mit:
```
/ai-review ac-waiver <reason mit mindestens 30 Zeichen>
```
Das erzeugt einen strukturierten Audit-Trail. Kein Label-Override. Details in
[acceptance-criteria-style.md](acceptance-criteria-style.md#ac-waiver-prozess).
