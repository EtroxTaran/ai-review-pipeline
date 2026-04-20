"""Tests for ai_review_pipeline.stages.stage + design-stage path filter.

Portiert aus ai-portal/scripts/ai-review/stage_test.py.
Imports umgestellt auf absolute ai_review_pipeline.*-Pfade.

Focus: pure-function behaviors (sentinel detection, path-filter) without
invoking any CLI. The full run_stage() orchestration requires git-worktree
and real gh-API — that's covered by the smoke PR in the verification plan.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from ai_review_pipeline import scoring
from ai_review_pipeline.stages import design_review
from ai_review_pipeline.stages import stage
from tests.test_common import FakeRunner


class IsCleanOutputTests(unittest.TestCase):
    def test_exact_sentinel_match_is_clean(self) -> None:
        self.assertTrue(stage.is_clean_output("LGTM", ("LGTM",)))

    def test_sentinel_with_trailing_whitespace(self) -> None:
        self.assertTrue(stage.is_clean_output("LGTM\n   \n", ("LGTM",)))

    def test_sentinel_on_own_line_with_no_findings_is_clean(self) -> None:
        out = "Reviewed the diff.\nLGTM\nThanks!"
        self.assertTrue(stage.is_clean_output(out, ("LGTM",)))

    def test_sentinel_but_with_findings_is_not_clean(self) -> None:
        # "LGTM but also `foo.ts:42` needs a fix" — must NOT be clean
        out = "LGTM overall.\n- `apps/foo.ts:42` actually wait, fix this"
        self.assertFalse(stage.is_clean_output(out, ("LGTM",)))

    def test_no_sentinel_no_findings_is_not_clean(self) -> None:
        # Reviewer rambled but said nothing actionable. Better to fail-safe.
        self.assertFalse(
            stage.is_clean_output("Hmm, maybe consider refactoring.", ("LGTM",)),
        )

    def test_different_sentinels_per_stage(self) -> None:
        self.assertTrue(stage.is_clean_output("SEC-OK", ("SEC-OK",)))
        self.assertTrue(stage.is_clean_output("DESIGN-OK", ("DESIGN-OK",)))
        self.assertFalse(stage.is_clean_output("LGTM", ("SEC-OK",)))

    def test_treat_no_findings_as_clean_when_enabled(self) -> None:
        # Regression: the code stage invokes `codex review` ohne unseren
        # Prompt zu übergeben (CLI-Parser-Limit: --base + [PROMPT] sind
        # mutually exclusive). Codex antwortet deshalb mit seinem Default-
        # Wording ("No issues found." o.ä.), nicht mit "LGTM". Der code-
        # Stage muss "keine geparste Finding-Line" als sauber akzeptieren,
        # sonst rutscht jede saubere PR fälschlich in die Fix-Loop.
        codex_default_clean = "Reviewed all diff hunks. No issues found."
        self.assertTrue(stage.is_clean_output(
            codex_default_clean, ("LGTM",), treat_no_findings_as_clean=True,
        ))

    def test_treat_no_findings_as_clean_still_rejects_findings(self) -> None:
        # Safety: auch mit treat_no_findings_as_clean=True darf ein Output
        # mit `path:line`-Findings nicht als clean durchgehen.
        out = "Minor issue at `apps/foo.ts:42` — please fix."
        self.assertFalse(stage.is_clean_output(
            out, ("LGTM",), treat_no_findings_as_clean=True,
        ))

    def test_treat_no_findings_as_clean_default_off_is_fail_safe(self) -> None:
        # Default-Verhalten (security/design) bleibt fail-safe: ohne explizites
        # Sentinel UND ohne Findings → rot (Reviewer hat gerambled).
        out = "Hmm, reviewed it, nothing jumps out."
        self.assertFalse(stage.is_clean_output(out, ("LGTM",)))

    def test_treat_no_findings_as_clean_rejects_codex_severity_tag(self) -> None:
        # Regression: Codex' Default-Review-Prompt emittiert Findings im
        # Format `[P0]`/`[P1]`/`[P2]`/`[P3]` — ohne Backticks um den Pfad.
        # Der bisherige SOURCE_FILE_RE matched nur backtickte Pfade, wodurch
        # ein echtes Finding mit `treat_no_findings_as_clean=True` fälschlich
        # als sauber durchging. Severity-Tags müssen daher als Finding zählen.
        out = (
            "- [P1] Grant contents: read before checkout — "
            "/tmp/pr/.github/workflows/ai-review-consensus.yml:22-26"
        )
        self.assertFalse(stage.is_clean_output(
            out, ("LGTM",), treat_no_findings_as_clean=True,
        ))

    def test_treat_no_findings_as_clean_rejects_unbackticked_path_line(self) -> None:
        # Codex nennt Pfade häufig ohne Backticks (`scripts/ai-review/stage.py:321-325`).
        # Auch diese müssen als Finding gelten, sonst slippt ein
        # Kommentar-Review ohne [P*]-Tag als clean durch.
        out = (
            "Concern at scripts/ai-review/stage.py:321-325 — status may "
            "target an unpushed local commit."
        )
        self.assertFalse(stage.is_clean_output(
            out, ("LGTM",), treat_no_findings_as_clean=True,
        ))

    def test_treat_no_findings_as_clean_accepts_prose_without_markers(self) -> None:
        # Gegenprobe: echter Clean-Output (kein P-Tag, keine file:line-Referenz)
        # bleibt grün. Schützt den Happy-Path gegen Over-Triggering der neuen
        # Detektoren.
        out = "Reviewed all diff hunks carefully. No issues found."
        self.assertTrue(stage.is_clean_output(
            out, ("LGTM",), treat_no_findings_as_clean=True,
        ))


class StageConfigTreatNoFindingsTests(unittest.TestCase):
    def test_code_stage_treats_no_findings_as_clean(self) -> None:
        # Der Codex-Review-Pfad kann unser LGTM-Sentinel nicht erzwingen
        # (Prompt wird nicht durchgereicht), also muss der Stage-Config-
        # Schalter aktiv sein.
        from ai_review_pipeline.stages import code_review
        self.assertTrue(code_review.CONFIG.treat_no_findings_as_clean)

    def test_security_and_design_default_fail_safe(self) -> None:
        from ai_review_pipeline.stages import design_review, security_review
        self.assertFalse(security_review.CONFIG.treat_no_findings_as_clean)
        self.assertFalse(design_review.CONFIG.treat_no_findings_as_clean)


class DesignPathFilterTests(unittest.TestCase):
    def test_tsx_file_triggers_design_review(self) -> None:
        self.assertTrue(design_review._has_ui_changes(
            ["plugins/finance-plugin/src/App.tsx"],
        ))

    def test_css_file_triggers(self) -> None:
        self.assertTrue(design_review._has_ui_changes(
            ["apps/portal-shell/src/styles.css"],
        ))

    def test_pure_backend_change_does_not_trigger(self) -> None:
        self.assertFalse(design_review._has_ui_changes(
            ["apps/portal-api/src/routes/finance.ts",
             "apps/portal-api/src/app.ts",
             "pnpm-lock.yaml"],
        ))

    def test_ts_under_shared_ui_triggers(self) -> None:
        # Non-tsx ts file but under packages/shared-ui/ — likely touches tokens
        self.assertTrue(design_review._has_ui_changes(
            ["packages/shared-ui/src/components/chart.ts"],
        ))

    def test_empty_changes_does_not_trigger(self) -> None:
        self.assertFalse(design_review._has_ui_changes([]))

    def test_mixed_change_with_at_least_one_ui_file_triggers(self) -> None:
        self.assertTrue(design_review._has_ui_changes([
            "apps/portal-api/src/routes/finance.ts",
            "plugins/finance-plugin/src/FinanceCharts.tsx",
            "pnpm-lock.yaml",
        ]))


class BuildReviewPromptTests(unittest.TestCase):
    """Regression: the review prompt MUST be rebuilt on every iteration.

    Wenn `full_prompt` einmal vor der Fix-Loop gebaut wird und dann in
    do_review() wiederverwendet wird, sieht der Reviewer immer den
    Original-Diff — auch wenn Claude die Findings zwischenzeitlich gefixt
    hat. Konsequenz: bereits gefixte Findings erscheinen weiterhin,
    Regressions durch Claude werden übersehen.
    """

    def test_build_review_prompt_reads_diff_from_worktree_each_call(self) -> None:
        # Arrange
        runner = FakeRunner()
        runner.on(["git", "diff"], stdout="diff content")

        # Act: two invocations — simulating two review iterations
        prompt1 = stage._build_review_prompt(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            preflight_ctx="",
            base_prompt="REVIEW",
            runner=runner,
        )
        prompt2 = stage._build_review_prompt(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            preflight_ctx="",
            base_prompt="REVIEW",
            runner=runner,
        )

        # Assert: each call must re-shell `git diff` (stat + full)
        diff_calls = [c for c in runner.calls if c[:2] == ["git", "diff"]]
        self.assertGreaterEqual(
            len(diff_calls), 4,
            "each _build_review_prompt call must re-read git diff (stat+full)",
        )
        self.assertIn("REVIEW", prompt1)
        self.assertIn("REVIEW", prompt2)

    def test_build_review_prompt_includes_preflight_when_present(self) -> None:
        runner = FakeRunner()
        runner.on(["git", "diff"], stdout="x")

        prompt = stage._build_review_prompt(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            preflight_ctx="## Pre-Flight OK",
            base_prompt="REVIEW",
            runner=runner,
        )

        self.assertIn("Pre-Flight OK", prompt)
        self.assertIn("REVIEW", prompt)


class ClassifyOutputTests(unittest.TestCase):
    """Wave 2b: classify_output routes to scoring-aware or sentinel-fallback.

    Kontrakt:
      - Wenn JSON-Block parsbar: ScoredVerdict entscheidet (green=success, sonst failure)
      - Wenn Parse schlägt fehl: Fallback auf is_clean_output() (backward-compat)
      - Security-Role: score<=7 => hard (kein soft-band)
    """

    def _cfg_code(self) -> stage.StageConfig:
        return stage.StageConfig(
            name="code",
            status_context="ai-review/code",
            sticky_marker="<!-- x -->",
            title_prefix="",
            prompt_file="code_review.md",
            reviewer_label="r",
            ok_sentinels=("LGTM",),
            reviewer_fn=lambda **_kw: "",
        )

    def _cfg_security(self) -> stage.StageConfig:
        return stage.StageConfig(
            name="security",
            status_context="ai-review/security",
            sticky_marker="<!-- x -->",
            title_prefix="",
            prompt_file="security_review.md",
            reviewer_label="r",
            ok_sentinels=("SEC-OK",),
            reviewer_fn=lambda **_kw: "",
        )

    def test_scoring_green_returns_success(self) -> None:
        raw = '```json\n{"score": 9, "verdict": "green", "summary": "solid"}\n```'
        state, desc, sv = stage.classify_output(raw, self._cfg_code())

        self.assertEqual(state, "success")
        self.assertIn("9/10", desc)
        self.assertIn("green", desc)
        self.assertIsNotNone(sv)
        self.assertEqual(sv.score, 9)

    def test_scoring_soft_returns_failure(self) -> None:
        # Code-Role: 6 → soft → failure (blocks, fix-loop läuft)
        raw = '```json\n{"score": 6, "verdict": "soft", "summary": "minor issues"}\n```'
        state, desc, sv = stage.classify_output(raw, self._cfg_code())

        self.assertEqual(state, "failure")
        self.assertIn("6/10", desc)
        self.assertIn("soft", desc)
        self.assertEqual(sv.verdict, "soft")

    def test_scoring_hard_returns_failure(self) -> None:
        raw = '```json\n{"score": 3, "verdict": "hard", "summary": "broken"}\n```'
        state, desc, _ = stage.classify_output(raw, self._cfg_code())
        self.assertEqual(state, "failure")
        self.assertIn("3/10", desc)

    def test_security_role_treats_score_7_as_hard_even_if_verdict_soft(self) -> None:
        # Security-Veto: role-aware Override — score 7 darf bei Security NICHT
        # durchrutschen, auch wenn der LLM 'soft' geschrieben hat.
        raw = '```json\n{"score": 7, "verdict": "soft", "summary": "borderline"}\n```'
        state, desc, sv = stage.classify_output(raw, self._cfg_security())

        self.assertEqual(state, "failure")
        # Role-override muss in der description sichtbar sein
        self.assertIn("7/10", desc)
        self.assertIn("hard", desc)
        # Das ursprüngliche ScoredVerdict bleibt für Escalation-Trace erhalten
        self.assertEqual(sv.score, 7)

    def test_no_json_falls_back_to_sentinel(self) -> None:
        # Kein JSON → fallback auf sentinel. LGTM matcht, also success.
        raw = "LGTM"
        state, desc, sv = stage.classify_output(raw, self._cfg_code())

        self.assertEqual(state, "success")
        self.assertIsNone(sv)

    def test_no_json_no_sentinel_falls_back_to_failure(self) -> None:
        raw = "Hmm, some issues, `src/foo.ts:42` needs fix"
        state, _, sv = stage.classify_output(raw, self._cfg_code())

        self.assertEqual(state, "failure")
        self.assertIsNone(sv)

    def test_parse_failed_json_treated_as_failure(self) -> None:
        # Malformed JSON-Block — parse_scored_verdict returnt parse_failed=True.
        # Classify muss das als failure behandeln (fail-closed).
        raw = '```json\n{"score": 11, "verdict": "green", "summary": "x"}\n```'
        state, desc, sv = stage.classify_output(raw, self._cfg_code())

        self.assertEqual(state, "failure")
        self.assertIsNotNone(sv)
        self.assertTrue(sv.parse_failed)
        self.assertIn("parse-fail", desc)


class LoadPromptTests(unittest.TestCase):
    """load_prompt liest eine Datei aus dem prompts/-Verzeichnis relativ zu stage.py."""

    def test_load_prompt_reads_file_contents(self) -> None:
        # Arrange: temporäres prompts-Verzeichnis neben stage.py simulieren
        with tempfile.TemporaryDirectory() as tmpdir:
            prompts_dir = Path(tmpdir)
            prompt_file = prompts_dir / "test_prompt.md"
            prompt_file.write_text("## Hello World\nSome review prompt.", encoding="utf-8")

            # Act: PROMPTS_DIR temporär überschreiben
            original_dir = stage.PROMPTS_DIR
            stage.PROMPTS_DIR = prompts_dir
            try:
                result = stage.load_prompt("test_prompt.md")
            finally:
                stage.PROMPTS_DIR = original_dir

        # Assert
        self.assertIn("Hello World", result)
        self.assertIn("Some review prompt.", result)

    def test_load_prompt_raises_file_not_found(self) -> None:
        # Arrange: nicht existierendes Verzeichnis
        original_dir = stage.PROMPTS_DIR
        stage.PROMPTS_DIR = Path("/nonexistent/path")
        try:
            with self.assertRaises(FileNotFoundError):
                stage.load_prompt("missing.md")
        finally:
            stage.PROMPTS_DIR = original_dir


# ---------------------------------------------------------------------------
# Helper: Fake GhClient für run_stage Tests
# ---------------------------------------------------------------------------

def _make_fake_gh(
    *,
    pr_data: dict[str, Any] | None = None,
    is_draft: bool = False,
    raise_on_get_pr: Exception | None = None,
) -> MagicMock:
    """Erstellt einen Mock-GhClient der keine echten gh-Calls macht."""
    gh = MagicMock()
    if raise_on_get_pr is not None:
        gh.get_pr.side_effect = raise_on_get_pr
    else:
        data = pr_data or {
            "title": "feat: test PR",
            "body": "Test body",
            "baseRefName": "main",
            "headRefOid": "abc123sha",
            "headRefName": "feat/test-branch",
            "isDraft": is_draft,
        }
        gh.get_pr.return_value = data
    gh.set_commit_status.return_value = None
    gh.post_sticky_comment.return_value = None
    return gh


def _make_stage_cfg(
    *,
    reviewer_fn: Any = None,
    path_filter: Any = None,
    treat_no_findings_as_clean: bool = False,
    name: str = "code",
) -> stage.StageConfig:
    """Minimale StageConfig für run_stage-Tests."""
    if reviewer_fn is None:
        reviewer_fn = lambda **_kw: "LGTM"
    return stage.StageConfig(
        name=name,
        status_context=f"ai-review/{name}",
        sticky_marker=f"<!-- nexus-ai-review-{name} -->",
        title_prefix=f"AI {name.capitalize()} Review",
        prompt_file=f"{name}_review.md",
        reviewer_label=f"Test Reviewer ({name})",
        ok_sentinels=("LGTM",),
        reviewer_fn=reviewer_fn,
        path_filter=path_filter,
        treat_no_findings_as_clean=treat_no_findings_as_clean,
    )


class RunStageTests(unittest.TestCase):
    """run_stage Orchestrierungstests mit gemockten externen Deps.

    Kein echter git-Worktree, kein echtes gh. Die Kernel-Logik (draft-Skip,
    path-filter-Skip, clean-first-pass, skip-fix-loop) ist vollständig testbar.
    """

    def test_returns_2_on_pr_fetch_failure(self) -> None:
        # Arrange
        gh = _make_fake_gh(raise_on_get_pr=RuntimeError("connection refused"))
        cfg = _make_stage_cfg()

        # Act
        result = stage.run_stage(cfg, pr_number=42, gh=gh)

        # Assert
        self.assertEqual(result, 2)

    def test_returns_0_for_draft_pr(self) -> None:
        # Arrange: Draft-PRs werden übersprungen → exit 0
        gh = _make_fake_gh(is_draft=True)
        cfg = _make_stage_cfg()

        # Act
        result = stage.run_stage(cfg, pr_number=1, gh=gh)

        # Assert
        self.assertEqual(result, 0)
        # Kein status-set für draft (außer initial pending is OK — wir prüfen
        # nur den Return-Code)

    @patch("subprocess.run")
    def test_returns_0_when_path_filter_skips(self, mock_subprocess: Any) -> None:
        # Arrange: path_filter gibt False zurück → Stage überspringt sich selbst
        mock_subprocess.return_value = MagicMock(returncode=0)
        gh = _make_fake_gh()

        def never_has_ui_changes(files: list[str]) -> bool:
            return False

        cfg = _make_stage_cfg(path_filter=never_has_ui_changes, name="design")

        with patch("ai_review_pipeline.common.git_changed_files", return_value=["backend.ts"]):
            result = stage.run_stage(cfg, pr_number=10, gh=gh, skip_preflight=True)

        # Assert
        self.assertEqual(result, 0)
        # Set status to success mit "skipped" description
        calls = gh.set_commit_status.call_args_list
        success_calls = [c for c in calls if c.kwargs.get("state") == "success"]
        self.assertTrue(
            any("skipped" in (c.kwargs.get("description", "")) for c in success_calls),
            f"Expected 'skipped' in status description, got: {success_calls}",
        )

    @patch("subprocess.run")
    def test_returns_0_on_clean_first_pass(self, mock_subprocess: Any) -> None:
        # Arrange: reviewer gibt LGTM → clean first pass → exit 0
        mock_subprocess.return_value = MagicMock(returncode=0)
        gh = _make_fake_gh()

        reviewer_fn = MagicMock(return_value="LGTM")
        cfg = _make_stage_cfg(reviewer_fn=reviewer_fn)

        with patch("ai_review_pipeline.common.git_diff_stat", return_value="1 file"), \
             patch("ai_review_pipeline.common.git_diff_full", return_value="+ line"), \
             patch("ai_review_pipeline.common.detect_rate_limit", return_value=False), \
             patch("ai_review_pipeline.common.build_sticky_comment", return_value="comment"), \
             patch("ai_review_pipeline.issue_context.build_task_context", return_value=""), \
             patch.object(stage, "load_prompt", return_value="## Prompt"):
            result = stage.run_stage(cfg, pr_number=5, gh=gh, skip_preflight=True)

        # Assert
        self.assertEqual(result, 0)
        # Check success status was set
        success_calls = [
            c for c in gh.set_commit_status.call_args_list
            if c.kwargs.get("state") == "success"
        ]
        self.assertTrue(len(success_calls) >= 1)

    @patch("subprocess.run")
    def test_returns_1_when_skip_fix_loop_and_finding(self, mock_subprocess: Any) -> None:
        # Arrange: reviewer findet Issue, skip_fix_loop=True → exit 1 (kein Fix-Loop)
        mock_subprocess.return_value = MagicMock(returncode=0)
        gh = _make_fake_gh()

        reviewer_fn = MagicMock(return_value="Issue at `src/foo.ts:42` — fix required")
        cfg = _make_stage_cfg(reviewer_fn=reviewer_fn)

        with patch("ai_review_pipeline.common.git_diff_stat", return_value="1 file"), \
             patch("ai_review_pipeline.common.git_diff_full", return_value="+ line"), \
             patch("ai_review_pipeline.common.detect_rate_limit", return_value=False), \
             patch("ai_review_pipeline.common.build_sticky_comment", return_value="comment"), \
             patch("ai_review_pipeline.issue_context.build_task_context", return_value=""), \
             patch.object(stage, "load_prompt", return_value="## Prompt"):
            result = stage.run_stage(
                cfg, pr_number=7, gh=gh, skip_preflight=True, skip_fix_loop=True,
            )

        # Assert
        self.assertEqual(result, 1)
        failure_calls = [
            c for c in gh.set_commit_status.call_args_list
            if c.kwargs.get("state") == "failure"
        ]
        self.assertTrue(len(failure_calls) >= 1)

    @patch("subprocess.run")
    def test_rate_limit_detected_returns_0_with_skipped_status(
        self, mock_subprocess: Any
    ) -> None:
        # Arrange: Reviewer-Output enthält Rate-Limit-Signal → Stage überspringt sich
        mock_subprocess.return_value = MagicMock(returncode=0)
        gh = _make_fake_gh()

        rate_limited_output = "Error: rate limit exceeded — try again later"
        reviewer_fn = MagicMock(return_value=rate_limited_output)
        cfg = _make_stage_cfg(reviewer_fn=reviewer_fn)

        with patch("ai_review_pipeline.common.git_diff_stat", return_value="x"), \
             patch("ai_review_pipeline.common.git_diff_full", return_value="x"), \
             patch("ai_review_pipeline.common.detect_rate_limit", return_value=True), \
             patch("ai_review_pipeline.issue_context.build_task_context", return_value=""), \
             patch.object(stage, "load_prompt", return_value="## Prompt"):
            result = stage.run_stage(cfg, pr_number=3, gh=gh, skip_preflight=True)

        # Assert — Rate-Limit → exit 0 (keine false-Failure)
        self.assertEqual(result, 0)
        skip_calls = [
            c for c in gh.set_commit_status.call_args_list
            if "skipped" in (c.kwargs.get("description", ""))
        ]
        self.assertTrue(len(skip_calls) >= 1)

    @patch("subprocess.run")
    def test_pending_status_failure_doesnt_crash(self, mock_subprocess: Any) -> None:
        # Arrange: set_commit_status(pending) wirft Exception → kein Crash, weiter
        mock_subprocess.return_value = MagicMock(returncode=0)
        gh = _make_fake_gh()

        call_count = [0]
        def status_side_effect(**kwargs: Any) -> None:
            call_count[0] += 1
            if kwargs.get("state") == "pending":
                raise RuntimeError("network timeout")
            return None

        gh.set_commit_status.side_effect = status_side_effect
        reviewer_fn = MagicMock(return_value="LGTM")
        cfg = _make_stage_cfg(reviewer_fn=reviewer_fn)

        with patch("ai_review_pipeline.common.git_diff_stat", return_value="x"), \
             patch("ai_review_pipeline.common.git_diff_full", return_value="x"), \
             patch("ai_review_pipeline.common.detect_rate_limit", return_value=False), \
             patch("ai_review_pipeline.common.build_sticky_comment", return_value=""), \
             patch("ai_review_pipeline.issue_context.build_task_context", return_value=""), \
             patch.object(stage, "load_prompt", return_value="## Prompt"):
            result = stage.run_stage(cfg, pr_number=6, gh=gh, skip_preflight=True)

        # Assert — trotz pending-Exception läuft Stage durch
        self.assertEqual(result, 0)

    @patch("subprocess.run")
    def test_preflight_exception_sets_error_context(self, mock_subprocess: Any) -> None:
        # Arrange: preflight.run_preflight wirft → preflight_ctx wird Error-String
        mock_subprocess.return_value = MagicMock(returncode=0)
        gh = _make_fake_gh()
        reviewer_fn = MagicMock(return_value="LGTM")
        cfg = _make_stage_cfg(reviewer_fn=reviewer_fn)

        with patch("ai_review_pipeline.common.git_diff_stat", return_value="x"), \
             patch("ai_review_pipeline.common.git_diff_full", return_value="x"), \
             patch("ai_review_pipeline.common.detect_rate_limit", return_value=False), \
             patch("ai_review_pipeline.common.build_sticky_comment", return_value=""), \
             patch("ai_review_pipeline.issue_context.build_task_context", return_value=""), \
             patch("ai_review_pipeline.preflight.run_preflight",
                   side_effect=RuntimeError("typecheck failed")), \
             patch.object(stage, "load_prompt", return_value="## Prompt"):
            result = stage.run_stage(cfg, pr_number=8, gh=gh, skip_preflight=False)

        # Assert — Stage läuft durch, preflight-Error ist kein Fatal
        self.assertEqual(result, 0)
        # reviewer wurde mit dem error-preflight_ctx aufgerufen
        call_kwargs = reviewer_fn.call_args.kwargs
        self.assertIn("prompt", call_kwargs)
        # Error-Block muss im Prompt sein
        self.assertIn("Error", call_kwargs["prompt"])

    @patch("subprocess.run")
    def test_sticky_comment_exception_does_not_block(self, mock_subprocess: Any) -> None:
        # Arrange: post_sticky_comment wirft → kein Crash
        mock_subprocess.return_value = MagicMock(returncode=0)
        gh = _make_fake_gh()
        gh.post_sticky_comment.side_effect = RuntimeError("comment too long")
        reviewer_fn = MagicMock(return_value="LGTM")
        cfg = _make_stage_cfg(reviewer_fn=reviewer_fn)

        with patch("ai_review_pipeline.common.git_diff_stat", return_value="x"), \
             patch("ai_review_pipeline.common.git_diff_full", return_value="x"), \
             patch("ai_review_pipeline.common.detect_rate_limit", return_value=False), \
             patch("ai_review_pipeline.common.build_sticky_comment", return_value=""), \
             patch("ai_review_pipeline.issue_context.build_task_context", return_value=""), \
             patch.object(stage, "load_prompt", return_value="## Prompt"):
            result = stage.run_stage(cfg, pr_number=9, gh=gh, skip_preflight=True)

        # Assert — trotz Sticky-Comment-Exception läuft Stage durch
        self.assertEqual(result, 0)

    @patch("subprocess.run")
    def test_run_stage_uses_scoring_in_status_description(
        self, mock_subprocess: Any
    ) -> None:
        # Arrange: Reviewer liefert JSON-Score-Block → Beschreibung enthält Score
        mock_subprocess.return_value = MagicMock(returncode=0)
        gh = _make_fake_gh()

        scored_output = '```json\n{"score": 9, "verdict": "green", "summary": "excellent"}\n```'
        reviewer_fn = MagicMock(return_value=scored_output)
        cfg = _make_stage_cfg(reviewer_fn=reviewer_fn)

        with patch("ai_review_pipeline.common.git_diff_stat", return_value="x"), \
             patch("ai_review_pipeline.common.git_diff_full", return_value="x"), \
             patch("ai_review_pipeline.common.detect_rate_limit", return_value=False), \
             patch("ai_review_pipeline.common.build_sticky_comment", return_value=""), \
             patch("ai_review_pipeline.issue_context.build_task_context", return_value=""), \
             patch.object(stage, "load_prompt", return_value="## Prompt"):
            result = stage.run_stage(cfg, pr_number=11, gh=gh, skip_preflight=True)

        # Assert — success, description enthält Score
        self.assertEqual(result, 0)
        success_calls = [
            c for c in gh.set_commit_status.call_args_list
            if c.kwargs.get("state") == "success" and "9/10" in c.kwargs.get("description", "")
        ]
        self.assertTrue(len(success_calls) >= 1, f"Expected 9/10 in description, got {gh.set_commit_status.call_args_list}")


class BuildArgParserTests(unittest.TestCase):
    """build_arg_parser liefert einen vollständig konfigurierten ArgumentParser."""

    def test_parser_requires_pr_argument(self) -> None:
        ap = stage.build_arg_parser("code")
        with self.assertRaises(SystemExit):
            ap.parse_args([])  # --pr fehlt → SystemExit

    def test_parser_parses_pr_number(self) -> None:
        ap = stage.build_arg_parser("code")
        args = ap.parse_args(["--pr", "42"])
        self.assertEqual(args.pr, 42)

    def test_parser_skip_preflight_default_false(self) -> None:
        ap = stage.build_arg_parser("design")
        args = ap.parse_args(["--pr", "1"])
        self.assertFalse(args.skip_preflight)

    def test_parser_skip_preflight_flag_sets_true(self) -> None:
        ap = stage.build_arg_parser("design")
        args = ap.parse_args(["--pr", "1", "--skip-preflight"])
        self.assertTrue(args.skip_preflight)

    def test_parser_skip_fix_loop_default_false(self) -> None:
        ap = stage.build_arg_parser("security")
        args = ap.parse_args(["--pr", "5"])
        self.assertFalse(args.skip_fix_loop)

    def test_parser_skip_fix_loop_flag_sets_true(self) -> None:
        ap = stage.build_arg_parser("security")
        args = ap.parse_args(["--pr", "5", "--skip-fix-loop"])
        self.assertTrue(args.skip_fix_loop)

    def test_parser_max_iterations_default_two(self) -> None:
        ap = stage.build_arg_parser("code")
        args = ap.parse_args(["--pr", "3"])
        self.assertEqual(args.max_iterations, 2)

    def test_parser_max_iterations_custom(self) -> None:
        ap = stage.build_arg_parser("code")
        args = ap.parse_args(["--pr", "3", "--max-iterations", "4"])
        self.assertEqual(args.max_iterations, 4)

    def test_parser_description_includes_stage_name(self) -> None:
        ap = stage.build_arg_parser("my-special-stage")
        self.assertIn("my-special-stage", ap.description or "")


class BuildReviewPromptTaskContextTests(unittest.TestCase):
    """Prüft den task_context-Pfad in _build_review_prompt."""

    def test_build_review_prompt_includes_task_context_when_present(self) -> None:
        # Arrange
        runner = FakeRunner()
        runner.on(["git", "diff"], stdout="some diff")

        # Act
        prompt = stage._build_review_prompt(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            preflight_ctx="",
            base_prompt="REVIEW",
            task_context="## Task: Fix the bug",
            runner=runner,
        )

        # Assert: task_context muss im Prompt erscheinen
        self.assertIn("Fix the bug", prompt)
        self.assertIn("REVIEW", prompt)

    def test_build_review_prompt_no_task_context_no_extra_block(self) -> None:
        # Arrange
        runner = FakeRunner()
        runner.on(["git", "diff"], stdout="x")

        # Act — kein task_context
        prompt = stage._build_review_prompt(
            worktree=Path("/tmp/wt"),
            base_branch="main",
            preflight_ctx="",
            base_prompt="REVIEW",
            runner=runner,
        )

        # Assert — kein leerer Block durch fehlenden task_context
        self.assertIn("REVIEW", prompt)
        # Kein "Task"-Block wenn kein task_context
        self.assertNotIn("## Task:", prompt)


if __name__ == "__main__":
    unittest.main()
