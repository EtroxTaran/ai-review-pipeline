"""Nachfrage-Pfad für soft-code-consensus (Wave 6c).

Portiert aus ai-portal/scripts/ai-review/nachfrage.py.

Wenn Codex + Cursor zusammen einen avg-score zwischen 5 und 8 liefern
(= "soft"), wird dieser Pfad aktiviert. Er postet einen sticky-PR-Comment
mit 3 Optionen:
  • `/ai-review approve` — Nico ACKed, Merge erlaubt
  • `/ai-review retry`   — Iter 3 mit neuem Kontext
  • nichts in 30min       → Auto-Escalation (via ai-review-auto-escalate.yml)

Design-Prinzipien:
  • Nicht blockierend im Runner — der GitHub-Actions-Job ist nach dem
    Posten fertig. Der Antwort-Trigger ist ein separater Workflow
    (ai-review-nachfrage.yml, triggert auf issue_comment:created).
  • Idempotent — bei wiederholtem Posten (neuer Iter-Run) wird der
    bestehende Sticky-Comment geupdated statt dupliziert.
  • Security: nur PR-Author darf approve/retry (check im Workflow).

Empirische Einordnung: Nachfrage-Pattern ist nicht state-of-the-art
(Perplexity 2026-04-19 — Mature-Teams auto-eskalieren). Wir nutzen es als
**hybrid** — informational, mit auto-escalate-Fallback. Kein GitHub-Runner-
Wartezeit für Human-Antwort.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


STICKY_MARKER = "<!-- nexus-ai-review-soft-consensus -->"
SECURITY_WAIVER_MARKER = "<!-- nexus-ai-review-security-waiver -->"

_APPROVE_RE = re.compile(r"^/ai-review\s+approve\b", re.IGNORECASE | re.MULTILINE)
_RETRY_RE = re.compile(r"^/ai-review\s+retry\b", re.IGNORECASE | re.MULTILINE)
# Wave 7a: Security-Waiver — muss mit min 30-Zeichen reason gefolgt werden.
# Matcht gesamte Kommentar-Zeile, damit wir die Reason sauber extrahieren.
_SECURITY_WAIVER_RE = re.compile(
    r"^/ai-review\s+security-waiver\s*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

# Min-Länge für Waiver-Reason. 30 Zeichen zwingt zu echter Begründung
# (nicht "fp" oder "ok"). Wählbar via env AI_REVIEW_WAIVER_MIN_REASON_LENGTH,
# default 30.
MIN_WAIVER_REASON_LENGTH = 30


class _GhLike(Protocol):
    def list_pr_comments(self, pr_number: int) -> list[dict]: ...

    def post_sticky_comment(
        self, *, pr_number: int, marker: str, body: str,
    ) -> None: ...


def build_nachfrage_body(
    *,
    codex_score: int,
    cursor_score: int,
    pr_number: int,
) -> str:
    """Rendert den Sticky-Comment-Body mit den 3 Optionen."""
    avg = (codex_score + cursor_score) / 2.0
    return "\n".join([
        STICKY_MARKER,
        "## 🤔 Code-Review soft-consensus",
        "",
        f"Die beiden Code-Reviewer sind sich nicht einig — **avg {avg:.1f}/10**:",
        "",
        f"- 🤖 **Codex:** {codex_score}/10",
        f"- 🐱 **Cursor:** {cursor_score}/10",
        "",
        "Der Score liegt in der Soft-Zone (5 ≤ avg < 8). Bitte schau drüber.",
        "",
        "### Deine Optionen",
        "",
        "| Kommando | Wirkung |",
        "|---|---|",
        "| `/ai-review approve` | Du bestätigst, Merge wird erlaubt (Human-Override) |",
        "| `/ai-review retry` | Alle 4 Review-Stages laufen neu (frische Kontextverarbeitung) |",
        "| _nichts in 30min_ | Auto-Eskalation zu `consensus=failure` |",
        "",
        "_Nur der PR-Author kann approve/retry triggern. Siehe "
        "[AI-Review-Runbook](docs/v2/30-guides/AI-REVIEW-RUNBOOK.md)._",
    ])


def post_nachfrage_comment(
    *,
    pr_number: int,
    codex_score: int,
    cursor_score: int,
    gh: _GhLike,
) -> None:
    """Postet/updated den Sticky-Nachfrage-Comment auf dem PR."""
    body = build_nachfrage_body(
        codex_score=codex_score, cursor_score=cursor_score, pr_number=pr_number,
    )
    gh.post_sticky_comment(
        pr_number=pr_number, marker=STICKY_MARKER, body=body,
    )


@dataclass
class SecurityWaiverResult:
    """Wave 7a: Ergebnis der Waiver-Suche auf einem PR.

    status:
      - "none"     : kein Waiver-Kommentar gefunden
      - "invalid"  : Waiver vorhanden aber reason zu kurz/fehlt
      - "waivered" : valider Waiver vom PR-Author, mit reason

    `reason` ist nur bei status='waivered' gesetzt.
    `error_message` ist nur bei status='invalid' gesetzt.
    """
    status: str
    reason: str | None = None
    error_message: str | None = None


def check_security_waiver(
    *,
    pr_number: int,
    pr_author: str,
    gh: _GhLike,
) -> SecurityWaiverResult:
    """Sucht nach `/ai-review security-waiver <reason>` vom PR-Author.

    Policy:
      - Nur vom PR-Author — fremde Comments werden ignoriert (→ "none").
      - Reason muss ≥ MIN_WAIVER_REASON_LENGTH Zeichen haben → sonst "invalid".
      - Wenn valid → status="waivered", reason trimmed.

    Use-Case: Security-Review hat False-Positive geliefert (score<=7 = hard).
    Nico kann per expliziter Waiver mit Begründung override machen; die
    Begründung wird als Audit-Trail im commit-status-description abgelegt.
    """
    comments = gh.list_pr_comments(pr_number)
    for c in comments:
        if c.get("user", {}).get("login") != pr_author:
            continue
        body = c.get("body") or ""
        match = _SECURITY_WAIVER_RE.search(body)
        if not match:
            continue
        reason = (match.group(1) or "").strip()
        if len(reason) < MIN_WAIVER_REASON_LENGTH:
            return SecurityWaiverResult(
                status="invalid",
                error_message=(
                    f"Security-Waiver-Reason muss mindestens "
                    f"{MIN_WAIVER_REASON_LENGTH} Zeichen enthalten — "
                    f"gib eine ausführliche Begründung ab."
                ),
            )
        return SecurityWaiverResult(status="waivered", reason=reason)
    return SecurityWaiverResult(status="none")


def check_nachfrage_response(
    *,
    pr_number: int,
    pr_author: str,
    gh: _GhLike,
) -> str:
    """Liest PR-Comments und sucht nach Nachfrage-Antworten vom PR-Author.

    Returns eines von:
      - "approved" — PR-Author hat `/ai-review approve` nach der Nachfrage gepostet
      - "retry"    — PR-Author hat `/ai-review retry` nach der Nachfrage gepostet
      - "pending"  — keine relevante Antwort oder gar keine Nachfrage

    Security: Nur der PR-Author wird akzeptiert (Policy-Enforcement).
    """
    comments = gh.list_pr_comments(pr_number)
    # Finde den Nachfrage-Comment (zuletzt gepostet, falls re-run)
    nachfrage_time: str | None = None
    for c in comments:
        if STICKY_MARKER in (c.get("body") or ""):
            # Neueste Variante — letzter gewinnt
            nachfrage_time = c.get("created_at") or nachfrage_time
    if nachfrage_time is None:
        return "pending"

    # Suche Author-Antwort NACH der Nachfrage
    for c in comments:
        if c.get("user", {}).get("login") != pr_author:
            continue
        created = c.get("created_at") or ""
        if created <= nachfrage_time:
            continue  # vor oder gleichzeitig mit der Nachfrage → nicht zählen
        body = c.get("body") or ""
        if _APPROVE_RE.search(body):
            return "approved"
        if _RETRY_RE.search(body):
            return "retry"
    return "pending"
