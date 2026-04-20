"""Unified console-script entry point for the ai-review pipeline.

Subcommands
-----------
  ai-review stage <name>   -- run a specific review stage
  ai-review consensus      -- aggregate + post consensus
  ai-review nachfrage      -- nachfrage/waiver commands (TODO: no main() yet)
  ai-review auto-fix       -- single-pass auto-fix
  ai-review fix-loop       -- iterative fix-loop
  ai-review ac-validate    -- Stage-5 AC-Validation (inline, no LLM judge)
  ai-review metrics        -- metrics summary
  ai-review --version      -- print package version
  ai-review --help         -- print usage

Jeder Subcommand delegiert an das jeweilige Modul-main() und propagiert
den Exit-Code 1:1.  Fail-closed: fehlende Stage = exit 2.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ai_review_pipeline import __version__

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Bekannte Stage-Names → Modul-Pfade
# ---------------------------------------------------------------------------

_STAGE_MAP: dict[str, str] = {
    "code-review": "ai_review_pipeline.stages.code_review",
    "cursor-review": "ai_review_pipeline.stages.cursor_review",
    "security": "ai_review_pipeline.stages.security_review",
    "design": "ai_review_pipeline.stages.design_review",
    "ac-validation": "ai_review_pipeline.stages.ac_validation",
}

_VALID_STAGES = sorted(_STAGE_MAP.keys())


# ---------------------------------------------------------------------------
# Subcommand-Handler
# ---------------------------------------------------------------------------


def _handle_stage(args: argparse.Namespace, remaining: list[str]) -> int:
    """Dispatcht an das jeweilige Stage-Modul.

    ac-validation ist ein Sonderfall: das Modul hat kein main(), daher
    leitet cli.py weiter an `_handle_ac_validate`.
    """
    stage_name: str = args.stage_name
    if stage_name not in _STAGE_MAP:
        print(
            f"Error: unknown stage '{stage_name}'. "
            f"Valid: {', '.join(_VALID_STAGES)}",
            file=sys.stderr,
        )
        return 2

    if stage_name == "ac-validation":
        # ac-validation hat kein main() — über den separaten ac-validate-Subcommand
        # erreichbar. Hier ist es via `stage ac-validation` erreichbar, verhält sich
        # genauso wie `ac-validate` ohne extra Flags.
        print(
            "Note: use 'ai-review ac-validate' for full CLI options. "
            "Falling back to no-op pre-check.",
            file=sys.stderr,
        )
        return 2

    import importlib
    module = importlib.import_module(_STAGE_MAP[stage_name])
    return module.main(remaining)  # type: ignore[attr-defined]


def _handle_consensus(remaining: list[str]) -> int:
    """Delegiert an consensus.main(argv)."""
    from ai_review_pipeline import consensus
    return consensus.main(remaining)


def _handle_nachfrage(remaining: list[str]) -> int:  # noqa: ARG001
    """nachfrage hat kein main() — TODO für Folge-PR."""
    print(
        "Error: 'nachfrage' subcommand is not yet implemented as a CLI entry point. "
        "TODO: implement nachfrage.main() in a follow-up PR.",
        file=sys.stderr,
    )
    return 2


def _handle_auto_fix(remaining: list[str]) -> int:
    """Delegiert an auto_fix.main(argv)."""
    from ai_review_pipeline import auto_fix
    return auto_fix.main(remaining)


def _handle_fix_loop(remaining: list[str]) -> int:
    """Delegiert an fix_loop.main(argv)."""
    from ai_review_pipeline import fix_loop
    return fix_loop.main(remaining)


def _handle_ac_validate(args: argparse.Namespace) -> int:
    """Inline AC-Validation via validate_ac_coverage().

    Liest --pr-body-file, --linked-issues-file (JSON), --changed-files,
    --diff-file, optional --waiver-reason.  Kein LLM-Judge in der CLI-Variante
    (Judge wird von der Pipeline injiziert — hier nur Pre-Check).
    """
    from ai_review_pipeline.issue_parser import parse_gherkin_ac
    from ai_review_pipeline.stages.ac_validation import ACValidationInput, validate_ac_coverage

    # PR-Body lesen
    pr_body = ""
    if args.pr_body_file:
        try:
            pr_body = Path(args.pr_body_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error reading pr-body-file: {exc}", file=sys.stderr)
            return 2

    # Linked-Issues-JSON lesen: { "1": ["issue body text"] } ODER { "1": [] }
    linked_issues_raw: dict[str, list[str]] = {}
    if args.linked_issues_file:
        try:
            raw = json.loads(Path(args.linked_issues_file).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                linked_issues_raw = {str(k): v if isinstance(v, list) else [] for k, v in raw.items()}
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error reading linked-issues-file: {exc}", file=sys.stderr)
            return 2

    # AcceptanceCriterion-Objekte aus Issue-Bodies parsen
    from ai_review_pipeline.stages.ac_validation import ACValidationInput  # noqa: F811 (re-import OK)

    linked_issues = {}
    for issue_str, bodies in linked_issues_raw.items():
        issue_num = int(issue_str)
        acs = []
        for body in bodies:
            acs.extend(parse_gherkin_ac(body, issue_num))
        linked_issues[issue_num] = acs

    # Changed-files
    changed_files: list[str] = []
    if args.changed_files:
        changed_files = [f.strip() for f in args.changed_files.split(",") if f.strip()]

    # Diff
    pr_diff = ""
    if args.diff_file:
        try:
            pr_diff = Path(args.diff_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error reading diff-file: {exc}", file=sys.stderr)
            return 2

    inp = ACValidationInput(
        pr_body=pr_body,
        linked_issues=linked_issues,
        changed_files=changed_files,
        pr_diff=pr_diff,
        waiver_reason=args.waiver_reason or None,
    )

    result = validate_ac_coverage(inp, llm_judge=None)

    # Ausgabe
    status_icon = "✅" if result.score >= 8 else ("⚠️" if result.score >= 5 else "❌")
    print(
        f"{status_icon} AC-Validation: score={result.score}/10, "
        f"confidence={result.confidence:.2f}, waived={result.waived}"
    )
    for finding in result.findings:
        prefix = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}.get(finding.severity, "·")
        print(f"  {prefix} [{finding.severity.upper()}] {finding.message}")

    # Exit-Code: score >=8 = success, sonst failure
    return 0 if result.score >= 8 else 1


def _handle_metrics(remaining: list[str]) -> int:
    """Delegiert an metrics_summary.main(argv)."""
    from ai_review_pipeline import metrics_summary
    return metrics_summary.main(remaining)


# ---------------------------------------------------------------------------
# Argument Parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-review",
        description="Unified console script for the ai-review-pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Subcommands:
  stage <name>      Run a specific stage: code-review | cursor-review | security | design | ac-validation
  consensus         Aggregate + post consensus (requires --sha)
  nachfrage         Process nachfrage/waiver commands [TODO: not yet implemented]
  auto-fix          Single-pass auto-fix (requires --pr --reason)
  fix-loop          Iterative fix-loop (requires --stage --pr-number --summary --worktree --base-branch --branch)
  ac-validate       Stage-5 AC-Validation inline (no LLM judge in CLI mode)
  metrics           Metrics summary (optional: --since --path --json)

Exit codes: 0=success, 1=failure/findings, 2=error/not-implemented
""",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ai-review-pipeline {__version__}",
    )

    subparsers = parser.add_subparsers(dest="subcommand", metavar="<subcommand>")

    # --- stage ---
    stage_parser = subparsers.add_parser(
        "stage",
        help="Run a specific review stage",
        description="Run a specific stage by name. Extra args are forwarded to the stage module.",
    )
    stage_parser.add_argument(
        "stage_name",
        metavar="<name>",
        choices=_VALID_STAGES,
        help=f"Stage name: {' | '.join(_VALID_STAGES)}",
    )

    # --- consensus ---
    subparsers.add_parser(
        "consensus",
        help="Aggregate + post consensus",
        description="Aggregate all stage results and post a consensus status. "
                    "Extra args (--sha, --pr, --target-url) are forwarded to consensus.main().",
        add_help=True,
    )

    # --- nachfrage ---
    subparsers.add_parser(
        "nachfrage",
        help="Process nachfrage/waiver commands [TODO: not yet implemented]",
    )

    # --- auto-fix ---
    subparsers.add_parser(
        "auto-fix",
        help="Single-pass auto-fix (requires --pr --reason)",
        description="Extra args are forwarded to auto_fix.main().",
    )

    # --- fix-loop ---
    subparsers.add_parser(
        "fix-loop",
        help="Iterative fix-loop",
        description="Extra args are forwarded to fix_loop.main().",
    )

    # --- ac-validate ---
    ac_parser = subparsers.add_parser(
        "ac-validate",
        help="Stage-5 AC-Validation inline (no LLM judge in CLI mode)",
        description="Validate Acceptance-Criteria coverage from PR body + linked issue bodies.",
    )
    ac_parser.add_argument(
        "--pr-body-file",
        default=None,
        help="Path to file containing the PR body text",
    )
    ac_parser.add_argument(
        "--linked-issues-file",
        default=None,
        help="Path to JSON file: { issue_number: [issue_body_str, ...] }",
    )
    ac_parser.add_argument(
        "--changed-files",
        default="",
        help="Comma-separated list of changed file paths",
    )
    ac_parser.add_argument(
        "--diff-file",
        default=None,
        help="Path to file containing the PR diff",
    )
    ac_parser.add_argument(
        "--config-file",
        default=".ai-review/config.yaml",
        help="Path to .ai-review/config.yaml (default: .ai-review/config.yaml)",
    )
    ac_parser.add_argument(
        "--waiver-reason",
        default=None,
        help="Waiver reason (≥30 chars) — wenn gesetzt, wird Stage als waived behandelt",
    )

    # --- metrics ---
    subparsers.add_parser(
        "metrics",
        help="Metrics summary",
        description="Extra args (--since, --path, --json) are forwarded to metrics_summary.main().",
    )

    return parser


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `ai-review` console script.

    Parameters
    ----------
    argv:
        Argument list (default: sys.argv[1:]).

    Returns
    -------
    int
        Exit code: 0 = success, 1 = failure/findings, 2 = error/not-implemented.
    """
    parser = _build_parser()

    # Wir parsen nur den ersten Teil bis zum bekannten Subcommand und lassen
    # den Rest ("remaining") ans delegierte Modul weiterlaufen.
    # parse_known_args statt parse_args damit zusätzliche Flags (z.B. --sha, --pr)
    # an die downstream-Modul-main()s durchgereicht werden können.
    args, remaining = parser.parse_known_args(argv)

    if args.subcommand is None:
        parser.print_usage(sys.stderr)
        return 2

    if args.subcommand == "stage":
        return _handle_stage(args, remaining)
    if args.subcommand == "consensus":
        return _handle_consensus(remaining)
    if args.subcommand == "nachfrage":
        return _handle_nachfrage(remaining)
    if args.subcommand == "auto-fix":
        return _handle_auto_fix(remaining)
    if args.subcommand == "fix-loop":
        return _handle_fix_loop(remaining)
    if args.subcommand == "ac-validate":
        return _handle_ac_validate(args)
    if args.subcommand == "metrics":
        return _handle_metrics(remaining)

    # Sollte durch argparse-choices bereits abgefangen sein — defensive fallback
    print(f"Error: unknown subcommand '{args.subcommand}'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
