"""TDD-Tests für Stage 3 Security Review (Wave 4a Extract).

Abdeckung:
1. Happy-Path: Gemini liefert SEC-OK Sentinel → success
2. Semgrep-Befund: sauberer Scan → no-findings-Block
3. Semgrep-Befunde-Aggregation: mehrere Findings → cap auf 25, formatiert
4. Semgrep non-zero exit: parsbares JSON mit results → Findings-Block
5. Gemini-CLI-Fehler (Timeout) → Timeout-String landet im Prompt, Stage läuft durch
6. Kombinierte Findings: semgrep + Gemini-Review-Text werden zusammengebaut
7. nosemgrep-Marker-Handling: nosemgrep-Annotation im Output hängt extra-Note an
8. Config-Path-Resolution: STATUS_SECURITY + MARKER_SECURITY_REVIEW korrekt verdrahtet
9. Semgrep FileNotFoundError → SKIP-Message
10. Semgrep Timeout → TIMEOUT-Message

Architektur-Annahmen:
- stage.py ist noch NICHT in ai-review-pipeline portiert (Wave 4b).
  security_review.py nutzt einen StageConfig-Stub + build_arg_parser-Stub.
- common.run_gemini nimmt (prompt, worktree, base_branch, runner=...) — DI über runner.
- Semgrep-CLI: `semgrep scan --config p/default ... --json` (OSS CLI ≥1.60).
- Gemini-CLI: `gemini -m gemini-2.5-pro -p <prompt>` (yargs-flag-order: -m vor -p).
- Alle subprocess-Calls gehen über den injected runner → Tests sind vollständig offline.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# Wir importieren erst nach dem Schreiben der Implementierung — während der
# RED-Phase schlägt der Import fehl, das ist beabsichtigt.
from ai_review_pipeline.stages.security_review import (
    CONFIG,
    _gemini_reviewer,
    _run_semgrep_baseline,
)
from ai_review_pipeline.common import (
    MARKER_SECURITY_REVIEW,
    STATUS_SECURITY,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    """Fake subprocess.CompletedProcess."""
    p = MagicMock()
    p.stdout = stdout
    p.stderr = stderr
    p.returncode = returncode
    return p


def _semgrep_json(results: list[dict]) -> str:
    """Semgrep JSON-Output-Format simulieren."""
    return json.dumps({"results": results, "errors": []})


def _finding(path: str = "src/app.ts", line: int = 42,
             check_id: str = "python.lang.security.audit.sql-injection",
             message: str = "SQL injection risk") -> dict:
    return {
        "path": path,
        "start": {"line": line},
        "check_id": check_id,
        "extra": {"message": message},
    }


WORKTREE = Path("/tmp/fake-worktree")
BASE_BRANCH = "main"


# ---------------------------------------------------------------------------
# Test 1 — Happy-Path: Gemini liefert SEC-OK Sentinel
# ---------------------------------------------------------------------------

class TestGeminiHappyPath:
    def test_sec_ok_sentinel_propagated(self) -> None:
        """Arrange: Semgrep sauber, Gemini liefert SEC-OK.
        Act: _gemini_reviewer aufrufen.
        Assert: Rückgabe enthält SEC-OK (Sentinel für die Stage).
        """
        # Arrange
        semgrep_clean = _semgrep_json([])
        gemini_output = "SEC-OK"

        def fake_runner(cmd: list[str], **kwargs: Any) -> MagicMock:
            if cmd[0] == "semgrep":
                return _proc(stdout=semgrep_clean)
            if cmd[0] == "gemini":
                return _proc(stdout=gemini_output)
            return _proc()

        # Act
        result = _gemini_reviewer(
            prompt="Review this diff",
            worktree=WORKTREE,
            base_branch=BASE_BRANCH,
            runner=fake_runner,
        )

        # Assert
        assert "SEC-OK" in result


# ---------------------------------------------------------------------------
# Test 2 — Semgrep sauberer Scan → no-findings Block
# ---------------------------------------------------------------------------

class TestSemgrepClean:
    def test_clean_scan_returns_no_findings_message(self) -> None:
        """Arrange: Semgrep exitiert 0, leeres results-Array.
        Act: _run_semgrep_baseline aufrufen.
        Assert: Rückgabe enthält 'no new findings'.
        """
        # Arrange
        def fake_runner(cmd: list[str], **kwargs: Any) -> MagicMock:
            return _proc(stdout=_semgrep_json([]))

        # Act
        result = _run_semgrep_baseline(WORKTREE, BASE_BRANCH, runner=fake_runner)

        # Assert
        assert "no new findings" in result.lower()


# ---------------------------------------------------------------------------
# Test 3 — Semgrep-Befunde-Aggregation
# ---------------------------------------------------------------------------

class TestSemgrepFindingsAggregation:
    def test_findings_formatted_with_path_and_check(self) -> None:
        """Arrange: 3 Semgrep-Findings in JSON.
        Act: _run_semgrep_baseline aufrufen.
        Assert: Alle 3 Pfade erscheinen im Output, Anzahl korrekt.
        """
        # Arrange
        findings = [
            _finding("src/auth.ts", 10, "auth.weak-crypto", "Weak crypto"),
            _finding("src/db.ts", 20, "sql.injection", "SQL injection"),
            _finding("src/api.ts", 30, "xss.reflected", "XSS reflected"),
        ]

        def fake_runner(cmd: list[str], **kwargs: Any) -> MagicMock:
            return _proc(stdout=_semgrep_json(findings))

        # Act
        result = _run_semgrep_baseline(WORKTREE, BASE_BRANCH, runner=fake_runner)

        # Assert — Header mit Anzahl + alle 3 Pfade
        assert "3" in result
        assert "src/auth.ts" in result
        assert "src/db.ts" in result
        assert "src/api.ts" in result

    def test_findings_capped_at_25(self) -> None:
        """Arrange: 30 Findings.
        Act: _run_semgrep_baseline aufrufen.
        Assert: Ausgabe zeigt max 25 Einträge + '… +5 more' Hinweis.
        """
        # Arrange
        findings = [_finding(f"src/file{i}.ts", i + 1) for i in range(30)]

        def fake_runner(cmd: list[str], **kwargs: Any) -> MagicMock:
            return _proc(stdout=_semgrep_json(findings))

        # Act
        result = _run_semgrep_baseline(WORKTREE, BASE_BRANCH, runner=fake_runner)

        # Assert
        assert "+5 more" in result


# ---------------------------------------------------------------------------
# Test 4 — Semgrep non-zero exit mit parsbaren Findings
# ---------------------------------------------------------------------------

class TestSemgrepNonZeroExit:
    def test_nonzero_with_findings_still_parsed(self) -> None:
        """Arrange: Semgrep exit 1 (Findings gefunden), aber valides JSON.
        Act: _run_semgrep_baseline aufrufen.
        Assert: Findings werden trotzdem geparst und zurückgegeben.
        """
        # Arrange
        findings = [_finding("src/secret.ts", 5, "hardcoded-secret", "Hardcoded secret")]

        def fake_runner(cmd: list[str], **kwargs: Any) -> MagicMock:
            return _proc(stdout=_semgrep_json(findings), returncode=1)

        # Act
        result = _run_semgrep_baseline(WORKTREE, BASE_BRANCH, runner=fake_runner)

        # Assert
        assert "src/secret.ts" in result
        assert "1" in result  # Anzahl


# ---------------------------------------------------------------------------
# Test 5 — Gemini-CLI-Fehler (Timeout)
# ---------------------------------------------------------------------------

class TestGeminiTimeout:
    def test_gemini_timeout_returns_timeout_string(self) -> None:
        """Arrange: Gemini-Runner wirft TimeoutExpired.
        Act: _gemini_reviewer aufrufen.
        Assert: Rückgabe enthält Timeout-Hinweis, keine Exception propagiert.
        """
        # Arrange
        semgrep_clean = _semgrep_json([])

        def fake_runner(cmd: list[str], **kwargs: Any) -> MagicMock:
            if cmd[0] == "semgrep":
                return _proc(stdout=semgrep_clean)
            if cmd[0] == "gemini":
                raise subprocess.TimeoutExpired(cmd, timeout=300)
            return _proc()

        # Act
        result = _gemini_reviewer(
            prompt="Review this",
            worktree=WORKTREE,
            base_branch=BASE_BRANCH,
            runner=fake_runner,
        )

        # Assert
        assert "timeout" in result.lower() or "Timeout" in result


# ---------------------------------------------------------------------------
# Test 6 — Kombinierte Findings: semgrep-Block VOR Gemini-Output
# ---------------------------------------------------------------------------

class TestCombinedFindings:
    def test_semgrep_block_prepended_to_gemini_prompt(self) -> None:
        """Arrange: Semgrep hat 1 Finding; Gemini erhält kombinierten Prompt.
        Act: _gemini_reviewer aufrufen, Gemini-Prompt aus runner-Call extrahieren.
        Assert: Der an Gemini gesendete Prompt enthält sowohl Semgrep-Block
                als auch den ursprünglichen user-Prompt.
        """
        # Arrange
        findings = [_finding("src/vuln.ts", 99, "sqli", "SQL injection")]
        captured_prompt: list[str] = []

        def fake_runner(cmd: list[str], **kwargs: Any) -> MagicMock:
            if cmd[0] == "semgrep":
                return _proc(stdout=_semgrep_json(findings))
            if cmd[0] == "gemini":
                # gemini -m <model> -p <prompt> — Prompt ist cmd[4]
                if len(cmd) >= 5:
                    captured_prompt.append(cmd[4])
                return _proc(stdout="SEC-OK")
            return _proc()

        # Act
        _gemini_reviewer(
            prompt="User review instructions here",
            worktree=WORKTREE,
            base_branch=BASE_BRANCH,
            runner=fake_runner,
        )

        # Assert — Prompt enthält Semgrep-Block + original Prompt
        assert captured_prompt, "Gemini wurde nicht aufgerufen"
        combined = captured_prompt[0]
        assert "Semgrep" in combined
        assert "User review instructions here" in combined
        assert "src/vuln.ts" in combined


# ---------------------------------------------------------------------------
# Test 7 — nosemgrep-Marker-Handling
# ---------------------------------------------------------------------------

class TestNosemgrepMarker:
    def test_nosemgrep_annotation_parsed_in_baseline(self) -> None:
        """Arrange: Semgrep-JSON enthält Finding mit nosemgrep-Annotation
                    im message-Text.
        Act: _run_semgrep_baseline aufrufen.
        Assert: Die Finding-Zeile erscheint im Output (Semgrep meldet sie trotzdem;
                Markierung 'nosemgrep' ist ein Code-Kommentar, kein Filter für uns).

        Hintergrund: Wenn ein Entwickler `# nosemgrep: rule-id` setzt, supprimiert
        Semgrep die Finding selbst. Wenn sie trotzdem im JSON auftaucht, wurde die
        Annotation falsch gesetzt oder gilt für eine andere Regel — wir zeigen sie.
        """
        # Arrange
        finding_with_nosemgrep_note = _finding(
            path="src/bypass.ts",
            line=77,
            check_id="audit.missing-check",
            message="Missing authorization check  # nosemgrep: audit.missing-check (intentional)",
        )

        def fake_runner(cmd: list[str], **kwargs: Any) -> MagicMock:
            return _proc(stdout=_semgrep_json([finding_with_nosemgrep_note]))

        # Act
        result = _run_semgrep_baseline(WORKTREE, BASE_BRANCH, runner=fake_runner)

        # Assert — Finding ist trotzdem im Output
        assert "src/bypass.ts" in result
        assert "77" in result

    def test_nosemgrep_in_output_surfaced_in_combined_prompt(self) -> None:
        """Arrange: Semgrep meldet Finding das nosemgrep-Annotation enthält.
        Act: _gemini_reviewer aufrufen.
        Assert: Gemini-Prompt enthält den Finding-Pfad (Audit-Visibility).
        """
        # Arrange
        finding = _finding("src/suppress.py", 10, "suppress.rule", "nosemgrep bypassed")
        captured_prompts: list[str] = []

        def fake_runner(cmd: list[str], **kwargs: Any) -> MagicMock:
            if cmd[0] == "semgrep":
                return _proc(stdout=_semgrep_json([finding]))
            if cmd[0] == "gemini":
                if len(cmd) >= 5:
                    captured_prompts.append(cmd[4])
                return _proc(stdout="SEC-FINDINGS: check bypass")
            return _proc()

        # Act
        _gemini_reviewer(
            prompt="Prompt",
            worktree=WORKTREE,
            base_branch=BASE_BRANCH,
            runner=fake_runner,
        )

        # Assert
        assert captured_prompts
        assert "src/suppress.py" in captured_prompts[0]


# ---------------------------------------------------------------------------
# Test 8 — Config-Path-Resolution
# ---------------------------------------------------------------------------

class TestConfigPathResolution:
    def test_config_uses_correct_status_context(self) -> None:
        """Arrange: CONFIG ist das exportierte StageConfig-Objekt.
        Act: CONFIG.status_context auslesen.
        Assert: Stimmt mit common.STATUS_SECURITY überein.
        """
        # Arrange / Act
        status = CONFIG.status_context

        # Assert
        assert status == STATUS_SECURITY

    def test_config_uses_correct_sticky_marker(self) -> None:
        """Arrange: CONFIG ist das exportierte StageConfig-Objekt.
        Act: CONFIG.sticky_marker auslesen.
        Assert: Stimmt mit common.MARKER_SECURITY_REVIEW überein.
        """
        # Arrange / Act
        marker = CONFIG.sticky_marker

        # Assert
        assert marker == MARKER_SECURITY_REVIEW

    def test_config_name_is_security(self) -> None:
        """Arrange: CONFIG ist das exportierte StageConfig-Objekt.
        Act: CONFIG.name auslesen.
        Assert: Name ist 'security'.
        """
        assert CONFIG.name == "security"

    def test_config_path_filter_is_none(self) -> None:
        """Security-Stage läuft auf jedem PR (kein Path-Filter)."""
        assert CONFIG.path_filter is None

    def test_config_ok_sentinels_contains_sec_ok(self) -> None:
        """Sentinel-String SEC-OK muss in ok_sentinels sein."""
        assert "SEC-OK" in CONFIG.ok_sentinels


# ---------------------------------------------------------------------------
# Test 9 — Semgrep FileNotFoundError → SKIP
# ---------------------------------------------------------------------------

class TestSemgrepNotInPath:
    def test_semgrep_missing_returns_skip_message(self) -> None:
        """Arrange: Runner wirft FileNotFoundError (semgrep nicht im PATH).
        Act: _run_semgrep_baseline aufrufen.
        Assert: Rückgabe enthält 'SKIP' (kein Crash).
        """
        # Arrange
        def fake_runner(cmd: list[str], **kwargs: Any) -> MagicMock:
            if cmd[0] == "semgrep":
                raise FileNotFoundError("semgrep: not found")
            return _proc()

        # Act
        result = _run_semgrep_baseline(WORKTREE, BASE_BRANCH, runner=fake_runner)

        # Assert
        assert "SKIP" in result


# ---------------------------------------------------------------------------
# Test 10 — Semgrep Timeout → TIMEOUT
# ---------------------------------------------------------------------------

class TestSemgrepTimeout:
    def test_semgrep_timeout_returns_timeout_message(self) -> None:
        """Arrange: Runner wirft TimeoutExpired (>240s).
        Act: _run_semgrep_baseline aufrufen.
        Assert: Rückgabe enthält 'TIMEOUT' (kein Crash).
        """
        # Arrange
        def fake_runner(cmd: list[str], **kwargs: Any) -> MagicMock:
            if cmd[0] == "semgrep":
                raise subprocess.TimeoutExpired(["semgrep"], timeout=240)
            return _proc()

        # Act
        result = _run_semgrep_baseline(WORKTREE, BASE_BRANCH, runner=fake_runner)

        # Assert
        assert "TIMEOUT" in result


# ---------------------------------------------------------------------------
# Test 11 — Semgrep invalides JSON (parse-Fehler)
# ---------------------------------------------------------------------------

class TestSemgrepInvalidJson:
    def test_invalid_json_returns_parse_error_message(self) -> None:
        """Arrange: Semgrep liefert nicht-parsebaren stdout.
        Act: _run_semgrep_baseline aufrufen.
        Assert: Rückgabe enthält Hinweis auf Parse-Fehler, kein Crash.
        """
        # Arrange
        def fake_runner(cmd: list[str], **kwargs: Any) -> MagicMock:
            return _proc(stdout="not-valid-json!!!!", returncode=2)

        # Act
        result = _run_semgrep_baseline(WORKTREE, BASE_BRANCH, runner=fake_runner)

        # Assert
        assert "could not parse" in result or "parse" in result.lower()


# ---------------------------------------------------------------------------
# Test 12 — build_arg_parser Stub
# ---------------------------------------------------------------------------

class TestBuildArgParserStub:
    def test_arg_parser_accepts_pr_flag(self) -> None:
        """Arrange: build_arg_parser("security") aufrufen.
        Act: --pr 42 parsen.
        Assert: args.pr == 42.
        """
        # Arrange
        from ai_review_pipeline.stages.security_review import build_arg_parser
        ap = build_arg_parser("security")

        # Act
        args = ap.parse_args(["--pr", "42"])

        # Assert
        assert args.pr == 42

    def test_arg_parser_skip_fix_loop_default_false(self) -> None:
        """Arrange: build_arg_parser ohne --skip-fix-loop.
        Act: parsen.
        Assert: skip_fix_loop ist False per Default.
        """
        # Arrange
        from ai_review_pipeline.stages.security_review import build_arg_parser
        ap = build_arg_parser("security")

        # Act
        args = ap.parse_args(["--pr", "1"])

        # Assert
        assert args.skip_fix_loop is False


# ---------------------------------------------------------------------------
# Test 13 — run_stage Stub gibt 2 zurück (Wave 4b nicht implementiert)
# ---------------------------------------------------------------------------

class TestRunStageStub:
    def test_run_stage_stub_returns_2(self, capsys) -> None:
        """Arrange: run_stage mit Stub aufrufen.
        Act: Funktion aufrufen.
        Assert: Rückgabe ist 2 (= nicht implementiert), Warnung auf stderr.
        """
        # Arrange
        from ai_review_pipeline.stages.security_review import run_stage

        # Act
        result = run_stage(CONFIG, pr_number=99)

        # Assert
        assert result == 2
        captured = capsys.readouterr()
        assert "Wave 4b" in captured.err or "nicht portiert" in captured.err


# ---------------------------------------------------------------------------
# Test 14 — main() delegiert an run_stage
# ---------------------------------------------------------------------------

class TestMainFunction:
    def test_main_calls_run_stage_and_returns_exit_code(self, capsys) -> None:
        """Arrange: main() mit --pr 7 aufrufen.
        Act: main(["--pr", "7"]) aufrufen.
        Assert: Rückgabe ist int (exit code).
        """
        # Arrange
        from ai_review_pipeline.stages.security_review import main

        # Act
        result = main(["--pr", "7"])

        # Assert
        assert isinstance(result, int)
