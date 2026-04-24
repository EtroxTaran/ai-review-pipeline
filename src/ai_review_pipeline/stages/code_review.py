"""Stage 1: Code review via Codex CLI (GPT-5).

Portiert aus ai-portal/scripts/ai-review/code_review.py.

Focuses on functional correctness + engineering quality (TypeScript strict,
test coverage, conventional commits). Security + design are deliberately
out of scope — separate stages cover those.

Run locally:
    python3 -m ai_review_pipeline.stages.code_review --pr 42

Wave-4b-TODO: `ai_review_pipeline.stages.stage` existiert noch nicht.
  Das Modul wird zur Laufzeit lazy importiert (nicht auf Top-Level),
  damit der package-Import nicht fehlschlägt bevor stage.py portiert ist.
  Sobald stage.py in Wave-4b portiert wurde:
    1. Den lazy `_get_stage()`-Helper entfernen.
    2. Den direkten Import `from ai_review_pipeline.stages import stage` einsetzen.
    3. Den TYPE_CHECKING-Block unten entfernen.
    4. `_STAGE_MODULE`-Cache entfernen.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from ai_review_pipeline import common

# TYPE_CHECKING-Block: nur für statische Analyzer / IDE-Autocomplete.
# Zur Laufzeit wird stage via lazy import geladen (siehe _get_stage()).
# Wave-4b-TODO: nach Portierung von stage.py direkt importieren.
if TYPE_CHECKING:
    import types
    from dataclasses import dataclass as _dataclass  # noqa: F401 — nur für TYPE_CHECKING-Stubs
    from typing import Any


# ---------------------------------------------------------------------------
# Lazy stage-Modul-Accessor (Wave-4b-TODO: nach Portierung entfernen)
# ---------------------------------------------------------------------------

_STAGE_MODULE: "Any | None" = None


def _get_stage() -> "Any":
    """Gibt das `ai_review_pipeline.stages.stage`-Modul zurück (lazy).

    Wirft ImportError mit einem klaren Wave-4b-Hinweis wenn das Modul fehlt.
    Wave-4b-TODO: direkt importieren statt lazy, dann diese Funktion löschen.
    """
    global _STAGE_MODULE
    if _STAGE_MODULE is None:
        try:
            from ai_review_pipeline.stages import stage as _stage_mod  # type: ignore[import-untyped]
            _STAGE_MODULE = _stage_mod
        except ModuleNotFoundError as exc:
            raise ImportError(
                "ai_review_pipeline.stages.stage ist noch nicht portiert. "
                "Wave-4b muss stage.py extrahieren, bevor code_review im Vollbetrieb läuft. "
                "Im Test-Kontext wird stage via sys.modules-Patching injiziert."
            ) from exc
    return _STAGE_MODULE


# ---------------------------------------------------------------------------
# Codex reviewer delegate
# ---------------------------------------------------------------------------

def _codex_reviewer(prompt: str, worktree: "Any", base_branch: str, pr_title: str) -> str:
    """Delegiert an common.run_codex — dünner Wrapper für die StageConfig."""
    return common.run_codex(
        prompt=prompt,
        worktree=worktree,
        base_branch=base_branch,
        pr_title=pr_title,
    )


# ---------------------------------------------------------------------------
# Module-level CONFIG (gebaut beim Import via lazy stage-Zugriff)
# ---------------------------------------------------------------------------

def _build_config() -> "Any":
    """Erstellt das StageConfig-Objekt. Lazy um den Wave-4b-Import-Guard zu respektieren."""
    stage = _get_stage()
    return stage.StageConfig(
        name="code",
        status_context=common.STATUS_CODE,
        sticky_marker=common.MARKER_CODE_REVIEW,
        title_prefix="🤖 AI Code Review",
        prompt_file="code_review.md",
        reviewer_label="Codex",  # Generisch; konkrete Version = codex CLI-Default
        ok_sentinels=("LGTM",),
        reviewer_fn=_codex_reviewer,
        path_filter=None,  # Code-Review läuft immer — kein Path-Filter
        # `codex review` akzeptiert nicht gleichzeitig --base und [PROMPT], also
        # nutzt Codex seinen Default-Review-Prompt (kein LGTM-Sentinel garantiert).
        # Abwesenheit jeglicher parsebarer Findings ist in diesem Stage die
        # Freigabe-Semantik; Security/Design steuern ihren Prompt selbst.
        treat_no_findings_as_clean=True,
    )


CONFIG = _build_config()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI-Einstiegspunkt. Gibt Exit-Code zurück: 0=grün, 1=rot, 2=Fehler."""
    stage = _get_stage()
    ap = stage.build_arg_parser("code")
    args = ap.parse_args(argv)
    return stage.run_stage(
        CONFIG,
        pr_number=args.pr,
        skip_preflight=args.skip_preflight,
        skip_fix_loop=args.skip_fix_loop,
        max_iterations=args.max_iterations,
    )


if __name__ == "__main__":
    sys.exit(main())
