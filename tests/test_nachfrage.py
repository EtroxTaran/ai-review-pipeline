"""Tests für src/ai_review_pipeline/nachfrage.py (Wave 6c).

Der Nachfrage-Pfad wird aktiviert wenn `code-consensus == "soft"` (avg-score
zwischen 5 und 8). Er postet einen sticky-PR-Comment mit 3 Optionen und
prüft später den PR-Verlauf nach Nicos Antwort (approve|retry|timeout).

Portiert aus ai-portal/scripts/ai-review/nachfrage_test.py.
Import-Anpassung: `from . import nachfrage` → `from ai_review_pipeline import nachfrage`
"""

from __future__ import annotations

import unittest
from typing import Any

from ai_review_pipeline import nachfrage


class FakeGh:
    """Fake GhClient für Nachfrage-Tests. Trackt posted/updated comments +
    simuliert PR-Comment-History."""

    def __init__(self, comments: list[dict] | None = None) -> None:
        self._comments = list(comments or [])
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    def list_pr_comments(self, pr_number: int) -> list[dict]:
        return list(self._comments)

    def post_sticky_comment(
        self, *, pr_number: int, marker: str, body: str,
    ) -> None:
        # Idempotent: wenn Marker schon existiert → update, sonst append.
        for c in self._comments:
            if marker in (c.get("body") or ""):
                c["body"] = body
                self.updated.append({"pr_number": pr_number, "marker": marker, "body": body})
                return
        self._comments.append({
            "id": 100 + len(self._comments),
            "user": {"login": "github-actions[bot]"},
            "body": body,
        })
        self.posted.append({"pr_number": pr_number, "marker": marker, "body": body})


class PostNachfrageCommentTests(unittest.TestCase):
    def test_posts_sticky_comment_with_options(self) -> None:
        gh = FakeGh()
        nachfrage.post_nachfrage_comment(
            pr_number=42,
            codex_score=8,
            cursor_score=5,
            gh=gh,
        )
        self.assertEqual(len(gh.posted), 1)
        body = gh.posted[0]["body"]
        # Marker vorhanden für Idempotenz
        self.assertIn("nexus-ai-review-soft-consensus", body)
        # Beide Scores sichtbar
        self.assertIn("8", body)
        self.assertIn("5", body)
        # Avg kommuniziert
        self.assertIn("6.5", body)
        # Die drei Optionen
        self.assertIn("/ai-review approve", body)
        self.assertIn("/ai-review retry", body)
        self.assertIn("30", body)  # 30 min timeout mention

    def test_second_call_updates_not_appends(self) -> None:
        gh = FakeGh()
        nachfrage.post_nachfrage_comment(
            pr_number=42, codex_score=8, cursor_score=5, gh=gh,
        )
        nachfrage.post_nachfrage_comment(
            pr_number=42, codex_score=7, cursor_score=6, gh=gh,
        )
        self.assertEqual(len(gh.posted), 1)
        self.assertEqual(len(gh.updated), 1)


class CheckNachfrageResponseTests(unittest.TestCase):
    def _nachfrage_comment(self) -> dict:
        return {
            "id": 1,
            "user": {"login": "github-actions[bot]"},
            "body": "<!-- nexus-ai-review-soft-consensus --> ...",
            "created_at": "2026-04-19T19:00:00Z",
        }

    def test_returns_pending_when_no_response(self) -> None:
        gh = FakeGh(comments=[self._nachfrage_comment()])
        result = nachfrage.check_nachfrage_response(
            pr_number=42, pr_author="EtroxTaran", gh=gh,
        )
        self.assertEqual(result, "pending")

    def test_detects_approve_from_pr_author(self) -> None:
        gh = FakeGh(comments=[
            self._nachfrage_comment(),
            {
                "id": 2, "user": {"login": "EtroxTaran"},
                "body": "/ai-review approve",
                "created_at": "2026-04-19T19:10:00Z",
            },
        ])
        result = nachfrage.check_nachfrage_response(
            pr_number=42, pr_author="EtroxTaran", gh=gh,
        )
        self.assertEqual(result, "approved")

    def test_detects_retry_from_pr_author(self) -> None:
        gh = FakeGh(comments=[
            self._nachfrage_comment(),
            {
                "id": 2, "user": {"login": "EtroxTaran"},
                "body": "/ai-review retry please",
                "created_at": "2026-04-19T19:12:00Z",
            },
        ])
        result = nachfrage.check_nachfrage_response(
            pr_number=42, pr_author="EtroxTaran", gh=gh,
        )
        self.assertEqual(result, "retry")

    def test_ignores_approve_from_non_author(self) -> None:
        # Security: nur PR-Author darf approve. Fremde Comments werden ignoriert.
        gh = FakeGh(comments=[
            self._nachfrage_comment(),
            {
                "id": 2, "user": {"login": "randomuser"},
                "body": "/ai-review approve",
                "created_at": "2026-04-19T19:11:00Z",
            },
        ])
        result = nachfrage.check_nachfrage_response(
            pr_number=42, pr_author="EtroxTaran", gh=gh,
        )
        self.assertEqual(result, "pending")

    def test_approve_comment_must_be_after_nachfrage(self) -> None:
        # Wenn der Approve-Comment VOR der Nachfrage kam, zählt er nicht
        # (vermeidet stale-ACK-Interpretation bei re-run).
        gh = FakeGh(comments=[
            {
                "id": 2, "user": {"login": "EtroxTaran"},
                "body": "/ai-review approve",
                "created_at": "2026-04-19T18:00:00Z",  # VOR Nachfrage
            },
            self._nachfrage_comment(),  # 19:00:00Z
        ])
        result = nachfrage.check_nachfrage_response(
            pr_number=42, pr_author="EtroxTaran", gh=gh,
        )
        self.assertEqual(result, "pending")


class SecurityWaiverParsingTests(unittest.TestCase):
    """Wave 7a: `/ai-review security-waiver <reason>` parsing.

    Required: min 30 Zeichen reason (damit kein Kommentar wie `/ai-review
    security-waiver fp` durchrutscht — das wäre kein Audit-Trail).
    """

    def _nachfrage_comment(self) -> dict:
        return {
            "id": 1,
            "user": {"login": "github-actions[bot]"},
            "body": "<!-- nexus-ai-review-soft-consensus --> ...",
            "created_at": "2026-04-19T19:00:00Z",
        }

    def test_parses_valid_security_waiver(self) -> None:
        reason = "False-Positive: line 29 is runs-on not actions/checkout"
        gh = FakeGh(comments=[
            self._nachfrage_comment(),
            {
                "id": 2, "user": {"login": "EtroxTaran"},
                "body": f"/ai-review security-waiver {reason}",
                "created_at": "2026-04-19T19:10:00Z",
            },
        ])
        result = nachfrage.check_security_waiver(
            pr_number=42, pr_author="EtroxTaran", gh=gh,
        )
        self.assertEqual(result.status, "waivered")
        self.assertEqual(result.reason, reason)

    def test_rejects_waiver_reason_too_short(self) -> None:
        # min 30 Zeichen Pflicht
        gh = FakeGh(comments=[
            {
                "id": 2, "user": {"login": "EtroxTaran"},
                "body": "/ai-review security-waiver too short",
                "created_at": "2026-04-19T19:10:00Z",
            },
        ])
        result = nachfrage.check_security_waiver(
            pr_number=42, pr_author="EtroxTaran", gh=gh,
        )
        self.assertEqual(result.status, "invalid")
        self.assertIn("30", result.error_message)

    def test_rejects_waiver_without_reason(self) -> None:
        gh = FakeGh(comments=[
            {
                "id": 2, "user": {"login": "EtroxTaran"},
                "body": "/ai-review security-waiver",
                "created_at": "2026-04-19T19:10:00Z",
            },
        ])
        result = nachfrage.check_security_waiver(
            pr_number=42, pr_author="EtroxTaran", gh=gh,
        )
        self.assertEqual(result.status, "invalid")

    def test_ignores_waiver_from_non_author(self) -> None:
        reason = "False-Positive: line 29 is runs-on not actions/checkout"
        gh = FakeGh(comments=[
            {
                "id": 2, "user": {"login": "randomuser"},
                "body": f"/ai-review security-waiver {reason}",
                "created_at": "2026-04-19T19:10:00Z",
            },
        ])
        result = nachfrage.check_security_waiver(
            pr_number=42, pr_author="EtroxTaran", gh=gh,
        )
        self.assertEqual(result.status, "none")

    def test_no_waiver_returns_none(self) -> None:
        gh = FakeGh(comments=[])
        result = nachfrage.check_security_waiver(
            pr_number=42, pr_author="EtroxTaran", gh=gh,
        )
        self.assertEqual(result.status, "none")

    def test_waiver_strips_leading_whitespace_in_reason(self) -> None:
        # Author may add extra spaces before the reason
        reason = "False-Positive: Gemini parser misidentified YAML line-numbers"
        gh = FakeGh(comments=[
            {
                "id": 2, "user": {"login": "EtroxTaran"},
                "body": f"/ai-review security-waiver   {reason}",
                "created_at": "2026-04-19T19:10:00Z",
            },
        ])
        result = nachfrage.check_security_waiver(
            pr_number=42, pr_author="EtroxTaran", gh=gh,
        )
        self.assertEqual(result.status, "waivered")
        # Leading whitespace getrimmt
        self.assertEqual(result.reason, reason)


if __name__ == "__main__":
    unittest.main()
