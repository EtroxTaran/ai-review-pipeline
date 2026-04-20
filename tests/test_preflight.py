"""Unit-Tests für ai_review_pipeline.preflight.

TDD — Tests zuerst geschrieben (Red), dann preflight.py portiert (Green).
Keine Source-Tests existierten im Original (ai-portal/scripts/ai-review/preflight.py),
daher werden alle Tests hier neu definiert.

Strategie:
 - Alle subprocess-Calls laufen durch FakeRunner (kein echtes pnpm/git im Test).
 - `_safe_out` und `run_preflight` sind die öffentlichen Symbole.
 - Happy-Path (pass/fail/timeout/skip) + 2 Edge-Cases pro Pfad.
 - Lockfile-Drift-Shortcircuit als eigenständige Test-Gruppe.
 - os.environ wird per monkeypatch isoliert.

Laufen mit:
    pytest tests/test_preflight.py -v
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

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

    Unterstützt `.on()` für prefix-matching und `_default` als Fallback.
    Kann auch `TimeoutExpired` oder `FileNotFoundError` werfen, wenn
    `.raises_on()` konfiguriert ist.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._responses: list[tuple[list[str], Any]] = []
        self._default = FakeCompletedProcess(returncode=0, stdout="", stderr="")

    def on(
        self,
        cmd_prefix: list[str],
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        """Match by command-prefix (first N tokens)."""
        self._responses.append((
            cmd_prefix,
            FakeCompletedProcess(returncode=returncode, stdout=stdout, stderr=stderr),
        ))

    def raises_on(self, cmd_prefix: list[str], exc: Exception) -> None:
        """Match by cmd_prefix — raise exc instead of returning."""
        self._responses.append((cmd_prefix, exc))

    def __call__(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        stdin_data: str | None = None,
    ) -> Any:
        self.calls.append(list(cmd))
        for prefix, response in self._responses:
            if cmd[: len(prefix)] == prefix:
                self._responses.remove((prefix, response))
                if isinstance(response, Exception):
                    raise response
                return response
        return self._default


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_WORKTREE = Path("/fake/worktree")


def _make_timeout_exc(cmd: list[str], timeout: int = 30) -> subprocess.TimeoutExpired:
    """Erzeugt eine parametrisierte TimeoutExpired-Exception."""
    return subprocess.TimeoutExpired(cmd, timeout)


# ---------------------------------------------------------------------------
# Tests für _safe_out
# ---------------------------------------------------------------------------


class TestSafeOut:
    """_safe_out kombiniert stdout + stderr und entfernt ANSI-Sequenzen."""

    def test_happy_path_stdout_only(self) -> None:
        # Arrange
        from ai_review_pipeline.preflight import _safe_out

        proc = FakeCompletedProcess(returncode=0, stdout="hello", stderr="")

        # Act
        result = _safe_out(proc)

        # Assert
        assert result == "hello"

    def test_happy_path_stderr_only(self) -> None:
        # Arrange
        from ai_review_pipeline.preflight import _safe_out

        proc = FakeCompletedProcess(returncode=0, stdout="", stderr="world")

        # Act
        result = _safe_out(proc)

        # Assert
        assert result == "world"

    def test_combines_stdout_and_stderr(self) -> None:
        # Arrange
        from ai_review_pipeline.preflight import _safe_out

        proc = FakeCompletedProcess(returncode=0, stdout="out", stderr="err")

        # Act
        result = _safe_out(proc)

        # Assert
        assert "out" in result
        assert "err" in result

    def test_strips_ansi_codes(self) -> None:
        # Arrange
        from ai_review_pipeline.preflight import _safe_out

        proc = FakeCompletedProcess(
            returncode=0, stdout="\x1b[31mred text\x1b[0m", stderr=""
        )

        # Act
        result = _safe_out(proc)

        # Assert
        assert result == "red text"
        assert "\x1b" not in result

    def test_none_stdout_and_stderr_returns_empty(self) -> None:
        # Arrange
        from ai_review_pipeline.preflight import _safe_out

        # Simuliert Objekt ohne stdout/stderr (oder None-Werte)
        class MinimalProc:
            pass

        proc = MinimalProc()

        # Act
        result = _safe_out(proc)

        # Assert
        assert result == ""

    def test_handles_none_stdout(self) -> None:
        # Arrange
        from ai_review_pipeline.preflight import _safe_out

        class ProcWithNoneStdout:
            stdout: None = None
            stderr = "only-stderr"

        proc = ProcWithNoneStdout()

        # Act
        result = _safe_out(proc)

        # Assert
        assert result == "only-stderr"


# ---------------------------------------------------------------------------
# Tests für run_preflight — Typecheck-Pfad
# ---------------------------------------------------------------------------


class TestRunPreflightTypecheck:
    """Typecheck-Ergebnis wird korrekt in den Markdown-Block eingebettet."""

    def test_typecheck_pass_emits_check_mark(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        # git diff (lockfile-check) → kein pnpm-lock.yaml
        runner.on(["git", "diff", "--name-only"], stdout="src/foo.ts\n")
        # typecheck → PASS
        runner.on(["pnpm", "-w", "typecheck"], returncode=0, stdout="")
        # git fetch (ignoriert)
        # tests → PASS
        runner.on(["pnpm", "-w", "test"], returncode=0, stdout="")

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert
        assert "Typecheck: ✅ PASS" in result
        assert "## Pre-Flight CI Context" in result

    def test_typecheck_fail_emits_fail_with_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        runner.on(["git", "diff", "--name-only"], stdout="src/foo.ts\n")
        runner.on(
            ["pnpm", "-w", "typecheck"],
            returncode=1,
            stdout="error TS2322: Type 'string' is not assignable to type 'number'",
        )
        runner.on(["pnpm", "-w", "test"], returncode=0, stdout="")

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert
        assert "Typecheck: ❌ FAIL" in result
        assert "TS2322" in result

    def test_typecheck_timeout_emits_timeout_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        runner.on(["git", "diff", "--name-only"], stdout="src/foo.ts\n")
        runner.raises_on(
            ["pnpm", "-w", "typecheck"],
            _make_timeout_exc(["pnpm", "-w", "typecheck"]),
        )
        runner.on(["pnpm", "-w", "test"], returncode=0, stdout="")

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert
        assert "Typecheck: ⏱️ TIMEOUT" in result

    def test_typecheck_pnpm_not_found_emits_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        runner.on(["git", "diff", "--name-only"], stdout="src/foo.ts\n")
        runner.raises_on(["pnpm", "-w", "typecheck"], FileNotFoundError("pnpm not found"))
        runner.raises_on(["pnpm", "-w", "test"], FileNotFoundError("pnpm not found"))

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert
        assert "Typecheck: ⏭️ SKIP (pnpm nicht im PATH)" in result


# ---------------------------------------------------------------------------
# Tests für run_preflight — Tests-Pfad (vitest --changed)
# ---------------------------------------------------------------------------


class TestRunPreflightTests:
    """vitest-changed-Ergebnis wird korrekt in den Markdown-Block eingebettet."""

    def test_tests_pass_emits_check_mark(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        runner.on(["git", "diff", "--name-only"], stdout="src/foo.ts\n")
        runner.on(["pnpm", "-w", "typecheck"], returncode=0, stdout="")
        # git fetch (ignoriert)
        runner.on(["pnpm", "-w", "test"], returncode=0, stdout="")

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert
        assert "Tests (changed): ✅ PASS" in result

    def test_tests_fail_emits_fail_with_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        runner.on(["git", "diff", "--name-only"], stdout="src/foo.ts\n")
        runner.on(["pnpm", "-w", "typecheck"], returncode=0, stdout="")
        runner.on(
            ["pnpm", "-w", "test"],
            returncode=1,
            stdout="FAIL src/components/Foo.test.ts > should render",
        )

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert
        assert "Tests (changed): ❌ FAIL" in result
        assert "Foo.test.ts" in result

    def test_tests_timeout_emits_timeout_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        runner.on(["git", "diff", "--name-only"], stdout="src/foo.ts\n")
        runner.on(["pnpm", "-w", "typecheck"], returncode=0, stdout="")
        runner.raises_on(
            ["pnpm", "-w", "test"],
            _make_timeout_exc(["pnpm", "-w", "test"]),
        )

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert
        assert "Tests (changed): ⏱️ TIMEOUT" in result

    def test_tests_pnpm_not_found_emits_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        runner.on(["git", "diff", "--name-only"], stdout="src/foo.ts\n")
        runner.on(["pnpm", "-w", "typecheck"], returncode=0, stdout="")
        runner.raises_on(["pnpm", "-w", "test"], FileNotFoundError("pnpm not found"))

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert
        assert "Tests (changed): ⏭️ SKIP (pnpm nicht im PATH)" in result


# ---------------------------------------------------------------------------
# Tests für run_preflight — Lockfile-Drift-Shortcircuit
# ---------------------------------------------------------------------------


class TestRunPreflightLockfileDrift:
    """Wenn pnpm-lock.yaml im Diff ist, wird Preflight komplett geskippt."""

    def test_lockfile_change_emits_skip_and_returns_early(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        # git diff enthält pnpm-lock.yaml → Shortcircuit
        runner.on(
            ["git", "diff", "--name-only"],
            stdout="package.json\npnpm-lock.yaml\nsrc/app.ts\n",
        )

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert — kein pnpm-Aufruf, nur der Skip-Block
        assert "⚠️ SKIP" in result
        assert "pnpm-lock.yaml" in result
        assert "Typecheck" not in result
        assert "Tests" not in result
        # Nur git diff wurde aufgerufen, kein pnpm
        pnpm_calls = [c for c in runner.calls if c[0] == "pnpm"]
        assert pnpm_calls == []

    def test_lockfile_not_in_diff_proceeds_normally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        runner.on(["git", "diff", "--name-only"], stdout="src/button.tsx\n")
        runner.on(["pnpm", "-w", "typecheck"], returncode=0)
        runner.on(["pnpm", "-w", "test"], returncode=0)

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert — SKIP-Block darf NICHT erscheinen
        assert "⚠️ SKIP" not in result
        assert "Typecheck" in result

    def test_git_diff_exception_treated_as_no_lockfile_change(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange — git diff schlägt fehl (z.B. kein git-Repo)
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        runner.raises_on(["git", "diff", "--name-only"], RuntimeError("not a git repo"))
        runner.on(["pnpm", "-w", "typecheck"], returncode=0)
        runner.on(["pnpm", "-w", "test"], returncode=0)

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act — darf nicht crashen
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert — kein Lockfile-SKIP, normaler Ablauf
        assert "⚠️ SKIP" not in result
        assert "## Pre-Flight CI Context" in result


# ---------------------------------------------------------------------------
# Tests für run_preflight — Output-Format
# ---------------------------------------------------------------------------


class TestRunPreflightOutputFormat:
    """Das Ergebnis ist immer ein Markdown-Block der richtigen Form."""

    def test_result_starts_with_preflight_heading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        runner.on(["git", "diff", "--name-only"], stdout="")
        runner.on(["pnpm", "-w", "typecheck"], returncode=0)
        runner.on(["pnpm", "-w", "test"], returncode=0)

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert
        assert result.startswith("## Pre-Flight CI Context")

    def test_result_contains_both_sections_on_full_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        runner.on(["git", "diff", "--name-only"], stdout="src/x.ts\n")
        runner.on(["pnpm", "-w", "typecheck"], returncode=0)
        runner.on(["pnpm", "-w", "test"], returncode=0)

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert
        assert "Typecheck: ✅ PASS" in result
        assert "Tests (changed): ✅ PASS" in result

    def test_base_branch_is_used_in_git_diff_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange — prüft, dass base_branch korrekt weitergereicht wird
        from ai_review_pipeline import preflight

        runner = FakeRunner()
        # Keine git-diff-Antwort konfiguriert → default FakeRunner gibt "" zurück
        runner.on(["pnpm", "-w", "typecheck"], returncode=0)
        runner.on(["pnpm", "-w", "test"], returncode=0)

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        preflight.run_preflight(FAKE_WORKTREE, "develop", runner=runner)

        # Assert — git diff-Aufruf enthält "origin/develop"
        git_diff_calls = [c for c in runner.calls if "diff" in c]
        assert git_diff_calls, "Kein git diff-Aufruf gefunden"
        first_diff = git_diff_calls[0]
        assert any("origin/develop" in token for token in first_diff)

    def test_large_typecheck_output_is_tailed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange — Output größer als MAX_PREFLIGHT_OUTPUT_CHARS
        from ai_review_pipeline import preflight
        from ai_review_pipeline.common import MAX_PREFLIGHT_OUTPUT_CHARS

        long_output = "error line\n" * (MAX_PREFLIGHT_OUTPUT_CHARS // 5)
        runner = FakeRunner()
        runner.on(["git", "diff", "--name-only"], stdout="src/foo.ts\n")
        runner.on(["pnpm", "-w", "typecheck"], returncode=1, stdout=long_output)
        runner.on(["pnpm", "-w", "test"], returncode=0)

        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        result = preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)

        # Assert — tail() wurde angewendet → Output ist begrenzt
        assert "Typecheck: ❌ FAIL" in result
        assert "Anfang gekürzt" in result


# ---------------------------------------------------------------------------
# Tests für run_preflight — env-Isolation
# ---------------------------------------------------------------------------


class TestRunPreflightEnv:
    """run_preflight setzt NO_COLOR=1 und CI=true für pnpm-Aufrufe."""

    def test_pnpm_env_contains_no_color_and_ci(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from ai_review_pipeline import preflight

        captured_envs: list[dict[str, str] | None] = []

        class EnvCapturingRunner:
            calls: list[list[str]] = []

            def __call__(
                self,
                cmd: list[str],
                *,
                cwd: Path | None = None,
                timeout: int | None = None,
                env: dict[str, str] | None = None,
                stdin_data: str | None = None,
            ) -> FakeCompletedProcess:
                self.calls.append(list(cmd))
                if cmd[:2] == ["pnpm", "-w"]:
                    captured_envs.append(env)
                return FakeCompletedProcess(returncode=0)

        runner = EnvCapturingRunner()
        monkeypatch.setattr(preflight, "REPO_ROOT", FAKE_WORKTREE)

        # Act
        preflight.run_preflight(FAKE_WORKTREE, "main", runner=runner)  # type: ignore[arg-type]

        # Assert — mindestens ein pnpm-Aufruf mit NO_COLOR + CI
        assert captured_envs, "Keine pnpm-Aufrufe mit env gefunden"
        for env in captured_envs:
            assert env is not None
            assert env.get("NO_COLOR") == "1"
            assert env.get("CI") == "true"
