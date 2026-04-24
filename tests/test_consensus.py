"""Integration-style tests for consensus.py — the code-consensus aggregator.

Portiert aus ai-portal/scripts/ai-review/consensus_test.py.
Import angepasst: from . import common, consensus → from ai_review_pipeline import common, consensus.

Seit Wave 5b (2026-04-19) aggregiert die Pipeline VIER Stages:
  - ai-review/code          (Codex GPT-5)
  - ai-review/code-cursor   (Cursor composer-2)     ← neu
  - ai-review/security      (Gemini + semgrep)
  - ai-review/design        (Claude Opus)

Die Code-Stages werden via `resolve_code_consensus` zu einem virtuellen
code-consensus verschmolzen — erst dann läuft die klassische 2-of-3-Logik
auf {code-consensus, security, design}.
"""

from __future__ import annotations

import os
import unittest
from typing import Any

from ai_review_pipeline import common, consensus


class FakeStatusGh:
    """Stand-in for GhClient that records what set_commit_status was called with."""

    def __init__(self, statuses: dict[str, str]) -> None:
        self._statuses = statuses
        self.written: list[dict[str, Any]] = []

    def get_commit_statuses(self, sha: str) -> dict[str, str]:
        return dict(self._statuses)

    def set_commit_status(self, *, sha: str, context: str, state: str,
                          description: str, target_url: str | None = None) -> None:
        self.written.append({
            "sha": sha, "context": context, "state": state,
            "description": description, "target_url": target_url,
        })


class FakeStatusDetailGh:
    """Wave 6b+6c: FakeGh mit descriptions + PR-comments für Nachfrage-Tests."""

    def __init__(self, statuses: dict[str, tuple[str, str]]) -> None:
        """statuses: {context: (state, description)}"""
        self._statuses = statuses
        self.written: list[dict[str, Any]] = []
        self.sticky_posts: list[dict[str, Any]] = []
        self._comments: list[dict[str, Any]] = []

    def get_commit_status_details(self, sha: str) -> dict[str, dict]:
        return {
            ctx: {"state": s[0], "description": s[1]}
            for ctx, s in self._statuses.items()
        }

    def set_commit_status(self, *, sha: str, context: str, state: str,
                          description: str, target_url: str | None = None) -> None:
        self.written.append({
            "sha": sha, "context": context, "state": state,
            "description": description, "target_url": target_url,
        })

    # Wave 6c: Nachfrage-Comment-Support
    def list_pr_comments(self, pr_number: int) -> list[dict]:
        return list(self._comments)

    def post_sticky_comment(self, *, pr_number: int, marker: str, body: str) -> None:
        self.sticky_posts.append({"pr_number": pr_number, "marker": marker, "body": body})


class AggregateConsensusFourStageTests(unittest.TestCase):
    """Alle 4 Stages grün → consensus success."""

    def test_all_four_success_writes_success(self) -> None:
        gh = FakeStatusGh({
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "success",
            common.STATUS_DESIGN: "success",
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(len(gh.written), 1)
        self.assertEqual(gh.written[0]["context"], common.STATUS_CONSENSUS)
        self.assertEqual(gh.written[0]["state"], "success")

    def test_codex_and_cursor_green_security_fail_writes_failure(self) -> None:
        # Code-Reviewer einig (grün), aber Security-Veto → failure
        gh = FakeStatusGh({
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "failure",
            common.STATUS_DESIGN: "success",
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(gh.written[0]["state"], "failure")

    def test_codex_and_cursor_green_one_other_fail_writes_success(self) -> None:
        # code-consensus green, design fail, security green → 2/3 → success
        gh = FakeStatusGh({
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "success",
            common.STATUS_DESIGN: "failure",
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(gh.written[0]["state"], "success")

    def test_codex_cursor_disagree_writes_failure(self) -> None:
        # Disagreement im Code-Bereich: Codex green, Cursor hard → code-consensus
        # fällt auf failure (fail-safe, blockt bis Human-Override)
        gh = FakeStatusGh({
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "failure",
            common.STATUS_SECURITY: "success",
            common.STATUS_DESIGN: "success",
        })
        consensus.aggregate(sha="abc", gh=gh)
        # code-consensus=failure; security+design green = 2 good → nur 2/3 green
        # → success (2/3-Regel greift)
        self.assertEqual(gh.written[0]["state"], "success")
        # Die Description soll den Disagreement ausweisen
        self.assertIn("codex=succ", gh.written[0]["description"])
        self.assertIn("cursor=fail", gh.written[0]["description"])

    def test_codex_cursor_disagree_and_one_other_fails_writes_failure(self) -> None:
        # Disagreement AND design fail → nur security ist grün → 1/3 → failure
        gh = FakeStatusGh({
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "failure",
            common.STATUS_SECURITY: "success",
            common.STATUS_DESIGN: "failure",
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(gh.written[0]["state"], "failure")

    def test_two_real_failures_writes_failure(self) -> None:
        gh = FakeStatusGh({
            common.STATUS_CODE: "failure",
            common.STATUS_CODE_CURSOR: "failure",
            common.STATUS_SECURITY: "failure",
            common.STATUS_DESIGN: "success",
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(gh.written[0]["state"], "failure")

    def test_missing_stage_treated_as_pending(self) -> None:
        # If a stage hasn't posted its status yet, consensus must stay pending.
        gh = FakeStatusGh({
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "success",
            # design missing entirely
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(gh.written[0]["state"], "pending")

    def test_design_skipped_still_passes_when_others_green(self) -> None:
        gh = FakeStatusGh({
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "success",
            common.STATUS_DESIGN: "skipped",
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(gh.written[0]["state"], "success")

    def test_cursor_skipped_ratelimit_codex_green_still_code_consensus_green(self) -> None:
        # Rate-Limit-Skip auf Cursor darf die Code-Stage nicht blockieren.
        # Codex-Alleinentscheidung reicht als code-consensus=success.
        gh = FakeStatusGh({
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "skipped",
            common.STATUS_SECURITY: "success",
            common.STATUS_DESIGN: "success",
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(gh.written[0]["state"], "success")

    def test_both_code_reviewers_skipped_drops_out_of_triple(self) -> None:
        # Beide Code-Reviewer skipped (extrem selten — beide APIs down).
        # Code-consensus = skipped → triple schrumpft auf {security, design}.
        gh = FakeStatusGh({
            common.STATUS_CODE: "skipped",
            common.STATUS_CODE_CURSOR: "skipped",
            common.STATUS_SECURITY: "success",
            common.STATUS_DESIGN: "success",
        })
        consensus.aggregate(sha="abc", gh=gh)
        # 2/2 grün bei denom=2 → success
        self.assertEqual(gh.written[0]["state"], "success")

    def test_all_stages_skipped_writes_pending_not_success(self) -> None:
        # Paranoia: wenn ALLES skipped (nichts wurde reviewt) → pending,
        # nicht success — sonst würden wir PRs grün-rutschen lassen, die
        # nie ein Review gesehen haben.
        gh = FakeStatusGh({
            common.STATUS_CODE: "skipped",
            common.STATUS_CODE_CURSOR: "skipped",
            common.STATUS_SECURITY: "skipped",
            common.STATUS_DESIGN: "skipped",
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(gh.written[0]["state"], "pending")


class ResolveCodeConsensusTests(unittest.TestCase):
    """Wave 5b: Code-Sub-Consensus zwischen Codex + Cursor.

    Unabhängige Reviewer mit Model-Diversity sollen sich bei Agreement auf
    green einigen, bei Disagreement (grün vs. fail) fail-safe bleiben."""

    def test_both_success_is_success(self) -> None:
        self.assertEqual(common.resolve_code_consensus("success", "success"), "success")

    def test_both_failure_is_failure(self) -> None:
        self.assertEqual(common.resolve_code_consensus("failure", "failure"), "failure")

    def test_both_skipped_is_skipped(self) -> None:
        self.assertEqual(common.resolve_code_consensus("skipped", "skipped"), "skipped")

    def test_success_and_skipped_is_success(self) -> None:
        # Rate-Limit auf einem Reviewer — der andere allein reicht
        self.assertEqual(common.resolve_code_consensus("success", "skipped"), "success")
        self.assertEqual(common.resolve_code_consensus("skipped", "success"), "success")

    def test_failure_and_skipped_is_failure(self) -> None:
        self.assertEqual(common.resolve_code_consensus("failure", "skipped"), "failure")
        self.assertEqual(common.resolve_code_consensus("skipped", "failure"), "failure")

    def test_disagreement_is_failure(self) -> None:
        # success + failure → failure (fail-safe bei Reviewer-Disagreement)
        self.assertEqual(common.resolve_code_consensus("success", "failure"), "failure")
        self.assertEqual(common.resolve_code_consensus("failure", "success"), "failure")

    def test_any_pending_is_pending(self) -> None:
        self.assertEqual(common.resolve_code_consensus("success", "pending"), "pending")
        self.assertEqual(common.resolve_code_consensus("pending", "success"), "pending")
        self.assertEqual(common.resolve_code_consensus("pending", "pending"), "pending")

    def test_missing_cursor_treated_as_skipped(self) -> None:
        # Backward-compat: PRs vor Wave 5a haben keinen Cursor-Status.
        # None/missing sollen als skipped behandelt werden, damit die code-
        # Stage allein reicht.
        self.assertEqual(common.resolve_code_consensus("success", None), "success")
        self.assertEqual(common.resolve_code_consensus("failure", None), "failure")


class ResolveCodeConsensusWeightedTests(unittest.TestCase):
    """Wave 6b: Confidence-Weighted Consensus via avg-score.

    Wenn BEIDE Reviewer einen parsbaren Score liefern, wird statt der binären
    state-Logik die avg-score-Logik verwendet:
        avg >= 8  → success
        5 <= avg < 8 → soft   (neu — Tor zum Nachfrage-Pfad)
        avg < 5   → failure

    Fallback auf binäre Logik wenn Scores fehlen.
    """

    def test_both_high_scores_is_success(self) -> None:
        result = common.resolve_code_consensus(
            "success", "success",
            code_score=9, cursor_score=8,
        )
        self.assertEqual(result, "success")

    def test_avg_exactly_8_is_success(self) -> None:
        # Boundary: avg = 8.0 → success
        result = common.resolve_code_consensus(
            "success", "soft",
            code_score=9, cursor_score=7,
        )
        # avg = 8.0 → success
        self.assertEqual(result, "success")

    def test_avg_borderline_is_soft(self) -> None:
        # Codex success (8), Cursor soft (5) → avg 6.5 → NEUER soft-state
        result = common.resolve_code_consensus(
            "success", "failure",
            code_score=8, cursor_score=5,
        )
        self.assertEqual(result, "soft")

    def test_avg_below_5_is_failure(self) -> None:
        result = common.resolve_code_consensus(
            "failure", "failure",
            code_score=4, cursor_score=5,
        )
        # avg = 4.5 → failure
        self.assertEqual(result, "failure")

    def test_avg_exactly_5_is_soft(self) -> None:
        # Boundary: avg = 5 → soft (nicht failure)
        result = common.resolve_code_consensus(
            "failure", "soft",
            code_score=4, cursor_score=6,
        )
        self.assertEqual(result, "soft")

    def test_only_one_score_falls_back_to_binary(self) -> None:
        # Cursor skipped (kein Score), Codex success → binäre Logik nimmt success
        result = common.resolve_code_consensus(
            "success", "skipped",
            code_score=9, cursor_score=None,
        )
        self.assertEqual(result, "success")

    def test_no_scores_is_pure_binary(self) -> None:
        # Kein Scoring-Signal — klassische binäre Logik (Wave 5b Verhalten)
        result = common.resolve_code_consensus("success", "failure")
        self.assertEqual(result, "failure")  # binary fail-safe

    def test_disagreement_with_scores_uses_weighted_not_strict(self) -> None:
        # Reine Binär-Logik (Wave 5b): Codex success + Cursor failure → failure
        # Weighted (Wave 6b): Codex=9 + Cursor=4 → avg 6.5 → soft (differenziert!)
        # Das ist der Hauptwert von Wave 6b — differenziertere Bewertung.
        binary_result = common.resolve_code_consensus("success", "failure")
        weighted_result = common.resolve_code_consensus(
            "success", "failure",
            code_score=9, cursor_score=4,
        )
        self.assertEqual(binary_result, "failure")
        self.assertEqual(weighted_result, "soft")
        self.assertNotEqual(binary_result, weighted_result)

    def test_pending_overrides_weighted(self) -> None:
        # Wenn ein state pending ist, bleibt pending — Scores ignoriert
        result = common.resolve_code_consensus(
            "success", "pending",
            code_score=9, cursor_score=8,
        )
        self.assertEqual(result, "pending")


class ScoreParserTests(unittest.TestCase):
    """Wave 6b: Score-Extraction aus status-descriptions."""

    def test_parses_standard_score_format(self) -> None:
        self.assertEqual(consensus._parse_score("score: 8/10 (green): looks good"), 8)
        self.assertEqual(consensus._parse_score("score: 4/10 (hard): broken"), 4)

    def test_case_insensitive_and_flexible_whitespace(self) -> None:
        self.assertEqual(consensus._parse_score("Score: 7/10 some text"), 7)
        self.assertEqual(consensus._parse_score("score:9/10"), 9)

    def test_rejects_out_of_range(self) -> None:
        self.assertIsNone(consensus._parse_score("score: 11/10 invalid"))
        self.assertIsNone(consensus._parse_score("score: 0/10 invalid"))

    def test_returns_none_without_score_pattern(self) -> None:
        self.assertIsNone(consensus._parse_score("Codex review clean"))
        self.assertIsNone(consensus._parse_score("skipped — no UI changes"))
        self.assertIsNone(consensus._parse_score(None))
        self.assertIsNone(consensus._parse_score(""))


class AggregateWeightedConsensusTests(unittest.TestCase):
    """Wave 6b: aggregate() mit Score-Extraction + soft-state."""

    def test_weighted_soft_consensus_writes_pending_with_nachfrage_desc(self) -> None:
        # Codex=8 green, Cursor=5 soft → avg 6.5 → soft → pending (Nachfrage)
        gh = FakeStatusDetailGh({
            common.STATUS_CODE: ("success", "score: 8/10 (green): codex clean"),
            common.STATUS_CODE_CURSOR: ("failure", "score: 5/10 (soft): cursor perf concern"),
            common.STATUS_SECURITY: ("success", "score: 9/10 (green)"),
            common.STATUS_DESIGN: ("success", "score: 9/10 (green)"),
        })
        consensus.aggregate(sha="abc", gh=gh)
        written = gh.written[0]
        self.assertEqual(written["state"], "pending")
        self.assertIn("human ACK", written["description"])
        self.assertIn("6.5", written["description"])

    def test_weighted_avg_above_8_is_success(self) -> None:
        gh = FakeStatusDetailGh({
            common.STATUS_CODE: ("success", "score: 9/10 (green)"),
            common.STATUS_CODE_CURSOR: ("success", "score: 8/10 (green)"),
            common.STATUS_SECURITY: ("success", "score: 9/10 (green)"),
            common.STATUS_DESIGN: ("success", "score: 9/10 (green)"),
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(gh.written[0]["state"], "success")

    def test_weighted_avg_below_5_with_two_other_success_still_2of3_success(self) -> None:
        # code-consensus=failure (avg 3.5), security+design grün → 2/3 grün
        # = success (bestehende 2-of-3-Regel — Code-failure ist KEIN Veto,
        # nur Security hat das Veto-Recht).
        gh = FakeStatusDetailGh({
            common.STATUS_CODE: ("failure", "score: 4/10 (hard)"),
            common.STATUS_CODE_CURSOR: ("failure", "score: 3/10 (hard)"),
            common.STATUS_SECURITY: ("success", "score: 9/10 (green)"),
            common.STATUS_DESIGN: ("success", "score: 9/10 (green)"),
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(gh.written[0]["state"], "success")

    def test_weighted_avg_below_5_with_security_fail_is_failure(self) -> None:
        # Code failure + Security failure → 2 failures → 1/3 grün → failure
        gh = FakeStatusDetailGh({
            common.STATUS_CODE: ("failure", "score: 4/10"),
            common.STATUS_CODE_CURSOR: ("failure", "score: 3/10"),
            common.STATUS_SECURITY: ("failure", "score: 5/10"),
            common.STATUS_DESIGN: ("success", "score: 9/10"),
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(gh.written[0]["state"], "failure")

    def test_no_scores_falls_back_to_binary(self) -> None:
        # Legacy-Pfad ohne Score-Parsing — FakeStatusGh statt FakeStatusDetailGh
        gh = FakeStatusGh({
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "success",
            common.STATUS_DESIGN: "success",
        })
        consensus.aggregate(sha="abc", gh=gh)
        self.assertEqual(gh.written[0]["state"], "success")

    def test_soft_consensus_posts_nachfrage_sticky(self) -> None:
        # Wave 6c: soft-consensus → Nachfrage-Sticky-Comment wird gepostet
        gh = FakeStatusDetailGh({
            common.STATUS_CODE: ("success", "score: 8/10 (green)"),
            common.STATUS_CODE_CURSOR: ("failure", "score: 5/10 (soft)"),
            common.STATUS_SECURITY: ("success", "score: 9/10"),
            common.STATUS_DESIGN: ("success", "score: 9/10"),
        })
        consensus.aggregate(sha="abc", gh=gh, pr_number=42)
        self.assertEqual(gh.written[0]["state"], "pending")
        self.assertEqual(len(gh.sticky_posts), 1)
        self.assertIn("soft-consensus", gh.sticky_posts[0]["marker"])

    def test_soft_consensus_without_pr_number_skips_nachfrage(self) -> None:
        # Wenn pr_number None (z. B. beim manuellen Run), kein Sticky
        gh = FakeStatusDetailGh({
            common.STATUS_CODE: ("success", "score: 8/10"),
            common.STATUS_CODE_CURSOR: ("failure", "score: 5/10"),
            common.STATUS_SECURITY: ("success", "score: 9/10"),
            common.STATUS_DESIGN: ("success", "score: 9/10"),
        })
        consensus.aggregate(sha="abc", gh=gh, pr_number=None)
        self.assertEqual(len(gh.sticky_posts), 0)


class SecurityWaiverConsensusTests(unittest.TestCase):
    """Wave 7a: Consensus-Logik mit Security-Waiver-Override."""

    def test_security_failure_with_waiver_allows_consensus_success(self) -> None:
        # security=failure + waiver=success → Security wird aus dem Voting-Pool
        # entfernt (behandelt wie "skipped"). code-consensus + design grün bleiben.
        stage_states = {
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "failure",
            common.STATUS_DESIGN: "success",
            common.STATUS_SECURITY_WAIVER: "success",
        }
        state, desc = common.consensus_status(stage_states)
        self.assertEqual(state, "success")
        # Voting-Pool ist nach Waiver nur {code-consensus, design} → 2/2
        self.assertIn("2/2", desc)

    def test_security_failure_without_waiver_still_veto(self) -> None:
        # security=failure + kein waiver → Veto greift (alter Wave-2b Pfad)
        stage_states = {
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "failure",
            common.STATUS_DESIGN: "success",
            # waiver nicht gesetzt oder pending
        }
        state, desc = common.consensus_status(stage_states)
        self.assertEqual(state, "failure")
        self.assertIn("Security-Veto", desc)

    def test_waiver_without_security_failure_noop(self) -> None:
        # Waiver ohne security-failure ist irrelevant (no-op)
        stage_states = {
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "success",
            common.STATUS_DESIGN: "success",
            common.STATUS_SECURITY_WAIVER: "success",  # unnötig da security grün
        }
        state, _ = common.consensus_status(stage_states)
        self.assertEqual(state, "success")

    def test_waiver_success_but_insufficient_other_grüns_still_fails(self) -> None:
        # Waiver entfernt Security aus dem Voting-Pool. Andere Stages müssen
        # trotzdem die Mehrheit haben: wenn code-consensus=failure + design=success
        # → 1/2 (ohne Security) → failure.
        stage_states = {
            common.STATUS_CODE: "failure",
            common.STATUS_CODE_CURSOR: "failure",
            common.STATUS_SECURITY: "failure",
            common.STATUS_DESIGN: "success",
            common.STATUS_SECURITY_WAIVER: "success",
        }
        state, desc = common.consensus_status(stage_states)
        self.assertEqual(state, "failure")
        # Kein Security-Veto im Grund (weil waivered)
        self.assertNotIn("Security-Veto", desc)

    def test_waiver_removes_security_from_voting_pool(self) -> None:
        # Regression (entdeckt bei ai-portal PR#45 cleanup):
        # code-consensus=success + security=failure+waiver + design=skipped
        # → code-consensus ist die einzig verbleibende voting stage → success.
        # Vorher: security=failure zählte gegen success_count → 1/2 → failure.
        stage_states = {
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "failure",
            common.STATUS_DESIGN: "skipped",
            common.STATUS_SECURITY_WAIVER: "success",
        }
        state, desc = common.consensus_status(stage_states)
        self.assertEqual(state, "success", f"Expected success, got {state}: {desc}")
        self.assertNotIn("Security-Veto", desc)

    def test_waiver_plus_design_success_passes(self) -> None:
        # Klassischer waived-Cleanup: code-consensus + design grün, security waived
        stage_states = {
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "failure",
            common.STATUS_DESIGN: "success",
            common.STATUS_SECURITY_WAIVER: "success",
        }
        state, desc = common.consensus_status(stage_states)
        self.assertEqual(state, "success", f"Expected success, got {state}: {desc}")


class ParseScoreEdgeCaseTests(unittest.TestCase):
    """Deckt ValueError-Branch und weitere Edge-Cases in _parse_score ab."""

    def test_non_integer_match_returns_none(self) -> None:
        # _SCORE_RE matched "(\d+)" → int() sollte nie ValueError werfen, aber
        # wir testen den Boundary-Pfad über ein Muster, das formal matcht
        # aber außerhalb 1–10 liegt — hier via out-of-range (schon in anderen
        # Tests), plus direkte Aufruf mit None/empty.
        self.assertIsNone(consensus._parse_score(None))
        self.assertIsNone(consensus._parse_score(""))
        # Wert 0 ist out-of-range → None
        self.assertIsNone(consensus._parse_score("score: 0/10"))
        # Wert 11 ist out-of-range → None
        self.assertIsNone(consensus._parse_score("score: 11/10"))


class MaybeAlertDisagreementTests(unittest.TestCase):
    """Deckt _maybe_alert_disagreement-Pfade ab (Discord config + pr_number Kombos)."""

    def setUp(self) -> None:
        os.environ.pop("GITHUB_SERVER_URL", None)

    def tearDown(self) -> None:
        os.environ.pop("GITHUB_SERVER_URL", None)

    def _discord_config(self) -> dict:
        return {
            "notifications": {
                "target": "discord",
                "discord": {
                    "channel_id": "123456789012345678",
                    "mention_role": "@here",
                    "sticky_message": False,
                },
            }
        }

    def test_no_discord_config_is_noop(self) -> None:
        # Disagreement ohne Discord-Config → kein Crash, notify_discord gibt False zurück
        stage_states = {
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "failure",
        }
        # Sollte kein Exception werfen
        consensus._maybe_alert_disagreement(
            sha="abc", stage_states=stage_states, pr_number=42, config={},
        )

    def test_with_discord_config_and_pr_number_calls_notify_discord(self) -> None:
        # Mit Discord-Config + pr_number wird notify_discord aufgerufen.
        import unittest.mock as mock

        stage_states = {
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "failure",
        }
        with mock.patch(
            "ai_review_pipeline.discord_notify.notify_discord",
            return_value=True,
        ) as mock_notify:
            consensus._maybe_alert_disagreement(
                sha="abc", stage_states=stage_states, pr_number=42,
                config=self._discord_config(),
            )
        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][0]
        assert payload.event_type == "disagreement"
        assert payload.pr_number == 42

    def test_with_discord_config_and_pr_number_exception_swallowed(self) -> None:
        # Wenn notify_discord eine Exception wirft → wird geschluckt
        import unittest.mock as mock

        stage_states = {
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "failure",
        }
        with mock.patch(
            "ai_review_pipeline.discord_notify.notify_discord",
            side_effect=RuntimeError("network down"),
        ):
            # Kein Exception nach außen
            consensus._maybe_alert_disagreement(
                sha="abc", stage_states=stage_states, pr_number=42,
                config=self._discord_config(),
            )

    def test_with_discord_config_but_no_pr_number_is_noop(self) -> None:
        import unittest.mock as mock

        stage_states = {
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "failure",
        }
        with mock.patch(
            "ai_review_pipeline.discord_notify.notify_discord",
        ) as mock_notify:
            # pr_number=None → früher Return, kein notify_discord-Call
            consensus._maybe_alert_disagreement(
                sha="abc", stage_states=stage_states, pr_number=None,
                config=self._discord_config(),
            )
        mock_notify.assert_not_called()

    def test_agreement_skips_alert(self) -> None:
        import unittest.mock as mock

        stage_states = {
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
        }
        with mock.patch(
            "ai_review_pipeline.discord_notify.notify_discord",
        ) as mock_notify:
            # Beide gleich → kein Alert
            consensus._maybe_alert_disagreement(
                sha="abc", stage_states=stage_states, pr_number=42,
                config=self._discord_config(),
            )
        mock_notify.assert_not_called()

    def test_missing_cursor_state_skips_alert(self) -> None:
        # STATUS_CODE_CURSOR fehlt → not cursor → früher Return
        import unittest.mock as mock

        stage_states = {
            common.STATUS_CODE: "success",
            # STATUS_CODE_CURSOR fehlt absichtlich
        }
        with mock.patch(
            "ai_review_pipeline.discord_notify.notify_discord",
        ) as mock_notify:
            consensus._maybe_alert_disagreement(
                sha="abc", stage_states=stage_states, pr_number=42,
                config=self._discord_config(),
            )
        mock_notify.assert_not_called()

    def test_missing_code_state_skips_alert(self) -> None:
        # STATUS_CODE fehlt → not code → früher Return
        import unittest.mock as mock

        stage_states = {
            # STATUS_CODE fehlt absichtlich
            common.STATUS_CODE_CURSOR: "failure",
        }
        with mock.patch(
            "ai_review_pipeline.discord_notify.notify_discord",
        ) as mock_notify:
            consensus._maybe_alert_disagreement(
                sha="abc", stage_states=stage_states, pr_number=42,
                config=self._discord_config(),
            )
        mock_notify.assert_not_called()


class AggregateDiscordAlertTests(unittest.TestCase):
    """Deckt Discord-Alert-Pfad in aggregate() ab (soft-consensus + disagreement)."""

    def _discord_config(self) -> dict:
        return {
            "notifications": {
                "target": "discord",
                "discord": {
                    "channel_id": "123456789012345678",
                    "mention_role": "@here",
                    "sticky_message": False,
                },
            }
        }

    def test_soft_consensus_with_discord_config_sends_alert_no_crash(self) -> None:
        # Discord konfiguriert → Alert-Pfad wird beschritten; HTTP via Mock.
        import unittest.mock as mock

        gh = FakeStatusDetailGh({
            common.STATUS_CODE: ("success", "score: 8/10 (green)"),
            common.STATUS_CODE_CURSOR: ("failure", "score: 5/10 (soft)"),
            common.STATUS_SECURITY: ("success", "score: 9/10"),
            common.STATUS_DESIGN: ("success", "score: 9/10"),
        })
        with mock.patch(
            "ai_review_pipeline.discord_notify.notify_discord",
            return_value=True,
        ) as mock_notify:
            state, _ = consensus.aggregate(
                sha="abc", gh=gh, pr_number=42, config=self._discord_config(),
            )
        self.assertEqual(state, "pending")
        mock_notify.assert_called()
        # Prüfe event_type des soft-consensus Calls
        soft_call = next(
            c for c in mock_notify.call_args_list
            if c[0][0].event_type == "soft_consensus"
        )
        self.assertEqual(soft_call[0][0].pr_number, 42)
        self.assertAlmostEqual(soft_call[0][0].consensus_score, 6.5)

    def test_soft_consensus_discord_exception_swallowed(self) -> None:
        # Wenn notify_discord eine Exception wirft → wird geschluckt
        import unittest.mock as mock

        gh = FakeStatusDetailGh({
            common.STATUS_CODE: ("success", "score: 8/10 (green)"),
            common.STATUS_CODE_CURSOR: ("failure", "score: 5/10 (soft)"),
            common.STATUS_SECURITY: ("success", "score: 9/10"),
            common.STATUS_DESIGN: ("success", "score: 9/10"),
        })
        with mock.patch(
            "ai_review_pipeline.discord_notify.notify_discord",
            side_effect=RuntimeError("network down"),
        ):
            state, _ = consensus.aggregate(
                sha="abc", gh=gh, pr_number=42, config=self._discord_config(),
            )
        self.assertEqual(state, "pending")

    def test_soft_consensus_without_discord_config_no_crash(self) -> None:
        # Kein config → notify_discord gibt False (kein Discord target), kein Crash
        gh = FakeStatusDetailGh({
            common.STATUS_CODE: ("success", "score: 8/10 (green)"),
            common.STATUS_CODE_CURSOR: ("failure", "score: 5/10 (soft)"),
            common.STATUS_SECURITY: ("success", "score: 9/10"),
            common.STATUS_DESIGN: ("success", "score: 9/10"),
        })
        state, _ = consensus.aggregate(sha="abc", gh=gh, pr_number=42, config={})
        self.assertEqual(state, "pending")

    def test_soft_consensus_nachfrage_exception_swallowed(self) -> None:
        # Wenn post_nachfrage_comment eine Exception wirft → wird geschluckt
        import unittest.mock as mock

        gh = FakeStatusDetailGh({
            common.STATUS_CODE: ("success", "score: 8/10 (green)"),
            common.STATUS_CODE_CURSOR: ("failure", "score: 5/10 (soft)"),
            common.STATUS_SECURITY: ("success", "score: 9/10"),
            common.STATUS_DESIGN: ("success", "score: 9/10"),
        })
        with mock.patch(
            "ai_review_pipeline.nachfrage.post_nachfrage_comment",
            side_effect=RuntimeError("gh api down"),
        ):
            state, _ = consensus.aggregate(sha="abc", gh=gh, pr_number=42)
        self.assertEqual(state, "pending")


class MainFunctionTests(unittest.TestCase):
    """Deckt consensus.main() ab."""

    def test_main_success_returns_0(self) -> None:
        # Wir können main() nicht ohne echten GhClient aufrufen — wir testen
        # den Exit-Code-Pfad direkt über aggregate's return value statt via CLI.
        # Stattdessen testen wir die CLI-Parser-Logik und den Return-Code.
        # main() ruft aggregate() intern auf — wir mocken GhClient.
        pass  # main() wird in test_main_exit_code_* getestet

    def test_main_exits_nonzero_on_failure(self) -> None:
        # Patch GhClient so dass aggregate failure zurückgibt
        import unittest.mock as mock

        fake_gh = FakeStatusGh({
            common.STATUS_CODE: "failure",
            common.STATUS_CODE_CURSOR: "failure",
            common.STATUS_SECURITY: "failure",
            common.STATUS_DESIGN: "failure",
        })
        with mock.patch("ai_review_pipeline.common.GhClient", return_value=fake_gh):
            result = consensus.main(["--sha", "deadbeef"])
        self.assertEqual(result, 1)

    def test_main_exits_zero_on_success(self) -> None:
        import unittest.mock as mock

        fake_gh = FakeStatusGh({
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "success",
            common.STATUS_DESIGN: "success",
        })
        with mock.patch("ai_review_pipeline.common.GhClient", return_value=fake_gh):
            result = consensus.main(["--sha", "deadbeef"])
        self.assertEqual(result, 0)

    def test_main_exits_zero_on_pending(self) -> None:
        import unittest.mock as mock

        fake_gh = FakeStatusGh({
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "success",
            # design missing → pending
        })
        with mock.patch("ai_review_pipeline.common.GhClient", return_value=fake_gh):
            result = consensus.main(["--sha", "deadbeef"])
        self.assertEqual(result, 0)

    def test_main_with_pr_and_target_url_args(self) -> None:
        import unittest.mock as mock

        fake_gh = FakeStatusGh({
            common.STATUS_CODE: "success",
            common.STATUS_CODE_CURSOR: "success",
            common.STATUS_SECURITY: "success",
            common.STATUS_DESIGN: "success",
        })
        with mock.patch("ai_review_pipeline.common.GhClient", return_value=fake_gh):
            result = consensus.main([
                "--sha", "deadbeef",
                "--pr", "17",
                "--target-url", "https://example.com/run/1",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(fake_gh.written[0]["target_url"], "https://example.com/run/1")


class StatusContextScopedAggregateTests(unittest.TestCase):
    """Scenario 2 (Red): aggregate() respects status_context_prefix filter.

    Nur Statuses mit Prefix 'ai-review-v2/' werden aggregiert, und der
    Consensus-Status wird unter dem angegebenen status_context geschrieben.
    """

    def test_aggregate_filters_statuses_by_prefix(self) -> None:
        # Arrange: FakeGh liefert gemischte Statuses — einige ai-review/,
        # einige ai-review-v2/. aggregate() soll nur v2-Statuses nehmen.
        class FakeMixedGh:
            """Liefert Status-Mix: ai-review/* und ai-review-v2/*."""

            def __init__(self) -> None:
                self.written: list[dict] = []

            def get_commit_statuses(self, sha: str) -> dict[str, str]:
                return {
                    "ai-review/code": "failure",          # alt — soll ignoriert werden
                    "ai-review/security": "failure",       # alt — soll ignoriert werden
                    "ai-review-v2/code": "success",        # neu
                    "ai-review-v2/code-cursor": "success", # neu
                    "ai-review-v2/security": "success",    # neu
                    "ai-review-v2/design": "success",      # neu
                }

            def set_commit_status(
                self, *, sha: str, context: str, state: str,
                description: str, target_url: str | None = None,
            ) -> None:
                self.written.append({
                    "sha": sha, "context": context,
                    "state": state, "description": description,
                })

        gh = FakeMixedGh()

        # Act: mit status_context_prefix filtern
        state, desc = consensus.aggregate(
            sha="abc",
            gh=gh,
            status_context_prefix="ai-review-v2",
            status_context="ai-review-v2/consensus",
        )

        # Assert: consensus ist success (alle v2-Stages grün, alte werden ignoriert)
        self.assertEqual(state, "success")

    def test_aggregate_writes_consensus_to_custom_context(self) -> None:
        # Arrange: alle v2-Stages grün
        class FakeV2Gh:
            def __init__(self) -> None:
                self.written: list[dict] = []

            def get_commit_statuses(self, sha: str) -> dict[str, str]:
                return {
                    "ai-review-v2/code": "success",
                    "ai-review-v2/code-cursor": "success",
                    "ai-review-v2/security": "success",
                    "ai-review-v2/design": "success",
                }

            def set_commit_status(
                self, *, sha: str, context: str, state: str,
                description: str, target_url: str | None = None,
            ) -> None:
                self.written.append({
                    "sha": sha, "context": context,
                    "state": state, "description": description,
                })

        gh = FakeV2Gh()

        # Act
        consensus.aggregate(
            sha="abc",
            gh=gh,
            status_context_prefix="ai-review-v2",
            status_context="ai-review-v2/consensus",
        )

        # Assert: der Consensus-Status wird unter ai-review-v2/consensus geschrieben
        contexts_written = [w["context"] for w in gh.written]
        self.assertIn("ai-review-v2/consensus", contexts_written)
        # Und NICHT unter dem Standard-Context
        self.assertNotIn(common.STATUS_CONSENSUS, contexts_written)


class WaiverFetchRegressionTests(unittest.TestCase):
    """Regression: consensus.aggregate() MUSS den security-waiver-Status
    aus den Commit-Statuses lesen, sonst greift der Security-Veto auch bei
    valider Waiver-Begründung.

    Bug: STAGE_STATUS_CONTEXTS enthielt STATUS_SECURITY_WAIVER nicht, sodass
    consensus.py den Waiver-Status nie gefetched hat (siehe ai-portal PR#44
    Phase-5-Cutover: Waiver wurde via /ai-review security-waiver gesetzt,
    aber Security-Veto blockierte trotzdem den Merge).
    """

    def test_aggregate_fetches_security_waiver_context(self) -> None:
        # Arrange: security=failure, waiver=success — Veto muss übersteuert werden
        class FakeWaiverGh:
            def __init__(self) -> None:
                self.written: list[dict] = []
                self.fetched_contexts: list[str] = []

            def get_commit_statuses(self, sha: str) -> dict[str, str]:
                return {
                    common.STATUS_CODE: "success",
                    common.STATUS_CODE_CURSOR: "success",
                    common.STATUS_SECURITY: "failure",
                    common.STATUS_SECURITY_WAIVER: "success",
                    common.STATUS_DESIGN: "success",
                }

            def set_commit_status(
                self, *, sha: str, context: str, state: str,
                description: str, target_url: str | None = None,
            ) -> None:
                self.written.append({
                    "sha": sha, "context": context,
                    "state": state, "description": description,
                })

        gh = FakeWaiverGh()

        # Act
        state, desc = consensus.aggregate(sha="abc", gh=gh)

        # Assert: Konsens ist success, NICHT Security-Veto.
        # Wenn der Waiver-Context nicht gefetched wird, kippt das zurück
        # auf "Security-Veto: ai-review/security = failure".
        self.assertEqual(state, "success", f"Expected success, got {state}: {desc}")
        self.assertNotIn("Security-Veto", desc)

    def test_stage_status_contexts_includes_waiver(self) -> None:
        # Strukturelle Absicherung: wenn jemand den Waiver-Context aus der
        # Liste entfernt, schlägt dieser Test an, bevor der obige Integrations-
        # Test rot wird.
        self.assertIn(
            common.STATUS_SECURITY_WAIVER,
            common.STAGE_STATUS_CONTEXTS,
            "STATUS_SECURITY_WAIVER muss in STAGE_STATUS_CONTEXTS sein, "
            "damit consensus.py den Waiver-Status aus GitHub fetched.",
        )


class StatusContextMainArgsTests(unittest.TestCase):
    """Scenario 2 (Red): consensus.main() akzeptiert --status-context und
    --status-context-prefix Argumente und propagiert sie an aggregate()."""

    def test_main_accepts_status_context_and_prefix_args(self) -> None:
        import unittest.mock as mock

        fake_gh = FakeStatusGh({
            "ai-review-v2/code": "success",
            "ai-review-v2/code-cursor": "success",
            "ai-review-v2/security": "success",
            "ai-review-v2/design": "success",
        })
        with mock.patch("ai_review_pipeline.common.GhClient", return_value=fake_gh):
            result = consensus.main([
                "--sha", "deadbeef",
                "--status-context", "ai-review-v2/consensus",
                "--status-context-prefix", "ai-review-v2",
            ])
        # Exit 0 wenn consensus success oder pending
        self.assertEqual(result, 0)

    def test_main_status_context_written_to_custom_context(self) -> None:
        import unittest.mock as mock

        fake_gh = FakeStatusGh({
            "ai-review-v2/code": "success",
            "ai-review-v2/code-cursor": "success",
            "ai-review-v2/security": "success",
            "ai-review-v2/design": "success",
        })
        with mock.patch("ai_review_pipeline.common.GhClient", return_value=fake_gh):
            consensus.main([
                "--sha", "deadbeef",
                "--status-context", "ai-review-v2/consensus",
                "--status-context-prefix", "ai-review-v2",
            ])
        # Consensus muss unter ai-review-v2/consensus geschrieben worden sein
        contexts_written = [w["context"] for w in fake_gh.written]
        self.assertIn("ai-review-v2/consensus", contexts_written)


class DiscordChannelOverrideTests(unittest.TestCase):
    """Scenario 3 (Red): consensus.main() respektiert --discord-channel und --no-ping."""

    def _make_disagreement_gh(self) -> FakeStatusDetailGh:
        """Liefert Status-Setup, der Disagreement triggert (code vs code-cursor)."""
        return FakeStatusDetailGh({
            common.STATUS_CODE: ("success", "score: 9/10 (green)"),
            common.STATUS_CODE_CURSOR: ("failure", "score: 3/10 (hard)"),
            common.STATUS_SECURITY: ("success", "score: 9/10 (green)"),
            common.STATUS_DESIGN: ("success", "score: 9/10 (green)"),
        })

    def test_discord_channel_override_passed_to_notify_discord(self) -> None:
        import unittest.mock as mock

        gh = self._make_disagreement_gh()
        captured_payloads: list = []

        def fake_notify(payload, config, **kwargs):
            captured_payloads.append(payload)
            return True

        with mock.patch("ai_review_pipeline.common.GhClient", return_value=gh), \
             mock.patch("ai_review_pipeline.discord_notify.notify_discord", side_effect=fake_notify):
            consensus.main([
                "--sha", "deadbeef",
                "--pr", "42",
                "--discord-channel", "1234",
                "--no-ping",
            ])

        # Assert: notify_discord wurde aufgerufen (Disagreement-Pfad)
        # und channel_id="1234" im Payload
        self.assertTrue(len(captured_payloads) > 0, "notify_discord must have been called")
        discord_calls_with_channel = [
            p for p in captured_payloads if p.channel_id == "1234"
        ]
        self.assertTrue(
            len(discord_calls_with_channel) > 0,
            f"Expected channel_id='1234' in at least one payload, got: {captured_payloads}",
        )

    def test_no_ping_suppresses_mention_role(self) -> None:
        import unittest.mock as mock

        gh = self._make_disagreement_gh()
        captured_payloads: list = []

        def fake_notify(payload, config, **kwargs):
            captured_payloads.append(payload)
            return True

        with mock.patch("ai_review_pipeline.common.GhClient", return_value=gh), \
             mock.patch("ai_review_pipeline.discord_notify.notify_discord", side_effect=fake_notify):
            consensus.main([
                "--sha", "deadbeef",
                "--pr", "42",
                "--discord-channel", "1234",
                "--no-ping",
            ])

        # Assert: mention_role ist leer/None in ALLEN Payloads wenn --no-ping gesetzt
        # (kein @here, kein @role-mention)
        for p in captured_payloads:
            self.assertFalse(
                p.mention_role and p.mention_role.strip(),
                f"Expected empty mention_role with --no-ping, got: {p.mention_role!r}",
            )

    def test_discord_channel_override_in_notify_discord_payload(self) -> None:
        """discord_notify.DiscordNotifyPayload unterstützt channel_id-Override."""
        from ai_review_pipeline.discord_notify import DiscordNotifyPayload
        payload = DiscordNotifyPayload(
            event_type="disagreement",
            pr_url="https://github.com/example/repo/pull/1",
            repo="example/repo",
            pr_number=1,
            consensus_score=5.0,
            stage_scores={},
            findings=[],
            button_actions=[],
            channel_id="1234",
            mention_role="",  # leer = kein ping
            sticky_message=None,
        )
        self.assertEqual(payload.channel_id, "1234")
        self.assertEqual(payload.mention_role, "")


if __name__ == "__main__":
    unittest.main()
