"""TDD-Tests für ai_review_pipeline.stages.cursor_review.

Stage 1b: Cursor-Agent als zweiter Code-Reviewer (composer-2, Wave 5a).
Läuft parallel zu Stage 1 (Codex GPT-5) für Vendor-Diversität.
Fix-Loop ist explizit deaktiviert (skip_fix_loop=True) — nur Read-Only-Review.

Test-Philosophie:
 - AAA (Arrange-Act-Assert) mit sichtbaren Trennungen.
 - pytest-mock (mocker) für Patches, wo nötig; DI via runner= oder gh= bevorzugt.
 - Keine echten CLI-Calls, keine Netzwerkzugriffe.
 - Error-Path-Parität: jede Happy-Path-Variante hat mindestens einen Error-Path-Gegenpart.

Annahmen zum stage.py-Interface (Wave 4b noch nicht auf main):
 - `stage.StageConfig` — dataclass mit den in stage.py definierten Feldern.
 - `stage.build_arg_parser(name)` — gibt argparse.ArgumentParser zurück.
 - `stage.run_stage(cfg, *, pr_number, skip_preflight, skip_fix_loop, max_iterations)` → int.
 - Diese Typen sind als Stubs in cursor_review.py bis Wave 4b geimportiert.

Cursor-CLI-Annahmen:
 - Binary-Name: `cursor-agent`
 - Flags: `--print --force --output-format json --model <model> <prompt>`
 - Model-Default: `composer-2`
 - Auth: OAuth-Pfad (kein API-Key) — kein Env-Var im common.run_cursor benötigt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ai_review_pipeline import common

# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

def _make_fake_runner(stdout: str = "", returncode: int = 0) -> Any:
    """Liefert einen minimalen FakeRunner-Callable (subprocess-free)."""
    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def _runner(cmd: list[str], **_kw: Any) -> _FakeProc:
        return _FakeProc()

    return _runner


# ---------------------------------------------------------------------------
# CONFIG integrity
# ---------------------------------------------------------------------------

class TestCursorReviewConfig:
    """Smoke-Tests: CONFIG-Objekt hat die erwartete Struktur."""

    def test_config_name_is_code_cursor(self) -> None:
        # Arrange
        from ai_review_pipeline.stages.cursor_review import CONFIG

        # Act / Assert
        assert CONFIG.name == "code-cursor"

    def test_config_status_context_is_code_cursor(self) -> None:
        # Arrange
        from ai_review_pipeline.stages.cursor_review import CONFIG

        # Act / Assert
        assert CONFIG.status_context == common.STATUS_CODE_CURSOR

    def test_config_sticky_marker_matches_common_constant(self) -> None:
        # Arrange
        from ai_review_pipeline.stages.cursor_review import CONFIG

        # Act / Assert
        assert CONFIG.sticky_marker == common.MARKER_CODE_CURSOR_REVIEW

    def test_config_treat_no_findings_as_clean_is_false(self) -> None:
        # Arrange — Cursor prompt ist scoring-strict; Abwesenheit von Findings
        # ist NICHT automatisch clean, wir erwarten explizit einen JSON-Block.
        from ai_review_pipeline.stages.cursor_review import CONFIG

        # Act / Assert
        assert CONFIG.treat_no_findings_as_clean is False

    def test_config_ok_sentinels_contains_lgtm(self) -> None:
        # Arrange
        from ai_review_pipeline.stages.cursor_review import CONFIG

        # Act / Assert
        assert "LGTM" in CONFIG.ok_sentinels

    def test_config_path_filter_is_none(self) -> None:
        # Arrange — Cursor läuft auf jedem PR (zweiter Code-Reviewer, kein Filter)
        from ai_review_pipeline.stages.cursor_review import CONFIG

        # Act / Assert
        assert CONFIG.path_filter is None


# ---------------------------------------------------------------------------
# _cursor_reviewer — Unit-Tests der internen Reviewer-Funktion
# ---------------------------------------------------------------------------

class TestCursorReviewerFn:
    """Tests für die _cursor_reviewer-Wrapper-Funktion."""

    def test_calls_common_run_cursor_with_cli_default(self) -> None:
        # Policy: Cursor-Reviewer nutzt CLI-Default → model=None, kein
        # --model-Flag wird an cursor-agent weitergereicht. Registry pinnt
        # stattdessen die CLI-Binary-Version.
        from ai_review_pipeline.stages import cursor_review

        worktree = Path("/tmp/fake-worktree")
        captured: dict[str, Any] = {}

        def fake_run_cursor(
            *,
            prompt: str,
            worktree: Path,
            base_branch: str,
            model: str | None = None,
            **_kw: Any,
        ) -> str:
            captured["model"] = model
            captured["prompt"] = prompt
            captured["base_branch"] = base_branch
            return "LGTM"

        with patch.object(common, "run_cursor", side_effect=fake_run_cursor):
            # Act
            result = cursor_review._cursor_reviewer(
                "review this", worktree, "main"
            )

        # Assert
        assert result == "LGTM"
        assert captured["model"] is None, "Cursor-Reviewer darf kein --model-Flag setzen (CLI-Default-Policy)"
        assert captured["base_branch"] == "main"

    def test_env_override_can_force_explicit_model(self) -> None:
        # Wenn Dev ein Modell erzwingen will (z.B. Composer-3-Testing),
        # funktioniert AI_REVIEW_MODEL_CODE_CURSOR=... — der Reviewer liefert
        # dann expliziten Model-String an run_cursor, der das --model-Flag setzt.
        import os
        from ai_review_pipeline.stages import cursor_review

        captured: dict[str, Any] = {}

        def fake_run_cursor(*, model: str | None = None, **_kw: Any) -> str:
            captured["model"] = model
            return "LGTM"

        with patch.dict(os.environ, {"AI_REVIEW_MODEL_CODE_CURSOR": "composer-test"}):
            with patch.object(common, "run_cursor", side_effect=fake_run_cursor):
                cursor_review._cursor_reviewer("x", Path("/tmp"), "main")

        assert captured["model"] == "composer-test"

    def test_cursor_reviewer_forwards_prompt(self) -> None:
        # Arrange
        from ai_review_pipeline.stages import cursor_review

        received_prompt: list[str] = []

        def fake_run_cursor(*, prompt: str, **_kw: Any) -> str:
            received_prompt.append(prompt)
            return "some output"

        with patch.object(common, "run_cursor", side_effect=fake_run_cursor):
            # Act
            cursor_review._cursor_reviewer(
                "my-custom-prompt", Path("/tmp/wt"), "dev"
            )

        # Assert
        assert received_prompt == ["my-custom-prompt"]

    def test_cursor_reviewer_returns_run_cursor_output(self) -> None:
        # Arrange
        from ai_review_pipeline.stages import cursor_review

        expected = "Reviewer output with findings `src/foo.py`:42"

        with patch.object(common, "run_cursor", return_value=expected):
            # Act
            result = cursor_review._cursor_reviewer("prompt", Path("/tmp/wt"), "main")

        # Assert
        assert result == expected

    def test_cursor_reviewer_propagates_timeout_error(self) -> None:
        # Arrange — run_cursor fängt TimeoutExpired intern ab und gibt einen
        # Timeout-String zurück (kein raise). Wir testen dass der String
        # das Timeout-Token enthält.
        from ai_review_pipeline.stages import cursor_review

        timeout_msg = "_(Cursor Timeout nach 300s — lokal erneut laufen lassen)_"

        with patch.object(common, "run_cursor", return_value=timeout_msg):
            # Act
            result = cursor_review._cursor_reviewer("p", Path("/tmp/wt"), "main")

        # Assert
        assert "Timeout" in result


# ---------------------------------------------------------------------------
# main() — CLI-Einstiegspunkt
# ---------------------------------------------------------------------------

class TestMain:
    """Tests für die main()-Funktion."""

    def test_main_requires_pr_argument(self) -> None:
        # Arrange
        from ai_review_pipeline.stages.cursor_review import main

        # Act
        with pytest.raises(SystemExit) as exc_info:
            main([])  # keine --pr angegeben

        # Assert
        assert exc_info.value.code != 0

    def test_main_calls_run_stage_with_skip_fix_loop_true(self) -> None:
        # Arrange — Cursor ist review-only; skip_fix_loop muss True sein
        # um Fix-Commit-Races mit Stage 1 (Codex) zu vermeiden.
        from ai_review_pipeline.stages import cursor_review

        captured: dict[str, Any] = {}

        def fake_run_stage(cfg: Any, *, pr_number: int, skip_preflight: bool,
                           skip_fix_loop: bool, max_iterations: int) -> int:
            captured["skip_fix_loop"] = skip_fix_loop
            captured["pr_number"] = pr_number
            return 0

        # stage ist noch Stub/TODO (Wave 4b) — wir patchen run_stage direkt
        with patch.object(cursor_review.stage, "run_stage", side_effect=fake_run_stage):
            # Act
            result = cursor_review.main(["--pr", "99"])

        # Assert
        assert captured["skip_fix_loop"] is True
        assert captured["pr_number"] == 99
        assert result == 0

    def test_main_passes_pr_number_to_run_stage(self) -> None:
        # Arrange
        from ai_review_pipeline.stages import cursor_review

        received: list[int] = []

        def fake_run_stage(cfg: Any, *, pr_number: int, **_kw: Any) -> int:
            received.append(pr_number)
            return 0

        with patch.object(cursor_review.stage, "run_stage", side_effect=fake_run_stage):
            # Act
            cursor_review.main(["--pr", "123"])

        # Assert
        assert received == [123]

    def test_main_propagates_run_stage_exit_code(self) -> None:
        # Arrange — Failure-Pfad: run_stage gibt 1 zurück (Stage rot)
        from ai_review_pipeline.stages import cursor_review

        with patch.object(cursor_review.stage, "run_stage", return_value=1):
            # Act
            result = cursor_review.main(["--pr", "7"])

        # Assert
        assert result == 1

    def test_main_skip_preflight_flag(self) -> None:
        # Arrange
        from ai_review_pipeline.stages import cursor_review

        captured: dict[str, Any] = {}

        def fake_run_stage(cfg: Any, *, skip_preflight: bool, **_kw: Any) -> int:
            captured["skip_preflight"] = skip_preflight
            return 0

        with patch.object(cursor_review.stage, "run_stage", side_effect=fake_run_stage):
            # Act
            cursor_review.main(["--pr", "1", "--skip-preflight"])

        # Assert
        assert captured["skip_preflight"] is True
