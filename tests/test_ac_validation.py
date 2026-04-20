"""TDD-Tests für Stage 5 AC-Validation.

Stage-Verhalten:
- Pre-Checks ohne LLM: kein Issue-Ref → fail; keine ACs → fail; keine Test-Files → warning.
- Mit LLM-Judge (injected): pro AC eine Coverage-Entscheidung, Score = abgedeckt/gesamt * 10.
- Waiver: liefert score=10, confidence=1.0, waived=True (wird via CLI `/ai-review ac-waiver` gesetzt).
"""

from ai_review_pipeline.issue_parser import AcceptanceCriterion
from ai_review_pipeline.stages.ac_validation import (
    ACValidationInput,
    Finding,
    StageResult,
    validate_ac_coverage,
)


def _ac(scenario: str, issue: int = 1) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        scenario=scenario,
        given=["precondition"],
        when=["action"],
        then=["outcome"],
        issue_number=issue,
    )


class TestPreChecks:
    def test_no_issue_refs_fails_closed(self) -> None:
        inp = ACValidationInput(
            pr_body="No refs here",
            linked_issues={},
            changed_files=["src/foo.py"],
            pr_diff="",
        )
        result = validate_ac_coverage(inp, llm_judge=None)
        assert result.score == 1
        assert result.confidence == 1.0
        assert any(f.severity == "error" and "issue" in f.message.lower() for f in result.findings)

    def test_linked_issue_but_no_acs_fails_closed(self) -> None:
        inp = ACValidationInput(
            pr_body="Closes #42",
            linked_issues={42: []},
            changed_files=["src/foo.py"],
            pr_diff="",
        )
        result = validate_ac_coverage(inp, llm_judge=None)
        assert result.score == 2
        assert any("no gherkin ac" in f.message.lower() for f in result.findings)

    def test_acs_present_but_no_test_files_warns(self) -> None:
        inp = ACValidationInput(
            pr_body="Closes #42",
            linked_issues={42: [_ac("Some scenario", issue=42)]},
            changed_files=["src/foo.py"],
            pr_diff="",
        )
        # Ohne LLM-Judge: mit Test-Files-Warning, aber ACs sind da → soft result.
        result = validate_ac_coverage(inp, llm_judge=None)
        assert any(f.severity == "warning" and "test" in f.message.lower() for f in result.findings)


class TestWithLlmJudge:
    def test_all_acs_covered_yields_score_10(self) -> None:
        acs = [_ac("A", 42), _ac("B", 42)]
        inp = ACValidationInput(
            pr_body="Closes #42",
            linked_issues={42: acs},
            changed_files=["tests/test_foo.py", "src/foo.py"],
            pr_diff="diff content",
        )

        def judge(ac: AcceptanceCriterion, _: str) -> tuple[bool, str, float]:
            return True, f"matched test for {ac.scenario}", 0.9

        result = validate_ac_coverage(inp, llm_judge=judge)
        assert result.score == 10
        assert result.confidence == 0.9
        assert len([f for f in result.findings if f.severity == "error"]) == 0

    def test_half_covered_yields_score_5(self) -> None:
        acs = [_ac("A", 42), _ac("B", 42)]
        inp = ACValidationInput(
            pr_body="Closes #42",
            linked_issues={42: acs},
            changed_files=["tests/test_foo.py"],
            pr_diff="diff",
        )

        def judge(ac: AcceptanceCriterion, _: str) -> tuple[bool, str, float]:
            return (ac.scenario == "A", f"{ac.scenario} judged", 0.8)

        result = validate_ac_coverage(inp, llm_judge=judge)
        assert result.score == 5
        uncovered = [f for f in result.findings if f.severity == "error"]
        assert len(uncovered) == 1
        assert "B" in uncovered[0].message

    def test_none_covered_yields_score_1(self) -> None:
        acs = [_ac("A", 42)]
        inp = ACValidationInput(
            pr_body="Closes #42",
            linked_issues={42: acs},
            changed_files=["tests/test_foo.py"],
            pr_diff="",
        )

        def judge(_ac: AcceptanceCriterion, _: str) -> tuple[bool, str, float]:
            return False, "no matching test", 0.7

        result = validate_ac_coverage(inp, llm_judge=judge)
        assert result.score == 1

    def test_confidence_is_min_across_judgements(self) -> None:
        acs = [_ac("A", 42), _ac("B", 42), _ac("C", 42)]
        inp = ACValidationInput(
            pr_body="Closes #42",
            linked_issues={42: acs},
            changed_files=["tests/test_foo.py"],
            pr_diff="",
        )
        confidences = iter([0.95, 0.6, 0.8])

        def judge(_ac: AcceptanceCriterion, _: str) -> tuple[bool, str, float]:
            return True, "ok", next(confidences)

        result = validate_ac_coverage(inp, llm_judge=judge)
        assert result.confidence == 0.6


class TestWaiver:
    def test_valid_waiver_yields_score_10_waived(self) -> None:
        inp = ACValidationInput(
            pr_body="Closes #42",
            linked_issues={},
            changed_files=[],
            pr_diff="",
            waiver_reason="Pipeline-Bootstrap: first bootstrap PR for agent-stack self-hosting",
        )
        result = validate_ac_coverage(inp, llm_judge=None)
        assert result.score == 10
        assert result.waived is True
        assert result.waiver_reason.startswith("Pipeline-Bootstrap")

    def test_short_waiver_reason_rejected(self) -> None:
        inp = ACValidationInput(
            pr_body="Closes #42",
            linked_issues={},
            changed_files=[],
            pr_diff="",
            waiver_reason="too short",
        )
        result = validate_ac_coverage(inp, llm_judge=None)
        assert result.waived is False
        assert result.score < 10
        assert any("waiver" in f.message.lower() for f in result.findings)


class TestStageResultShape:
    def test_result_has_required_stage_name(self) -> None:
        inp = ACValidationInput(pr_body="", linked_issues={}, changed_files=[], pr_diff="")
        result = validate_ac_coverage(inp, llm_judge=None)
        assert isinstance(result, StageResult)
        assert result.stage == "ac_validation"
        assert 1 <= result.score <= 10
        assert 0.0 <= result.confidence <= 1.0
        assert all(isinstance(f, Finding) for f in result.findings)
