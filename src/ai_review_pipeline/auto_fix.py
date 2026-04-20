"""Cross-Stage Auto-Fix-Agent (Wave 7c).

Getriggert via `workflow_dispatch` aus `.github/workflows/ai-review-auto-fix.yml`.
Aufrufer:
  - Discord-Button "Auto-Fix" (n8n-callback-Workflow, Wave 7b)
  - `/ai-review retry` PR-Comment (delegiert via nachfrage.yml)
  - Manuell via `gh workflow run ai-review-auto-fix.yml`

Flow (single-pass, kein fix-loop — re-review läuft separat via workflow_run):

  1. Resolve PR + Review-Findings aus Sticky-Comments (code, security, design)
  2. Extract whitelisted file-paths aus den Findings
  3. Invoke `claude --permission-mode acceptEdits` mit strukturiertem Prompt
  4. Read `git diff --name-only` → validate guard-rails:
       - max `AUTO_FIX_MAX_FILES` (default 10) Files changed
       - jeder changed path muss in whitelist ODER korrespondierendes
         Test-File zu whitelisted source sein
  5. Post-fix `pnpm typecheck` + `pnpm test --changed origin/<base>` beide grün
     MUSS — sonst rollback, kein push
  6. `git push` → triggert normale Stages automatisch via pull_request sync

Unterschied zu `ClaudeFixer`:
  - ClaudeFixer läuft IN der Stage-Fix-Loop (reviewer fand Problem, Claude fixt,
    Reviewer wird re-run in derselben Stage-Iteration).
  - auto_fix läuft EXTERN als eigener Workflow-Run; Re-Review passiert über die
    reguläre pull_request-sync-Cascade nach dem push.

Identity: `github-actions[bot]` + default GITHUB_TOKEN (kein Bot-Account nötig —
self-hosted Runner auf r2d2 hat OAuth für Claude/Codex/Gemini/Cursor lokal).

Guard-Rails empirisch begründet:
  - 10-File-Cap: typische Review-Findings addressieren 1-3 Files; ein Agent der
    >10 Files anfassen will signalisiert Scope-Creep oder Prompt-Missverständnis.
  - Path-Whitelist: verhindert dass Auto-Fix unrelated Files berührt (z. B.
    Package-Locks, Config-Drifts) die ein separates Review brauchen würden.
  - Post-Fix-Test-Gate: fail-closed — wenn Tests nicht grün sind, darf der Fix
    nicht in die PR (sonst CI rot nach dem push, Auto-Fix wird kontraproduktiv).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from ai_review_pipeline import common


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AUTO_FIX_MAX_FILES = int(os.environ.get("AUTO_FIX_MAX_FILES", "10"))
AUTO_FIX_CLAUDE_TIMEOUT = int(
    os.environ.get("AUTO_FIX_CLAUDE_TIMEOUT", str(common.CLI_FIX_TIMEOUT)),
)

# Sticky-Marker für die 4 Review-Stages (müssen mit common.MARKER_* matchen —
# aber wir duplizieren die String-Konstanten hier um Import-Zyklen zu meiden).
_STICKY_MARKERS = {
    "code":     "<!-- nexus-ai-review-code -->",
    "cursor":   "<!-- nexus-ai-review-code-cursor -->",
    "security": "<!-- nexus-ai-review-security -->",
    "design":   "<!-- nexus-ai-review-design -->",
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class AutoFixResult:
    """Ergebnis eines Auto-Fix-Runs.

    Felder:
      - success: True wenn ein valider Fix gepusht wurde ODER nichts zu tun war.
      - files_changed: Anzahl tatsächlich committeter File-Änderungen.
      - guard_violated: True wenn Max-Files oder Path-Whitelist greifte.
      - error: Menschenlesbare Fehlermeldung bei Fail (None bei success).
    """
    success: bool
    files_changed: int = 0
    guard_violated: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# Gh Protocol (DI — auto_fix erwartet nur diese Methoden)
# ---------------------------------------------------------------------------


class _GhLike(Protocol):
    def get_pr(self, pr_number: int) -> dict: ...

    def get_sticky_comment_body(
        self, pr_number: int, marker: str,
    ) -> str | None: ...

    def post_pr_comment(self, pr_number: int, body: str) -> None: ...


# ---------------------------------------------------------------------------
# Pure helpers — testbar ohne subprocess
# ---------------------------------------------------------------------------


_FINDING_PATH_RE = re.compile(
    r'`([A-Za-z0-9_./\-]+\.(?:ts|tsx|js|jsx|mjs|cjs|css|scss|json|md|yml|yaml|sh|py|surql|sql)):(\d+)`?',
)


def extract_allowed_paths(findings_text: str) -> set[str]:
    """Parst alle `path:line`-Referenzen aus Review-Findings → Set der Pfade.

    Deduped: derselbe Path mit verschiedenen Line-Numbers zählt einmal.
    """
    allowed: set[str] = set()
    for m in _FINDING_PATH_RE.finditer(findings_text or ""):
        allowed.add(m.group(1))
    return allowed


def is_path_allowed(path: str, allowed: set[str]) -> bool:
    """Ist `path` direkt in der Findings-Whitelist?

    Wave 7c-fix (Gemini security finding PR #39): Wir lassen den Auto-Fix
    NICHT in Test-Dateien schreiben — sonst könnte ein prompt-injizierter
    Claude erst die Tests aufweichen und DANN eine Vulnerability einbauen,
    und der post-fix `pnpm test --changed`-Gate würde nichts merken
    (weil die geschwächten Tests grün durchlaufen).

    Konsequenz: Wenn ein Fix Test-Anpassungen braucht, MUSS auto_fix
    abbrechen → Eskalation → Mensch entscheidet, ob die Test-Änderung
    gerechtfertigt ist. Das ist der sichere Default.

    Falls ein Source-File-Fix die Tests rot macht (z. B. neue API-Signatur),
    schlagen die post-fix Tests an, der Worktree wird zurückgerollt — kein
    Push. Das ist der gewünschte fail-closed Pfad.
    """
    return path in allowed


def validate_changed_files(
    *,
    changed: list[str],
    allowed_paths: set[str],
    max_files: int,
) -> tuple[bool, str]:
    """Prüft ob die Liste der veränderten Files die Guard-Rails respektiert.

    Returns (ok, reason). Bei ok=True ist reason leer.
    """
    if not changed:
        return True, ""
    if len(changed) > max_files:
        return False, (
            f"Auto-Fix wollte {len(changed)} Files ändern — max erlaubt {max_files}. "
            f"Abort. (Files: {', '.join(changed[:5])}{'...' if len(changed) > 5 else ''})"
        )
    disallowed = [p for p in changed if not is_path_allowed(p, allowed_paths)]
    if disallowed:
        return False, (
            f"Auto-Fix hat Files außerhalb der Review-Findings-Whitelist geändert: "
            f"{', '.join(disallowed[:5])}{'...' if len(disallowed) > 5 else ''}"
        )
    return True, ""


def build_auto_fix_prompt(
    *,
    pr_number: int,
    reason: str,
    context_hint: str,
    findings_text: str,
    base_branch: str,
) -> str:
    """Rendert den Claude-Prompt für einen Auto-Fix-Pass.

    Bewusst strukturiert gehalten — Claude soll die Findings priorisieren,
    nicht kreativ Scope ausweiten.
    """
    return f"""You are the Auto-Fix-Agent for Nexus Portal PR #{pr_number}.

## Trigger

- **Reason:** {reason}
- **Context-Hint:** {context_hint or "(none)"}
- **Base branch:** {base_branch}

## Review-Findings from AI reviewers

```
{findings_text or "(no findings — investigate the PR context yourself)"}
```

## Your task

1. Read the findings above, prioritize by severity (security > correctness > style).
2. Apply **minimal-invasive** edits — only change files referenced in the
   findings (or their corresponding test files `foo.ts` ↔ `foo.test.ts`).
3. Preserve every existing test — do not weaken assertions.
4. Run `pnpm typecheck` locally. If it fails, fix the type errors too.
5. Run `pnpm test -- --run --changed origin/{base_branch}`. Both MUST be green
   before committing. If you cannot make them green, STOP and leave the
   worktree clean — do NOT commit a broken state.
6. Stage & commit with message:
   `[ai-fix] auto: <concise-summary>`
7. **Do not push** — the surrounding harness handles the push after validating
   guard-rails (max {AUTO_FIX_MAX_FILES} files changed, path-whitelist).

## Constraints (Nexus Portal coding rules)

- TypeScript strict, never `any`, explicit return types.
- Tailwind tokens only (no raw hex / no text-green-*).
- No raw HTML in plugin code (no <table>, <button>, <select>, <textarea>).
- Deutsche Kommentare, englischer Code.
- TDD: jeder neue Logik-Block braucht einen Test vorher.

## If you cannot fix

If the findings are contradictory, unclear, or require human judgment (e.g.
architectural choices, business decisions), do NOT commit anything — just
return a short explanation of why you're punting. The harness will escalate
to the PR author.
"""


# ---------------------------------------------------------------------------
# Gh sticky-comment aggregator (used when GhClient lacks get_sticky_comment_body)
# ---------------------------------------------------------------------------


def _collect_findings_text(gh: _GhLike, pr_number: int) -> str:
    """Sammelt alle Sticky-Comment-Bodies der 4 Review-Stages.

    Duck-types: falls das gh-Objekt kein `get_sticky_comment_body` hat,
    returnen wir leer — der Prompt ist dann "Findings-frei" und Claude
    investigiert den PR-Context selbst.
    """
    getter = getattr(gh, "get_sticky_comment_body", None)
    if getter is None:
        return ""
    parts: list[str] = []
    for label, marker in _STICKY_MARKERS.items():
        body = getter(pr_number, marker) or ""
        if body.strip():
            parts.append(f"### Stage: {label}\n\n{body.strip()}")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def run_auto_fix(
    *,
    pr_number: int,
    reason: str,
    context_hint: str,
    gh: _GhLike,
    runner: common.Runner = common.default_runner,
    worktree: Path,
    model: str = "claude-opus-4-7",
    max_files: int = AUTO_FIX_MAX_FILES,
    skip_push: bool = False,
) -> AutoFixResult:
    """Führt einen einmaligen Auto-Fix-Pass aus.

    Kernablauf:
      1. PR meta + findings sammeln
      2. allowed paths aus findings extrahieren
      3. Claude invoke mit strukturiertem prompt
      4. Diff prüfen gegen Guard-Rails
      5. Post-Fix typecheck + tests
      6. Push
    """
    try:
        pr_meta = gh.get_pr(pr_number)
    except Exception as e:  # noqa: BLE001
        return AutoFixResult(success=False, error=f"get_pr({pr_number}) failed: {e}")

    base_branch = pr_meta.get("baseRefName") or "main"
    head_branch = pr_meta.get("headRefName") or ""

    findings_text = _collect_findings_text(gh, pr_number)
    allowed_paths = extract_allowed_paths(findings_text)

    # Snapshot HEAD — wir vergleichen nachher, ob Claude einen Commit produziert hat
    head_before = _git_head_sha(runner, worktree)

    prompt = build_auto_fix_prompt(
        pr_number=pr_number,
        reason=reason,
        context_hint=context_hint,
        findings_text=findings_text,
        base_branch=base_branch,
    )

    # 1) Claude invocation
    try:
        proc = runner(
            ["claude", "--model", model, "--permission-mode", "acceptEdits",
             "-p", prompt],
            cwd=worktree,
            timeout=AUTO_FIX_CLAUDE_TIMEOUT,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        _rollback(runner, worktree, head_before)
        return AutoFixResult(
            success=False,
            error=f"Claude CLI timeout ({AUTO_FIX_CLAUDE_TIMEOUT}s)",
        )
    if getattr(proc, "returncode", 1) != 0:
        _rollback(runner, worktree, head_before)
        return AutoFixResult(
            success=False,
            error=f"Claude CLI exit {proc.returncode}: "
                  f"{(getattr(proc, 'stderr', '') or '')[:400]}",
        )

    # 2) Was hat Claude geändert?
    head_after = _git_head_sha(runner, worktree)
    diff_range = f"{head_before}..{head_after}" if (
        head_before and head_after and head_before != head_after
    ) else None
    changed = _git_changed_files(runner, worktree, diff_range=diff_range)

    if not changed:
        # Kein Diff — Claude hat bewusst nichts getan (z. B. Findings unklar).
        # Das ist KEIN Fehler (auto_fix soll nicht bluffen), aber auch kein push.
        return AutoFixResult(success=True, files_changed=0)

    # 3) Guard-Rails
    ok, reason_violation = validate_changed_files(
        changed=changed,
        allowed_paths=allowed_paths,
        max_files=max_files,
    )
    if not ok:
        _rollback(runner, worktree, head_before)
        return AutoFixResult(
            success=False,
            files_changed=len(changed),
            guard_violated=True,
            error=reason_violation,
        )

    # 4) Post-Fix: typecheck MUSS grün sein
    tc = runner(
        ["pnpm", "-w", "typecheck"], cwd=worktree,
        timeout=common.PREFLIGHT_TYPECHECK_TIMEOUT,
    )
    if getattr(tc, "returncode", 1) != 0:
        _rollback(runner, worktree, head_before)
        return AutoFixResult(
            success=False,
            files_changed=len(changed),
            error=f"post-fix typecheck failed: "
                  f"{(getattr(tc, 'stderr', '') or '')[:400]}",
        )

    # 5) Post-Fix: test --changed MUSS grün sein
    ts = runner(
        ["pnpm", "-w", "test", "--", "--run", "--changed", f"origin/{base_branch}"],
        cwd=worktree,
        timeout=common.PREFLIGHT_TEST_TIMEOUT,
    )
    if getattr(ts, "returncode", 1) != 0:
        _rollback(runner, worktree, head_before)
        return AutoFixResult(
            success=False,
            files_changed=len(changed),
            error=f"post-fix tests failed: "
                  f"{(getattr(ts, 'stderr', '') or '')[:400]}",
        )

    # 6) Push (unless skip_push for dry-run/tests)
    if skip_push:
        return AutoFixResult(success=True, files_changed=len(changed))

    push_target = head_branch or "HEAD"
    push_refspec = f"HEAD:refs/heads/{push_target}" if head_branch else "HEAD"
    push = runner(
        ["git", "push", "--no-verify", "origin", push_refspec],
        cwd=worktree,
        timeout=180,
    )
    if getattr(push, "returncode", 1) != 0:
        _rollback(runner, worktree, head_before)
        return AutoFixResult(
            success=False,
            files_changed=len(changed),
            error=f"git push failed: {(getattr(push, 'stderr', '') or '')[:400]}",
        )

    return AutoFixResult(success=True, files_changed=len(changed))


# ---------------------------------------------------------------------------
# Git low-level helpers (via DI-Runner)
# ---------------------------------------------------------------------------


def _git_head_sha(runner: common.Runner, worktree: Path) -> str | None:
    try:
        proc = runner(["git", "rev-parse", "HEAD"], cwd=worktree, timeout=15)
    except Exception:
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    sha = (getattr(proc, "stdout", "") or "").strip()
    return sha or None


def _git_changed_files(
    runner: common.Runner,
    worktree: Path,
    *,
    diff_range: str | None,
) -> list[str]:
    """Liefert die Liste der geänderten Files — entweder zwischen zwei SHAs
    (wenn diff_range gesetzt ist) oder working-tree-vs-HEAD."""
    cmd: list[str] = ["git", "diff", "--name-only"]
    if diff_range:
        cmd.append(diff_range)
    try:
        proc = runner(cmd, cwd=worktree, timeout=30)
    except Exception:
        return []
    if getattr(proc, "returncode", 1) != 0:
        return []
    out = getattr(proc, "stdout", "") or ""
    return [line.strip() for line in out.splitlines() if line.strip()]


def _rollback(
    runner: common.Runner, worktree: Path, head_before: str | None,
) -> None:
    """Resetet HEAD auf head_before, damit kein unvalidierter Commit stehen bleibt.

    Best-effort: bei jedem Fehler einfach weiter — wir wollen keinen
    Exception-Ping-Pong in den Error-Paths.
    """
    if not head_before:
        return
    try:
        runner(
            ["git", "reset", "--hard", head_before],
            cwd=worktree, timeout=30,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Auto-Fix-Agent: single-pass Claude-Fix gegen PR-Findings.",
    )
    ap.add_argument("--pr", type=int, required=True,
                    help="PR number to auto-fix")
    ap.add_argument("--reason", required=True,
                    help="Warum wurde der Auto-Fix getriggert (discord-button, "
                         "manual-retry, direct)")
    ap.add_argument("--context-hint", default="",
                    help="Optionale zusätzliche Info für Claude "
                         "(z. B. 'Cursor finding: N+1 in routes/x.ts:42')")
    ap.add_argument("--max-files", type=int, default=AUTO_FIX_MAX_FILES,
                    help=f"Guard-Rail: max changed files (default {AUTO_FIX_MAX_FILES})")
    ap.add_argument("--model", default="claude-opus-4-7")
    ap.add_argument("--dry-run", action="store_true",
                    help="Nimm keinen push vor — nur Fix + validate (für local-test)")
    ap.add_argument("--worktree", type=Path, default=Path.cwd(),
                    help="Working directory (default cwd)")
    args = ap.parse_args(argv)

    gh = common.GhClient()
    result = run_auto_fix(
        pr_number=args.pr,
        reason=args.reason,
        context_hint=args.context_hint,
        gh=gh,
        worktree=args.worktree,
        model=args.model,
        max_files=args.max_files,
        skip_push=args.dry_run,
    )
    if result.success:
        print(
            f"auto-fix ok (files_changed={result.files_changed}"
            f"{' -- dry-run, no push' if args.dry_run else ''})",
        )
        return 0

    print(f"auto-fix failed: {result.error}", file=sys.stderr)
    if result.guard_violated:
        print("guard-rail violation -- no push, rolled back", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
