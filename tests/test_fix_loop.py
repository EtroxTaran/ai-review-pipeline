"""Tests für ai_review_pipeline.fix_loop.

Portiert aus ai-portal/scripts/ai-review/fix_loop_test.py.
Imports angepasst:
  from . import fix_loop           → from ai_review_pipeline import fix_loop
  from .common_test import FakeRunner → FakeRunner + FakeCompletedProcess inline

Der Loop ist bewusst als reine Orchestrierung gehalten — alle Subprocess-I/O
läuft über injizierte Callables, sodass Iterationszählung, Escalation-Trigger
und Success-Short-Circuit ohne echte CLIs oder Git-History testbar sind.

TDD: Tests zuerst (Red), dann fix_loop.py portiert (Green).

Laufen mit:
    pytest tests/test_fix_loop.py -v
    pytest tests/test_fix_loop.py -v --cov=ai_review_pipeline.fix_loop
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_review_pipeline import fix_loop


# ---------------------------------------------------------------------------
# Fake subprocess.run result (gleiche Struktur wie in test_common.py)
# ---------------------------------------------------------------------------

@dataclass
class FakeCompletedProcess:
    """Minimaler subprocess.CompletedProcess stand-in für den Runner-DI."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeRunner:
    """Records calls and returns pre-programmed responses.

    Usage:
        runner = FakeRunner()
        runner.on(["git", "rev-parse"], stdout="sha123\\n")
        runner.default(returncode=0, stdout="")
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._responses: list[tuple[list[str], FakeCompletedProcess]] = []
        self._default = FakeCompletedProcess(returncode=0, stdout="", stderr="")

    def on(self, cmd_prefix: list[str], *, stdout: str = "", stderr: str = "",
           returncode: int = 0) -> None:
        """Match by command-prefix (first N tokens)."""
        self._responses.append((
            cmd_prefix,
            FakeCompletedProcess(returncode=returncode, stdout=stdout, stderr=stderr),
        ))

    def default(self, *, stdout: str = "", stderr: str = "",
                returncode: int = 0) -> None:
        self._default = FakeCompletedProcess(
            returncode=returncode, stdout=stdout, stderr=stderr,
        )

    def __call__(self, cmd: list[str], **_kwargs: Any) -> FakeCompletedProcess:
        self.calls.append(list(cmd))
        for prefix, response in self._responses:
            if cmd[: len(prefix)] == prefix:
                return response
        return self._default


# ---------------------------------------------------------------------------
# Test-Doubles für review_fn und fix_fn
# ---------------------------------------------------------------------------

class _Review:
    """Test double für eine Review-Funktion. Gibt (success, summary) pro Call zurück."""

    def __init__(self, sequence: list[tuple[bool, str]]) -> None:
        self.sequence = list(sequence)
        self.calls = 0

    def __call__(self) -> tuple[bool, str]:
        self.calls += 1
        if self.sequence:
            return self.sequence.pop(0)
        return (False, "still red")  # Default: konvergiert nie


class _Fixer:
    """Test double für claude-fix Invocation + commit/push."""

    def __init__(self, *, fail_on_iter: int | None = None) -> None:
        self.fail_on_iter = fail_on_iter
        self.calls: list[int] = []

    def __call__(self, *, stage: str, iteration: int, summary: str,
                 pr_number: int) -> bool:
        self.calls.append(iteration)
        if self.fail_on_iter is not None and iteration == self.fail_on_iter:
            return False
        return True


# ---------------------------------------------------------------------------
# Score-Regression-Guard Tests (Wave 2b)
# ---------------------------------------------------------------------------

class FixLoopScoreRegressionTests(unittest.TestCase):
    """Wave 2b: Score-Regression-Guard.

    Wenn der Reviewer in Iter N einen niedrigeren Score liefert als in Iter N-1,
    konvergiert die Fix-Loop nicht — im Gegenteil, sie macht es schlimmer.
    Dann lieber früh eskalieren statt weiteres Budget zu verbrennen.
    """

    def _scored_output(self, score: int, verdict: str) -> str:
        return f'```json\n{{"score": {score}, "verdict": "{verdict}", "summary": "iter {score}"}}\n```'

    def test_aborts_when_score_regresses(self) -> None:
        # Iter 1: score 6 (soft), Iter 2: score 4 (hard) — Regression.
        # Loop muss nach Iter 2 abbrechen und NICHT iter 3 versuchen.
        review = _Review([
            (False, self._scored_output(6, "soft")),
            (False, self._scored_output(4, "hard")),
            (False, self._scored_output(3, "hard")),  # darf nie aufgerufen werden
        ])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=3,
        )

        self.assertFalse(outcome.success)
        self.assertTrue(outcome.escalated)
        # Nach Regression abgebrochen — nicht alle 3 Iterationen gelaufen
        self.assertEqual(review.calls, 2)
        # Fix wurde nur 1× versucht (zwischen iter 1 und 2)
        self.assertEqual(len(fixer.calls), 1)

    def test_continues_when_score_stable_or_up(self) -> None:
        # Iter 1: 5, Iter 2: 6, Iter 3: 7 — kein Regression, Loop läuft durch
        review = _Review([
            (False, self._scored_output(5, "soft")),
            (False, self._scored_output(6, "soft")),
            (False, self._scored_output(7, "soft")),
        ])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=3,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(review.calls, 3)

    def test_no_guard_when_reviewer_outputs_no_json(self) -> None:
        # Legacy-Pfad (keine JSON-Blocks). Score-Guard darf NICHT feuern —
        # ohne Scoring-Signal kann keine Regression detektiert werden.
        review = _Review([
            (False, "iter 1 prose"),
            (False, "iter 2 prose"),
            (False, "iter 3 prose"),
        ])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=3,
        )

        self.assertFalse(outcome.success)
        # Alle 3 Iter liefen (Guard inaktiv bei fehlendem Score-Signal)
        self.assertEqual(review.calls, 3)


# ---------------------------------------------------------------------------
# Basis-Loop-Tests
# ---------------------------------------------------------------------------

class FixLoopTests(unittest.TestCase):
    def test_returns_success_when_first_review_is_green(self) -> None:
        review = _Review([(True, "LGTM")])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=4,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.iterations, 1)
        self.assertEqual(len(fixer.calls), 0, "no fix should run when initial review green")

    def test_runs_fixes_until_review_green(self) -> None:
        review = _Review([
            (False, "issue 1"),
            (False, "issue 2"),
            (True, "all clean"),
        ])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=4,
        )

        self.assertTrue(outcome.success)
        # 3 review calls → 2 fix calls (one between iter 1→2, one between 2→3)
        self.assertEqual(review.calls, 3)
        self.assertEqual(len(fixer.calls), 2)

    def test_escalates_after_max_iterations(self) -> None:
        # Konvergiert nie — gibt immer (False, summary) zurück
        review = _Review([])  # fällt auf Default-False zurück
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=3,
        )

        self.assertFalse(outcome.success)
        self.assertTrue(outcome.escalated)
        self.assertEqual(review.calls, 3)  # Review 3× versucht
        self.assertEqual(len(fixer.calls), 2)  # Fix zwischen iter 1→2, 2→3

    def test_fix_failure_aborts_immediately(self) -> None:
        # Reviewer meldet red, aber claude-fix gibt False zurück (CLI exit != 0).
        # Loop darf NICHT weitermachen — das würde Rate-Limits verschwenden.
        review = _Review([(False, "issue"), (False, "issue")])
        fixer = _Fixer(fail_on_iter=1)  # erster Fix schlägt fehl

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=4,
        )

        self.assertFalse(outcome.success)
        self.assertTrue(outcome.escalated)
        # Nur EIN Fix versucht — kein Retry nach CLI-Failure
        self.assertEqual(len(fixer.calls), 1)

    def test_records_per_iteration_summaries_for_escalation_comment(self) -> None:
        review = _Review([(False, "A"), (False, "B"), (False, "C")])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="security", pr_number=7, review_fn=review, fix_fn=fixer,
            max_iterations=3,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.summaries, ["A", "B", "C"])
        self.assertEqual(outcome.stage, "security")


# ---------------------------------------------------------------------------
# ClaudeFixer Commit-Check-Tests
# ---------------------------------------------------------------------------

class ClaudeFixerCommitCheckTests(unittest.TestCase):
    """Regression: `git push` exits 0 wenn es nichts zu pushen gibt.

    Wenn Claude keine Files committet, bleibt HEAD unverändert und `git push`
    wäre trotzdem grün — die Stage würde Success melden, obwohl die
    PR-Branch unverändert ist. BEFORE/AFTER HEAD vergleichen und abbrechen,
    wenn kein neuer Commit entstand.
    """

    def _make_runner(
        self, *, head_before: str, head_after: str,
    ) -> FakeRunner:
        """Runner der sequenziell unterschiedliche SHAs bei git rev-parse liefert."""

        class SequentialRunner(FakeRunner):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self._rev_parse_stack = [head_before, head_after]

            def __call__(inner_self, cmd: list[str], **kw: Any):
                inner_self.calls.append(list(cmd))
                # git rev-parse sequenziell (before + after Claude)
                if cmd[:2] == ["git", "rev-parse"]:
                    if inner_self._rev_parse_stack:
                        sha = inner_self._rev_parse_stack.pop(0)
                    else:
                        sha = head_after
                    return FakeCompletedProcess(returncode=0, stdout=f"{sha}\n")
                # Ansonsten normale Prefix-Match-Logik
                for prefix, response in inner_self._responses:
                    if cmd[: len(prefix)] == prefix:
                        return response
                return inner_self._default

        return SequentialRunner()

    def test_returns_false_when_claude_makes_no_commit(self) -> None:
        # Arrange: HEAD unverändert → Claude hat nichts committet
        runner = self._make_runner(
            head_before="sha-original",
            head_after="sha-original",
        )
        runner.default(returncode=0, stdout="")

        fixer = fix_loop.ClaudeFixer(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            branch="feat/x",
            runner=runner,
        )

        # Act
        ok = fixer(stage="code", iteration=1, summary="x", pr_number=42)

        # Assert
        self.assertFalse(ok, "fixer must abort when Claude produced no commit")
        push_calls = [c for c in runner.calls if c[:2] == ["git", "push"]]
        self.assertEqual(
            len(push_calls), 0,
            "must not push when no new commit exists",
        )

    def test_returns_true_when_claude_created_a_commit(self) -> None:
        # Arrange: HEAD advanced → Claude committet → push + verify-success
        runner = self._make_runner(
            head_before="sha-old",
            head_after="sha-new-after-fix",
        )
        runner.default(returncode=0, stdout="")

        fixer = fix_loop.ClaudeFixer(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            branch="feat/x",
            runner=runner,
        )

        # Act
        ok = fixer(stage="code", iteration=1, summary="x", pr_number=42)

        # Assert
        self.assertTrue(ok)
        push_calls = [c for c in runner.calls if c[:2] == ["git", "push"]]
        self.assertEqual(len(push_calls), 1)

    def test_push_uses_head_refspec_for_detached_worktree(self) -> None:
        # Regression: stage.py erstellt den Worktree bei einer SHA → detached HEAD.
        # `git push origin <branch>` pusht den lokalen Branch-Ref (der auf einem
        # detached Worktree nicht existiert) → Fehler "src refspec ... does not match any".
        # Muss HEAD via explizitem Refspec zum Remote-Branch-Ref pushen.
        runner = self._make_runner(
            head_before="sha-old",
            head_after="sha-new-after-fix",
        )
        runner.default(returncode=0, stdout="")

        fixer = fix_loop.ClaudeFixer(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            branch="feat/ai-review-pipeline",
            runner=runner,
        )

        ok = fixer(stage="code", iteration=1, summary="x", pr_number=42)

        self.assertTrue(ok)
        push_calls = [c for c in runner.calls if c[:2] == ["git", "push"]]
        self.assertEqual(len(push_calls), 1)
        push_cmd = push_calls[0]
        # Letztes Argument muss Refspec sein: HEAD → refs/heads/<branch>
        self.assertEqual(
            push_cmd[-1], "HEAD:refs/heads/feat/ai-review-pipeline",
            f"expected HEAD-refspec push, got {push_cmd!r}",
        )

    def test_fixer_resets_head_when_typecheck_fails_after_commit(self) -> None:
        """Regression: nach einem lokalen Fix-Commit, bei dem typecheck scheitert,
        darf die Worktree-HEAD nicht auf dem unpushed Commit stehen bleiben.

        stage.run_stage() ruft nach der Fix-Loop `current_head_sha(worktree)`
        und postet den Terminal-Status auf die zurückgegebene SHA. Wenn
        HEAD auf einem lokalen-only Commit steht (nie auf origin gepushed),
        targetet der Status eine SHA, die GitHub nicht kennt → Stage-Crash
        statt Eskalation. ClaudeFixer muss deshalb bei abort-after-commit
        die HEAD zurück auf den letzten gepushten Stand resetten.
        """
        runner = self._make_runner(
            head_before="sha-last-pushed",
            head_after="sha-local-only",
        )
        runner.on(["pnpm", "-w", "typecheck"], returncode=1, stderr="TS2304")
        runner.default(returncode=0, stdout="")

        fixer = fix_loop.ClaudeFixer(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            branch="feat/x",
            runner=runner,
        )

        ok = fixer(stage="code", iteration=1, summary="x", pr_number=42)

        self.assertFalse(ok)
        reset_calls = [c for c in runner.calls if c[:3] == ["git", "reset", "--hard"]]
        self.assertEqual(
            len(reset_calls), 1,
            f"expected HEAD-reset after failed typecheck, got {runner.calls!r}",
        )
        self.assertEqual(reset_calls[0][-1], "sha-last-pushed")

    def test_fixer_resets_head_when_tests_fail_after_commit(self) -> None:
        """Pendant zum typecheck-Pfad: auch bei fehlschlagenden Tests muss
        HEAD zurück, bevor wir False zurückgeben."""
        runner = self._make_runner(
            head_before="sha-last-pushed",
            head_after="sha-local-only",
        )
        runner.on(["pnpm", "-w", "test"], returncode=1, stderr="2 tests failed")
        runner.default(returncode=0, stdout="")

        fixer = fix_loop.ClaudeFixer(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            branch="feat/x",
            runner=runner,
        )

        ok = fixer(stage="code", iteration=1, summary="x", pr_number=42)

        self.assertFalse(ok)
        reset_calls = [c for c in runner.calls if c[:3] == ["git", "reset", "--hard"]]
        self.assertEqual(len(reset_calls), 1)
        self.assertEqual(reset_calls[0][-1], "sha-last-pushed")

    def test_fixer_resets_head_when_push_fails_after_commit(self) -> None:
        """Wenn `git push` nach erfolgreichem Commit+Verifikation fehlschlägt
        (z. B. Remote-Reject, Netzfehler), darf der lokale Commit nicht als
        HEAD stehenbleiben."""
        runner = self._make_runner(
            head_before="sha-last-pushed",
            head_after="sha-local-only",
        )
        runner.on(
            ["git", "push", "--no-verify"],
            returncode=1, stderr="remote rejected",
        )
        runner.default(returncode=0, stdout="")

        fixer = fix_loop.ClaudeFixer(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            branch="feat/x",
            runner=runner,
        )

        ok = fixer(stage="code", iteration=1, summary="x", pr_number=42)

        self.assertFalse(ok)
        reset_calls = [c for c in runner.calls if c[:3] == ["git", "reset", "--hard"]]
        self.assertEqual(len(reset_calls), 1)
        self.assertEqual(reset_calls[0][-1], "sha-last-pushed")

    def test_fixer_does_not_reset_when_no_commit_was_created(self) -> None:
        """Gegenprobe: wenn Claude gar keinen Commit gemacht hat (head_before
        == head_after), gibt es nichts zu resetten."""
        runner = self._make_runner(
            head_before="sha-unchanged",
            head_after="sha-unchanged",
        )
        runner.default(returncode=0, stdout="")

        fixer = fix_loop.ClaudeFixer(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            branch="feat/x",
            runner=runner,
        )

        ok = fixer(stage="code", iteration=1, summary="x", pr_number=42)

        self.assertFalse(ok)
        reset_calls = [c for c in runner.calls if c[:3] == ["git", "reset", "--hard"]]
        self.assertEqual(
            len(reset_calls), 0,
            "no reset expected when no commit existed to roll back",
        )

    def test_push_bypasses_husky_pre_push_hook(self) -> None:
        """Regression: pre-push hook läuft das vollständige pnpm typecheck+test+e2e
        (~3-5min), was den git-push subprocess-Timeout überschreitet. Fix-Loop
        hat bereits typecheck + `pnpm test --changed` geprüft; Hook-Arbeit ist
        Duplikat. Wir pushen mit `--no-verify`.
        """
        # Arrange: HEAD advanced, damit wir den Push-Schritt erreichen
        runner = self._make_runner(
            head_before="sha-old",
            head_after="sha-new-after-fix",
        )
        runner.default(returncode=0, stdout="")

        fixer = fix_loop.ClaudeFixer(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            branch="feat/x",
            runner=runner,
        )

        # Act
        ok = fixer(stage="code", iteration=1, summary="x", pr_number=42)

        # Assert
        self.assertTrue(ok)
        push_calls = [c for c in runner.calls if c[:2] == ["git", "push"]]
        self.assertEqual(len(push_calls), 1)
        self.assertIn(
            "--no-verify", push_calls[0],
            "push must bypass husky pre-push to avoid re-running the full "
            "test suite (fix-loop already verified typecheck+tests)",
        )


# ---------------------------------------------------------------------------
# Escalation-Message-Tests
# ---------------------------------------------------------------------------

class EscalationMessageTests(unittest.TestCase):
    def test_escalation_body_contains_stage_iteration_and_summaries(self) -> None:
        body = fix_loop.build_escalation_comment(
            stage="design",
            iterations=4,
            summaries=["first fail", "second fail", "third", "fourth"],
            pr_number=42,
        )
        self.assertIn("design", body.lower())
        self.assertIn("4", body)
        self.assertIn("first fail", body)
        self.assertIn("fourth", body)
        # Human-Tag damit Nico benachrichtigt wird
        self.assertIn("@", body)


# ---------------------------------------------------------------------------
# Score-Trend-Gate Tests (Wave 6a)
# ---------------------------------------------------------------------------

class FixLoopScoreTrendTests(unittest.TestCase):
    """Wave 6a: Score-Trend-Gate für bedingte Iter 3.

    Regel:
      - Default max_iterations=2 (starr für Legacy-Pfade ohne Score)
      - Wenn beide Iter 1+2 parsbare Scores liefern UND
        Score(iter2) - Score(iter1) >= MIN_SCORE_IMPROVEMENT (=2) → Iter 3 erlaubt
      - Sonst Escalation nach Iter 2 (heutiges Verhalten bleibt)
      - Iter 3 ist das harte Ende (Empirie: arXiv:2603.26458 max 3 Iter)
    """

    def _scored(self, score: int, verdict: str = "soft") -> str:
        return f'```json\n{{"score": {score}, "verdict": "{verdict}", "summary": "iter score {score}"}}\n```'

    def test_iter3_triggered_when_score_improves_by_2_or_more(self) -> None:
        # Iter 1 = 4, Iter 2 = 6 (Δ=2, triggert Iter 3)
        # Iter 3 gibt 7 zurück (kein green, aber kein Regression)
        review = _Review([
            (False, self._scored(4, "hard")),
            (False, self._scored(6, "soft")),
            (False, self._scored(7, "soft")),
        ])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=2,  # default
        )

        # Iter 3 wurde erreicht trotz max_iterations=2
        self.assertEqual(review.calls, 3)
        self.assertEqual(len(fixer.calls), 2)  # Fix zwischen iter 1→2 und 2→3
        self.assertFalse(outcome.success)
        self.assertTrue(outcome.escalated)
        self.assertEqual(outcome.iterations, 3)

    def test_no_iter3_when_improvement_below_threshold(self) -> None:
        # Iter 1 = 4, Iter 2 = 5 (Δ=1, unter Schwelle → Escalation nach Iter 2)
        review = _Review([
            (False, self._scored(4, "hard")),
            (False, self._scored(5, "soft")),
            (False, self._scored(6, "soft")),  # darf nie aufgerufen werden
        ])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=2,
        )

        self.assertEqual(review.calls, 2)
        self.assertEqual(len(fixer.calls), 1)
        self.assertTrue(outcome.escalated)

    def test_no_iter3_when_score_stagnates(self) -> None:
        # Iter 1 = 5, Iter 2 = 5 (Δ=0 → Stagnation → kein Iter 3)
        review = _Review([
            (False, self._scored(5, "soft")),
            (False, self._scored(5, "soft")),
        ])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=2,
        )

        self.assertEqual(review.calls, 2)
        self.assertTrue(outcome.escalated)

    def test_iter3_then_converges_to_green(self) -> None:
        # Iter 1 = 4, Iter 2 = 6, Iter 3 = 8 (green!) → success
        review = _Review([
            (False, self._scored(4, "hard")),
            (False, self._scored(6, "soft")),
            (True, self._scored(9, "green")),
        ])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=2,
        )

        self.assertTrue(outcome.success)
        self.assertFalse(outcome.escalated)
        self.assertEqual(outcome.iterations, 3)

    def test_legacy_no_scores_respects_max_iterations(self) -> None:
        # Ohne JSON-Scores bleibt die Logik bei max_iterations=2 (backward-compat)
        review = _Review([
            (False, "iter 1 prose without score"),
            (False, "iter 2 prose without score"),
            (False, "iter 3 prose never called"),
        ])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=2,
        )

        # Nur 2 Iter ohne Score-Signal — kein Extra-Iter
        self.assertEqual(review.calls, 2)
        self.assertTrue(outcome.escalated)

    def test_iter3_respects_regression_guard(self) -> None:
        # Iter 1 = 4, Iter 2 = 6 (Trend gut → Iter 3),
        # Iter 3 = 5 (Regression gegenüber Iter 2!) → abort bei Iter 3
        review = _Review([
            (False, self._scored(4, "hard")),
            (False, self._scored(6, "soft")),
            (False, self._scored(5, "soft")),  # Regression!
        ])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=2,
        )

        self.assertEqual(review.calls, 3)  # Iter 3 wird erreicht
        self.assertTrue(outcome.escalated)
        # Summary enthält die Regression-Meldung
        self.assertTrue(
            any("score-regression" in s for s in outcome.summaries),
            f"Expected regression message in summaries: {outcome.summaries}",
        )

    def test_hard_cap_at_three_even_with_improving_scores(self) -> None:
        # Iter 1 = 4, Iter 2 = 6, Iter 3 = 7 — Trend weiter positiv,
        # aber 3 ist das harte Ende (Empirie-basiert)
        review = _Review([
            (False, self._scored(4, "hard")),
            (False, self._scored(6, "soft")),
            (False, self._scored(7, "soft")),
            (False, self._scored(9, "green")),  # darf nie aufgerufen werden
        ])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=2,
        )

        self.assertEqual(review.calls, 3)  # hartes Cap
        self.assertTrue(outcome.escalated)

    def test_explicit_max_iterations_3_works_as_before(self) -> None:
        # Wenn User explizit max_iterations=3 übergibt, läuft die klassische
        # 3-Iter-Logik weiter — kein Konflikt mit dem Score-Trend-Gate.
        review = _Review([
            (False, self._scored(5, "soft")),
            (False, self._scored(5, "soft")),  # Stagnation aber max=3 explizit
            (False, self._scored(5, "soft")),
        ])
        fixer = _Fixer()

        outcome = fix_loop.run_fix_loop(
            stage="code", pr_number=42, review_fn=review, fix_fn=fixer,
            max_iterations=3,
        )

        self.assertEqual(review.calls, 3)
        self.assertTrue(outcome.escalated)


if __name__ == "__main__":
    unittest.main()
