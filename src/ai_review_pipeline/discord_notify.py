"""Discord-Notification-Bridge (Phase 3.4 — ops-n8n Webhook-Relay).

Architektur-Entscheidung (Plan §§269-399):
  Dieses Modul sendet NICHT direkt an die Discord-API. Stattdessen postet es
  einen strukturierten JSON-Payload an den ops-n8n-Webhook auf r2d2:5679.
  ops-n8n übernimmt:
    - Discord Components v2 Rendering (Embed + ActionRow + Buttons)
    - Sticky-Message-Logik (Edit statt Neu-Post)
    - Discord-API-Auth via DISCORD_BOT_TOKEN (lebt exklusiv im n8n Credential-Store)
    - Interaction-Callback-Routing (Button-Klick → GitHub API)

Fail-Open-Design (Plan S6):
  Wenn ops-n8n down ist oder die HTTP-Anfrage scheitert:
    → kein Exception-Raise in der Pipeline
    → Return False
    → Eintrag in .ai-review/metrics.jsonl (append-only)

Payload-Schema (JSON):
  event_type, pr_url, repo, pr_number, consensus_score, stage_scores,
  findings, button_actions, channel_id, mention_role, sticky_message, waived

Discord vs. Telegram Unterschiede:
  - `channel_id` statt `chat_id` (Discord Snowflake-IDs)
  - `custom_id` statt `callback_data` für Button-Interaktionen
  - `mention_role` statt Telegram-@mention-Semantik
  - kein Bot-Token im Python-Modul (nur in ops-n8n)

Environment Variables:
  AI_REVIEW_DISPATCH_URL  — Override der ops-n8n Webhook-URL (Default: localhost:5679)
  AI_REVIEW_METRICS_PATH  — Override des Metrics-JSONL-Pfads
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Default-URL für den ops-n8n Dispatch-Webhook (überschreibbar via Env)
_DEFAULT_DISPATCH_URL = "http://127.0.0.1:5679/webhook/ai-review-dispatch"

# Timeout für den HTTP-POST (Plan: 5s)
_REQUEST_TIMEOUT = 5

# Event-Types die eine @mention auslösen (nur kritische Alerts)
_MENTION_EVENT_TYPES: frozenset[str] = frozenset({"escalation"})


@dataclass
class DiscordNotifyPayload:
    """Strukturierter Payload für den ops-n8n Discord-Dispatch-Webhook.

    Felder:
        event_type:      Art des Events (z.B. 'escalation', 'review_success', 'ac_waiver')
        pr_url:          Vollständige GitHub PR-URL
        repo:            Repository im Format 'owner/repo'
        pr_number:       PR-Nummer (int)
        consensus_score: Gewichteter Durchschnittsscore (1.0–10.0)
        stage_scores:    Dict mit Einzelscores pro Stage
        findings:        Liste von Findings/Hinweisen (gekappt auf 500 Zeichen pro Eintrag)
        button_actions:  Liste von Button-Dicts mit 'action', 'text', 'custom_id'
                         (Discord Components v2 — custom_id statt Telegram callback_data)
        channel_id:      Discord Channel-Snowflake (None → aus config lesen)
        mention_role:    Discord @mention-String (None → aus config oder kein @mention)
        sticky_message:  Wenn True: update bestehende Nachricht statt neu posten (None → aus config)
        waived:          Wenn True: Waiver-Modus (keine Buttons, Score=10)
    """

    event_type: str
    pr_url: str
    repo: str
    pr_number: int
    consensus_score: float
    stage_scores: dict[str, Any]
    findings: list[str]
    button_actions: list[dict[str, Any]]
    channel_id: str | None
    mention_role: str | None
    sticky_message: bool | None
    waived: bool = field(default=False)


def _get_dispatch_url() -> str:
    """Gibt die ops-n8n Webhook-URL zurück. Überschreibbar via Env-Var."""
    return os.environ.get("AI_REVIEW_DISPATCH_URL", _DEFAULT_DISPATCH_URL)


def _get_metrics_path(metrics_path: Path | None) -> Path | None:
    """Gibt den Metrics-Pfad zurück: explizit > Env-Var > None (kein Logging)."""
    if metrics_path is not None:
        return metrics_path
    env_path = os.environ.get("AI_REVIEW_METRICS_PATH")
    if env_path:
        return Path(env_path)
    return None


def _log_failure(
    metrics_path: Path | None,
    *,
    event_type: str,
    pr_number: int,
    error: str,
    status_code: int | None = None,
) -> None:
    """Schreibt einen failure-Eintrag in die Metrics-JSONL-Datei (append-only).

    Fail-silent: Wenn das Schreiben selbst fehlschlägt, wird geloggt aber kein Exception geworfen.
    """
    if metrics_path is None:
        return

    entry: dict[str, Any] = {
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "module": "discord_notify",
        "status": "failure",
        "event_type": event_type,
        "pr_number": pr_number,
        "error": error,
    }
    if status_code is not None:
        entry["status_code"] = status_code

    try:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        with metrics_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.warning("Konnte Metrics-Eintrag nicht schreiben: %s", exc)


def _resolve_config_discord(config: dict) -> dict | None:
    """Extrahiert den Discord-Block aus der Config. Gibt None zurück wenn nicht vorhanden."""
    notifications = config.get("notifications", {})
    if notifications.get("target") != "discord":
        return None
    return notifications.get("discord")


def _build_payload(
    payload: DiscordNotifyPayload,
    discord_config: dict,
) -> dict[str, Any]:
    """Baut den JSON-Body für den ops-n8n Webhook zusammen.

    Merge-Strategie: Payload-Wert hat Vorrang vor Config-Default (Override-Pfad).
    mention_role: nur bei Escalation-Events aus Config befüllen (kein unnötiges @mention).
    """
    # channel_id: Payload-Override > Config-Default
    channel_id = payload.channel_id or discord_config.get("channel_id")

    # sticky_message: Payload-Override > Config-Default > False
    if payload.sticky_message is not None:
        sticky_message = payload.sticky_message
    else:
        sticky_message = discord_config.get("sticky_message", False)

    # mention_role: nur bei Escalation-Events
    if payload.mention_role is not None:
        mention_role = payload.mention_role
    elif payload.event_type in _MENTION_EVENT_TYPES:
        mention_role = discord_config.get("mention_role")
    else:
        mention_role = None

    return {
        "event_type": payload.event_type,
        "pr_url": payload.pr_url,
        "repo": payload.repo,
        "pr_number": payload.pr_number,
        "consensus_score": payload.consensus_score,
        "stage_scores": payload.stage_scores,
        "findings": payload.findings,
        "button_actions": payload.button_actions,
        "channel_id": channel_id,
        "mention_role": mention_role,
        "sticky_message": sticky_message,
        "waived": payload.waived,
    }


def notify_discord(
    payload: DiscordNotifyPayload,
    config: dict,
    *,
    metrics_path: Path | None = None,
) -> bool:
    """Sendet einen strukturierten Event-Payload an den ops-n8n Discord-Dispatch-Webhook.

    Args:
        payload:      DiscordNotifyPayload mit allen Feldern.
        config:       Geparste .ai-review/config.yaml als Dict.
        metrics_path: Optionaler Pfad für Failure-Metriken (Fallback: AI_REVIEW_METRICS_PATH).

    Returns:
        True bei HTTP 2xx-Antwort, False in allen anderen Fällen (Fail-Open).

    Fail-Open-Garantie:
        Jede Exception (Connection, Timeout, unbekannt) wird geschluckt.
        Ein Fehler beim Discord-Notify darf NIEMALS die Pipeline crashen.
    """
    discord_config = _resolve_config_discord(config)
    if discord_config is None:
        logger.debug("Discord-Notifications nicht konfiguriert (target != discord). Überspringe.")
        return False

    effective_metrics_path = _get_metrics_path(metrics_path)
    dispatch_url = _get_dispatch_url()
    body = _build_payload(payload, discord_config)

    try:
        response = requests.post(dispatch_url, json=body, timeout=_REQUEST_TIMEOUT)
        if 200 <= response.status_code < 300:
            return True

        # HTTP-Fehler von ops-n8n (4xx, 5xx)
        logger.warning(
            "ops-n8n antwortete mit Status %d für PR #%d",
            response.status_code,
            payload.pr_number,
        )
        _log_failure(
            effective_metrics_path,
            event_type=payload.event_type,
            pr_number=payload.pr_number,
            error=f"HTTP {response.status_code}",
            status_code=response.status_code,
        )
        return False

    except requests.RequestException as exc:
        # Netzwerkfehler: Connection refused, Timeout, DNS-Fehler, etc.
        logger.warning(
            "Discord-Notify fehlgeschlagen (RequestException) für PR #%d: %s",
            payload.pr_number,
            exc,
        )
        _log_failure(
            effective_metrics_path,
            event_type=payload.event_type,
            pr_number=payload.pr_number,
            error=str(exc),
        )
        return False

    except Exception as exc:  # noqa: BLE001 — Fail-Open: keine Exception darf die Pipeline crashen
        logger.error(
            "Discord-Notify unerwarteter Fehler für PR #%d: %s",
            payload.pr_number,
            exc,
        )
        _log_failure(
            effective_metrics_path,
            event_type=payload.event_type,
            pr_number=payload.pr_number,
            error=f"unexpected: {exc}",
        )
        return False
