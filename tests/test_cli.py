"""Tests fuer cli.py — unified ai-review console script.

TDD: Tests zuerst (Red), dann Implementation (Green).
Alle Tests folgen Arrange-Act-Assert (AAA).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_main(argv: list[str]) -> int:
    """Ruft cli.main(argv) auf und gibt den Exit-Code zurück."""
    from ai_review_pipeline.cli import main
    return main(argv)


# ---------------------------------------------------------------------------
# 1. --help exits 0
# ---------------------------------------------------------------------------

class TestHelpExitsZero:
    """test_main_help_exits_zero"""

    def test_main_help_exits_zero(self):
        # Arrange
        argv = ["--help"]
        # Act / Assert
        with pytest.raises(SystemExit) as exc_info:
            _run_main(argv)
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# 2. --version prints version
# ---------------------------------------------------------------------------

class TestVersionPrintsVersion:
    """test_main_version_prints_version"""

    def test_main_version_prints_version(self, capsys):
        # Arrange
        argv = ["--version"]
        from ai_review_pipeline import __version__
        # Act
        with pytest.raises(SystemExit) as exc_info:
            _run_main(argv)
        # Assert — argparse version action exits 0 and prints to stdout
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert __version__ in (captured.out + captured.err)


# ---------------------------------------------------------------------------
# 3. No args → usage + exit nonzero
# ---------------------------------------------------------------------------

class TestNoArgsPrintsUsageExitsNonzero:
    """test_no_args_prints_usage_exits_nonzero"""

    def test_no_args_prints_usage_exits_nonzero(self, capsys):
        # Arrange
        argv = []
        # Act
        result = _run_main(argv)
        # Assert
        assert result != 0
        captured = capsys.readouterr()
        assert "usage" in (captured.out + captured.err).lower()


# ---------------------------------------------------------------------------
# 4. Unknown subcommand → exit nonzero
# ---------------------------------------------------------------------------

class TestUnknownSubcommandExitsNonzero:
    """test_unknown_subcommand_exits_nonzero"""

    def test_unknown_subcommand_exits_nonzero(self):
        # Arrange
        argv = ["nonexistent-command"]
        # Act
        with pytest.raises(SystemExit) as exc_info:
            _run_main(argv)
        # Assert
        assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# 5. stage subcommand dispatches to stage module
# ---------------------------------------------------------------------------

class TestStageDispatchesToStageModule:
    """test_stage_dispatches_to_stage_module"""

    def test_stage_code_review_dispatches(self, mocker):
        # Arrange — patch the code_review main at the point cli.py imports it
        mock_main = mocker.patch(
            "ai_review_pipeline.stages.code_review.main",
            return_value=0,
        )
        argv = ["stage", "code-review", "--pr", "42"]
        # Act
        result = _run_main(argv)
        # Assert
        assert result == 0
        mock_main.assert_called_once()

    def test_stage_cursor_review_dispatches(self, mocker):
        # Arrange
        mock_main = mocker.patch(
            "ai_review_pipeline.stages.cursor_review.main",
            return_value=0,
        )
        argv = ["stage", "cursor-review", "--pr", "42"]
        # Act
        result = _run_main(argv)
        # Assert
        assert result == 0
        mock_main.assert_called_once()

    def test_stage_security_dispatches(self, mocker):
        # Arrange
        mock_main = mocker.patch(
            "ai_review_pipeline.stages.security_review.main",
            return_value=0,
        )
        argv = ["stage", "security", "--pr", "42"]
        # Act
        result = _run_main(argv)
        # Assert
        assert result == 0
        mock_main.assert_called_once()

    def test_stage_design_dispatches(self, mocker):
        # Arrange
        mock_main = mocker.patch(
            "ai_review_pipeline.stages.design_review.main",
            return_value=0,
        )
        argv = ["stage", "design", "--pr", "42"]
        # Act
        result = _run_main(argv)
        # Assert
        assert result == 0
        mock_main.assert_called_once()

    def test_stage_ac_validation_dispatches(self, mocker, capsys):
        # Arrange — stage ac-validation hat kein eigenes main(), dispatcht mit exit 2 + Hinweis
        argv = ["stage", "ac-validation"]
        # Act — cli gibt exit 2 zurück und zeigt einen Hinweis auf ac-validate
        result = _run_main(argv)
        # Assert
        assert result == 2
        captured = capsys.readouterr()
        assert "ac-validate" in (captured.out + captured.err)


# ---------------------------------------------------------------------------
# 6. consensus dispatches to consensus.main
# ---------------------------------------------------------------------------

class TestConsensusDispatchesToConsensusMain:
    """test_consensus_dispatches_to_consensus_main"""

    def test_consensus_dispatches(self, mocker):
        # Arrange
        mock_main = mocker.patch(
            "ai_review_pipeline.consensus.main",
            return_value=0,
        )
        argv = ["consensus", "--sha", "abc123"]
        # Act
        result = _run_main(argv)
        # Assert
        assert result == 0
        mock_main.assert_called_once()

    def test_consensus_passes_remaining_argv(self, mocker):
        # Arrange — consensus.main bekommt verbleibende argv-Args übergeben
        captured_argv: list[list[str]] = []

        def capture(argv=None):
            captured_argv.append(argv or [])
            return 0

        mocker.patch("ai_review_pipeline.consensus.main", side_effect=capture)
        argv = ["consensus", "--sha", "deadbeef", "--pr", "99"]
        # Act
        _run_main(argv)
        # Assert — die Args nach "consensus" werden weitergegeben
        assert "--sha" in captured_argv[0]
        assert "deadbeef" in captured_argv[0]


# ---------------------------------------------------------------------------
# 7. auto-fix dispatches to auto_fix.main
# ---------------------------------------------------------------------------

class TestAutoFixDispatchesToAutoFixMain:
    """test_auto_fix_dispatches_to_auto_fix_main"""

    def test_auto_fix_dispatches(self, mocker):
        # Arrange
        mock_main = mocker.patch(
            "ai_review_pipeline.auto_fix.main",
            return_value=0,
        )
        argv = ["auto-fix", "--pr", "10", "--reason", "manual-retry"]
        # Act
        result = _run_main(argv)
        # Assert
        assert result == 0
        mock_main.assert_called_once()

    def test_auto_fix_propagates_nonzero_exit(self, mocker):
        # Arrange
        mocker.patch("ai_review_pipeline.auto_fix.main", return_value=1)
        argv = ["auto-fix", "--pr", "10", "--reason", "manual-retry"]
        # Act
        result = _run_main(argv)
        # Assert
        assert result == 1


# ---------------------------------------------------------------------------
# 8. fix-loop dispatches to fix_loop.main
# ---------------------------------------------------------------------------

class TestFixLoopDispatchesToFixLoopMain:
    """test_fix_loop_dispatches_to_fix_loop_main"""

    def test_fix_loop_dispatches(self, mocker):
        # Arrange
        mock_main = mocker.patch(
            "ai_review_pipeline.fix_loop.main",
            return_value=0,
        )
        argv = [
            "fix-loop",
            "--stage", "code",
            "--pr-number", "5",
            "--summary", "test summary",
            "--worktree", "/tmp/wt",
            "--base-branch", "main",
            "--branch", "feat/test",
        ]
        # Act
        result = _run_main(argv)
        # Assert
        assert result == 0
        mock_main.assert_called_once()


# ---------------------------------------------------------------------------
# 9. ac-validate smoke test with mock judge
# ---------------------------------------------------------------------------

class TestAcValidateBasicFlow:
    """test_ac_validate_basic_flow"""

    def test_ac_validate_no_linked_issues_exits_nonzero(self, tmp_path, mocker):
        """PR-Body ohne Issue-Refs → fail-closed, exit 1."""
        # Arrange
        pr_body_file = tmp_path / "pr_body.txt"
        pr_body_file.write_text("This PR does some things.")

        linked_issues_file = tmp_path / "issues.json"
        linked_issues_file.write_text("{}")  # kein Issue

        argv = [
            "ac-validate",
            "--pr-body-file", str(pr_body_file),
            "--linked-issues-file", str(linked_issues_file),
        ]
        # Act
        result = _run_main(argv)
        # Assert — fail-closed: keine Issues = score=1 → exit 1
        assert result != 0

    def test_ac_validate_waiver_exits_zero(self, tmp_path):
        """Waiver mit ≥30-Zeichen-Reason → score 10 → exit 0."""
        # Arrange
        pr_body_file = tmp_path / "pr_body.txt"
        pr_body_file.write_text("Closes #1\nThis is the PR body.")

        linked_issues_file = tmp_path / "issues.json"
        linked_issues_file.write_text('{"1": []}')

        reason = "Emergency hotfix: deployment-critical regression in prod"
        assert len(reason) >= 30

        argv = [
            "ac-validate",
            "--pr-body-file", str(pr_body_file),
            "--linked-issues-file", str(linked_issues_file),
            "--waiver-reason", reason,
        ]
        # Act
        result = _run_main(argv)
        # Assert — waiver → score 10 → exit 0
        assert result == 0


# ---------------------------------------------------------------------------
# 10. metrics dispatches to metrics_summary.main
# ---------------------------------------------------------------------------

class TestMetricsDispatchesToMetricsSummaryMain:
    """test_metrics_dispatches_to_metrics_summary_main"""

    def test_metrics_dispatches(self, mocker):
        # Arrange
        mock_main = mocker.patch(
            "ai_review_pipeline.metrics_summary.main",
            return_value=0,
        )
        argv = ["metrics"]
        # Act
        result = _run_main(argv)
        # Assert
        assert result == 0
        mock_main.assert_called_once()

    def test_metrics_passes_remaining_args(self, mocker):
        # Arrange
        captured: list[list[str]] = []

        def capture(argv=None):
            captured.append(argv or [])
            return 0

        mocker.patch("ai_review_pipeline.metrics_summary.main", side_effect=capture)
        argv = ["metrics", "--since", "24h", "--json"]
        # Act
        _run_main(argv)
        # Assert
        assert "--since" in captured[0]
        assert "24h" in captured[0]


# ---------------------------------------------------------------------------
# 11. exit code propagated from subcommand
# ---------------------------------------------------------------------------

class TestExitCodePropagatedFromSubcommand:
    """test_exit_code_propagated_from_subcommand"""

    @pytest.mark.parametrize("exit_code", [0, 1, 2])
    def test_consensus_exit_code_propagated(self, mocker, exit_code):
        # Arrange
        mocker.patch("ai_review_pipeline.consensus.main", return_value=exit_code)
        argv = ["consensus", "--sha", "abc"]
        # Act
        result = _run_main(argv)
        # Assert
        assert result == exit_code


# ---------------------------------------------------------------------------
# 12. stage name validation rejects unknown
# ---------------------------------------------------------------------------

class TestStageNameValidationRejectsUnknown:
    """test_stage_name_validation_rejects_unknown"""

    def test_unknown_stage_name_exits_nonzero(self):
        # Arrange
        argv = ["stage", "bogus-stage-name"]
        # Act
        with pytest.raises(SystemExit) as exc_info:
            _run_main(argv)
        # Assert
        assert exc_info.value.code != 0

    def test_valid_stage_names_accepted(self, mocker):
        # Arrange — patch all stage mains so they return immediately
        valid_stages = [
            ("code-review", "ai_review_pipeline.stages.code_review.main"),
            ("cursor-review", "ai_review_pipeline.stages.cursor_review.main"),
            ("security", "ai_review_pipeline.stages.security_review.main"),
            ("design", "ai_review_pipeline.stages.design_review.main"),
        ]
        for stage_name, module_path in valid_stages:
            mocker.patch(module_path, return_value=0)
            argv = ["stage", stage_name, "--pr", "1"]
            # Act
            result = _run_main(argv)
            # Assert
            assert result == 0, f"stage {stage_name} unexpectedly returned {result}"


# ---------------------------------------------------------------------------
# 13. nachfrage → TODO / not-implemented path
# ---------------------------------------------------------------------------

class TestNachfrageNotImplemented:
    """nachfrage has no main() — cli muss 'not implemented' melden und exit 2."""

    def test_nachfrage_exits_2(self, capsys):
        # Arrange
        argv = ["nachfrage"]
        # Act
        result = _run_main(argv)
        # Assert
        assert result == 2
        captured = capsys.readouterr()
        assert "not yet implemented" in (captured.out + captured.err).lower()
