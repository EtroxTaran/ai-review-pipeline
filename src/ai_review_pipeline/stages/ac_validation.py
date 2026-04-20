"""Stage 5 — Acceptance-Criteria-Validation.

Input: PR-Body, resolved linked issues mit geparsten ACs, changed files, diff.
Output: StageResult mit Score 1-10, Findings, Confidence.

Verhalten:
- Waiver (reason ≥30 chars) → score 10, waived=True (Audit via waiver_reason).
- Keine linked_issues → fail-closed, score 1.
- Linked issues aber 0 ACs → fail-closed, score 2.
- ACs vorhanden, keine Test-Files im Diff → warning (Score kommt vom Judge, default 3 ohne judge).
- ACs + judge: score = round(covered/total * 10), Confidence = min der judge-Confidences.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ai_review_pipeline.issue_parser import AcceptanceCriterion

LlmJudge = Callable[[AcceptanceCriterion, str], tuple[bool, str, float]]
"""(ac, diff_context) -> (covered: bool, reason: str, confidence: 0..1)."""

MIN_WAIVER_LENGTH = 30


@dataclass
class Finding:
    severity: str  # "info" | "warning" | "error"
    message: str
    context: dict = field(default_factory=dict)


@dataclass
class StageResult:
    stage: str
    score: int
    confidence: float
    findings: list[Finding] = field(default_factory=list)
    waived: bool = False
    waiver_reason: str | None = None


@dataclass
class ACValidationInput:
    pr_body: str
    linked_issues: dict[int, list[AcceptanceCriterion]]
    changed_files: list[str]
    pr_diff: str
    waiver_reason: str | None = None


def validate_ac_coverage(
    inp: ACValidationInput,
    llm_judge: LlmJudge | None,
) -> StageResult:
    """Reine Coverage-Validation.

    `llm_judge` ist optional — ohne Judge macht die Stage nur die Pre-Checks
    und liefert einen soft Score (damit Tests deterministisch bleiben).
    In Produktion wird der Judge von der Pipeline (Codex/Claude) injiziert.
    """
    if inp.waiver_reason:
        return _handle_waiver(inp.waiver_reason)

    all_acs: list[AcceptanceCriterion] = _flatten_acs(inp.linked_issues)

    if not inp.linked_issues:
        return StageResult(
            stage="ac_validation",
            score=1,
            confidence=1.0,
            findings=[
                Finding(
                    severity="error",
                    message="PR has no linked issue (Closes/Refs #N). AC coverage cannot be validated.",
                )
            ],
        )

    if not all_acs:
        return StageResult(
            stage="ac_validation",
            score=2,
            confidence=1.0,
            findings=[
                Finding(
                    severity="error",
                    message="Linked issue(s) contain no Gherkin AC blocks. Cannot validate coverage.",
                    context={"issues": list(inp.linked_issues.keys())},
                )
            ],
        )

    findings: list[Finding] = []
    test_file_warning = _maybe_test_file_warning(inp.changed_files)
    if test_file_warning:
        findings.append(test_file_warning)

    if llm_judge is None:
        return StageResult(
            stage="ac_validation",
            score=3,
            confidence=0.5,
            findings=findings
            + [
                Finding(
                    severity="info",
                    message="Judge disabled (pre-check only). Enable LLM judge in config.",
                )
            ],
        )

    covered_count = 0
    confidences: list[float] = []
    for ac in all_acs:
        covered, reason, confidence = llm_judge(ac, inp.pr_diff)
        confidences.append(confidence)
        if covered:
            covered_count += 1
            findings.append(
                Finding(severity="info", message=f"AC covered: {ac.scenario} — {reason}")
            )
        else:
            findings.append(
                Finding(
                    severity="error",
                    message=f"AC not covered: {ac.scenario} — {reason}",
                    context={"issue": ac.issue_number},
                )
            )

    ratio = covered_count / len(all_acs)
    score = max(1, round(ratio * 10))
    return StageResult(
        stage="ac_validation",
        score=score,
        confidence=min(confidences) if confidences else 0.0,
        findings=findings,
    )


def _handle_waiver(reason: str) -> StageResult:
    if len(reason.strip()) < MIN_WAIVER_LENGTH:
        return StageResult(
            stage="ac_validation",
            score=2,
            confidence=1.0,
            findings=[
                Finding(
                    severity="error",
                    message=(
                        f"Waiver reason too short (<{MIN_WAIVER_LENGTH} chars). "
                        "Provide audit-grade justification."
                    ),
                )
            ],
        )
    return StageResult(
        stage="ac_validation",
        score=10,
        confidence=1.0,
        findings=[Finding(severity="info", message=f"Waived: {reason}")],
        waived=True,
        waiver_reason=reason,
    )


def _flatten_acs(
    linked_issues: dict[int, list[AcceptanceCriterion]],
) -> list[AcceptanceCriterion]:
    return [ac for acs in linked_issues.values() for ac in acs]


def _maybe_test_file_warning(changed_files: list[str]) -> Finding | None:
    test_files = [f for f in changed_files if _looks_like_test(f)]
    if test_files:
        return None
    return Finding(
        severity="warning",
        message="No test files appear in changed_files. AC coverage likely incomplete.",
    )


def _looks_like_test(path: str) -> bool:
    lower = path.lower()
    return (
        "test" in lower
        or lower.endswith("_spec.ts")
        or lower.endswith(".spec.ts")
        or "/e2e/" in lower
    )
