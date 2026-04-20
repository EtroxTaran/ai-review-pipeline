"""Tests für src/ai_review_pipeline/auto_fix.py (Wave 7c).

Portiert aus ai-portal/scripts/ai-review/auto_fix_test.py.

Der Auto-Fix-Agent ist ein cross-stage Claude-Code-Fixer, getriggert via
`workflow_dispatch` (oder via `/ai-review retry` / Telegram-Button).

Guard-Rails (mandatory, getestet):
  - Max AUTO_FIX_MAX_FILES (default 10) Files changed pro Run.
  - Nur Files-Pfade dürfen geändert werden, die im Review-Finding sind
    (+ korrespondierende Test-Dateien, Pattern `foo.ts` → `foo.test.ts`).
  - Post-Fix `pnpm typecheck` + `pnpm test --changed` MÜSSEN beide grün sein.
  - Bei Fail: Kein Push, Eskalation via PR-Comment.
"""

from __future__ import annotations

import subprocess
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_review_pipeline import auto_fix


# ---------------------------------------------------------------------------
# Fake Runner + Fake Gh
# ---------------------------------------------------------------------------


@dataclass
class FakeProc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class RunnerCall:
    cmd: list[str]
    cwd: Path | None = None
    env: dict[str, str] | None = None


@dataclass
class FakeRunner:
    """Scripted Runner — returns pre-programmed FakeProc's by cmd-pattern.

    Usage:
        r = FakeRunner()
        r.on(lambda c: c[0] == "git" and c[1] == "rev-parse", FakeProc(stdout="sha1\\n"))
        r.on(lambda c: c[0] == "claude", FakeProc())  # empty stdout = no edit
    """

    calls: list[RunnerCall] = field(default_factory=list)
    _scripts: list[tuple[Any, Any]] = field(default_factory=list)
    _default: FakeProc = field(default_factory=lambda: FakeProc())

    def on(self, matcher, proc_or_fn):
        """Register a matcher (callable taking cmd list) + FakeProc (or callable → FakeProc)."""
        self._scripts.append((matcher, proc_or_fn))
        return self

    def __call__(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        stdin_data: str | None = None,
    ) -> FakeProc:
        self.calls.append(RunnerCall(cmd=list(cmd), cwd=cwd, env=env))
        for matcher, proc_or_fn in self._scripts:
            try:
                if matcher(cmd):
                    if callable(proc_or_fn):
                        return proc_or_fn(cmd, self.calls)
                    return proc_or_fn
            except Exception:
                continue
        return self._default

    def cmds_matching(self, predicate) -> list[list[str]]:
        return [c.cmd for c in self.calls if predicate(c.cmd)]


class FakeGh:
    """Fake GhClient — scripts PR meta + sticky-comments."""

    def __init__(
        self,
        *,
        pr_meta: dict | None = None,
        sticky_bodies: dict[str, str] | None = None,
        comments: list[dict] | None = None,
    ) -> None:
        self._pr_meta = pr_meta or {
            "title": "Test PR",
            "body": "",
            "baseRefName": "main",
            "headRefOid": "abc123",
            "headRefName": "feat/test",
            "isDraft": False,
        }
        self._sticky_bodies = sticky_bodies or {}
        self._comments = comments or []
        self.posted_comments: list[dict] = []

    def get_pr(self, pr_number: int) -> dict:
        return dict(self._pr_meta)

    def get_sticky_comment_body(self, pr_number: int, marker: str) -> str | None:
        return self._sticky_bodies.get(marker)

    def list_pr_comments(self, pr_number: int) -> list[dict]:
        return list(self._comments)

    def post_pr_comment(self, pr_number: int, body: str) -> None:
        self.posted_comments.append({"pr": pr_number, "body": body})

    def post_sticky_comment(
        self, *, pr_number: int, marker: str, body: str,
    ) -> None:
        self._sticky_bodies[marker] = body
        self.posted_comments.append({"pr": pr_number, "marker": marker, "body": body})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class ExtractAllowedPathsTests(unittest.TestCase):
    """extract_allowed_paths parst Review-Findings und gibt die erlaubten
    File-Paths zurück, die der Auto-Fixer anfassen darf."""

    def test_extracts_from_backtick_path_line(self) -> None:
        findings_text = """
        - `apps/portal-api/src/routes/finance.ts:42`: N+1 query
        - `packages/shared-ui/src/Button.tsx:10`: hardcoded color
        """
        allowed = auto_fix.extract_allowed_paths(findings_text)
        self.assertIn("apps/portal-api/src/routes/finance.ts", allowed)
        self.assertIn("packages/shared-ui/src/Button.tsx", allowed)

    def test_empty_findings_returns_empty_set(self) -> None:
        allowed = auto_fix.extract_allowed_paths("")
        self.assertEqual(allowed, set())

    def test_dedupes_same_file_multiple_lines(self) -> None:
        findings_text = "`foo.ts:10` and `foo.ts:42` and `foo.ts:99`"
        allowed = auto_fix.extract_allowed_paths(findings_text)
        self.assertEqual(allowed, {"foo.ts"})


class IsPathAllowedTests(unittest.TestCase):
    """is_path_allowed: nur Files in der Findings-Whitelist sind erlaubt.
    Test-Files SIND NICHT erlaubt (Wave 7c-fix, Gemini-Finding PR #39):
    sonst könnte ein prompt-injizierter Claude erst Tests aufweichen,
    dann eine Vulnerability einschmuggeln, und der post-fix-Test-Gate
    würde nichts bemerken."""

    def test_direct_match_allowed(self) -> None:
        allowed = {"apps/api/src/routes/finance.ts"}
        self.assertTrue(auto_fix.is_path_allowed(
            "apps/api/src/routes/finance.ts", allowed,
        ))

    def test_corresponding_test_file_blocked(self) -> None:
        # Wave 7c-fix: foo.ts → foo.test.ts ist NICHT mehr erlaubt.
        # Wenn ein Fix Test-Anpassungen braucht, soll auto_fix abbrechen +
        # zur Human-Eskalation übergeben.
        allowed = {"apps/api/src/routes/finance.ts"}
        self.assertFalse(auto_fix.is_path_allowed(
            "apps/api/src/routes/finance.test.ts", allowed,
        ))

    def test_corresponding_spec_file_blocked(self) -> None:
        allowed = {"apps/api/src/routes/finance.ts"}
        self.assertFalse(auto_fix.is_path_allowed(
            "apps/api/src/routes/finance.spec.ts", allowed,
        ))

    def test_tsx_test_file_blocked(self) -> None:
        allowed = {"packages/shared-ui/src/Button.tsx"}
        self.assertFalse(auto_fix.is_path_allowed(
            "packages/shared-ui/src/Button.test.tsx", allowed,
        ))

    def test_unrelated_path_blocked(self) -> None:
        allowed = {"apps/api/src/routes/finance.ts"}
        self.assertFalse(auto_fix.is_path_allowed(
            "apps/api/src/routes/other.ts", allowed,
        ))


class ValidateChangedFilesTests(unittest.TestCase):
    """validate_changed_files läuft nach dem Claude-Edit-Commit und prüft,
    dass die tatsächlich veränderten Files innerhalb der Guard-Rails sind.
    """

    def test_accepts_changes_within_whitelist(self) -> None:
        # Wave 7c-fix: nur Source-Files erlaubt (keine Test-Files mehr).
        changed = ["apps/api/src/routes/finance.ts"]
        allowed = {"apps/api/src/routes/finance.ts"}
        ok, reason = auto_fix.validate_changed_files(
            changed=changed, allowed_paths=allowed, max_files=10,
        )
        self.assertTrue(ok, reason)

    def test_blocks_when_too_many_files(self) -> None:
        changed = [f"file{i}.ts" for i in range(15)]
        allowed = {f"file{i}.ts" for i in range(15)}
        ok, reason = auto_fix.validate_changed_files(
            changed=changed, allowed_paths=allowed, max_files=10,
        )
        self.assertFalse(ok)
        self.assertIn("10", reason)

    def test_blocks_when_path_outside_whitelist(self) -> None:
        changed = ["apps/api/src/routes/finance.ts", "apps/api/src/routes/sneaky.ts"]
        allowed = {"apps/api/src/routes/finance.ts"}
        ok, reason = auto_fix.validate_changed_files(
            changed=changed, allowed_paths=allowed, max_files=10,
        )
        self.assertFalse(ok)
        self.assertIn("sneaky.ts", reason)

    def test_empty_changed_list_accepted(self) -> None:
        # Claude hat nichts geändert — das ist kein Guard-Violation (auto_fix
        # erkennt "kein Diff" separat als Normalfall).
        ok, _ = auto_fix.validate_changed_files(
            changed=[], allowed_paths={"foo.ts"}, max_files=10,
        )
        self.assertTrue(ok)


class RunAutoFixHappyPathTests(unittest.TestCase):
    """End-to-end Happy-Path: Claude editiert Whitelisted File, typecheck +
    tests grün, push erfolgreich."""

    def _runner_happy(self) -> FakeRunner:
        r = FakeRunner()
        # git rev-parse HEAD — erst "abc", nach claude "def"
        state = {"head_calls": 0}

        def head_sha(cmd, calls):
            state["head_calls"] += 1
            if state["head_calls"] == 1:
                return FakeProc(stdout="abc\n", returncode=0)
            return FakeProc(stdout="def\n", returncode=0)

        r.on(
            lambda c: len(c) >= 3 and c[0] == "git" and c[1] == "rev-parse" and c[2] == "HEAD",
            head_sha,
        )
        # claude CLI — success
        r.on(lambda c: c and c[0] == "claude", FakeProc(returncode=0, stdout="done"))
        # git diff --name-only — Claude hat finance.ts geändert
        r.on(
            lambda c: c[:2] == ["git", "diff"] and "--name-only" in c,
            FakeProc(returncode=0, stdout="apps/api/src/routes/finance.ts\n"),
        )
        # pnpm typecheck — grün
        r.on(
            lambda c: c[0] == "pnpm" and "typecheck" in c,
            FakeProc(returncode=0),
        )
        # pnpm test --changed — grün
        r.on(
            lambda c: c[0] == "pnpm" and "test" in c,
            FakeProc(returncode=0),
        )
        # git push — grün
        r.on(
            lambda c: c[0] == "git" and c[1] == "push",
            FakeProc(returncode=0),
        )
        return r

    def test_happy_path_writes_commit_and_pushes(self) -> None:
        r = self._runner_happy()
        gh = FakeGh(
            pr_meta={"headRefOid": "abc", "headRefName": "feat/x", "baseRefName": "main",
                     "title": "Test", "body": "", "isDraft": False},
            sticky_bodies={
                "<!-- nexus-ai-review-code -->":
                    "`apps/api/src/routes/finance.ts:42`: N+1 issue",
            },
        )
        result = auto_fix.run_auto_fix(
            pr_number=42,
            reason="telegram-button",
            context_hint="Cursor flagged N+1",
            gh=gh,
            runner=r,
            worktree=Path("/tmp/fake-worktree"),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.files_changed, 1)
        self.assertFalse(result.guard_violated)
        # Verify git push was called
        push_calls = r.cmds_matching(lambda c: c[0] == "git" and c[1] == "push")
        self.assertEqual(len(push_calls), 1)

    def test_test_file_modification_aborts_with_guard_violation(self) -> None:
        # Wave 7c-fix: Wenn Claude eine .test.ts-Datei ändert, MUSS auto_fix
        # mit guard_violated=True abbrechen — sonst könnte ein prompt-
        # injizierter Claude die Tests aufweichen + Vulnerability einbauen.
        r = FakeRunner()
        state = {"head_calls": 0}

        def head_sha(cmd, calls):
            state["head_calls"] += 1
            return FakeProc(stdout="abc\n" if state["head_calls"] == 1 else "def\n")

        r.on(
            lambda c: len(c) >= 3 and c[:3] == ["git", "rev-parse", "HEAD"],
            head_sha,
        )
        r.on(lambda c: c and c[0] == "claude", FakeProc(returncode=0))
        # Claude ändert source + corresponding test file
        r.on(
            lambda c: c[:2] == ["git", "diff"] and "--name-only" in c,
            FakeProc(returncode=0,
                     stdout="apps/api/src/routes/finance.ts\napps/api/src/routes/finance.test.ts\n"),
        )
        gh = FakeGh(
            sticky_bodies={
                "<!-- nexus-ai-review-code -->":
                    "`apps/api/src/routes/finance.ts:42`: N+1 issue",
            },
        )
        result = auto_fix.run_auto_fix(
            pr_number=42, reason="test", context_hint="",
            gh=gh, runner=r, worktree=Path("/tmp/fake"),
        )
        self.assertFalse(result.success)
        self.assertTrue(result.guard_violated)
        self.assertIn("finance.test.ts", (result.error or ""))

    def test_returns_failure_when_typecheck_fails(self) -> None:
        r = self._runner_happy()
        # Override typecheck to fail
        r._scripts = [
            (m, p) for m, p in r._scripts
            if not (callable(m) and m(["pnpm", "-w", "typecheck"]))
        ]
        r.on(
            lambda c: c[0] == "pnpm" and "typecheck" in c,
            FakeProc(returncode=1, stderr="type error in finance.ts"),
        )
        gh = FakeGh(
            sticky_bodies={
                "<!-- nexus-ai-review-code -->":
                    "`apps/api/src/routes/finance.ts:42`: N+1 issue",
            },
        )
        result = auto_fix.run_auto_fix(
            pr_number=42, reason="test", context_hint="test",
            gh=gh, runner=r, worktree=Path("/tmp/fake"),
        )
        self.assertFalse(result.success)
        self.assertIn("typecheck", (result.error or "").lower())
        # No push on typecheck fail
        self.assertEqual(
            len(r.cmds_matching(lambda c: c[0] == "git" and c[1] == "push")), 0,
        )

    def test_returns_failure_when_guard_violated_too_many_files(self) -> None:
        r = FakeRunner()
        state = {"head_calls": 0}

        def head_sha(cmd, calls):
            state["head_calls"] += 1
            return FakeProc(stdout="abc\n" if state["head_calls"] == 1 else "def\n")

        r.on(
            lambda c: len(c) >= 3 and c[:3] == ["git", "rev-parse", "HEAD"],
            head_sha,
        )
        r.on(lambda c: c and c[0] == "claude", FakeProc(returncode=0))
        # Claude hat 15 Files geändert
        many_files = "\n".join(f"file{i}.ts" for i in range(15))
        r.on(
            lambda c: c[:2] == ["git", "diff"] and "--name-only" in c,
            FakeProc(returncode=0, stdout=many_files),
        )

        gh = FakeGh(
            sticky_bodies={
                "<!-- nexus-ai-review-code -->":
                    " ".join(f"`file{i}.ts:1`" for i in range(15)),
            },
        )
        result = auto_fix.run_auto_fix(
            pr_number=42, reason="test", context_hint="",
            gh=gh, runner=r, worktree=Path("/tmp/fake"),
            max_files=10,
        )
        self.assertFalse(result.success)
        self.assertTrue(result.guard_violated)
        # Reset MUST have been called (rollback after guard violation)
        resets = r.cmds_matching(
            lambda c: c[:3] == ["git", "reset", "--hard"],
        )
        self.assertGreaterEqual(len(resets), 1)

    def test_returns_failure_when_path_outside_whitelist(self) -> None:
        r = FakeRunner()
        state = {"head_calls": 0}

        def head_sha(cmd, calls):
            state["head_calls"] += 1
            return FakeProc(stdout="abc\n" if state["head_calls"] == 1 else "def\n")

        r.on(
            lambda c: len(c) >= 3 and c[:3] == ["git", "rev-parse", "HEAD"],
            head_sha,
        )
        r.on(lambda c: c and c[0] == "claude", FakeProc(returncode=0))
        # Claude hat ein File geändert, das NICHT in den Findings steht
        r.on(
            lambda c: c[:2] == ["git", "diff"] and "--name-only" in c,
            FakeProc(returncode=0, stdout="unrelated/file.ts\n"),
        )
        gh = FakeGh(
            sticky_bodies={
                "<!-- nexus-ai-review-code -->":
                    "`allowed/file.ts:42`: issue",
            },
        )
        result = auto_fix.run_auto_fix(
            pr_number=42, reason="test", context_hint="",
            gh=gh, runner=r, worktree=Path("/tmp/fake"),
        )
        self.assertFalse(result.success)
        self.assertTrue(result.guard_violated)
        self.assertIn("unrelated", (result.error or ""))

    def test_no_diff_is_no_op_not_failure(self) -> None:
        # Claude entschied, keine Änderung nötig — result.success=True,
        # files_changed=0, kein push.
        r = FakeRunner()
        r.on(
            lambda c: len(c) >= 3 and c[:3] == ["git", "rev-parse", "HEAD"],
            FakeProc(stdout="abc\n"),  # HEAD bleibt gleich
        )
        r.on(lambda c: c and c[0] == "claude", FakeProc(returncode=0))
        r.on(
            lambda c: c[:2] == ["git", "diff"] and "--name-only" in c,
            FakeProc(returncode=0, stdout=""),  # kein diff
        )
        gh = FakeGh(
            sticky_bodies={"<!-- nexus-ai-review-code -->": "`foo.ts:1`"},
        )
        result = auto_fix.run_auto_fix(
            pr_number=42, reason="test", context_hint="",
            gh=gh, runner=r, worktree=Path("/tmp/fake"),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.files_changed, 0)
        # Kein push ohne Diff
        self.assertEqual(
            len(r.cmds_matching(lambda c: c[0] == "git" and c[1] == "push")), 0,
        )


class BuildFixPromptTests(unittest.TestCase):
    """build_auto_fix_prompt rendert einen strukturierten Claude-Prompt."""

    def test_includes_pr_number_reason_and_findings(self) -> None:
        prompt = auto_fix.build_auto_fix_prompt(
            pr_number=42,
            reason="telegram-button",
            context_hint="Cursor flagged N+1 query",
            findings_text="`src/routes/x.ts:42`: N+1 query",
            base_branch="main",
        )
        self.assertIn("42", prompt)
        self.assertIn("telegram-button", prompt)
        self.assertIn("Cursor flagged N+1", prompt)
        self.assertIn("src/routes/x.ts:42", prompt)
        self.assertIn("main", prompt)
        # Enthält die Guard-Rail-Vermittlung an Claude
        self.assertIn("typecheck", prompt.lower())
        self.assertIn("ai-fix", prompt.lower())


class CollectFindingsTextTests(unittest.TestCase):
    """_collect_findings_text sammelt alle Sticky-Comment-Bodies."""

    def test_returns_empty_when_gh_has_no_getter(self) -> None:
        # Duck-type: Objekt ohne get_sticky_comment_body → leer zurück
        class MinimalGh:
            def get_pr(self, pr_number: int) -> dict:
                return {}

            def post_pr_comment(self, pr_number: int, body: str) -> None:
                pass

        gh = MinimalGh()
        result = auto_fix._collect_findings_text(gh, 42)  # type: ignore[arg-type]
        self.assertEqual(result, "")

    def test_aggregates_multiple_stage_bodies(self) -> None:
        gh = FakeGh(
            sticky_bodies={
                "<!-- nexus-ai-review-code -->": "code finding",
                "<!-- nexus-ai-review-security -->": "sec finding",
            },
        )
        result = auto_fix._collect_findings_text(gh, 42)
        self.assertIn("code finding", result)
        self.assertIn("sec finding", result)

    def test_skips_empty_stage_bodies(self) -> None:
        gh = FakeGh(
            sticky_bodies={
                "<!-- nexus-ai-review-code -->": "  ",  # nur whitespace
                "<!-- nexus-ai-review-security -->": "real finding",
            },
        )
        result = auto_fix._collect_findings_text(gh, 42)
        self.assertNotIn("Stage: code", result)
        self.assertIn("Stage: security", result)


class GitHelpersTests(unittest.TestCase):
    """Interne Git-Helfer: _git_head_sha, _git_changed_files, _rollback."""

    def test_git_head_sha_returns_none_on_runner_exception(self) -> None:
        def exploding_runner(cmd, **kwargs):
            raise RuntimeError("git not found")

        result = auto_fix._git_head_sha(exploding_runner, Path("/tmp"))
        self.assertIsNone(result)

    def test_git_head_sha_returns_none_on_nonzero_returncode(self) -> None:
        r = FakeRunner()
        r.on(
            lambda c: c[0] == "git" and c[1] == "rev-parse",
            FakeProc(returncode=1, stderr="not a git repo"),
        )
        result = auto_fix._git_head_sha(r, Path("/tmp"))
        self.assertIsNone(result)

    def test_git_head_sha_returns_none_on_empty_stdout(self) -> None:
        r = FakeRunner()
        r.on(
            lambda c: c[0] == "git" and c[1] == "rev-parse",
            FakeProc(returncode=0, stdout=""),
        )
        result = auto_fix._git_head_sha(r, Path("/tmp"))
        self.assertIsNone(result)

    def test_git_changed_files_returns_empty_on_exception(self) -> None:
        def exploding_runner(cmd, **kwargs):
            raise RuntimeError("git exploded")

        result = auto_fix._git_changed_files(
            exploding_runner, Path("/tmp"), diff_range=None,
        )
        self.assertEqual(result, [])

    def test_git_changed_files_returns_empty_on_nonzero_exit(self) -> None:
        r = FakeRunner()
        r.on(
            lambda c: c[0] == "git" and c[1] == "diff",
            FakeProc(returncode=1, stderr="error"),
        )
        result = auto_fix._git_changed_files(r, Path("/tmp"), diff_range=None)
        self.assertEqual(result, [])

    def test_rollback_is_noop_when_head_before_is_none(self) -> None:
        r = FakeRunner()
        # Should not call any git commands
        auto_fix._rollback(r, Path("/tmp"), None)
        self.assertEqual(len(r.calls), 0)

    def test_rollback_silently_ignores_runner_exceptions(self) -> None:
        def exploding_runner(cmd, **kwargs):
            raise RuntimeError("git reset failed")

        # Should not raise
        auto_fix._rollback(exploding_runner, Path("/tmp"), "abc123")


class RunAutoFixEdgeCaseTests(unittest.TestCase):
    """Edge-cases und Error-Paths für run_auto_fix."""

    def test_get_pr_exception_returns_failure(self) -> None:
        class BrokenGh:
            def get_pr(self, pr_number: int) -> dict:
                raise RuntimeError("GitHub API down")

            def post_pr_comment(self, pr_number: int, body: str) -> None:
                pass

        r = FakeRunner()
        result = auto_fix.run_auto_fix(
            pr_number=42, reason="test", context_hint="",
            gh=BrokenGh(),  # type: ignore[arg-type]
            runner=r, worktree=Path("/tmp/fake"),
        )
        self.assertFalse(result.success)
        self.assertIn("get_pr(42) failed", (result.error or ""))

    def test_claude_timeout_triggers_rollback_and_failure(self) -> None:
        # FakeRunner swallows exceptions in its try/except, so wir verwenden
        # eine direkte Callable-Implementierung die TimeoutExpired selektiv wirft.
        reset_calls: list[list[str]] = []
        head_calls = {"n": 0}

        def smart_runner(cmd: list[str], **kwargs: Any) -> FakeProc:
            if cmd[:3] == ["git", "rev-parse", "HEAD"]:
                head_calls["n"] += 1
                return FakeProc(stdout="abc\n", returncode=0)
            if cmd and cmd[0] == "claude":
                raise subprocess.TimeoutExpired(cmd, 480)
            if cmd[:3] == ["git", "reset", "--hard"]:
                reset_calls.append(cmd)
                return FakeProc(returncode=0)
            return FakeProc(returncode=0)

        gh = FakeGh()
        result = auto_fix.run_auto_fix(
            pr_number=42, reason="test", context_hint="",
            gh=gh, runner=smart_runner, worktree=Path("/tmp/fake"),
        )
        self.assertFalse(result.success)
        self.assertIn("timeout", (result.error or "").lower())
        # Rollback muss aufgerufen worden sein
        self.assertGreaterEqual(len(reset_calls), 1)

    def test_claude_nonzero_exit_returns_failure(self) -> None:
        r = FakeRunner()
        r.on(
            lambda c: c[0] == "git" and c[1] == "rev-parse",
            FakeProc(stdout="abc\n", returncode=0),
        )
        r.on(
            lambda c: c and c[0] == "claude",
            FakeProc(returncode=1, stderr="Claude error output"),
        )
        r.on(
            lambda c: c[:3] == ["git", "reset", "--hard"],
            FakeProc(returncode=0),
        )
        gh = FakeGh()
        result = auto_fix.run_auto_fix(
            pr_number=42, reason="test", context_hint="",
            gh=gh, runner=r, worktree=Path("/tmp/fake"),
        )
        self.assertFalse(result.success)
        self.assertIn("Claude CLI exit 1", (result.error or ""))

    def test_skip_push_returns_success_without_pushing(self) -> None:
        """skip_push=True darf nach validem Fix keinen git push aufrufen."""
        r = FakeRunner()
        state = {"head_calls": 0}

        def head_sha(cmd, calls):
            state["head_calls"] += 1
            return FakeProc(stdout="abc\n" if state["head_calls"] == 1 else "def\n")

        r.on(
            lambda c: c[0] == "git" and c[1] == "rev-parse" and c[2] == "HEAD",
            head_sha,
        )
        r.on(lambda c: c and c[0] == "claude", FakeProc(returncode=0))
        r.on(
            lambda c: c[:2] == ["git", "diff"] and "--name-only" in c,
            FakeProc(returncode=0, stdout="apps/api/src/routes/finance.ts\n"),
        )
        r.on(lambda c: c[0] == "pnpm" and "typecheck" in c, FakeProc(returncode=0))
        r.on(lambda c: c[0] == "pnpm" and "test" in c, FakeProc(returncode=0))
        gh = FakeGh(
            sticky_bodies={
                "<!-- nexus-ai-review-code -->":
                    "`apps/api/src/routes/finance.ts:42`: issue",
            },
        )
        result = auto_fix.run_auto_fix(
            pr_number=42, reason="dry-run-test", context_hint="",
            gh=gh, runner=r, worktree=Path("/tmp/fake"),
            skip_push=True,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.files_changed, 1)
        push_calls = r.cmds_matching(lambda c: c[0] == "git" and c[1] == "push")
        self.assertEqual(len(push_calls), 0)

    def test_git_push_failure_returns_failure(self) -> None:
        r = FakeRunner()
        state = {"head_calls": 0}

        def head_sha(cmd, calls):
            state["head_calls"] += 1
            return FakeProc(stdout="abc\n" if state["head_calls"] == 1 else "def\n")

        r.on(
            lambda c: c[0] == "git" and c[1] == "rev-parse" and c[2] == "HEAD",
            head_sha,
        )
        r.on(lambda c: c and c[0] == "claude", FakeProc(returncode=0))
        r.on(
            lambda c: c[:2] == ["git", "diff"] and "--name-only" in c,
            FakeProc(returncode=0, stdout="apps/api/src/routes/finance.ts\n"),
        )
        r.on(lambda c: c[0] == "pnpm" and "typecheck" in c, FakeProc(returncode=0))
        r.on(lambda c: c[0] == "pnpm" and "test" in c, FakeProc(returncode=0))
        r.on(
            lambda c: c[0] == "git" and c[1] == "push",
            FakeProc(returncode=1, stderr="push rejected"),
        )
        r.on(lambda c: c[:3] == ["git", "reset", "--hard"], FakeProc(returncode=0))
        gh = FakeGh(
            sticky_bodies={
                "<!-- nexus-ai-review-code -->":
                    "`apps/api/src/routes/finance.ts:42`: issue",
            },
        )
        result = auto_fix.run_auto_fix(
            pr_number=42, reason="test", context_hint="",
            gh=gh, runner=r, worktree=Path("/tmp/fake"),
        )
        self.assertFalse(result.success)
        self.assertIn("git push failed", (result.error or ""))

    def test_post_fix_tests_failure_triggers_rollback(self) -> None:
        r = FakeRunner()
        state = {"head_calls": 0}

        def head_sha(cmd, calls):
            state["head_calls"] += 1
            return FakeProc(stdout="abc\n" if state["head_calls"] == 1 else "def\n")

        r.on(
            lambda c: c[0] == "git" and c[1] == "rev-parse" and c[2] == "HEAD",
            head_sha,
        )
        r.on(lambda c: c and c[0] == "claude", FakeProc(returncode=0))
        r.on(
            lambda c: c[:2] == ["git", "diff"] and "--name-only" in c,
            FakeProc(returncode=0, stdout="apps/api/src/routes/finance.ts\n"),
        )
        r.on(lambda c: c[0] == "pnpm" and "typecheck" in c, FakeProc(returncode=0))
        r.on(
            lambda c: c[0] == "pnpm" and "test" in c,
            FakeProc(returncode=1, stderr="1 test failed"),
        )
        r.on(lambda c: c[:3] == ["git", "reset", "--hard"], FakeProc(returncode=0))
        gh = FakeGh(
            sticky_bodies={
                "<!-- nexus-ai-review-code -->":
                    "`apps/api/src/routes/finance.ts:42`: issue",
            },
        )
        result = auto_fix.run_auto_fix(
            pr_number=42, reason="test", context_hint="",
            gh=gh, runner=r, worktree=Path("/tmp/fake"),
        )
        self.assertFalse(result.success)
        self.assertIn("post-fix tests failed", (result.error or ""))
        resets = r.cmds_matching(lambda c: c[:3] == ["git", "reset", "--hard"])
        self.assertGreaterEqual(len(resets), 1)


class MainCliTests(unittest.TestCase):
    """CLI-Entrypoint main() — Error- und Success-Paths."""

    def test_main_returns_1_on_missing_required_args(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            auto_fix.main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_main_help_exits_zero(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            auto_fix.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
