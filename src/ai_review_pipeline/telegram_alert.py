"""Telegram-Escalation-Alert (Wave 4b).

Portiert aus ai-portal/scripts/ai-review/telegram_alert.py.

Wenn eine Fix-Loop nach `max_iterations` ohne Konvergenz beendet wird, soll
Nico sofort eine Nachricht bekommen — sonst bleibt ein PR unerkannt hängen.
Kein direkter Zugriff auf TELEGRAM_BOT_TOKEN vom GH-Actions-Runner: Token
lebt exklusiv in n8n. Stattdessen POST an einen n8n-Webhook, der das
Telegram-Formatting + -Versand macht.

Secret-Layout (GH Actions):
  TELEGRAM_NOTIFICATION_WEBHOOK  — n8n-URL, z. B.
    http://r2d2-host:5678/webhook/ai-review-escalation

Design:
  - HTTP-Call ist injectable (`http_post_fn`), damit Tests kein Netz brauchen.
  - Fehler werden geschluckt — der Alert-Fehler darf nicht die Escalation
    blockieren. Der PR-Comment ist weiterhin die primäre Eskalations-Kanal.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable


HttpPostFn = Callable[..., bool]
"""
Signature: http_post_fn(url, *, json_body: dict, timeout: int) -> bool

True bei 2xx, False sonst. Exceptions bubbeln auf, der Caller fängt.
"""


def _default_http_post(url: str, *, json_body: dict, timeout: int = 10) -> bool:
    """Production-HTTP-Client via urllib (stdlib only, kein requests-Dep).

    Justification für die nosemgrep-Marker: `url` ist kein User-Input; die
    Ziel-URL kommt aus dem GH-Actions-Secret `TELEGRAM_NOTIFICATION_WEBHOOK`,
    welches nur von Repo-Admins gesetzt werden kann (siehe README). Die
    Alternative (requests-Library) wurde verworfen, um portal-side runtime-
    Dependencies klein zu halten — stdlib-only ist Policy.
    """
    data = json.dumps(json_body).encode("utf-8")
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    req = urllib.request.Request(  # noqa: S310 — URL comes from config secret
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except urllib.error.URLError:
        return False


RUNBOOK_URL = (
    "https://github.com/EtroxTaran/ai-portal/blob/main/docs/v2/30-guides/"
    "AI-REVIEW-RUNBOOK.md"
)


def _rerun_command(pr_number: int) -> str:
    """Gib den copy-pasteable CLI-Befehl zurück, mit dem Nico manuell re-triggert."""
    return f'gh pr comment {pr_number} --body "/rerun ai-review"'


def _default_button_actions(pr_number: int) -> list[dict[str, Any]]:
    """Wave 7b: Render die 3 Standard-Aktions-Buttons für Telegram inline_keyboard.

    Der n8n-callback-Workflow parst `callback_data` als `<action>:<pr_number>`
    und dispatcht:
      - approve → POST commit-status ai-review/consensus=success
        (mit Security-Veto-Guard — falls security=failure, Hinweis-Nachricht
        statt Override)
      - autofix → gh workflow run ai-review-auto-fix.yml --field pr_number=N
      - manual  → PR-Label `needs-human-review` setzen, keine weitere Aktion

    Das `text`-Feld ist was der User im Telegram-Chat sieht.
    """
    return [
        {
            "action": "approve",
            "text": "✅ Approve",
            "callback_data": f"approve:{pr_number}",
        },
        {
            "action": "autofix",
            "text": "🔧 Auto-Fix",
            "callback_data": f"autofix:{pr_number}",
        },
        {
            "action": "manual",
            "text": "👀 Manual Review",
            "callback_data": f"manual:{pr_number}",
        },
    ]


def send_escalation_alert(
    *,
    webhook_url: str,
    pr_number: int,
    pr_url: str,
    stage: str,
    iterations: int,
    last_score: int | None,
    summary: str,
    http_post_fn: HttpPostFn = _default_http_post,
    include_buttons: bool = True,
) -> bool:
    """POST an n8n-Webhook mit Eskalations-Payload.

    Wave 5c: Payload enthält jetzt `runbook_url` + `rerun_cmd` — das
    n8n-Workflow nutzt die, um dem Telegram-User direkte Handlungsoptionen
    zu geben ("was kann ich tun?").

    Wave 7b: `button_actions` für Telegram-inline-keyboard. Wenn
    `include_buttons=False`, wird das Feld weggelassen (backward-compat mit
    älterem n8n-Workflow-Deployment).

    Returns True bei erfolgreichem Versand. Wenn `webhook_url` leer ist
    (Secret nicht gesetzt), kein Crash — wir melden nur False zurück.
    """
    if not webhook_url:
        return False

    payload: dict[str, Any] = {
        "event": "ai-review/escalation",
        "alert_type": "escalation",
        "pr": pr_number,
        "pr_url": pr_url,
        "stage": stage,
        "iterations": iterations,
        "last_score": last_score,
        "summary": summary[:500],  # cap damit Telegram 4096-char-Limit sicher unterschritten
        "runbook_url": RUNBOOK_URL,
        "rerun_cmd": _rerun_command(pr_number),
    }
    if include_buttons:
        payload["button_actions"] = _default_button_actions(pr_number)

    try:
        return http_post_fn(webhook_url, json_body=payload, timeout=10)
    except Exception:
        # Alert-Fehler darf NICHT den Workflow crashen. Wir loggen implizit
        # via Return-False; der Caller kann das in den Run-Log schreiben.
        return False


def send_disagreement_alert(
    *,
    webhook_url: str,
    pr_number: int,
    pr_url: str,
    codex_verdict: str,
    codex_score: int | None,
    cursor_verdict: str,
    cursor_score: int | None,
    http_post_fn: HttpPostFn = _default_http_post,
    include_buttons: bool = True,
) -> bool:
    """Wave 5c: Alert wenn Codex + Cursor im Code-Review disagreen.

    Unterscheidet sich vom Escalation-Alert durch `alert_type="disagreement"`
    — informational, nicht "Pipeline hat aufgegeben". Nico darf entscheiden,
    ob er das PR selbst reviewt oder via `/rerun ai-review` neu startet.
    """
    if not webhook_url:
        return False

    payload: dict[str, Any] = {
        "event": "ai-review/disagreement",
        "alert_type": "disagreement",
        "pr": pr_number,
        "pr_url": pr_url,
        "codex": {"verdict": codex_verdict, "score": codex_score},
        "cursor": {"verdict": cursor_verdict, "score": cursor_score},
        "runbook_url": RUNBOOK_URL,
        "rerun_cmd": _rerun_command(pr_number),
    }
    if include_buttons:
        payload["button_actions"] = _default_button_actions(pr_number)

    try:
        return http_post_fn(webhook_url, json_body=payload, timeout=10)
    except Exception:
        return False


def send_soft_consensus_alert(
    *,
    webhook_url: str,
    pr_number: int,
    pr_url: str,
    codex_score: int,
    cursor_score: int,
    http_post_fn: HttpPostFn = _default_http_post,
    include_buttons: bool = True,
) -> bool:
    """Wave 6c: Soft-Consensus-Alert (avg-score zwischen 5 und 8).

    Unterscheidet sich vom Disagreement-Alert durch konkrete Option-Liste
    (/approve vs. /retry) und spezifische Nachfrage-Semantik. Informational,
    nicht blockierend. Auto-Escalation greift nach 30min falls keine
    Reaktion kommt (separater workflow).
    """
    if not webhook_url:
        return False

    avg = (codex_score + cursor_score) / 2.0
    payload: dict[str, Any] = {
        "event": "ai-review/soft-consensus",
        "alert_type": "soft_consensus",
        "pr": pr_number,
        "pr_url": pr_url,
        "codex_score": codex_score,
        "cursor_score": cursor_score,
        "avg_score": round(avg, 1),
        "options": {
            "approve": f'gh pr comment {pr_number} --body "/ai-review approve"',
            "retry":   f'gh pr comment {pr_number} --body "/ai-review retry"',
            "timeout_minutes": 30,
        },
        "runbook_url": RUNBOOK_URL,
        "rerun_cmd": _rerun_command(pr_number),
    }
    if include_buttons:
        payload["button_actions"] = _default_button_actions(pr_number)

    try:
        return http_post_fn(webhook_url, json_body=payload, timeout=10)
    except Exception:
        return False
