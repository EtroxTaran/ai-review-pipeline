"""Tests für issue_context.py — Wave 3 Issue-Context-Extractor.

Portiert aus ai-portal/scripts/ai-review/issue_context_test.py.
Testet: extract_issue_numbers, extract_acceptance_criteria,
build_task_context_block, fetch_issues.
"""

from __future__ import annotations

import pytest

from ai_review_pipeline import issue_context


class TestExtractLinkedIssuesRegex:
    """Der Regex muss die GitHub-Keyword-Liste abdecken + dedupen."""

    def test_extracts_closes_single(self) -> None:
        # Arrange
        body = "Fixes build issue.\n\nCloses #42."
        # Act
        result = issue_context.extract_issue_numbers(body)
        # Assert
        assert result == [42]

    def test_extracts_fixes_multiple(self) -> None:
        # Arrange
        body = "Closes #12\nFixes #13\nResolves #14"
        # Act
        result = issue_context.extract_issue_numbers(body)
        # Assert
        assert result == [12, 13, 14]

    def test_extracts_refs_variants(self) -> None:
        # Arrange
        body = "Refs #100 and Ref #101, also References #102"
        # `References #N` ist NICHT eines der GitHub-Closing-Keywords;
        # wir halten uns strikt an die offizielle Liste.
        # Act
        result = issue_context.extract_issue_numbers(body)
        # Assert
        assert result == [100, 101]

    def test_case_insensitive(self) -> None:
        # Arrange
        body = "CLOSES #5\ncloses #6\nCloses #7"
        # Act
        result = issue_context.extract_issue_numbers(body)
        # Assert
        assert result == [5, 6, 7]

    def test_deduplicates(self) -> None:
        # Arrange
        body = "Closes #42\nFixes #42"
        # Act
        result = issue_context.extract_issue_numbers(body)
        # Assert
        assert result == [42]

    def test_ignores_non_issue_hash_patterns(self) -> None:
        # Arrange — `#feature-xyz` oder code-snippets mit #123 in comments
        # dürfen NICHT als Issue-Link interpretiert werden — nur wenn
        # Keyword davorsteht.
        body = "```\nconst x = arr[#123];\n```\nAlso PR #99 merged yesterday."
        # Act
        result = issue_context.extract_issue_numbers(body)
        # Assert
        assert result == []

    def test_caps_at_three_issues(self) -> None:
        # Arrange
        body = "Closes #1 Fixes #2 Resolves #3 Closes #4 Fixes #5"
        # Act — Cap: max 3 Issues, damit der Prompt-Context nicht explodiert
        result = issue_context.extract_issue_numbers(body)
        # Assert
        assert result == [1, 2, 3]

    def test_empty_body(self) -> None:
        assert issue_context.extract_issue_numbers("") == []

    def test_none_body(self) -> None:
        # GitHub kann PR-body=None liefern
        assert issue_context.extract_issue_numbers(None) == []


class TestAcceptanceCriteriaExtraction:
    """GitHub-Markdown-Checkboxes im Issue-Body = Acceptance-Criteria."""

    def test_extracts_unchecked_criteria(self) -> None:
        # Arrange
        body = """## Acceptance Criteria

- [ ] Endpoint returns 200 when valid
- [ ] Endpoint returns 400 on invalid input
- [ ] Logs a structured warn on failure
"""
        # Act
        criteria = issue_context.extract_acceptance_criteria(body)
        # Assert
        assert len(criteria) == 3
        assert "Endpoint returns 200 when valid" in criteria
        assert "Logs a structured warn on failure" in criteria

    def test_extracts_checked_and_unchecked(self) -> None:
        # Arrange
        body = "- [x] done bit\n- [ ] pending bit\n- [X] capital-X"
        # Act
        criteria = issue_context.extract_acceptance_criteria(body)
        # Assert
        assert len(criteria) == 3

    def test_ignores_non_checkbox_bullets(self) -> None:
        # Arrange
        body = "- plain bullet\n- [ ] real criterion\n* asterisk bullet"
        # Act
        criteria = issue_context.extract_acceptance_criteria(body)
        # Assert
        assert criteria == ["real criterion"]

    def test_empty_body(self) -> None:
        assert issue_context.extract_acceptance_criteria("") == []

    def test_none_body_returns_empty(self) -> None:
        # Error-Path: None statt leerer String
        assert issue_context.extract_acceptance_criteria(None) == []


class TestBuildTaskContextBlock:
    """Endgültiger Prompt-Block wie er dem Reviewer vorangestellt wird."""

    def test_renders_pr_title_and_body(self) -> None:
        # Arrange / Act
        block = issue_context.build_task_context_block(
            pr_title="feat: add X",
            pr_body="Adds X to Y.",
            linked_issues=[],
        )
        # Assert
        assert "## Task Context" in block
        assert "feat: add X" in block
        assert "Adds X to Y." in block

    def test_renders_linked_issues_with_criteria(self) -> None:
        # Arrange
        issues = [
            {
                "number": 42,
                "title": "Wire up version endpoint",
                "body": "## Acceptance Criteria\n- [ ] 200 on /api/version\n- [ ] no auth required",
                "labels": ["feat", "portal-api"],
                "state": "open",
            },
        ]
        # Act
        block = issue_context.build_task_context_block(
            pr_title="feat: version endpoint",
            pr_body="Closes #42",
            linked_issues=issues,
        )
        # Assert
        assert "#42" in block
        assert "Wire up version endpoint" in block
        assert "feat" in block  # label
        assert "Acceptance Criteria" in block
        assert "200 on /api/version" in block

    def test_truncates_long_issue_body(self) -> None:
        # Arrange
        huge_body = "A" * 10_000
        issues = [{"number": 1, "title": "big", "body": huge_body, "labels": [], "state": "open"}]
        # Act
        block = issue_context.build_task_context_block(
            pr_title="t", pr_body="", linked_issues=issues,
        )
        # Assert — Issue-Body sollte auf 2KB gekappt werden
        assert len(block) < 3500

    def test_empty_when_no_pr_and_no_issues(self) -> None:
        # Arrange / Act
        block = issue_context.build_task_context_block(
            pr_title="", pr_body="", linked_issues=[],
        )
        # Assert — Nichts zu sagen → leerer String (Reviewer-Prompt kommt klarer)
        assert block == ""

    def test_includes_review_charter_instruction(self) -> None:
        # Arrange / Act
        block = issue_context.build_task_context_block(
            pr_title="feat: something",
            pr_body="body text",
            linked_issues=[],
        )
        # Assert — Scope-mismatch-Hinweis muss im Block stehen
        assert "scope-mismatch" in block

    def test_issue_without_body_renders_cleanly(self) -> None:
        # Arrange — Issue ohne Body (None oder leerer String)
        issues = [{"number": 7, "title": "empty body issue", "body": None, "labels": [], "state": "open"}]
        # Act
        block = issue_context.build_task_context_block(
            pr_title="fix: something",
            pr_body="Fixes #7",
            linked_issues=issues,
        )
        # Assert — kein Crash, Issue-Titel erscheint
        assert "#7" in block
        assert "empty body issue" in block


class TestFetchIssues:
    """GhFetcher-Abstraktion: echter Call via gh CLI, testable via Stub."""

    def test_calls_fetch_for_each_number(self) -> None:
        # Arrange
        calls: list[int] = []

        def fake_fetch(n: int) -> dict:
            calls.append(n)
            return {"number": n, "title": f"t{n}", "body": "", "labels": [], "state": "open"}

        # Act
        issues = issue_context.fetch_issues([1, 2, 3], fetch_fn=fake_fetch)
        # Assert
        assert calls == [1, 2, 3]
        assert [i["number"] for i in issues] == [1, 2, 3]

    def test_skips_issues_that_fail_to_fetch(self) -> None:
        # Arrange
        def fake_fetch(n: int) -> dict | None:
            if n == 2:
                return None  # z.B. Issue gelöscht oder nicht sichtbar
            return {"number": n, "title": f"t{n}", "body": "", "labels": [], "state": "open"}

        # Act
        issues = issue_context.fetch_issues([1, 2, 3], fetch_fn=fake_fetch)
        # Assert
        assert [i["number"] for i in issues] == [1, 3]

    def test_empty_numbers_returns_empty(self) -> None:
        # Arrange
        called = False

        def fake_fetch(n: int) -> dict:
            nonlocal called
            called = True
            return {}

        # Act
        issues = issue_context.fetch_issues([], fetch_fn=fake_fetch)
        # Assert
        assert issues == []
        assert not called


class TestBuildTaskContext:
    """Wrapper build_task_context: Extract → Fetch → Render."""

    def test_integrates_extract_fetch_render(self) -> None:
        # Arrange
        fetched: list[int] = []

        def fake_fetch(n: int) -> dict:
            fetched.append(n)
            return {
                "number": n,
                "title": f"Issue {n}",
                "body": "- [ ] criterion A",
                "labels": [],
                "state": "open",
            }

        # Act
        block = issue_context.build_task_context(
            pr_title="feat: integrate",
            pr_body="Closes #10\nFixes #20",
            fetch_fn=fake_fetch,
        )
        # Assert
        assert fetched == [10, 20]
        assert "Issue 10" in block
        assert "Issue 20" in block

    def test_no_issue_links_renders_pr_only(self) -> None:
        # Arrange
        def fake_fetch(n: int) -> dict:
            raise AssertionError("should not be called")

        # Act
        block = issue_context.build_task_context(
            pr_title="chore: bump deps",
            pr_body="No issue references here.",
            fetch_fn=fake_fetch,
        )
        # Assert — kein fetch-call, aber PR-Content erscheint
        assert "chore: bump deps" in block
        assert "## Task Context" in block
