"""Stage 3: Security review via Gemini 2.5 Pro + semgrep SAST.

Portiert aus ai-portal/scripts/ai-review/security_review.py.

Gemini ist der dedizierte Security-Reviewer (Nico: "das beste LLM für Security").
Der LLM-Output wird mit einem semgrep-Diff-Scan als Ground-Truth angereichert,
damit deterministische SAST-Findings cross-checked werden können.

stage.py ist noch NICHT portiert (Wave 4b) — wir nutzen einen lokalen Stub für
StageConfig + build_arg_parser. Das TODO ist explizit markiert.

Run locally:
    python3 -m ai_review_pipeline.stages.security_review --pr 42

Semgrep-CLI-Annahmen:
  - Version ≥1.60 (OSS), installiert via pip oder apt auf dem Runner.
  - Kommando: semgrep scan --config p/default --config p/owasp-top-ten
              --config p/typescript --config p/javascript
              --baseline-ref origin/<base>
              --severity ERROR --severity WARNING --quiet --json
  - Exit 0: keine neuen Findings; Exit 1: Findings gefunden (JSON enthält results).
  - JSON-Schema: {"results": [{path, start.line, check_id, extra.message}, ...], "errors": []}

Gemini-CLI-Annahmen:
  - Modell: gemini-2.5-pro (via common.run_gemini; model-Flag: -m, MUSS vor -p kommen — yargs-Bug).
  - Prompt via `-p <prompt>` Argument (kein stdin).
  - common.run_gemini(prompt=..., worktree=..., base_branch=..., runner=...) kapselt das.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

from ai_review_pipeline import common

# Wave 4b: stage.py ist portiert — direkter Import.
from ai_review_pipeline.stages import stage

# Convenience-Alias damit bestehender Code weiterhin StageConfig, build_arg_parser,
# run_stage direkt referenzieren kann (abwärtskompatibel mit Tests).
StageConfig = stage.StageConfig
build_arg_parser = stage.build_arg_parser
run_stage = stage.run_stage

ReviewerFn = Callable[..., str]
"""(prompt, worktree, base_branch, **kwargs) → reviewer output string."""


# ---------------------------------------------------------------------------
# Semgrep SAST Baseline
# ---------------------------------------------------------------------------

def _run_semgrep_baseline(
    worktree: Path,
    base_branch: str,
    *,
    runner: common.Runner = common.default_runner,
) -> str:
    """Run semgrep against the diff and return a short summary block.

    `--baseline-ref` scopes the scan to new issues introduced by this PR
    (prevents noise from pre-existing code). Semgrep ist ein OSS CLI — wir
    erwarten es auf dem Runner PATH (installiert via pip oder apt).

    Args:
        worktree: Pfad zum isolierten Git-Worktree des PR-Heads.
        base_branch: Basis-Branch (z.B. "main") für --baseline-ref.
        runner: Injected Runner (default: common.default_runner). Tests nutzen FakeRunner.

    Returns:
        Markdown-Block mit Semgrep-Ergebnis — immer als String, nie Exception.
    """
    import os

    try:
        proc = runner(
            [
                "semgrep", "scan",
                "--config", "p/default",
                "--config", "p/owasp-top-ten",
                "--config", "p/typescript",
                "--config", "p/javascript",
                "--baseline-ref", f"origin/{base_branch}",
                "--severity", "ERROR",
                "--severity", "WARNING",
                "--quiet",
                "--json",
            ],
            cwd=worktree,
            timeout=240,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except FileNotFoundError:
        return "### Semgrep Baseline: ⏭️ SKIP (semgrep not in PATH)"
    except subprocess.TimeoutExpired:
        return "### Semgrep Baseline: ⏱️ TIMEOUT (>240s)"

    # Parse JSON results; if empty array → clean.
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return f"### Semgrep Baseline: ⚠️ could not parse output (exit {proc.returncode})"

    results = data.get("results", [])
    if not results:
        return "### Semgrep Baseline: ✅ no new findings vs. base"

    lines = [f"### Semgrep Baseline: ❌ {len(results)} new finding(s)", ""]
    for r in results[:25]:  # cap to keep the prompt small
        path = r.get("path", "?")
        line = r.get("start", {}).get("line", "?")
        check = r.get("check_id", "?")
        msg = r.get("extra", {}).get("message", "")[:160]
        lines.append(f"- `{path}:{line}` [{check}] {msg}")
    if len(results) > 25:
        lines.append(f"- … +{len(results) - 25} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini-Reviewer: injiziert semgrep-Block als Ground-Truth
# ---------------------------------------------------------------------------

def _gemini_reviewer(
    prompt: str,
    worktree: Path,
    base_branch: str,
    *,
    runner: common.Runner = common.default_runner,
) -> str:
    """Gemini-Reviewer mit semgrep-Baseline als Prior Art.

    Injiziert den semgrep-Block VOR dem eigentlichen Review-Prompt, damit
    Gemini deterministische SAST-Findings als Ground-Truth kennt und
    cross-checken kann.

    Args:
        prompt: Der Stage-spezifische Review-Prompt (aus prompts/security_review.md).
        worktree: Pfad zum isolierten Git-Worktree.
        base_branch: Basis-Branch für semgrep --baseline-ref.
        runner: Injected Runner (DI-Seam für Tests). Default: common.default_runner.

    Returns:
        Raw Gemini-Output als String.
    """
    semgrep_block = _run_semgrep_baseline(worktree, base_branch, runner=runner)
    full_prompt = (
        "## Semgrep SAST Baseline (ground-truth prior art)\n\n"
        + semgrep_block
        + "\n\n---\n\n"
        + prompt
    )
    return common.run_gemini(
        prompt=full_prompt,
        worktree=worktree,
        base_branch=base_branch,
        runner=runner,
    )


# ---------------------------------------------------------------------------
# Stage-Konfiguration
# ---------------------------------------------------------------------------

CONFIG = StageConfig(
    name="security",
    status_context=common.STATUS_SECURITY,
    sticky_marker=common.MARKER_SECURITY_REVIEW,
    title_prefix="🔒 AI Security Review",
    prompt_file="security_review.md",
    reviewer_label="Gemini 2.5 Pro (Security)",
    ok_sentinels=("SEC-OK",),
    reviewer_fn=_gemini_reviewer,
    path_filter=None,  # Security-Review läuft auf jedem PR
)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point für die Security-Review-Stage.

    Wave 2c: Security = review-only. Fix-Loop in Security-Stage ist riskant
    (Security-Fixes haben unklare Akzeptanzkriterien, können Attacks "maskieren"
    statt zu lösen) UND würde mit Stage 1's Fix-Loop um Fix-Commits racen, wenn
    Stages parallel laufen. Wir default auf skip_fix_loop=True — Security-
    Findings eskalieren zum Menschen.

    TODO(wave-4b): ap.parse_args → stage.build_arg_parser + stage.run_stage nutzen.
    """
    ap = build_arg_parser("security")
    args = ap.parse_args(argv)
    return run_stage(
        CONFIG,
        pr_number=args.pr,
        skip_preflight=args.skip_preflight,
        skip_fix_loop=args.skip_fix_loop or True,
        max_iterations=args.max_iterations,
    )


if __name__ == "__main__":
    sys.exit(main())
