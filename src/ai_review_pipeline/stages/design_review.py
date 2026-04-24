"""Stage 3: UI/Design-Konformitätsprüfung via Claude Opus.

Portiert aus ai-portal/scripts/ai-review/design_review.py.

Erzwingt DESIGN.md-Regeln: shadcn/@nexus-ui-Indirektion, Tailwind-Token-only-
Farben, keine raw HTML-Form/Table-Elemente, recharts-3.x-Type-Quirks usw.

Wird übersprungen wenn der PR keine UI-Dateien berührt (Pfad-Filter unten).

Lokal ausführen:
    python3 -m ai_review_pipeline.stages.design_review --pr 42

Wave 4b TODO: stage.py (StageConfig, run_stage, build_arg_parser) ist noch
nicht nach ai-review-pipeline portiert. Diese Datei importiert stage aus dem
Modul-Stub unten; sobald Wave 4b abgeschlossen ist, wird der Stub durch das
echte Modul ersetzt.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ai_review_pipeline import common

# Wave 4b: stage.py ist portiert — direkter Import.
from ai_review_pipeline.stages import stage


# ---------------------------------------------------------------------------
# UI-Pfad-Filter
# ---------------------------------------------------------------------------

# UI-File-Heuristik: Wenn eine geänderte Datei diesen Mustern entspricht, wird
# die Design-Review ausgeführt.
_UI_EXTENSIONS = (".tsx", ".jsx", ".css", ".scss")
_UI_DIR_HINTS = (
    "packages/shared-ui/",
    "apps/portal-shell/src/",
    "plugins/",
)


def _has_ui_changes(changed_files: list[str]) -> bool:
    """Gibt True zurück wenn mindestens eine geänderte Datei UI-relevant ist."""
    for f in changed_files:
        if f.endswith(_UI_EXTENSIONS):
            return True
        if any(hint in f for hint in _UI_DIR_HINTS) and f.endswith((".ts", ".mts")):
            # .ts-Dateien unter Plugin/shared-ui-Dirs — können Design-Tokens referenzieren
            return True
    return False


# ---------------------------------------------------------------------------
# Reviewer-Callable
# ---------------------------------------------------------------------------

def _claude_reviewer(prompt: str, worktree: Path, base_branch: str) -> str:
    """Ruft common.run_claude mit Design-spezifischem Modell auf.

    Modell kommt aus registry/MODEL_REGISTRY.env via resolve_model("design").
    Policy: Opus für Design (hohe Stakes, 1 Call/PR — kein Volume-Concern).
    """
    from ai_review_pipeline import models
    return common.run_claude(
        prompt=prompt,
        worktree=worktree,
        base_branch=base_branch,
        model=models.resolve_model("design"),
    )


# ---------------------------------------------------------------------------
# Stage-Konfiguration
# ---------------------------------------------------------------------------

CONFIG = stage.StageConfig(
    name="design",
    status_context=common.STATUS_DESIGN,
    sticky_marker=common.MARKER_DESIGN_REVIEW,
    title_prefix="AI Design Review",
    prompt_file="design_review.md",
    # Generisch — konkrete Modell-Version kommt aus resolve_model('design'),
    # drift-vermeidend. Kein hardcoded Version-Suffix im Label.
    reviewer_label="Claude (Design)",
    ok_sentinels=("DESIGN-OK",),
    reviewer_fn=_claude_reviewer,
    path_filter=_has_ui_changes,
)


# ---------------------------------------------------------------------------
# Einsprungpunkt
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI-Einsprungpunkt für die Design-Review-Stage.

    Wave 2c: Design = review-only (advisory). Fix-Loop würde mit Stage-1-
    Fix-Commits racen wenn Stages parallel laufen. Design-Findings gehen ins
    PR-UI zum ACKen statt automatisch gefixt zu werden — passt zur advisory-
    Rolle im Consensus-Modell (design-verdict=soft blockiert nicht wenn
    code+security grün sind, weil Consensus nur 2-of-N braucht).
    """
    ap = stage.build_arg_parser("design")
    args = ap.parse_args(argv)
    return stage.run_stage(
        CONFIG,
        pr_number=args.pr,
        skip_preflight=args.skip_preflight,
        # skip_fix_loop ist immer True für Design (advisory-Rolle) —
        # args.skip_fix_loop kann zusätzlich True sein, aber wir erzwingen
        # True unabhängig vom Flag.
        skip_fix_loop=True,
        max_iterations=args.max_iterations,
    )


if __name__ == "__main__":
    sys.exit(main())
