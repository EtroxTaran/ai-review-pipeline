"""Pre-flight CI context: pnpm typecheck + vitest --changed before LLM calls.

Zweck: die mechanischen Tooling-Layer laufen BEVOR die teuren LLM-Calls —
ihr Output wird als Kontext in den Review-Prompt injiziert. Die LLM weiß
dann schon, was der Compiler/Vitest gefunden hat, und verschwendet keine
Tokens auf Findings, die der TS-Compiler mechanisch greift.

Lockfile-Drift-Safeguard: wenn der PR `pnpm-lock.yaml` ändert, skippen wir
Preflight sichtbar (statt zu lügen). Das vom Reviewer-Runner gecachte
`node_modules` entspricht dann nicht der PR-Lockfile-Resolution.

Portiert aus ai-portal/scripts/ai-review/preflight.py.
Import-Anpassung: relative `.common`-Imports → `ai_review_pipeline.common`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from ai_review_pipeline.common import (
    MAX_PREFLIGHT_OUTPUT_CHARS,
    PREFLIGHT_TEST_TIMEOUT,
    PREFLIGHT_TYPECHECK_TIMEOUT,
    REPO_ROOT,
    Runner,
    default_runner,
    strip_ansi,
    tail,
)


def _safe_out(proc: Any) -> str:
    return strip_ansi(
        (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    )


def run_preflight(
    worktree: Path,
    base_branch: str,
    *,
    runner: Runner = default_runner,
) -> str:
    """Run typecheck + changed-tests in the worktree, return a Markdown context block."""
    parts: list[str] = []

    # Symlink node_modules from REPO_ROOT (fast-path — avoids a full pnpm install)
    nm_link = worktree / "node_modules"
    if not nm_link.exists():
        try:
            nm_link.symlink_to(REPO_ROOT / "node_modules")
        except OSError as exc:  # pragma: no cover — env-dependent
            parts.append(f"### Setup: ⚠️ node_modules symlink failed ({exc})")

    env = {**os.environ, "NO_COLOR": "1", "CI": "true"}

    # Lockfile drift short-circuit
    try:
        proc = runner(
            ["git", "diff", "--name-only", f"origin/{base_branch}...HEAD"],
            cwd=worktree, timeout=15,
        )
        changed = _safe_out(proc)
    except Exception:
        changed = ""
    if "pnpm-lock.yaml" in changed:
        parts.append(
            "### Pre-Flight: ⚠️ SKIP\n\n"
            "_PR ändert `pnpm-lock.yaml` — gemountete node_modules entsprechen nicht "
            "der neuen Resolution. Mechanische Validation läuft via Husky `pre-push` "
            "+ GitHub Actions `ci.yml`._"
        )
        return "## Pre-Flight CI Context\n\n" + "\n\n".join(parts)

    # 1) typecheck
    try:
        proc = runner(
            ["pnpm", "-w", "typecheck"],
            cwd=worktree, timeout=PREFLIGHT_TYPECHECK_TIMEOUT, env=env,
        )
        out = _safe_out(proc)
        if proc.returncode == 0:
            parts.append("### Typecheck: ✅ PASS")
        else:
            parts.append(
                "### Typecheck: ❌ FAIL\n\n```\n"
                + tail(out, MAX_PREFLIGHT_OUTPUT_CHARS) + "\n```"
            )
    except subprocess.TimeoutExpired:
        parts.append(f"### Typecheck: ⏱️ TIMEOUT (>{PREFLIGHT_TYPECHECK_TIMEOUT}s)")
    except FileNotFoundError:
        parts.append("### Typecheck: ⏭️ SKIP (pnpm nicht im PATH)")

    # 2) vitest --changed (fast subset scoped to the diff)
    try:
        runner(
            ["git", "fetch", "origin",
             f"{base_branch}:refs/remotes/origin/{base_branch}"],
            cwd=worktree, timeout=30,
        )
    except Exception:
        pass

    try:
        proc = runner(
            ["pnpm", "-w", "test", "--", "--run", "--changed", f"origin/{base_branch}"],
            cwd=worktree, timeout=PREFLIGHT_TEST_TIMEOUT, env=env,
        )
        out = _safe_out(proc)
        if proc.returncode == 0:
            parts.append("### Tests (changed): ✅ PASS")
        else:
            parts.append(
                "### Tests (changed): ❌ FAIL\n\n```\n"
                + tail(out, MAX_PREFLIGHT_OUTPUT_CHARS) + "\n```"
            )
    except subprocess.TimeoutExpired:
        parts.append(f"### Tests (changed): ⏱️ TIMEOUT (>{PREFLIGHT_TEST_TIMEOUT}s)")
    except FileNotFoundError:
        parts.append("### Tests (changed): ⏭️ SKIP (pnpm nicht im PATH)")

    return "## Pre-Flight CI Context\n\n" + "\n\n".join(parts)
