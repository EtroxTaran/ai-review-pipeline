# Messaging-Bridge — Discord + ops-n8n Runbook

Dieses Runbook beschreibt die Architektur und den Betrieb der Messaging-Bridge zwischen
der `ai-review-pipeline` und Discord. Ziel: Review-Ergebnisse und Eskalationen landen
als interaktive Nachrichten mit Inline-Buttons im projektspezifischen Discord-Channel,
ohne dass die Pipeline direkt mit der Discord-API kommuniziert.

---

## Architektur

```
GitHub Actions (r2d2 Runner)
  │
  │  HTTP POST JSON
  │  { "event": "stage_result" | "escalation" | "nachfrage",
  │    "pr": <number>, "repo": "<owner>/<repo>",
  │    "channel_id": "<discord_channel_id>",
  │    "payload": { ... } }
  ▼
ops-n8n  (r2d2:5679)
  Webhook-Path: /webhook/ai-review-dispatch
  Container:    ops-n8n  (image: n8nio/n8n:latest)
  Volume:       ops-n8n-data
  Env-File:     ~/.openclaw/.env
  │
  │  n8n verarbeitet Event:
  │    - ai-review-dispatcher.json  → Discord-Nachricht rendern + Inline-Buttons
  │    - ai-review-callback.json    → Discord Interaction → GitHub API
  │    - ai-review-escalation.json  → Alert an #ai-review-alerts-global
  │
  │  HTTP POST (Discord API v10)
  │  POST /channels/<id>/messages
  │  flags: IS_COMPONENTS_V2 (1<<15)
  ▼
Discord API  (discord.com/api/v10)
  │
  ▼
Discord Guild "Nathan Ops"
  Channel: #ai-review-<projekt>  (projektspezifisch)
  Channel: #ai-review-alerts-global  (cross-projekt, kritische Alerts)
```

Der Pipeline-Code (`discord_notify.py`) sendet **nie** direkt an Discord.
Er sendet an `ops-n8n:5679/webhook/ai-review-dispatch`. ops-n8n übernimmt:
- Rendering der Discord-Embed-Nachricht mit Stage-Scores
- Aufbau des Components-v2-Button-Layouts
- Eigentlicher Discord-API-Call (Retry-Logik, Rate-Limit-Handling)
- Rückweg: Discord-Interaction-Callbacks zurück zur GitHub-API

---

## Warum ops-n8n statt direktem Discord-Call?

**Rate-Limits**: Discord erlaubt pro Bot maximal 5 Nachrichten pro Sekunde (global) und
50 Nachrichten pro Channel pro Sekunde. Bei parallelen Stage-Ergebnissen kann ein Repo
mit mehreren aktiven PRs ohne Queuing an das Limit stoßen. ops-n8n serialisiert die
Sends ohne den Pipeline-Python-Code zu blockieren.

**Rendering-Komplexität**: Discord Components v2 (`IS_COMPONENTS_V2`, `flags: 1<<15`)
erfordert ein verschachteltes JSON-Objekt mit `ActionRow`-Containers und `Button`-Objekten.
Dieses Rendering in Python zu pflegen wäre fehleranfällig. n8n-Workflows sind visuell
editierbar und können ohne Deployment-Zyklus angepasst werden.

**Interaction-Dispatcher**: Discord POSTet Button-Clicks an einen public HTTPS-Endpoint
(nicht zurück an den GitHub-Runner). ops-n8n hält diesen Endpoint und leitet Interactions
an die GitHub-API weiter (Approve, Retry, Auto-Fix). Der Pipeline-Code ist davon entkoppelt.

---

## Discord Bot Setup (einmalig)

### 1. Application anlegen

1. Öffne [Discord Developer Portal](https://discord.com/developers/applications).
2. "New Application" → Name: `Nathan Ops Bot`.
3. Unter *Bot*: "Add Bot" → Token generieren.
4. Token in `~/.openclaw/.env` eintragen:
   ```bash
   echo 'DISCORD_BOT_TOKEN=<dein-token>' >> ~/.openclaw/.env
   ```
   **Niemals** in ein Repo committen. `.env` ist in `.gitignore`.

### 2. Bot-Permissions konfigurieren

Unter *OAuth2 → URL Generator* folgende Scopes und Permissions wählen:

| Scope | Grund |
|---|---|
| `bot` | Basis-Scope für Bot-Aktionen |
| `applications.commands` | Slash-Commands (`/ai-review`) |

Bot-Permissions:

| Permission | Grund |
|---|---|
| Send Messages | Stage-Ergebnisse posten |
| Embed Links | Embed-Nachrichten rendern |
| Use Slash Commands | `/ai-review` Commands verarbeiten |
| Manage Messages | Sticky-Messages updaten statt neu posten |
| Read Message History | Sticky-Message-ID nachschlagen |
| Mention @here / @everyone | Escalation-Pings |

Generierte OAuth-URL öffnen → Bot dem "Nathan Ops"-Discord-Guild hinzufügen.

### 3. Interactions Endpoint URL setzen

Unter *General Information → Interactions Endpoint URL*:
```
https://r2d2.tail4fc6dd.ts.net/webhook/discord-interaction
```

Discord verifiziert diesen Endpoint mit einem signierten Ping. Stelle sicher dass
ops-n8n mit dem Signature-Verify-Node läuft bevor du die URL einträgst — sonst schlägt
die Verifizierung fehl und die URL wird nicht akzeptiert.

Vollständiges Setup des öffentlichen Endpoints: [docs/discord-tailscale-funnel.md](discord-tailscale-funnel.md).

---

## Channel-Anlage-Checklist

Pro neuem Projekt einen Channel im Discord-Guild "Nathan Ops" anlegen:

- [ ] Channel erstellen: Name `#ai-review-<projekt>` (Beispiel: `#ai-review-ai-portal`)
- [ ] Bot zum Channel einladen (Rechtsklick Channel → Edit Channel → Permissions → Add bot)
- [ ] Channel-ID kopieren: Discord Developer Mode aktivieren (User Settings → Advanced),
      dann Rechtsklick auf Channel → "Copy Channel ID"
- [ ] GitHub-Secret setzen: `gh secret set DISCORD_CHANNEL_ID --body "<channel_id>"`
- [ ] `.ai-review/config.yaml` anpassen: `notifications.discord.channel_id: "<channel_id>"`
- [ ] Smoke-Test: Test-PR öffnen, prüfen ob Nachricht im Channel erscheint

Globaler Alert-Channel (einmalig, nicht pro Projekt):
- [ ] `#ai-review-alerts-global` anlegen — für cross-projekt kritische Eskalationen
- [ ] `DISCORD_ALERTS_GLOBAL_CHANNEL_ID` in `~/.openclaw/.env` eintragen

---

## Components v2 Button-Layout

Die drei Standard-Buttons pro Nachricht:

```json
{
  "type": 1,
  "components": [
    {
      "type": 2,
      "style": 1,
      "label": "Approve",
      "custom_id": "approve:{pr_number}:{repo}"
    },
    {
      "type": 2,
      "style": 2,
      "label": "Auto-Fix",
      "custom_id": "fix:{pr_number}:{repo}"
    },
    {
      "type": 2,
      "style": 4,
      "label": "Manual",
      "custom_id": "manual:{pr_number}:{repo}"
    }
  ]
}
```

`type: 1` = `ActionRow`, `type: 2` = `Button`.
Button-Styles: `1` = PRIMARY (blau), `2` = SECONDARY (grau), `4` = DANGER (rot).

Beim Senden der Nachricht muss `flags: 32768` (= `1<<15`, IS_COMPONENTS_V2) gesetzt sein,
sonst ignoriert Discord das `components`-Array.

Die n8n-Workflows in `agent-stack/ops/n8n/workflows/ai-review-dispatcher.json` bauen
dieses Layout dynamisch aus den Stage-Payload-Daten.

---

## Umgebungsvariablen und Config-Felder

### `~/.openclaw/.env` (r2d2-global, nie committen)

| Variable | Beschreibung |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot-Token aus Discord Developer Portal |
| `DISCORD_ALERTS_GLOBAL_CHANNEL_ID` | Channel-ID für `#ai-review-alerts-global` |
| `OPS_N8N_WEBHOOK_BASE` | Base-URL von ops-n8n (Standard: `http://127.0.0.1:5679`) |

### GitHub-Secrets (pro Projekt-Repo)

| Secret | Beschreibung |
|---|---|
| `DISCORD_BOT_TOKEN` | Kopie aus `~/.openclaw/.env`. Selber Token für alle Projekte. |
| `DISCORD_CHANNEL_ID` | Projektspezifische Channel-ID |

### `.ai-review/config.yaml` (pro Projekt)

```yaml
notifications:
  target: discord
  discord:
    channel_id: "1234567890123456789"   # Pflicht
    mention_role: "@here"               # Optional, Standard: @here
    sticky_message: true                # Optional, Standard: true
```

`sticky_message: true` bedeutet: ops-n8n updated die bestehende Nachricht pro PR
statt jedes Mal eine neue zu posten. Setzt voraus dass ops-n8n die Message-ID
im n8n-Workflow-State speichert.

---

## ops-n8n betreiben

### Container starten

```bash
# Im agent-stack-Repo
cd ~/projects/agent-stack/ops/n8n
docker compose up -d
```

### Status prüfen

```bash
docker ps --filter name=ops-n8n
curl -s http://localhost:5679/healthz
```

### Workflow-Import

```bash
# Alle drei ai-review-Workflows importieren
for f in workflows/ai-review-*.json; do
  curl -s -X POST http://localhost:5679/api/v1/workflows \
    -H "X-N8N-API-KEY: $(grep N8N_API_KEY ~/.openclaw/.env | cut -d= -f2)" \
    -H "Content-Type: application/json" \
    -d @"$f"
done
```

### Logs

```bash
docker logs ops-n8n --tail 100 -f
```

---

## Weiterführend

- Tailscale-Funnel-Setup (public HTTPS-Endpoint für Discord-Interactions):
  [docs/discord-tailscale-funnel.md](discord-tailscale-funnel.md)
- Vollständige ops-n8n Docker-Compose-Datei: `agent-stack/ops/n8n/docker-compose.yml`
- n8n-Workflow-JSONs: `agent-stack/ops/n8n/workflows/`
