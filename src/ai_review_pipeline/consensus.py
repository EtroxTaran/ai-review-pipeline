"""Consensus aggregator — writes the single `ai-review/consensus` commit-status.

Portiert aus ai-portal/scripts/ai-review/consensus.py.

Triggered by a GitHub Actions workflow_run event after any of the three stage
workflows (code/security/design) completes. Reads the current state of all
three stage statuses and writes `ai-review/consensus` with:

  ≥2/3 green            → success
  any stage pending     → pending
  otherwise             → failure
  all skipped / no run  → pending (never accidentally green)

Branch Protection on `main` lists only `ai-review/consensus` as required —
the three stage contexts are informational. That lets a 2/3 consensus merge
without forcing all three green (e.g. a legitimate design-reviewer noise-case
doesn't block a well-tested backend PR).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Protocol

from ai_review_pipeline import common, discord_notify, nachfrage


# Wave 6b: Parse-Pattern für Scores aus status-descriptions.
# Format (siehe stage.classify_output): "score: 8/10 (green): <summary>"
_SCORE_RE = re.compile(r"score:\s*(\d+)\s*/\s*10", re.IGNORECASE)


def _parse_score(description: str | None) -> int | None:
    """Extrahiert den Score aus einer Status-Description, oder None."""
    if not description:
        return None
    m = _SCORE_RE.search(description)
    if not m:
        return None
    try:
        val = int(m.group(1))
    except ValueError:
        return None
    if 1 <= val <= 10:
        return val
    return None


class _GhLike(Protocol):
    def get_commit_statuses(self, sha: str) -> dict[str, str]: ...

    def set_commit_status(
        self, *, sha: str, context: str, state: str,
        description: str, target_url: str | None = None,
    ) -> None: ...


def _maybe_alert_disagreement(
    *,
    sha: str,
    stage_states: dict[str, str],
    pr_number: int | None = None,
    config: dict | None = None,
    channel_override: str | None = None,
    suppress_ping: bool = False,
) -> None:
    """Wave 5c: Schickt nur wenn Codex+Cursor disagreen (verdict-Mismatch).

    Nur informational — der consensus-status (fail-safe=failure bei Disagreement)
    blockt den Merge separat. Dieser Alert informiert Nico, damit er aktiv
    re-reviewt oder re-triggert. Wenn Discord nicht konfiguriert (target != discord),
    no-op (kein Crash) — notify_discord gibt False zurück.
    """
    code = stage_states.get(common.STATUS_CODE)
    cursor = stage_states.get(common.STATUS_CODE_CURSOR)
    if not code or not cursor:
        return
    # Disagreement = beide terminal UND inhaltlich unterschiedlich
    if code == cursor or "pending" in (code, cursor) or "skipped" in (code, cursor):
        return
    # Resolve PR number für pr_url — wenn nicht übergeben, skip Alert
    if pr_number is None:
        return
    repo = common.REPO
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    try:
        discord_notify.notify_discord(
            discord_notify.DiscordNotifyPayload(
                event_type="disagreement",
                pr_url=f"{server}/{repo}/pull/{pr_number}",
                repo=repo,
                pr_number=pr_number,
                consensus_score=0.0,  # kein aggregierter Score beim reinen Disagreement
                stage_scores={
                    "codex_verdict": code,
                    "cursor_verdict": cursor,
                },
                findings=[
                    f"Codex verdict: {code}",
                    f"Cursor verdict: {cursor}",
                ],
                button_actions=[],
                channel_id=channel_override,
                mention_role="" if suppress_ping else "@here",
                sticky_message=None,
            ),
            config or {},
        )
    except Exception as exc:
        print(f"⚠️ Disagreement-alert failed: {exc}", file=sys.stderr)


def aggregate(
    *,
    sha: str,
    gh: _GhLike | None = None,
    target_url: str | None = None,
    pr_number: int | None = None,
    config: dict | None = None,
    status_context: str | None = None,
    status_context_prefix: str | None = None,
    channel_override: str | None = None,
    suppress_ping: bool = False,
) -> tuple[str, str]:
    """Read all stage statuses, compute consensus, and write it back.

    Wave 5b: STAGE_STATUS_CONTEXTS enthält jetzt 4 stages (code, code-cursor,
    security, design). Wave 5c: Bei Code-Reviewer-Disagreement wird ein
    Discord-Informational-Alert geschickt (Phase 5 Cutover: kein Telegram mehr).

    Wave 7 (Issue #1): Neue optionale Parameter:
      status_context_prefix — filtert die aggregierten Statuses nach Prefix
        (z.B. "ai-review-v2" → nur "ai-review-v2/*"-Kontexte werden gelesen).
        Die Standard-STAGE_STATUS_CONTEXTS werden entsprechend umgebaut.
      status_context — überschreibt den Kontext, unter dem der Consensus-Status
        geschrieben wird (Default: common.STATUS_CONSENSUS = "ai-review/consensus").
      channel_override — Discord-Channel-Override für Alerts.
      suppress_ping — wenn True, kein @mention in Discord-Alerts.
    """
    gh = gh or common.GhClient()

    # Effektiver Consensus-Context: custom > default
    effective_consensus_context = status_context or common.STATUS_CONSENSUS

    # Effektive Stage-Contexts: wenn prefix gesetzt, bauen wir die scoped Varianten.
    # Standard: "ai-review/code", "ai-review/code-cursor", etc.
    # Mit prefix="ai-review-v2": "ai-review-v2/code", "ai-review-v2/code-cursor", etc.
    if status_context_prefix is not None:
        effective_stage_contexts = tuple(
            f"{status_context_prefix}/{ctx.split('/', 1)[-1]}"
            for ctx in common.STAGE_STATUS_CONTEXTS
        )
        # Effektive STATUS_CODE / STATUS_CODE_CURSOR für Score-Extraction
        effective_status_code = f"{status_context_prefix}/code"
        effective_status_code_cursor = f"{status_context_prefix}/code-cursor"
    else:
        effective_stage_contexts = common.STAGE_STATUS_CONTEXTS
        effective_status_code = common.STATUS_CODE
        effective_status_code_cursor = common.STATUS_CODE_CURSOR

    # Wave 6b: Wenn der GhClient die neue `get_commit_status_details` Methode
    # hat, nutzen wir sie um auch descriptions (für Score-Extraction) zu
    # bekommen. Sonst fallback auf die alte get_commit_statuses (Tests mit
    # FakeStatusGh).
    code_score: int | None = None
    cursor_score: int | None = None
    if hasattr(gh, "get_commit_status_details"):
        details = gh.get_commit_status_details(sha)
        current = {ctx: details.get(ctx, {}).get("state", "pending")
                   for ctx in details}
        code_score = _parse_score(
            details.get(effective_status_code, {}).get("description")
        )
        cursor_score = _parse_score(
            details.get(effective_status_code_cursor, {}).get("description")
        )
    else:
        current = gh.get_commit_statuses(sha)

    # Normalize missing stages to "pending" — they're in-flight, not skipped.
    stage_states = {
        ctx: current.get(ctx, "pending")
        for ctx in effective_stage_contexts
    }

    # consensus_status erwartet Keys mit Standard-Präfix (common.STATUS_*).
    # Wenn wir scoped contexts verwenden, müssen wir die Keys auf die Standard-
    # STATUS_*-Konstanten remappen, damit die Logik (security-veto, code-consensus)
    # korrekt funktioniert.
    if status_context_prefix is not None:
        remapped_states = {}
        for ctx, state_val in stage_states.items():
            stage_part = ctx.split("/", 1)[-1]
            standard_key = f"ai-review/{stage_part}"
            remapped_states[standard_key] = state_val
    else:
        remapped_states = stage_states

    state, description = common.consensus_status(
        remapped_states,
        code_score=code_score,
        cursor_score=cursor_score,
    )
    gh.set_commit_status(
        sha=sha,
        context=effective_consensus_context,
        state=state,
        description=description,
        target_url=target_url,
    )

    # Wave 6c: Soft-Consensus-Nachfrage — wenn der consensus pending ist
    # UND der Grund die Code-Nachfrage ist (description beginnt mit
    # "Code-review needs human ACK"), posten wir Sticky-Comment + Telegram.
    if (
        state == "pending"
        and description.startswith("Code-review needs human ACK")
        and code_score is not None
        and cursor_score is not None
        and pr_number is not None
    ):
        try:
            nachfrage.post_nachfrage_comment(
                pr_number=pr_number,
                codex_score=code_score,
                cursor_score=cursor_score,
                gh=gh,
            )
        except Exception as exc:
            print(f"⚠️ Nachfrage-Comment failed: {exc}", file=sys.stderr)

        repo = common.REPO
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        avg = (code_score + cursor_score) / 2.0
        try:
            discord_notify.notify_discord(
                discord_notify.DiscordNotifyPayload(
                    event_type="soft_consensus",
                    pr_url=f"{server}/{repo}/pull/{pr_number}",
                    repo=repo,
                    pr_number=pr_number,
                    consensus_score=round(avg, 1),
                    stage_scores={
                        "codex_score": code_score,
                        "cursor_score": cursor_score,
                    },
                    findings=[
                        f"Codex score: {code_score}/10",
                        f"Cursor score: {cursor_score}/10",
                        f"Avg score: {round(avg, 1)}/10 — human ACK required",
                    ],
                    button_actions=[],
                    channel_id=channel_override,
                    mention_role="" if suppress_ping else "@here",
                    sticky_message=None,
                ),
                config or {},
            )
        except Exception as exc:
            print(f"⚠️ Soft-Consensus-Alert failed: {exc}", file=sys.stderr)

    # Wave 5c: Disagreement-Alert (nur wenn wirklich disagree + Discord konfiguriert)
    _maybe_alert_disagreement(
        sha=sha, stage_states=remapped_states, pr_number=pr_number, config=config,
        channel_override=channel_override, suppress_ping=suppress_ping,
    )
    return state, description


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI-review consensus aggregator")
    parser.add_argument("--sha", required=True, help="Commit SHA (the PR head)")
    parser.add_argument("--target-url", default=None, help="Optional status target_url")
    parser.add_argument("--pr", type=int, default=None, help="PR number (for Disagreement-Alert)")
    parser.add_argument(
        "--status-context",
        default=None,
        dest="status_context",
        help=(
            "Überschreibt den Kontext unter dem der Consensus-Status geschrieben wird. "
            "Default: ai-review/consensus. Shadow-Mode: ai-review-v2/consensus."
        ),
    )
    parser.add_argument(
        "--status-context-prefix",
        default=None,
        dest="status_context_prefix",
        help=(
            "Filtert aggregierte Stage-Statuses nach Prefix. "
            "Beispiel: --status-context-prefix ai-review-v2 liest nur "
            "'ai-review-v2/*'-Kontexte. Default: ai-review/."
        ),
    )
    parser.add_argument(
        "--discord-channel",
        default=None,
        dest="discord_channel",
        help="Override-Channel-ID für Discord-Alerts (Discord Snowflake). Default: aus config.",
    )
    parser.add_argument(
        "--no-ping",
        action="store_true",
        dest="no_ping",
        help="Unterdrückt @mention-role in Discord-Alerts (kein @here / @role).",
    )
    args = parser.parse_args(argv)

    state, desc = aggregate(
        sha=args.sha,
        target_url=args.target_url,
        pr_number=args.pr,
        status_context=args.status_context,
        status_context_prefix=args.status_context_prefix,
        channel_override=args.discord_channel,
        suppress_ping=args.no_ping,
    )
    print(f"{args.status_context or 'ai-review/consensus'}={state} — {desc}")
    # Fail the workflow if consensus is failure so Branch Protection lights up fast
    return 0 if state in ("success", "pending") else 1


if __name__ == "__main__":
    sys.exit(main())
