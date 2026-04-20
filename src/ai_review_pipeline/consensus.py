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

from ai_review_pipeline import common, nachfrage, telegram_alert


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
) -> None:
    """Wave 5c: Schickt nur wenn Codex+Cursor disagreen (verdict-Mismatch).

    Nur informational — der consensus-status (fail-safe=failure bei Disagreement)
    blockt den Merge separat. Dieser Alert informiert Nico, damit er aktiv
    re-reviewt oder re-triggert. Wenn TELEGRAM_NOTIFICATION_WEBHOOK nicht
    gesetzt, no-op (kein Crash).
    """
    code = stage_states.get(common.STATUS_CODE)
    cursor = stage_states.get(common.STATUS_CODE_CURSOR)
    if not code or not cursor:
        return
    # Disagreement = beide terminal UND inhaltlich unterschiedlich
    if code == cursor or "pending" in (code, cursor) or "skipped" in (code, cursor):
        return
    webhook = os.environ.get("TELEGRAM_NOTIFICATION_WEBHOOK", "")
    if not webhook:
        return
    # Resolve PR number for the rerun_cmd — wenn nicht übergeben, skip Alert
    if pr_number is None:
        return
    repo = common.REPO
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    try:
        telegram_alert.send_disagreement_alert(
            webhook_url=webhook,
            pr_number=pr_number,
            pr_url=f"{server}/{repo}/pull/{pr_number}",
            codex_verdict=code,
            codex_score=None,  # Scores nicht aus stage_states ablesbar — dafür
            cursor_verdict=cursor,  # bräuchten wir die full descriptions
            cursor_score=None,
        )
    except Exception as exc:
        print(f"⚠️ Disagreement-alert failed: {exc}", file=sys.stderr)


def aggregate(
    *,
    sha: str,
    gh: _GhLike | None = None,
    target_url: str | None = None,
    pr_number: int | None = None,
) -> tuple[str, str]:
    """Read all stage statuses, compute consensus, and write it back.

    Wave 5b: STAGE_STATUS_CONTEXTS enthält jetzt 4 stages (code, code-cursor,
    security, design). Wave 5c: Bei Code-Reviewer-Disagreement wird ein
    Telegram-Informational-Alert geschickt.
    """
    gh = gh or common.GhClient()

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
            details.get(common.STATUS_CODE, {}).get("description")
        )
        cursor_score = _parse_score(
            details.get(common.STATUS_CODE_CURSOR, {}).get("description")
        )
    else:
        current = gh.get_commit_statuses(sha)

    # Normalize missing stages to "pending" — they're in-flight, not skipped.
    stage_states = {
        ctx: current.get(ctx, "pending")
        for ctx in common.STAGE_STATUS_CONTEXTS
    }
    state, description = common.consensus_status(
        stage_states,
        code_score=code_score,
        cursor_score=cursor_score,
    )
    gh.set_commit_status(
        sha=sha,
        context=common.STATUS_CONSENSUS,
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

        webhook = os.environ.get("TELEGRAM_NOTIFICATION_WEBHOOK", "")
        if webhook:
            repo = common.REPO
            server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
            try:
                telegram_alert.send_soft_consensus_alert(
                    webhook_url=webhook,
                    pr_number=pr_number,
                    pr_url=f"{server}/{repo}/pull/{pr_number}",
                    codex_score=code_score,
                    cursor_score=cursor_score,
                )
            except Exception as exc:
                print(f"⚠️ Soft-Consensus-Alert failed: {exc}", file=sys.stderr)

    # Wave 5c: Disagreement-Alert (nur wenn wirklich disagree + Webhook da)
    _maybe_alert_disagreement(sha=sha, stage_states=stage_states, pr_number=pr_number)
    return state, description


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI-review consensus aggregator")
    parser.add_argument("--sha", required=True, help="Commit SHA (the PR head)")
    parser.add_argument("--target-url", default=None, help="Optional status target_url")
    parser.add_argument("--pr", type=int, default=None, help="PR number (for Disagreement-Alert)")
    args = parser.parse_args(argv)

    state, desc = aggregate(
        sha=args.sha, target_url=args.target_url, pr_number=args.pr,
    )
    print(f"ai-review/consensus={state} — {desc}")
    # Fail the workflow if consensus is failure so Branch Protection lights up fast
    return 0 if state in ("success", "pending") else 1


if __name__ == "__main__":
    sys.exit(main())
