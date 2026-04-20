"""Unit tests für ai_review_pipeline.common.

Portiert aus ai-portal scripts/ai-review/common_test.py.
Import angepasst: from . import common → from ai_review_pipeline import common.

TDD-Philosophie:
 - Pure-function Tests ohne Subprocess-Calls (parse/consensus/format)
 - Subprocess-abhängige Funktionen testen wir über den injizierten Runner:
   Wir übergeben ein FakeRunner-Callable statt subprocess.run zu monkey-patchen.

Laufen mit:
    pytest tests/test_common.py -v
"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_review_pipeline import common


# ---------------------------------------------------------------------------
# Fake subprocess.run result
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
        runner.on(["gh", "api", ...], stdout='{"number": 42}')
        common.some_fn(..., runner=runner)
        assert runner.calls[0][:3] == ["gh", "api", "..."]
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
# Pure helpers
# ---------------------------------------------------------------------------

class StripAnsiTests(unittest.TestCase):
    def test_removes_color_sequences(self) -> None:
        # Arrange
        text = "\x1b[31mred\x1b[0m normal \x1b[1;32mgreen\x1b[m"

        # Act
        stripped = common.strip_ansi(text)

        # Assert
        self.assertEqual(stripped, "red normal green")

    def test_passthrough_when_no_escapes(self) -> None:
        self.assertEqual(common.strip_ansi("plain text"), "plain text")


class TruncateTests(unittest.TestCase):
    def test_under_limit_returns_unchanged(self) -> None:
        self.assertEqual(common.truncate("abc", 10), "abc")

    def test_over_limit_appends_marker(self) -> None:
        result = common.truncate("x" * 50, 10)
        self.assertTrue(result.startswith("x" * 10))
        self.assertIn("gekürzt", result)

    def test_tail_keeps_end(self) -> None:
        result = common.tail("abcdefghij", 3)
        self.assertTrue(result.endswith("hij"))
        self.assertIn("gekürzt", result)


# ---------------------------------------------------------------------------
# parse_findings
# ---------------------------------------------------------------------------

class ParseFindingsTests(unittest.TestCase):
    def test_matches_backtick_colon_line_format(self) -> None:
        # Arrange
        output = "- `apps/portal-api/src/app.ts:42` kein explicit return type"

        # Act
        findings = common.parse_findings("Codex", output)

        # Assert
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["path"], "apps/portal-api/src/app.ts")
        self.assertEqual(findings[0]["line"], 42)
        self.assertEqual(findings[0]["model"], "Codex")

    def test_matches_tick_outside_colon_format(self) -> None:
        output = "see `src/foo.tsx`:17 for the violation"
        findings = common.parse_findings("Gemini", output)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["path"], "src/foo.tsx")
        self.assertEqual(findings[0]["line"], 17)

    def test_dedupes_same_path_line_within_one_model(self) -> None:
        output = "`a.ts:10` foo\n`a.ts:10` bar\n`a.ts:11` baz"
        findings = common.parse_findings("Codex", output)
        # Deduped: (a.ts, 10) only once + (a.ts, 11)
        self.assertEqual(len(findings), 2)
        lines = sorted(f["line"] for f in findings)
        self.assertEqual(lines, [10, 11])

    def test_ignores_non_source_extensions(self) -> None:
        # README.md matches (allowed), but .exe / .png are not source files
        output = "`thing.exe:5` should be ignored"
        findings = common.parse_findings("Codex", output)
        self.assertEqual(len(findings), 0)

    def test_requires_colon_between_path_and_line(self) -> None:
        # `foo.ts42` (no colon) must NOT match — guards against false-positives
        output = "the file `src/foo.ts42` has issues"
        findings = common.parse_findings("Codex", output)
        self.assertEqual(len(findings), 0)

    def test_handles_surql_and_sh_and_py(self) -> None:
        output = (
            "- `scripts/foo.sh:7` issue\n"
            "- `schema/init.surql:100` issue\n"
            "- `tools/bar.py:3` issue"
        )
        findings = common.parse_findings("Claude", output)
        exts = sorted(Path(f["path"]).suffix for f in findings)
        self.assertEqual(exts, [".py", ".sh", ".surql"])


# ---------------------------------------------------------------------------
# find_consensus
# ---------------------------------------------------------------------------

class FindConsensusTests(unittest.TestCase):
    def test_two_models_same_path_line_produces_consensus(self) -> None:
        all_findings = [
            {"path": "a.ts", "line": 1, "snippet": "codex says", "model": "Codex"},
            {"path": "a.ts", "line": 1, "snippet": "gemini says", "model": "Gemini"},
        ]
        consensus = common.find_consensus(all_findings)
        self.assertEqual(len(consensus), 1)
        self.assertEqual(consensus[0]["models"], ["Codex", "Gemini"])
        self.assertEqual(len(consensus[0]["snippets"]), 2)

    def test_single_model_no_consensus(self) -> None:
        all_findings = [
            {"path": "a.ts", "line": 1, "snippet": "x", "model": "Codex"},
            {"path": "a.ts", "line": 2, "snippet": "y", "model": "Codex"},
        ]
        self.assertEqual(common.find_consensus(all_findings), [])

    def test_same_model_twice_same_line_does_not_count_as_consensus(self) -> None:
        # Guard: if one model somehow emits the same path:line twice, that is
        # NOT 2-model consensus (same model = single perspective)
        all_findings = [
            {"path": "a.ts", "line": 1, "snippet": "x", "model": "Codex"},
            {"path": "a.ts", "line": 1, "snippet": "y", "model": "Codex"},
        ]
        self.assertEqual(common.find_consensus(all_findings), [])

    def test_triple_consensus_all_three(self) -> None:
        all_findings = [
            {"path": "a.ts", "line": 1, "snippet": "c", "model": "Codex"},
            {"path": "a.ts", "line": 1, "snippet": "g", "model": "Gemini"},
            {"path": "a.ts", "line": 1, "snippet": "cl", "model": "Claude"},
        ]
        consensus = common.find_consensus(all_findings)
        self.assertEqual(len(consensus), 1)
        self.assertEqual(consensus[0]["models"], ["Claude", "Codex", "Gemini"])

    def test_stable_sort_order(self) -> None:
        all_findings = [
            {"path": "z.ts", "line": 5, "snippet": "x", "model": "Codex"},
            {"path": "z.ts", "line": 5, "snippet": "y", "model": "Gemini"},
            {"path": "a.ts", "line": 1, "snippet": "x", "model": "Codex"},
            {"path": "a.ts", "line": 1, "snippet": "y", "model": "Gemini"},
        ]
        consensus = common.find_consensus(all_findings)
        self.assertEqual([c["path"] for c in consensus], ["a.ts", "z.ts"])


# ---------------------------------------------------------------------------
# Consensus score (for ai-review/consensus status)
# ---------------------------------------------------------------------------

class ConsensusScoreTests(unittest.TestCase):
    def test_all_three_success(self) -> None:
        state, desc = common.consensus_status({
            "ai-review/code": "success",
            "ai-review/security": "success",
            "ai-review/design": "success",
        })
        self.assertEqual(state, "success")
        self.assertIn("3/3", desc)

    def test_two_of_three_success_is_success(self) -> None:
        state, desc = common.consensus_status({
            "ai-review/code": "success",
            "ai-review/security": "success",
            "ai-review/design": "failure",
        })
        self.assertEqual(state, "success")
        self.assertIn("2/3", desc)

    def test_one_of_three_is_failure(self) -> None:
        state, desc = common.consensus_status({
            "ai-review/code": "success",
            "ai-review/security": "failure",
            "ai-review/design": "failure",
        })
        self.assertEqual(state, "failure")

    def test_skipped_reduces_denominator(self) -> None:
        # Design-review skip (no UI changes) → only code+security count.
        # Both success → 2/2 → success
        state, desc = common.consensus_status({
            "ai-review/code": "success",
            "ai-review/security": "success",
            "ai-review/design": "skipped",
        })
        self.assertEqual(state, "success")
        self.assertIn("2/2", desc)

    def test_all_skipped_or_pending_is_pending(self) -> None:
        state, _ = common.consensus_status({
            "ai-review/code": "pending",
            "ai-review/security": "pending",
            "ai-review/design": "skipped",
        })
        self.assertEqual(state, "pending")

    def test_pending_stage_keeps_consensus_pending(self) -> None:
        # Wenn irgendeine Stage noch pending ist, darf consensus nicht grün werden.
        state, _ = common.consensus_status({
            "ai-review/code": "success",
            "ai-review/security": "pending",
            "ai-review/design": "skipped",
        })
        self.assertEqual(state, "pending")

    def test_one_skip_plus_one_failure_is_failure(self) -> None:
        # 1 success + 1 failure + 1 skipped → 1/2 success → failure (needs ≥2/2 of non-skipped)
        state, _ = common.consensus_status({
            "ai-review/code": "success",
            "ai-review/security": "failure",
            "ai-review/design": "skipped",
        })
        self.assertEqual(state, "failure")


# ---------------------------------------------------------------------------
# Sticky comment builder
# ---------------------------------------------------------------------------

class StickyCommentTests(unittest.TestCase):
    def test_body_includes_marker_and_all_sections(self) -> None:
        body = common.build_sticky_comment(
            marker=common.MARKER_CODE_REVIEW,
            title="Code Review · PR #42",
            head_sha="abc123ef" * 5,
            sections=[
                ("🤖 Codex", "All looks good"),
                ("🧪 Pre-Flight", "✅ PASS"),
            ],
        )
        self.assertIn(common.MARKER_CODE_REVIEW, body)
        self.assertIn("PR #42", body)
        self.assertIn("All looks good", body)
        self.assertIn("✅ PASS", body)
        self.assertIn("abc123ef", body)  # short SHA

    def test_long_section_truncated(self) -> None:
        huge = "x" * (common.MAX_SECTION_CHARS + 1000)
        body = common.build_sticky_comment(
            marker=common.MARKER_CODE_REVIEW,
            title="PR",
            head_sha="deadbeef",
            sections=[("Huge", huge)],
        )
        self.assertLess(len(body), common.MAX_SECTION_CHARS + 2_000)
        self.assertIn("gekürzt", body)


# ---------------------------------------------------------------------------
# GhClient (uses FakeRunner — no real network calls)
# ---------------------------------------------------------------------------

class GhClientTests(unittest.TestCase):
    def test_get_pr_parses_json_from_gh_view(self) -> None:
        # Arrange
        runner = FakeRunner()
        runner.on(
            ["gh", "pr", "view"],
            stdout=json.dumps({
                "title": "My PR",
                "baseRefName": "main",
                "headRefOid": "abc123",
                "isDraft": False,
            }),
        )
        gh = common.GhClient(runner=runner)

        # Act
        pr = gh.get_pr(42)

        # Assert
        self.assertEqual(pr["title"], "My PR")
        self.assertEqual(pr["baseRefName"], "main")
        self.assertFalse(pr["isDraft"])
        # Verify the gh command was called with the expected args
        self.assertIn("42", runner.calls[0])

    def test_set_commit_status_posts_to_correct_endpoint(self) -> None:
        # Arrange
        runner = FakeRunner()
        runner.default(returncode=0, stdout="{}")
        gh = common.GhClient(runner=runner)

        # Act
        gh.set_commit_status(
            sha="abc123",
            context="ai-review/code",
            state="success",
            description="All good",
        )

        # Assert: one call to gh api POST statuses
        self.assertEqual(len(runner.calls), 1)
        cmd = runner.calls[0]
        self.assertIn("statuses/abc123", " ".join(cmd))
        self.assertIn("ai-review/code", " ".join(cmd))

    def test_set_commit_status_rejects_invalid_state(self) -> None:
        runner = FakeRunner()
        gh = common.GhClient(runner=runner)
        with self.assertRaises(ValueError):
            gh.set_commit_status(
                sha="abc", context="x", state="not-a-real-state", description="...",
            )

    def test_post_sticky_comment_updates_existing(self) -> None:
        # Arrange: first call (list comments) returns an existing comment id;
        # second call (PATCH) updates it.
        runner = FakeRunner()
        runner.on(
            ["gh", "api", "repos/EtroxTaran/ai-portal/issues/42/comments"],
            stdout="99\n",  # existing-id from the --jq query
        )
        runner.default(stdout="{}")
        gh = common.GhClient(runner=runner)

        # Act
        gh.post_sticky_comment(
            pr_number=42, marker=common.MARKER_CODE_REVIEW, body="new body",
        )

        # Assert: PATCH was called (update path)
        patch_call = [c for c in runner.calls if "PATCH" in c]
        self.assertTrue(patch_call, f"expected PATCH call, got {runner.calls}")

    def test_post_sticky_comment_creates_when_none_exists(self) -> None:
        runner = FakeRunner()
        runner.on(
            ["gh", "api", "repos/EtroxTaran/ai-portal/issues/42/comments"],
            stdout="null\n",
        )
        runner.default(stdout="{}")
        gh = common.GhClient(runner=runner)

        gh.post_sticky_comment(
            pr_number=42, marker=common.MARKER_CODE_REVIEW, body="new body",
        )

        # Expect a `gh pr comment` create call
        create_call = [c for c in runner.calls if "pr" in c and "comment" in c]
        self.assertTrue(create_call, f"expected create call, got {runner.calls}")

    def test_get_commit_statuses_normalizes_skipped_description_to_skipped(self) -> None:
        # Regression: stage.py encodes a skipped stage as state=success +
        # description starting with "skipped". `get_commit_statuses` must
        # return "skipped" in that case so consensus.aggregate does not
        # miscount the skipped stage as green.
        runner = FakeRunner()
        runner.default(stdout=json.dumps({
            "statuses": [
                {
                    "context": "ai-review/code",
                    "state": "success",
                    "description": "Codex clean",
                },
                {
                    "context": "ai-review/design",
                    "state": "success",
                    "description": "skipped — no design-relevant files changed",
                },
            ],
        }))
        gh = common.GhClient(runner=runner)

        states = gh.get_commit_statuses("abc")

        self.assertEqual(states["ai-review/code"], "success")
        self.assertEqual(states["ai-review/design"], "skipped")

    def test_get_commit_statuses_plain_success_stays_success(self) -> None:
        runner = FakeRunner()
        runner.default(stdout=json.dumps({
            "statuses": [
                {
                    "context": "ai-review/code",
                    "state": "success",
                    "description": "Codex clean",
                },
            ],
        }))
        gh = common.GhClient(runner=runner)

        states = gh.get_commit_statuses("abc")

        self.assertEqual(states["ai-review/code"], "success")


# ---------------------------------------------------------------------------
# CLI wrappers (just verify command shape + env — don't exec real CLIs)
# ---------------------------------------------------------------------------

class CliWrapperTests(unittest.TestCase):
    def test_run_codex_uses_review_subcommand_with_base_and_title(self) -> None:
        runner = FakeRunner()
        runner.default(stdout="LGTM")

        out = common.run_codex(
            prompt="review this",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            pr_title="Cool PR",
            runner=runner,
        )

        self.assertIn("LGTM", out)
        cmd = runner.calls[0]
        self.assertEqual(cmd[0], "codex")
        self.assertIn("review", cmd)
        self.assertIn("origin/main", cmd)
        self.assertIn("Cool PR", cmd)

    def test_run_codex_does_not_pass_positional_prompt_with_base(self) -> None:
        # Regression: `codex review` rejects `--base` + [PROMPT] als
        # gegenseitig exklusiv ("the argument '--base <BRANCH>' cannot be
        # used with '[PROMPT]'"). Der prompt-Parameter darf nicht als
        # positionales Argument in der CLI-Invocation auftauchen.
        runner = FakeRunner()
        runner.default(stdout="LGTM")

        unique_prompt = "UNIQUE_PROMPT_MARKER_do_not_leak_to_cli"
        common.run_codex(
            prompt=unique_prompt,
            worktree=Path("/tmp/wt"),
            base_branch="main",
            pr_title="Cool PR",
            runner=runner,
        )

        cmd = runner.calls[0]
        self.assertNotIn(unique_prompt, cmd,
                         "prompt must not be passed positionally alongside --base")
        # Und: `--base` muss weiterhin present sein
        self.assertIn("--base", cmd)

    def test_run_gemini_uses_m_before_p_flag_order(self) -> None:
        # CLAUDE.md: gemini yargs bug — -m MUST come before -p
        runner = FakeRunner()
        runner.default(stdout="ok")

        common.run_gemini(
            prompt="review this",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            runner=runner,
        )

        cmd = runner.calls[-1]  # last call = the gemini invocation
        self.assertEqual(cmd[0], "gemini")
        # -m MUST appear before -p in the arg list
        m_idx = cmd.index("-m")
        p_idx = cmd.index("-p")
        self.assertLess(m_idx, p_idx, "gemini -m must come before -p (yargs)")

    def test_run_claude_uses_print_mode(self) -> None:
        runner = FakeRunner()
        runner.default(stdout="done")

        common.run_claude(
            prompt="review this",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            runner=runner,
        )

        cmd = runner.calls[-1]
        self.assertEqual(cmd[0], "claude")
        self.assertIn("-p", cmd)  # print-mode flag

    def test_run_codex_timeout_yields_timeout_marker(self) -> None:
        import subprocess as _sp

        def timeout_runner(cmd: list[str], **_kwargs: Any) -> FakeCompletedProcess:
            raise _sp.TimeoutExpired(cmd=cmd, timeout=1)

        out = common.run_codex(
            prompt="x",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            pr_title="t",
            runner=timeout_runner,
        )
        self.assertIn("Timeout", out)


# ---------------------------------------------------------------------------
# current_head_sha
# ---------------------------------------------------------------------------

class CurrentHeadShaTests(unittest.TestCase):
    """Regression: after the fix-loop pushes new commits, stage.py must
    re-resolve the worktree HEAD so the terminal commit-status attaches to
    the new tip — not to the stale `head_sha` captured before the loop ran.
    """

    def test_returns_resolved_sha_when_git_succeeds(self) -> None:
        runner = FakeRunner()
        runner.on(
            ["git", "rev-parse", "HEAD"],
            stdout="abc123def456\n",
            returncode=0,
        )

        sha = common.current_head_sha(
            Path("/tmp/wt"), fallback="oldsha", runner=runner,
        )

        self.assertEqual(sha, "abc123def456")

    def test_falls_back_when_git_fails(self) -> None:
        runner = FakeRunner()
        runner.on(
            ["git", "rev-parse", "HEAD"],
            stdout="", stderr="not a git repo", returncode=128,
        )

        sha = common.current_head_sha(
            Path("/tmp/wt"), fallback="oldsha", runner=runner,
        )

        self.assertEqual(sha, "oldsha")

    def test_falls_back_when_stdout_is_empty(self) -> None:
        runner = FakeRunner()
        runner.on(["git", "rev-parse", "HEAD"], stdout="\n", returncode=0)

        sha = common.current_head_sha(
            Path("/tmp/wt"), fallback="oldsha", runner=runner,
        )

        self.assertEqual(sha, "oldsha")


class DetectRateLimitTests(unittest.TestCase):
    """Wave 4: erkennt Rate-Limit-Signaturen im CLI-Output, damit die Stage
    sich skippen kann statt als failure durchzugehen."""

    def test_detects_explicit_429(self) -> None:
        self.assertTrue(common.detect_rate_limit("API returned 429 Too Many Requests"))
        self.assertTrue(common.detect_rate_limit("HTTP 429"))

    def test_detects_rate_limit_phrase(self) -> None:
        self.assertTrue(common.detect_rate_limit("Error: rate limit exceeded, retry in 30s"))
        self.assertTrue(common.detect_rate_limit("Rate Limited"))

    def test_detects_quota_phrases(self) -> None:
        self.assertTrue(common.detect_rate_limit("quota exceeded for model"))
        self.assertTrue(common.detect_rate_limit("Monthly usage limit reached"))

    def test_does_not_match_unrelated_errors(self) -> None:
        self.assertFalse(common.detect_rate_limit("HTTP 500 internal server error"))
        self.assertFalse(common.detect_rate_limit("Syntax error in prompt"))
        self.assertFalse(common.detect_rate_limit(""))

    def test_case_insensitive(self) -> None:
        self.assertTrue(common.detect_rate_limit("RATE LIMIT"))
        self.assertTrue(common.detect_rate_limit("Quota Exceeded"))


class RunCursorTests(unittest.TestCase):
    """Wave 5a: Cursor CLI als zweiter Code-Reviewer.

    Cursor antwortet standardmäßig mit JSON (`--output-format json`). Das
    `result`-Feld enthält die eigentliche Review-Antwort; `session_id` + `usage`
    sind Telemetrie. Wir extrahieren `result` — bei parse-failure fallen wir
    auf stdout-raw zurück damit der reviewer auch mit text-Output arbeitet.
    """

    def test_uses_print_force_and_json_output_format(self) -> None:
        # JSON-Response mit result-Feld
        fake_json = json.dumps({"type": "result", "result": "LGTM", "session_id": "s1"})
        runner = FakeRunner()
        runner.default(stdout=fake_json)

        out = common.run_cursor(
            prompt="review this diff",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            runner=runner,
        )

        self.assertEqual(out.strip(), "LGTM")
        cmd = runner.calls[0]
        self.assertEqual(cmd[0], "cursor-agent")
        self.assertIn("--print", cmd)
        # --force (or --trust) muss anliegen, sonst blockt Workspace-Trust
        self.assertTrue(
            "--force" in cmd or "--trust" in cmd or "--yolo" in cmd,
            "cursor-agent blocks without workspace-trust flag",
        )
        self.assertIn("--output-format", cmd)
        self.assertIn("json", cmd)

    def test_passes_model_flag(self) -> None:
        runner = FakeRunner()
        runner.default(stdout=json.dumps({"type": "result", "result": "ok"}))

        common.run_cursor(
            prompt="x",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            model="composer-2",
            runner=runner,
        )

        cmd = runner.calls[0]
        self.assertIn("--model", cmd)
        idx = cmd.index("--model")
        self.assertEqual(cmd[idx + 1], "composer-2")

    def test_passes_prompt_as_positional(self) -> None:
        # Cursor erwartet den prompt als positionales Argument (anders als
        # Gemini/Claude die `-p <prompt>` nutzen)
        runner = FakeRunner()
        runner.default(stdout=json.dumps({"type": "result", "result": "ok"}))

        marker = "UNIQUE_CURSOR_PROMPT_MARKER"
        common.run_cursor(
            prompt=marker,
            worktree=Path("/tmp/wt"),
            base_branch="main",
            runner=runner,
        )

        cmd = runner.calls[0]
        # Prompt ist das letzte positionales Arg — direkt im cmd-array
        self.assertIn(marker, cmd)

    def test_returns_raw_stdout_when_json_parse_fails(self) -> None:
        # Cursor könnte bei CLI-Error auf stderr schreiben ohne JSON-struct
        runner = FakeRunner()
        runner.default(stdout="not valid json at all", stderr="")

        out = common.run_cursor(
            prompt="x",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            runner=runner,
        )

        # Fallback: raw stdout, damit der Escalation-Comment nicht leer ist
        self.assertIn("not valid json", out)

    def test_timeout_returns_descriptive_marker(self) -> None:
        import subprocess as sp

        class TimeoutRunner:
            def __call__(self, cmd: list[str], **_kw: Any):
                raise sp.TimeoutExpired(cmd, timeout=10)

        out = common.run_cursor(
            prompt="x",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            runner=TimeoutRunner(),
            timeout=10,
        )

        self.assertIn("Timeout", out)
        self.assertIn("Cursor", out)

    def test_rate_limit_pattern_detected_from_stderr(self) -> None:
        # Cursor kann bei Rate-Limit auf stderr schreiben + non-zero exit.
        # Wir wollen, dass detect_rate_limit auf dem Output-String anschlägt.
        runner = FakeRunner()
        runner.default(
            stdout="",
            stderr="Error: rate limit exceeded, retry in 60s",
            returncode=1,
        )

        out = common.run_cursor(
            prompt="x",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            runner=runner,
        )
        # Unser Output aggregiert stdout+stderr via _safe_stdout
        self.assertTrue(common.detect_rate_limit(out))


# ---------------------------------------------------------------------------
# Zusätzliche Tests für Coverage ≥80%
# ---------------------------------------------------------------------------

class ResolveCodeConsensusTests(unittest.TestCase):
    """Wave 5b/6b: resolve_code_consensus direkt testen."""

    def test_weighted_score_high_avg_returns_success(self) -> None:
        result = common.resolve_code_consensus("success", "success", code_score=9, cursor_score=9)
        self.assertEqual(result, "success")

    def test_weighted_score_mid_avg_returns_soft(self) -> None:
        # avg = (6 + 7) / 2 = 6.5 → soft
        result = common.resolve_code_consensus("success", "failure", code_score=6, cursor_score=7)
        self.assertEqual(result, "soft")

    def test_weighted_score_low_avg_returns_failure(self) -> None:
        # avg = (2 + 3) / 2 = 2.5 → failure
        result = common.resolve_code_consensus("failure", "failure", code_score=2, cursor_score=3)
        self.assertEqual(result, "failure")

    def test_pending_code_returns_pending(self) -> None:
        result = common.resolve_code_consensus("pending", "success")
        self.assertEqual(result, "pending")

    def test_pending_cursor_returns_pending(self) -> None:
        result = common.resolve_code_consensus("success", "pending")
        self.assertEqual(result, "pending")

    def test_both_skipped_returns_skipped(self) -> None:
        result = common.resolve_code_consensus("skipped", "skipped")
        self.assertEqual(result, "skipped")

    def test_none_code_cursor_treated_as_skipped(self) -> None:
        # None → "skipped" via `or "skipped"` shortcut
        result = common.resolve_code_consensus(None, None)
        self.assertEqual(result, "skipped")

    def test_success_and_skipped_returns_success(self) -> None:
        result = common.resolve_code_consensus("success", "skipped")
        self.assertEqual(result, "success")

    def test_failure_and_skipped_returns_failure(self) -> None:
        result = common.resolve_code_consensus("failure", None)
        self.assertEqual(result, "failure")


class ConsensusStatusExtendedTests(unittest.TestCase):
    """Erweiterte Tests für consensus_status — uncovered paths."""

    def test_soft_code_consensus_with_scores_returns_pending_with_avg_desc(self) -> None:
        # avg 6.5 → soft → pending mit konkreter Score-Description
        state, desc = common.consensus_status(
            {
                "ai-review/code": "success",
                "ai-review/code-cursor": "failure",
            },
            code_score=6,
            cursor_score=7,
        )
        self.assertEqual(state, "pending")
        self.assertIn("6.5", desc)
        self.assertIn("codex=6", desc)

    def test_soft_code_consensus_without_scores_returns_borderline_desc(self) -> None:
        # soft via binärer Logik ist nicht möglich (binäre Logik liefert kein soft).
        # Wir testen den borderline-Pfad: code_score=None aber weighted wäre soft.
        # Da weighted nur aktiv wenn BEIDE Scores gesetzt, bleibt es binär.
        # Deshalb testen wir stattdessen: wenn code_consensus=soft (nur via weighted)
        # aber code_score ist None (unmöglich in Praxis) — nicht testbar direkt.
        # Wir testen den "all stages skipped"-Pfad (line 412):
        state, desc = common.consensus_status({
            "ai-review/code": "skipped",
            "ai-review/security": "skipped",
            "ai-review/design": "skipped",
        })
        # code=skipped, cursor=None→skipped → code_consensus=skipped
        # triple: code-consensus=skipped, security=skipped, design=skipped
        # completed = {} → pending
        self.assertEqual(state, "pending")
        self.assertIn("skipped", desc.lower())

    def test_security_failure_with_waiver_success_overrides_veto(self) -> None:
        # Security-Waiver hebt Security-Veto auf
        state, desc = common.consensus_status({
            "ai-review/code": "success",
            "ai-review/security": "failure",
            "ai-review/design": "success",
            "ai-review/security-waiver": "success",
        })
        # Security-Veto aufgehoben → 2/3 success wäre es, aber design=success, code=success
        # → completed = {code-consensus: success, security: failure, design: success}
        # success_count=2, total=3 → success
        self.assertEqual(state, "success")

    def test_consensus_includes_code_detail_when_both_code_stages_present(self) -> None:
        # Prüft den code_detail-Pfad (lines 420-422)
        state, desc = common.consensus_status({
            "ai-review/code": "success",
            "ai-review/code-cursor": "success",
            "ai-review/security": "success",
            "ai-review/design": "success",
        })
        self.assertEqual(state, "success")
        # code_detail sollte in desc erscheinen: "[codex=succ, cursor=succ → success]"
        self.assertIn("codex=", desc)
        self.assertIn("cursor=", desc)

    def test_total_one_success_is_success(self) -> None:
        # denom=1 + success_count=1 → success
        state, desc = common.consensus_status({
            "ai-review/code": "success",
            "ai-review/security": "skipped",
            "ai-review/design": "skipped",
        })
        self.assertEqual(state, "success")


class CurrentHeadShaExceptionTests(unittest.TestCase):
    """Testet den Exception-Handler-Pfad in current_head_sha."""

    def test_falls_back_when_runner_raises_exception(self) -> None:
        def raising_runner(cmd: list[str], **_kw: Any) -> Any:
            raise RuntimeError("git not found")

        sha = common.current_head_sha(
            Path("/tmp/wt"), fallback="fallback-sha", runner=raising_runner,
        )
        self.assertEqual(sha, "fallback-sha")


class GeminiClaudeTimeoutTests(unittest.TestCase):
    """Timeout-Pfade für run_gemini und run_claude."""

    def test_run_gemini_timeout_yields_marker(self) -> None:
        import subprocess as _sp

        def timeout_runner(cmd: list[str], **_kw: Any) -> Any:
            raise _sp.TimeoutExpired(cmd=cmd, timeout=1)

        out = common.run_gemini(
            prompt="x",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            runner=timeout_runner,
        )
        self.assertIn("Timeout", out)
        self.assertIn("Gemini", out)

    def test_run_claude_timeout_yields_marker(self) -> None:
        import subprocess as _sp

        def timeout_runner(cmd: list[str], **_kw: Any) -> Any:
            raise _sp.TimeoutExpired(cmd=cmd, timeout=1)

        out = common.run_claude(
            prompt="x",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            runner=timeout_runner,
        )
        self.assertIn("Timeout", out)
        self.assertIn("Claude", out)

    def test_run_gemini_empty_output_returns_placeholder(self) -> None:
        runner = FakeRunner()
        runner.default(stdout="", stderr="")

        out = common.run_gemini(
            prompt="x",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            runner=runner,
        )
        self.assertIn("Gemini", out)

    def test_run_claude_empty_output_returns_placeholder(self) -> None:
        runner = FakeRunner()
        runner.default(stdout="", stderr="")

        out = common.run_claude(
            prompt="x",
            worktree=Path("/tmp/wt"),
            base_branch="main",
            runner=runner,
        )
        self.assertIn("Claude", out)


class ExtractCursorResultFallbackTests(unittest.TestCase):
    """Tests für _extract_cursor_result messages-Fallback-Pfad."""

    def test_extracts_from_messages_content_field(self) -> None:
        raw = json.dumps({
            "type": "result",
            "result": "",  # leer → Fallback
            "messages": [
                {"content": "review content from messages"},
            ],
        })
        result = common._extract_cursor_result(raw)
        self.assertEqual(result, "review content from messages")

    def test_extracts_from_messages_text_field(self) -> None:
        raw = json.dumps({
            "type": "result",
            "result": "",
            "messages": [
                {"text": "review via text field"},
            ],
        })
        result = common._extract_cursor_result(raw)
        self.assertEqual(result, "review via text field")

    def test_returns_raw_when_non_dict_json(self) -> None:
        # JSON-Array (kein dict) → raw zurück
        raw = '[1, 2, 3]'
        result = common._extract_cursor_result(raw)
        self.assertEqual(result, raw)

    def test_returns_raw_when_messages_empty(self) -> None:
        raw = json.dumps({"type": "result", "result": "", "messages": []})
        result = common._extract_cursor_result(raw)
        self.assertEqual(result, raw)

    def test_returns_raw_when_no_result_and_no_messages(self) -> None:
        raw = json.dumps({"type": "result"})
        result = common._extract_cursor_result(raw)
        self.assertEqual(result, raw)


class DiffHelpersTests(unittest.TestCase):
    """Tests für git_diff_stat, git_diff_full, git_changed_files."""

    def test_git_diff_stat_returns_output(self) -> None:
        runner = FakeRunner()
        runner.default(stdout=" 5 files changed", stderr="")

        result = common.git_diff_stat(Path("/tmp/wt"), "main", runner=runner)

        self.assertIn("5 files", result)
        cmd = runner.calls[0]
        self.assertIn("--stat", cmd)
        self.assertIn("origin/main...HEAD", cmd)

    def test_git_diff_full_truncates_large_diff(self) -> None:
        huge_diff = "+" * (common.MAX_DIFF_CHARS + 1000)
        runner = FakeRunner()
        runner.default(stdout=huge_diff, stderr="")

        result = common.git_diff_full(Path("/tmp/wt"), "main", runner=runner)

        self.assertIn("Diff gekürzt", result)
        self.assertLessEqual(len(result), common.MAX_DIFF_CHARS + 200)

    def test_git_diff_full_returns_full_diff_when_small(self) -> None:
        runner = FakeRunner()
        runner.default(stdout="small diff", stderr="")

        result = common.git_diff_full(Path("/tmp/wt"), "main", runner=runner)
        self.assertEqual(result, "small diff")

    def test_git_changed_files_returns_list(self) -> None:
        runner = FakeRunner()
        runner.default(stdout="src/a.ts\nsrc/b.ts\n", stderr="")

        files = common.git_changed_files(Path("/tmp/wt"), "main", runner=runner)

        self.assertEqual(files, ["src/a.ts", "src/b.ts"])

    def test_git_changed_files_filters_empty_lines(self) -> None:
        runner = FakeRunner()
        runner.default(stdout="src/a.ts\n\nsrc/b.ts\n", stderr="")

        files = common.git_changed_files(Path("/tmp/wt"), "main", runner=runner)
        self.assertEqual(len(files), 2)


class GhClientExtendedTests(unittest.TestCase):
    """Erweiterte GhClient-Tests für error-paths und weitere Methoden."""

    def test_get_pr_raises_on_failure(self) -> None:
        runner = FakeRunner()
        runner.default(returncode=1, stdout="", stderr="not found")
        gh = common.GhClient(runner=runner)

        with self.assertRaises(RuntimeError):
            gh.get_pr(42)

    def test_get_pr_for_current_branch_success(self) -> None:
        runner = FakeRunner()
        runner.default(returncode=0, stdout="42\n", stderr="")
        gh = common.GhClient(runner=runner)

        pr_num = gh.get_pr_for_current_branch()
        self.assertEqual(pr_num, 42)

    def test_get_pr_for_current_branch_raises_on_failure(self) -> None:
        runner = FakeRunner()
        runner.default(returncode=1, stdout="", stderr="error")
        gh = common.GhClient(runner=runner)

        with self.assertRaises(RuntimeError):
            gh.get_pr_for_current_branch()

    def test_get_pr_for_current_branch_raises_when_not_digit(self) -> None:
        runner = FakeRunner()
        runner.default(returncode=0, stdout="not-a-number\n", stderr="")
        gh = common.GhClient(runner=runner)

        with self.assertRaises(RuntimeError):
            gh.get_pr_for_current_branch()

    def test_set_commit_status_raises_on_gh_failure(self) -> None:
        runner = FakeRunner()
        runner.default(returncode=1, stdout="", stderr="api error")
        gh = common.GhClient(runner=runner)

        with self.assertRaises(RuntimeError):
            gh.set_commit_status(
                sha="abc", context="ctx", state="success", description="d",
            )

    def test_set_commit_status_with_target_url(self) -> None:
        runner = FakeRunner()
        runner.default(returncode=0, stdout="{}", stderr="")
        gh = common.GhClient(runner=runner)

        gh.set_commit_status(
            sha="abc123",
            context="ai-review/code",
            state="success",
            description="All good",
            target_url="https://example.com/run/42",
        )

        cmd = runner.calls[0]
        self.assertIn("target_url=https://example.com/run/42", " ".join(cmd))

    def test_get_commit_status_details_returns_parsed_data(self) -> None:
        runner = FakeRunner()
        runner.default(stdout=json.dumps({
            "statuses": [
                {"context": "ai-review/code", "state": "success", "description": "Codex clean"},
                {"context": "ai-review/design", "state": "success",
                 "description": "skipped — no ui changes"},
            ],
        }))
        gh = common.GhClient(runner=runner)

        details = gh.get_commit_status_details("abc")

        self.assertEqual(details["ai-review/code"]["state"], "success")
        self.assertEqual(details["ai-review/design"]["state"], "skipped")

    def test_get_commit_status_details_raises_on_failure(self) -> None:
        runner = FakeRunner()
        runner.default(returncode=1, stderr="error")
        gh = common.GhClient(runner=runner)

        with self.assertRaises(RuntimeError):
            gh.get_commit_status_details("abc")

    def test_get_commit_statuses_raises_on_failure(self) -> None:
        runner = FakeRunner()
        runner.default(returncode=1, stderr="error")
        gh = common.GhClient(runner=runner)

        with self.assertRaises(RuntimeError):
            gh.get_commit_statuses("abc")

    def test_get_commit_statuses_deduplicates_contexts(self) -> None:
        # First occurrence of a context wins (API returns newest-first)
        runner = FakeRunner()
        runner.default(stdout=json.dumps({
            "statuses": [
                {"context": "ai-review/code", "state": "success", "description": "first"},
                {"context": "ai-review/code", "state": "failure", "description": "second"},
            ],
        }))
        gh = common.GhClient(runner=runner)

        states = gh.get_commit_statuses("abc")
        self.assertEqual(states["ai-review/code"], "success")

    def test_post_sticky_comment_raises_on_patch_failure(self) -> None:
        runner = FakeRunner()
        runner.on(
            ["gh", "api", "repos/EtroxTaran/ai-portal/issues/42/comments"],
            stdout="99\n",
        )
        runner.default(returncode=1, stderr="patch failed")
        gh = common.GhClient(runner=runner)

        with self.assertRaises(RuntimeError):
            gh.post_sticky_comment(
                pr_number=42, marker=common.MARKER_CODE_REVIEW, body="body",
            )

    def test_post_review_logs_on_failure(self) -> None:
        import io
        import sys as _sys

        runner = FakeRunner()
        runner.default(returncode=1, stderr="REQUEST_CHANGES on own PR is denied")
        gh = common.GhClient(runner=runner)

        # post_review sollte NICHT werfen sondern nur auf stderr loggen
        captured = io.StringIO()
        _sys.stderr = captured
        try:
            gh.post_review(
                pr_number=42,
                head_sha="abc123",
                body="review body",
                event="REQUEST_CHANGES",
            )
        finally:
            _sys.stderr = _sys.__stderr__

        self.assertIn("failed", captured.getvalue())

    def test_post_review_with_line_comments(self) -> None:
        runner = FakeRunner()
        runner.default(returncode=0, stdout="{}")
        gh = common.GhClient(runner=runner)

        gh.post_review(
            pr_number=42,
            head_sha="abc",
            body="LGTM",
            event="APPROVE",
            line_comments=[{"path": "src/a.ts", "line": 5, "body": "nit"}],
        )

        # Stellt sicher, dass --input - aufgerufen wurde (JSON payload via stdin)
        cmd = runner.calls[0]
        self.assertIn("--input", cmd)

    def test_dismiss_stale_reviews_calls_dismissal_endpoint(self) -> None:
        runner = FakeRunner()
        # Erster Call (GET reviews) gibt eine ID zurück
        runner.on(
            ["gh", "api"],
            stdout=json.dumps([101, 102]),
        )
        runner.default(returncode=0, stdout="{}")
        gh = common.GhClient(runner=runner)

        gh.dismiss_stale_reviews(pr_number=42, marker=common.MARKER_CODE_REVIEW)

        # Mindestens ein Dismissal-Call (PUT) sollte abgesetzt worden sein
        put_calls = [c for c in runner.calls if "PUT" in c]
        self.assertTrue(put_calls, f"expected PUT dismissal calls, got {runner.calls}")

    def test_dismiss_stale_reviews_handles_json_decode_error(self) -> None:
        runner = FakeRunner()
        runner.default(returncode=0, stdout="NOT VALID JSON!!!")
        gh = common.GhClient(runner=runner)

        # Soll nicht werfen — json.JSONDecodeError wird intern behandelt
        gh.dismiss_stale_reviews(pr_number=42, marker=common.MARKER_CODE_REVIEW)


if __name__ == "__main__":
    unittest.main()
