"""Stage 1b: Second code review via Cursor Agent CLI (composer-2 default).

Portiert aus ai-portal/scripts/ai-review/cursor_review.py.

Läuft parallel zu Stage 1 (Codex GPT-5), bringt Model-Vendor-Diversität:
- Codex = GPT-5 (OpenAI)
- Cursor = composer-2 (Cursor's eigenes Modell, trainiert für Code)

Zweck: Doppel-Check mit unabhängigem Modell erhöht Issue-Coverage (siehe
Perplexity-Research 2026-04: Multi-Agent-Union hebt Coverage von ~40% auf
~41.5% mit komplementärem Signal).

Stage postet `ai-review/code-cursor`. Der Consensus-Aggregator kombiniert
diesen Status mit `ai-review/code` via `resolve_code_consensus`:
- beide green  → code-consensus green
- Disagreement → code-consensus failure (fail-safe)
- ein skipped  → der andere entscheidet

Wie Stage 2/3 (security, design) läuft dieser Review READ-ONLY — keine
Fix-Loop-Commits. Cursor-findings eskalieren zum Menschen ODER lassen sich
vom Stage-1-Fixer mit-adressieren.

Cursor-CLI-Annahmen:
  - Binary-Name: `cursor-agent`
  - Flags: `--print --force --output-format json --model <model> <prompt>`
  - Model-Default: `composer-2` (Cursor's eigenes Modell)
  - Auth-OAuth-Pfad: kein API-Key-Env nötig; Cursor-CLI nutzt OAuth-Token
    aus ~/.cursor/auth.json (gecacht nach `cursor-agent --auth-login`).
    Auf dem Self-Hosted GitHub-Runner wird das Token vorab injiziert.

TODO (Wave 4b): stage.py noch nicht auf main — StageConfig, build_arg_parser
und run_stage sind als Stubs implementiert bis stage.py gemergt wird.

Run locally:
    python3 -m ai_review_pipeline.stages.cursor_review --pr 42 --skip-fix-loop
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ai_review_pipeline import common  # noqa: I001

# ---------------------------------------------------------------------------
# TODO (Wave 4b): stage.py stub — ersetzen sobald stage.py auf main landet.
#
# Das folgende ist ein minimaler Stub damit dieses Modul importierbar und
# testbar ist, OHNE dass stage.py bereits in der Pipeline vorhanden ist.
# Die echte stage.py aus ai-portal/scripts/ai-review/stage.py wird in
# Wave 4b in src/ai_review_pipeline/stage.py portiert.
# ---------------------------------------------------------------------------

try:
    from ai_review_pipeline import stage  # type: ignore[import]
except ImportError:
    # Stub-Implementierung für Wave 4b — stage.py noch nicht auf main.
    import argparse
    from collections.abc import Callable
    from dataclasses import dataclass

    @dataclass  # type: ignore[no-redef]
    class _StageConfig:  # noqa: N801
        """Minimaler Stub — ersetzt durch stage.StageConfig in Wave 4b."""
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

    class _StageStub:  # noqa: N801
        """Namespace-Stub für stage.StageConfig / stage.run_stage / stage.build_arg_parser."""

        StageConfig = _StageConfig  # type: ignore[assignment]

        @staticmethod
        def build_arg_parser(stage_name: str) -> argparse.ArgumentParser:
            ap = argparse.ArgumentParser(description=f"AI {stage_name} review stage")
            ap.add_argument("--pr", type=int, required=True, help="PR number")
            ap.add_argument("--skip-preflight", action="store_true")
            ap.add_argument(
                "--skip-fix-loop", action="store_true",
                help="Only run the initial review — don't enter claude-fix loop.",
            )
            ap.add_argument(
                "--max-iterations", type=int, default=2,
                help="reifung-v2: Default 2 (Circuit-Breaker).",
            )
            return ap

        @staticmethod
        def run_stage(
            cfg: Any,
            *,
            pr_number: int,
            skip_preflight: bool = False,
            skip_fix_loop: bool = False,
            max_iterations: int = 2,
            gh: Any = None,
        ) -> int:
            """Stub: wird durch echte stage.run_stage in Wave 4b ersetzt."""
            raise NotImplementedError(
                "stage.run_stage nicht verfügbar — stage.py noch nicht auf main (Wave 4b). "
                "Stub durch echte Implementierung ersetzen."
            )

    stage = _StageStub()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Stage-Reviewer-Funktion
# ---------------------------------------------------------------------------

def _cursor_reviewer(prompt: str, worktree: Path, base_branch: str) -> str:
    # composer-2 ist Cursors eigenes Modell — gibt maximale Vendor-Diversität
    # gegenüber Codex (GPT-5). Für reines Code-Review genügt die Standard-Variante.
    return common.run_cursor(
        prompt=prompt,
        worktree=worktree,
        base_branch=base_branch,
        model="composer-2",
    )


# ---------------------------------------------------------------------------
# Stage-Konfiguration
# ---------------------------------------------------------------------------

CONFIG = stage.StageConfig(
    name="code-cursor",
    status_context=common.STATUS_CODE_CURSOR,
    sticky_marker=common.MARKER_CODE_CURSOR_REVIEW,
    title_prefix="🐱 AI Code Review (Cursor)",
    prompt_file="cursor_review.md",
    reviewer_label="Cursor (composer-2)",
    ok_sentinels=("LGTM",),
    reviewer_fn=_cursor_reviewer,
    path_filter=None,  # läuft auf jedem PR (zweiter Code-Reviewer)
    # Unser Prompt ist scoring-strict — Abwesenheit von Findings ist NICHT
    # automatisch clean, wir erwarten einen validen JSON-Block.
    treat_no_findings_as_clean=False,
)


# ---------------------------------------------------------------------------
# CLI-Einstiegspunkt
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = stage.build_arg_parser("code-cursor")
    args = ap.parse_args(argv)
    # Wave 2c/5a: Cursor ist review-only (skip_fix_loop=True by default), damit
    # keine Fix-Commit-Races mit Stage 1 (Codex) passieren. Nur Stage 1 darf
    # ClaudeFixer-Commits pushen.
    return stage.run_stage(
        CONFIG,
        pr_number=args.pr,
        skip_preflight=args.skip_preflight,
        skip_fix_loop=args.skip_fix_loop or True,
        max_iterations=args.max_iterations,
    )


if __name__ == "__main__":
    sys.exit(main())
