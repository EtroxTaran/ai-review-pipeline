"""TDD-Tests für stages/code_review.py (Phase 3.3 Wave 4a Stage-Runner).

Portiert + neu geschrieben aus ai-portal/scripts/ai-review (kein Original-Test vorhanden).
AAA-Pattern durchgängig. Mindestens 6 Tests.

Annahmen (Wave-4b-Reconcile):
  - `StageConfig` ist ein dataclass mit den Feldern:
      name: str, status_context: str, sticky_marker: str, title_prefix: str,
      prompt_file: str, reviewer_label: str, ok_sentinels: tuple[str,...],
      reviewer_fn: Callable, path_filter: Callable|None, treat_no_findings_as_clean: bool
  - `build_arg_parser(stage_name: str) -> argparse.ArgumentParser`
  - `run_stage(cfg: StageConfig, *, pr_number: int, skip_preflight: bool, skip_fix_loop: bool,
               max_iterations: int) -> int` (0=green, 1=red, 2=error)
  Diese Funktionen sind in `ai_review_pipeline.stages.stage` (Wave-4b) zu implementieren.
  Im Testkontext werden sie komplett gemockt — kein echter stage.py-Import notwendig.

Stubs/TODOs für Wave-4b-Reconcile (explizit aufgelistet):
  - TODO(wave-4b): `ai_review_pipeline.stages.stage` Modul existiert noch nicht.
    code_review.py importiert es unter TYPE_CHECKING + zur Laufzeit via lazy import.
    Sobald stage.py portiert ist, muss der Import-Guard entfernt werden.
  - TODO(wave-4b): `StageConfig`-Felder müssen exakt mit dem obigen Schema matchen.
  - TODO(wave-4b): `run_stage`-Signatur muss die `gh: GhClient | None`-Option unterstützen
    (optional, backward-compat).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal Stage Protocol-Stubs (Wave-4b-Reconcile placeholder)
# ---------------------------------------------------------------------------

@dataclass
class _StageConfigStub:
    """Minimales StageConfig-Double für Tests — matcht das erwartete Interface."""

    name: str
    status_context: str
    sticky_marker: str
    title_prefix: str
    prompt_file: str
    reviewer_label: str
    ok_sentinels: tuple[str, ...]
    reviewer_fn: Callable[..., str]
    path_filter: Callable[[list[str]], bool] | None = None
    treat_no_findings_as_clean: bool = False


def _make_fake_stage_module(
    *,
    run_stage_return: int = 0,
    config_stub: _StageConfigStub | None = None,
) -> Any:
    """Erstellt ein Fake-stage-Modul das `stage.StageConfig`, `stage.build_arg_parser`
    und `stage.run_stage` bereitstellt — ohne echten stage.py-Import."""
    stage_mod = MagicMock()
    stage_mod.StageConfig = _StageConfigStub
    stage_mod.build_arg_parser.return_value = _make_parser()
    stage_mod.run_stage.return_value = run_stage_return
    if config_stub is not None:
        # Damit CONFIG-Instanz beim Import bereits gesetzt ist
        stage_mod.StageConfig.side_effect = lambda **kw: config_stub
    return stage_mod


def _make_parser() -> argparse.ArgumentParser:
    """Parser-Stub der dieselbe CLI-API wie stage.build_arg_parser('code') bietet."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--skip-preflight", action="store_true")
    ap.add_argument("--skip-fix-loop", action="store_true")
    ap.add_argument("--max-iterations", type=int, default=2)
    return ap


# ---------------------------------------------------------------------------
# Import target module (mit Stage-Stub injiziert)
# ---------------------------------------------------------------------------

def _import_code_review(stage_mod: Any) -> Any:
    """Importiert code_review frisch unter gemocktem stage-Modul.

    Nutzt sys.modules-Patching damit der relative Import `from . import stage`
    den Stub bekommt. Module wird nach dem Test-Import aus sys.modules entfernt
    (Isolation). Approach ist analog zu den test_common.py FakeRunner-Tests.
    """
    modules_to_patch = {
        "ai_review_pipeline.stages.stage": stage_mod,
        # common wird echt importiert — wir wollen run_codex testen
    }
    with patch.dict("sys.modules", modules_to_patch):
        # Force-reimport (sauber für multi-Test isolation)
        for key in list(sys.modules):
            if "code_review" in key:
                del sys.modules[key]
        import ai_review_pipeline.stages.code_review as cr
        return cr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def stage_mod() -> Any:
    return _make_fake_stage_module()


@pytest.fixture()
def code_review(stage_mod: Any) -> Any:
    return _import_code_review(stage_mod)


# ---------------------------------------------------------------------------
# Test-Gruppe 1: Modul-Struktur & CONFIG
# ---------------------------------------------------------------------------

class TestModuleStructure:
    """Prüft dass die module-level CONFIG korrekt gebaut wird."""

    def test_config_name_is_code(self, code_review: Any) -> None:
        # Arrange — CONFIG wird beim Import gebaut
        # Act — lesen
        cfg = code_review.CONFIG
        # Assert
        assert cfg.name == "code"

    def test_config_uses_status_code_constant(self, code_review: Any) -> None:
        # Arrange
        from ai_review_pipeline import common
        # Act
        cfg = code_review.CONFIG
        # Assert — status_context muss dem common.STATUS_CODE entsprechen
        assert cfg.status_context == common.STATUS_CODE

    def test_config_sticky_marker_is_code_review_marker(self, code_review: Any) -> None:
        # Arrange
        from ai_review_pipeline import common
        # Act
        cfg = code_review.CONFIG
        # Assert
        assert cfg.sticky_marker == common.MARKER_CODE_REVIEW

    def test_config_treat_no_findings_as_clean_is_true(self, code_review: Any) -> None:
        """Codex-CLI kann --base + Prompt nicht kombinieren → kein LGTM-Sentinel garantiert."""
        # Arrange / Act
        cfg = code_review.CONFIG
        # Assert
        assert cfg.treat_no_findings_as_clean is True

    def test_config_path_filter_is_none(self, code_review: Any) -> None:
        """Code-Review läuft immer — kein Path-Filter (im Gegensatz zu Design-Review)."""
        # Arrange / Act
        cfg = code_review.CONFIG
        # Assert
        assert cfg.path_filter is None

    def test_config_prompt_file_is_code_review_md(self, code_review: Any) -> None:
        # Arrange / Act
        cfg = code_review.CONFIG
        # Assert
        assert cfg.prompt_file == "code_review.md"

    def test_config_reviewer_fn_calls_run_codex(self, code_review: Any) -> None:
        """reviewer_fn muss common.run_codex delegieren."""
        # Arrange
        dummy_worktree = Path("/tmp/wt")
        called_with: dict[str, Any] = {}

        def fake_run_codex(**kwargs: Any) -> str:
            called_with.update(kwargs)
            return "LGTM"

        with patch.object(code_review.common, "run_codex", fake_run_codex):
            # Act
            result = code_review.CONFIG.reviewer_fn(
                prompt="review this",
                worktree=dummy_worktree,
                base_branch="main",
                pr_title="feat: add thing",
            )
        # Assert
        assert result == "LGTM"
        assert called_with["prompt"] == "review this"
        assert called_with["worktree"] == dummy_worktree
        assert called_with["base_branch"] == "main"
        assert called_with["pr_title"] == "feat: add thing"


# ---------------------------------------------------------------------------
# Test-Gruppe 2: main() — CLI-Integration
# ---------------------------------------------------------------------------

class TestMain:
    """Prüft dass main() den stage.run_stage mit den richtigen Argumenten aufruft."""

    def test_main_returns_zero_on_success(self, code_review: Any, stage_mod: Any) -> None:
        # Arrange
        stage_mod.run_stage.return_value = 0
        # Act
        result = code_review.main(["--pr", "42"])
        # Assert
        assert result == 0

    def test_main_returns_one_on_failure(self, code_review: Any, stage_mod: Any) -> None:
        # Arrange
        stage_mod.run_stage.return_value = 1
        # Act
        result = code_review.main(["--pr", "99"])
        # Assert
        assert result == 1

    def test_main_passes_pr_number_to_run_stage(self, code_review: Any, stage_mod: Any) -> None:
        # Arrange
        stage_mod.run_stage.return_value = 0
        # Act
        code_review.main(["--pr", "77"])
        # Assert
        call_kwargs = stage_mod.run_stage.call_args
        assert call_kwargs.kwargs.get("pr_number") == 77 or (
            call_kwargs.args and call_kwargs.args[1] == 77
        )

    def test_main_skip_preflight_flag_is_forwarded(self, code_review: Any, stage_mod: Any) -> None:
        # Arrange
        stage_mod.run_stage.return_value = 0
        # Act
        code_review.main(["--pr", "10", "--skip-preflight"])
        # Assert
        call_kwargs = stage_mod.run_stage.call_args
        skip_pf = call_kwargs.kwargs.get("skip_preflight")
        assert skip_pf is True

    def test_main_skip_fix_loop_flag_is_forwarded(self, code_review: Any, stage_mod: Any) -> None:
        # Arrange
        stage_mod.run_stage.return_value = 0
        # Act
        code_review.main(["--pr", "10", "--skip-fix-loop"])
        # Assert
        call_kwargs = stage_mod.run_stage.call_args
        skip_fl = call_kwargs.kwargs.get("skip_fix_loop")
        assert skip_fl is True

    def test_main_default_max_iterations_is_two(self, code_review: Any, stage_mod: Any) -> None:
        # Arrange
        stage_mod.run_stage.return_value = 0
        # Act
        code_review.main(["--pr", "5"])
        # Assert
        call_kwargs = stage_mod.run_stage.call_args
        max_it = call_kwargs.kwargs.get("max_iterations")
        assert max_it == 2

    def test_main_custom_max_iterations_is_forwarded(self, code_review: Any, stage_mod: Any) -> None:
        # Arrange
        stage_mod.run_stage.return_value = 0
        # Act
        code_review.main(["--pr", "5", "--max-iterations", "4"])
        # Assert
        call_kwargs = stage_mod.run_stage.call_args
        max_it = call_kwargs.kwargs.get("max_iterations")
        assert max_it == 4

    def test_main_passes_config_as_first_positional(self, code_review: Any, stage_mod: Any) -> None:
        # Arrange
        stage_mod.run_stage.return_value = 0
        # Act
        code_review.main(["--pr", "1"])
        # Assert — erster positional arg muss das CONFIG-Objekt sein
        call_args = stage_mod.run_stage.call_args
        first_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("cfg")
        assert first_arg is code_review.CONFIG

    def test_main_returns_two_on_stage_error(self, code_review: Any, stage_mod: Any) -> None:
        # Arrange
        stage_mod.run_stage.return_value = 2
        # Act
        result = code_review.main(["--pr", "3"])
        # Assert
        assert result == 2


# ---------------------------------------------------------------------------
# Test-Gruppe 3: _codex_reviewer — interner Reviewer-Delegate
# ---------------------------------------------------------------------------

class TestCodexReviewer:
    """Prüft _codex_reviewer als dünner Wrapper um common.run_codex."""

    def test_codex_reviewer_delegates_all_params(self, code_review: Any) -> None:
        # Arrange
        fake_worktree = Path("/tmp/test-wt")
        received: dict[str, Any] = {}

        def _fake_codex(**kwargs: Any) -> str:
            received.update(kwargs)
            return "Codex output"

        with patch.object(code_review.common, "run_codex", _fake_codex):
            # Act
            result = code_review._codex_reviewer(
                prompt="check style",
                worktree=fake_worktree,
                base_branch="develop",
                pr_title="fix: correct logic",
            )

        # Assert
        assert result == "Codex output"
        assert received["prompt"] == "check style"
        assert received["worktree"] == fake_worktree
        assert received["base_branch"] == "develop"
        assert received["pr_title"] == "fix: correct logic"

    def test_codex_reviewer_returns_run_codex_output_verbatim(self, code_review: Any) -> None:
        # Arrange
        expected = "[P1] src/foo.ts:42 — magic number"
        with patch.object(code_review.common, "run_codex", return_value=expected):
            # Act
            result = code_review._codex_reviewer(
                prompt="",
                worktree=Path("/tmp"),
                base_branch="main",
                pr_title="chore: bump deps",
            )
        # Assert
        assert result == expected
