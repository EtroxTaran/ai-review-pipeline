"""TDD-Tests für Stage 3 Design-Review.

Getestetes Verhalten:
- _has_ui_changes: Pfad-Filter für UI-relevante Dateien.
- DesignReviewConfig: Korrekte Stage-Konstanten (name, status_context, etc.).
- _claude_reviewer: Delegiert an common.run_claude mit model=claude-opus-4-7.
- main(): Exit-0 bei grünem Review, Exit-1 bei gefundenen Issues.
- Waiver-Pfad (advisory-only, kein Fix-Loop).
- Rate-Limit-Skip-Verhalten (via design-stage path_filter + run_stage stub).

Pattern: Arrange-Act-Assert (AAA). Externe Deps (Runner, GhClient) via
DI-Fakes injiziert — kein echter CLI-Aufruf, kein Netz.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_review_pipeline import common
from ai_review_pipeline.stages.design_review import (
    CONFIG,
    _claude_reviewer,
    _has_ui_changes,
    main,
)


# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------

class FakeCompletedProcess:
    """Minimal subprocess.CompletedProcess-alike für den DI-Runner."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def make_fake_runner(stdout: str = "DESIGN-OK", returncode: int = 0):
    """Gibt eine Runner-Callable zurück, die canned Antworten liefert."""
    def runner(cmd, *, cwd=None, timeout=None, env=None, stdin_data=None):
        return FakeCompletedProcess(stdout=stdout, returncode=returncode)
    return runner


# ---------------------------------------------------------------------------
# 1. _has_ui_changes — Pfad-Filter
# ---------------------------------------------------------------------------

class TestHasUiChanges:
    def test_tsx_file_returns_true(self) -> None:
        # Arrange
        files = ["apps/portal-shell/src/components/Button.tsx"]
        # Act
        result = _has_ui_changes(files)
        # Assert
        assert result is True

    def test_jsx_file_returns_true(self) -> None:
        # Arrange
        files = ["plugins/finance-plugin/src/views/Dashboard.jsx"]
        # Act
        result = _has_ui_changes(files)
        # Assert
        assert result is True

    def test_css_file_returns_true(self) -> None:
        # Arrange
        files = ["apps/portal-shell/src/styles/global.css"]
        # Act
        result = _has_ui_changes(files)
        # Assert
        assert result is True

    def test_scss_file_returns_true(self) -> None:
        # Arrange
        files = ["packages/shared-ui/src/tokens.scss"]
        # Act
        result = _has_ui_changes(files)
        # Assert
        assert result is True

    def test_ts_file_under_shared_ui_dir_returns_true(self) -> None:
        # Arrange — .ts-Dateien unter shared-ui-Verzeichnis sollen ebenfalls matchen
        files = ["packages/shared-ui/src/components/chart.ts"]
        # Act
        result = _has_ui_changes(files)
        # Assert
        assert result is True

    def test_ts_file_under_plugins_dir_returns_true(self) -> None:
        # Arrange — .ts-Dateien unter plugins/-Verzeichnis sollen matchen
        files = ["plugins/finance-plugin/src/utils/tokens.ts"]
        # Act
        result = _has_ui_changes(files)
        # Assert
        assert result is True

    def test_backend_ts_file_returns_false(self) -> None:
        # Arrange — Backend-TS hat keinen UI-Bezug
        files = ["apps/portal-api/src/routes/finance.ts"]
        # Act
        result = _has_ui_changes(files)
        # Assert
        assert result is False

    def test_empty_file_list_returns_false(self) -> None:
        # Arrange
        files: list[str] = []
        # Act
        result = _has_ui_changes(files)
        # Assert
        assert result is False

    def test_only_backend_files_returns_false(self) -> None:
        # Arrange
        files = [
            "apps/portal-api/src/db/schema.sql",
            "apps/portal-api/src/lib/auth.ts",
            "pyproject.toml",
        ]
        # Act
        result = _has_ui_changes(files)
        # Assert
        assert result is False

    def test_mixed_files_returns_true_if_any_ui(self) -> None:
        # Arrange
        files = [
            "apps/portal-api/src/routes/users.ts",   # non-UI
            "apps/portal-shell/src/views/Home.tsx",  # UI
        ]
        # Act
        result = _has_ui_changes(files)
        # Assert
        assert result is True


# ---------------------------------------------------------------------------
# 2. CONFIG — Stage-Konfigurationskonstanten
# ---------------------------------------------------------------------------

class TestDesignReviewConfig:
    def test_stage_name_is_design(self) -> None:
        # Arrange / Act — CONFIG ist Modul-Level-Konstante
        # Assert
        assert CONFIG.name == "design"

    def test_status_context_matches_common(self) -> None:
        # Arrange / Act
        # Assert
        assert CONFIG.status_context == common.STATUS_DESIGN

    def test_sticky_marker_matches_common(self) -> None:
        # Arrange / Act
        # Assert
        assert CONFIG.sticky_marker == common.MARKER_DESIGN_REVIEW

    def test_ok_sentinel_is_design_ok(self) -> None:
        # Arrange / Act
        # Assert
        assert "DESIGN-OK" in CONFIG.ok_sentinels

    def test_path_filter_is_callable(self) -> None:
        # Arrange / Act
        # Assert
        assert callable(CONFIG.path_filter)

    def test_path_filter_delegates_to_has_ui_changes(self) -> None:
        # Arrange
        ui_files = ["src/App.tsx"]
        non_ui_files = ["src/server.ts"]
        # Act + Assert
        assert CONFIG.path_filter(ui_files) is True
        assert CONFIG.path_filter(non_ui_files) is False

    def test_reviewer_label_mentions_claude(self) -> None:
        # Arrange / Act
        # Assert
        assert "Claude" in CONFIG.reviewer_label

    def test_prompt_file_is_design_review_md(self) -> None:
        # Assert
        assert CONFIG.prompt_file == "design_review.md"


# ---------------------------------------------------------------------------
# 3. _claude_reviewer — Modell-Flag und DI-Runner
# ---------------------------------------------------------------------------

class TestClaudeReviewer:
    def test_invokes_run_claude_with_opus_model_from_registry(self) -> None:
        """_claude_reviewer ruft common.run_claude mit Opus-Model aus Registry.

        Regression-Hook: Der Modell-Name ist NICHT mehr hardcoded in design_review.py;
        er kommt aus resolve_model('design') → CLAUDE_OPUS aus registry/MODEL_REGISTRY.env.
        Dieser Test pinnt env-var, um test-environment-Drift zu vermeiden.
        """
        # Arrange
        captured_kwargs: list[dict] = []

        def fake_run_claude(**kwargs):
            captured_kwargs.append(kwargs)
            return "DESIGN-OK"

        worktree = Path("/tmp/fake-worktree")

        # Act — Env-Override pinnt das erwartete Modell determiniert
        with patch.dict(os.environ, {"AI_REVIEW_MODEL_DESIGN": "claude-opus-test-pin"}):
            with patch("ai_review_pipeline.stages.design_review.common.run_claude", new=fake_run_claude):
                _claude_reviewer(
                    prompt="Check this UI code",
                    worktree=worktree,
                    base_branch="main",
                )

        # Assert — Env-Override greift, Model kommt genau daher
        assert len(captured_kwargs) == 1
        assert captured_kwargs[0]["model"] == "claude-opus-test-pin"
        assert captured_kwargs[0]["prompt"] == "Check this UI code"

    def test_returns_reviewer_output_string(self) -> None:
        """_claude_reviewer gibt den Output-String aus run_claude zurück."""
        # Arrange
        expected = "DESIGN-OK"
        worktree = Path("/tmp/fake-worktree")

        # Act
        with patch(
            "ai_review_pipeline.stages.design_review.common.run_claude",
            return_value=expected,
        ):
            result = _claude_reviewer(
                prompt="any prompt",
                worktree=worktree,
                base_branch="main",
            )

        # Assert
        assert result == expected

    def test_timeout_output_contains_timeout_message(self) -> None:
        """Bei Timeout gibt run_claude eine Timeout-Meldung zurück (kein Exception-Crash)."""
        # Arrange
        expected_timeout_msg = "_(Claude Timeout nach 300s — lokal erneut laufen lassen)_"
        worktree = Path("/tmp/fake-worktree")

        # Act — simuliere Timeout-Antwort (run_claude fängt TimeoutExpired intern ab)
        with patch(
            "ai_review_pipeline.stages.design_review.common.run_claude",
            return_value=expected_timeout_msg,
        ):
            result = _claude_reviewer(
                prompt="any prompt",
                worktree=worktree,
                base_branch="main",
            )

        # Assert — Caller bekommt Meldung, keinen Exception-Crash
        assert "Timeout" in result or "timeout" in result.lower()


# ---------------------------------------------------------------------------
# 4. main() — CLI-Einsprungpunkt
# ---------------------------------------------------------------------------

class TestMain:
    def test_main_returns_0_on_clean_review(self) -> None:
        """main() soll 0 (Erfolg) zurückgeben wenn das Review grün ist."""
        # Arrange
        fake_gh = MagicMock()
        fake_gh.get_pr.return_value = {
            "title": "feat: add Button",
            "body": "Closes #1",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "headRefName": "feat/button",
            "isDraft": False,
        }
        fake_gh.get_commit_statuses.return_value = {}

        # Act — Patch stage.run_stage damit kein echter Git-Worktree gebaut wird
        with patch(
            "ai_review_pipeline.stages.design_review.stage.run_stage",
            return_value=0,
        ) as mock_run:
            exit_code = main(["--pr", "1"])

        # Assert
        assert exit_code == 0

    def test_main_passes_skip_fix_loop_true(self) -> None:
        """Design-Stage ist advisory-only: skip_fix_loop muss immer True sein."""
        # Arrange
        captured_kwargs: list[dict] = []

        def fake_run_stage(cfg, *, pr_number, skip_preflight, skip_fix_loop, max_iterations):
            captured_kwargs.append({
                "cfg": cfg,
                "skip_fix_loop": skip_fix_loop,
            })
            return 0

        # Act
        with patch(
            "ai_review_pipeline.stages.design_review.stage.run_stage",
            new=fake_run_stage,
        ):
            main(["--pr", "42"])

        # Assert — Fix-Loop MUSS übersprungen werden (advisory-Rolle)
        assert len(captured_kwargs) == 1
        assert captured_kwargs[0]["skip_fix_loop"] is True

    def test_main_returns_1_on_failing_review(self) -> None:
        """main() soll 1 (Failure) zurückgeben wenn der Reviewer Findings hat."""
        # Arrange / Act
        with patch(
            "ai_review_pipeline.stages.design_review.stage.run_stage",
            return_value=1,
        ):
            exit_code = main(["--pr", "99"])

        # Assert
        assert exit_code == 1

    def test_main_returns_2_on_error(self) -> None:
        """main() soll 2 (Error) zurückgeben bei internem Fehler."""
        # Arrange / Act
        with patch(
            "ai_review_pipeline.stages.design_review.stage.run_stage",
            return_value=2,
        ):
            exit_code = main(["--pr", "7"])

        # Assert
        assert exit_code == 2


# ---------------------------------------------------------------------------
# 5. Pfad-Filter — Skip-Verhalten für non-UI PRs (Integrations-Level)
# ---------------------------------------------------------------------------

class TestPathFilterIntegration:
    def test_path_filter_skips_pure_backend_pr(self) -> None:
        """Design-Stage darf für rein Backend-PRs (keine UI-Files) skippen."""
        # Arrange
        backend_only = [
            "apps/portal-api/src/routes/finance.ts",
            "apps/portal-api/src/db/migrations/001.sql",
        ]
        # Act
        should_run = CONFIG.path_filter(backend_only)
        # Assert — path_filter returnt False → Stage soll sich skippen
        assert should_run is False

    def test_path_filter_runs_for_full_stack_pr(self) -> None:
        """Wenn ein PR sowohl Backend als auch UI-Dateien enthält, soll Design laufen."""
        # Arrange
        full_stack = [
            "apps/portal-api/src/routes/finance.ts",
            "apps/portal-shell/src/views/Finance.tsx",
        ]
        # Act
        should_run = CONFIG.path_filter(full_stack)
        # Assert
        assert should_run is True
