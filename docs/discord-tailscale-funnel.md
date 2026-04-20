# Discord Interactions Endpoint — Tailscale Funnel Setup

Dieses Runbook beschreibt, wie der öffentliche HTTPS-Endpoint für Discord-Button-Interactions
auf r2d2 via Tailscale Funnel exponiert wird.

---

## Das Problem

Wenn ein Nutzer auf einen Discord-Button klickt (Approve, Auto-Fix, Manual), schickt
Discord einen HTTP POST an einen **öffentlich erreichbaren HTTPS-Endpoint** der Anwendung.
r2d2 liegt hinter einem Heimnetzwerk-NAT und hat keine eigene öffentliche IP-Adresse.

Port-Forwarding am Router ist keine Option (ändert sich bei IP-Wechsel, kein TLS, Sicherheitsrisiko).
Reverse-Proxy-Dienste wie ngrok erfordern kostenpflichtige Pläne für persistente URLs.

**Lösung**: Tailscale Funnel. r2d2 ist bereits im Tailnet `tail4fc6dd.ts.net`. Tailscale
Funnel leitet öffentliche HTTPS-Anfragen an den Tailnet-Hostnamen über Tailscales Infrastruktur
an einen lokalen Port weiter — ohne Port-Forwarding, mit automatischem TLS-Zertifikat.

---

## Voraussetzungen

- r2d2 ist mit dem Tailnet verbunden (`tailscale status` zeigt r2d2)
- Tailscale-Account hat Funnel aktiviert (kostenlos ab Personal-Plan)
- ops-n8n läuft auf `localhost:5679`

---

## Setup

### 1. Funnel aktivieren

Auf r2d2 als `clawd` (oder mit `sudo`):

```bash
sudo tailscale funnel --bg --set-path /webhook/discord-interaction localhost:5679
```

Optionen:
- `--bg`: Läuft im Hintergrund, überlebt SSH-Session-Ende
- `--set-path /webhook/discord-interaction`: Nur dieser Pfad wird exponiert (kein blanker Port)
- `localhost:5679`: ops-n8n Webhook-Port

Ergebnis prüfen:

```bash
tailscale funnel status
```

Erwartete Ausgabe:
```
https://r2d2.tail4fc6dd.ts.net/webhook/discord-interaction -> localhost:5679
```

### 2. Interactions Endpoint URL in Discord eintragen

Im [Discord Developer Portal](https://discord.com/developers/applications):

1. Deine Application ("Nathan Ops Bot") öffnen.
2. *General Information* → *Interactions Endpoint URL*:
   ```
   https://r2d2.tail4fc6dd.ts.net/webhook/discord-interaction
   ```
3. "Save Changes" klicken.

Discord sendet sofort einen Verification-Ping (Ed25519-signierte POST-Anfrage mit
`type: 1`). Der n8n-Signature-Verify-Node muss vor dem Speichern aktiv sein —
sonst antwortet der Endpoint falsch und Discord lehnt die URL ab.

---

## Ed25519 Signature-Verification in n8n

Discord signiert **jede** eingehende Interaction mit dem Ed25519-Private-Key der Application.
Der Endpoint muss die Signatur prüfen und bei Fehler `401 Unauthorized` zurückgeben.
Ohne diesen Auth-Layer kann jeder beliebige HTTP-Client an den öffentlichen Endpoint senden.

**Das ist kein Optional-Feature — es ist ein Sicherheitspflichtfeld.**

Der erste Node im `ai-review-callback.json`-Workflow in ops-n8n ist der Signature-Verify-Node:

```
[Webhook Node: POST /webhook/discord-interaction]
  │
  ▼
[Code Node: verify_ed25519_signature]
  Inputs:
    - headers.x-signature-ed25519
    - headers.x-signature-timestamp
    - body (raw bytes)
    - DISCORD_PUBLIC_KEY (aus n8n-Environment)
  Logik:
    1. message = timestamp_bytes + body_bytes
    2. nacl.sign.detached.verify(message, signature, publicKey)
    3. Wenn verify fehlschlägt → response 401, Stop
    4. Wenn type == 1 (PING) → response { "type": 1 } (PONG), Stop
    5. Sonst → weiter zum Interaction-Dispatcher
```

Den Public Key findest du im Discord Developer Portal unter
*General Information → Public Key* (nicht der Bot-Token).

In `~/.openclaw/.env` eintragen:

```bash
DISCORD_PUBLIC_KEY=<64-Zeichen-Hex-String aus Developer Portal>
```

In n8n als Credential oder direkt als Environment-Variable aus dem `env_file` verfügbar machen.

---

## Vollständiger Datenfluss (Button-Click)

```
Nutzer klickt "Approve" in Discord
  │
  │  POST https://r2d2.tail4fc6dd.ts.net/webhook/discord-interaction
  │  Headers:
  │    X-Signature-Ed25519: <hex>
  │    X-Signature-Timestamp: <unix_ts>
  │  Body: { "type": 3, "data": { "custom_id": "approve:42:owner/repo" }, ... }
  ▼
Tailscale Funnel (TLS-Termination, Weiterleitung)
  │
  │  HTTP POST  localhost:5679/webhook/discord-interaction
  ▼
ops-n8n  [ai-review-callback.json]
  ├── Node 1: Signature-Verify (Ed25519)
  ├── Node 2: Parse custom_id → action, pr_number, repo
  ├── Node 3: Auth-Check (ist Sender autorisiert? z.B. Guild-Member-Check via Discord API)
  ├── Node 4: GitHub-API-Call (POST /repos/{repo}/issues/{pr}/comments mit /ai-review approve)
  └── Node 5: Discord ACK-Response (HTTP 200, { "type": 6 } = DEFERRED_UPDATE_MESSAGE)
  │
  │  Nach GitHub-API-Call: GitHub-Actions-Workflow wird getriggert
  ▼
ai-review-nachfrage.yml (GitHub Actions)
  Verarbeitet /ai-review approve, merged PR oder triggert Re-Review
```

---

## Troubleshooting

### Discord lehnt "Interactions Endpoint URL" ab

Ursache: Der Signature-Verify-Node in n8n antwortet nicht korrekt auf den Verification-Ping.

Prüfe:
1. Läuft ops-n8n? `docker ps --filter name=ops-n8n`
2. Ist der Webhook-Path korrekt konfiguriert? `curl -sv https://r2d2.tail4fc6dd.ts.net/webhook/discord-interaction`
3. Ist `DISCORD_PUBLIC_KEY` in n8n korrekt gesetzt?
4. n8n-Logs: `docker logs ops-n8n --tail 50`

### Funnel-Port-Konflikt

Wenn Port 5679 bereits anderweitig gebunden ist:

```bash
ss -tlnp | grep 5679
```

Falls ops-n8n nicht läuft, aber der Port belegt ist: konfligierenden Prozess beenden,
dann `docker compose up -d` in `agent-stack/ops/n8n/`.

### Funnel nach Reboot nicht aktiv

Der `--bg`-Flag macht den Funnel persistent über Tailscale-Neuverbindungen, aber nicht
über System-Neustarts (der `tailscaled`-Dienst muss neu konfiguriert werden):

```bash
# Funnel-Konfiguration persistent machen (einmalig)
sudo tailscale funnel --bg --set-path /webhook/discord-interaction localhost:5679
```

Alternativ als systemd-Drop-In oder via `@reboot`-Cron-Job.

### Bot-Token-Rotation

Wenn der Discord-Bot-Token rotiert werden muss:

1. Discord Developer Portal → Bot → "Regenerate Token"
2. Neuen Token in `~/.openclaw/.env` eintragen
3. Alle betroffenen GitHub-Secrets aktualisieren:
   ```bash
   # Für jedes Projekt-Repo
   gh secret set DISCORD_BOT_TOKEN --body "<neuer-token>" --repo EtroxTaran/<projekt>
   ```
4. ops-n8n-Credential aktualisieren (in n8n-UI: Credentials → Discord Bot Token)
5. `docker restart ops-n8n` damit n8n die neue Env lädt

Der `DISCORD_PUBLIC_KEY` (für Signature-Verification) ändert sich bei Token-Rotation **nicht**.
Er ist an die Application, nicht an den Bot-Token gebunden.

### Tailscale-Hostname ändert sich

Der Tailnet-Hostname `r2d2.tail4fc6dd.ts.net` ist gebunden an Nicos Tailnet-ID und den
Maschinen-Namen auf r2d2. Er ändert sich nicht solange die Maschine im Tailnet registriert
ist. Falls r2d2 neu registriert werden muss (Neuinstallation):

1. `tailscale up` mit gleichem Account → Hostname bleibt bei gleicher Maschine
2. Falls Hostname ändert: Discord Developer Portal → Interactions Endpoint URL anpassen

---

## Weiterführend

- Messaging-Bridge-Übersicht: [docs/messaging-bridge.md](messaging-bridge.md)
- Discord-API Interactions-Dokumentation: https://discord.com/developers/docs/interactions/receiving-and-responding
- Tailscale Funnel-Dokumentation: https://tailscale.com/kb/1223/funnel
