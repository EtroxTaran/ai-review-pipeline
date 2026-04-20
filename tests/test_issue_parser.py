"""TDD-Tests für issue_parser.

Regeln:
- Arrange-Act-Assert Pattern
- Jede öffentliche Funktion bekommt Happy-Path + mindestens 2 Edge-Cases.
- Multi-Issue-Resolver muss `Closes`, `Fixes`, `Resolves`, `Refs` erkennen (case-insensitive).
- Gherkin-Parser muss Given/When/Then/And korrekt clustern; `And`-Step gehört zum vorhergehenden Keyword.
"""

from ai_review_pipeline.issue_parser import (
    AcceptanceCriterion,
    extract_issue_refs,
    parse_gherkin_ac,
)


class TestExtractIssueRefs:
    def test_single_closes(self) -> None:
        body = "Implements login flow.\n\nCloses #42"
        assert extract_issue_refs(body) == [42]

    def test_multiple_mixed_keywords(self) -> None:
        body = """## Summary
This PR covers:

Closes #10, Fixes #11
Refs #12
Resolves #13
"""
        assert extract_issue_refs(body) == [10, 11, 12, 13]

    def test_case_insensitive(self) -> None:
        body = "closes #1\nCLOSES #2\nfixes #3"
        assert extract_issue_refs(body) == [1, 2, 3]

    def test_ignores_unrelated_hashes(self) -> None:
        body = "See header #introduction, not a ref. Closes #99."
        assert extract_issue_refs(body) == [99]

    def test_ignores_markdown_headings(self) -> None:
        body = "## #1 Introduction\nCloses #55"
        assert extract_issue_refs(body) == [55]

    def test_empty_body(self) -> None:
        assert extract_issue_refs("") == []

    def test_none_body(self) -> None:
        assert extract_issue_refs(None) == []

    def test_deduplicates_preserving_order(self) -> None:
        body = "Closes #7\nRefs #7\nCloses #7"
        assert extract_issue_refs(body) == [7]

    def test_cross_repo_ref_ignored(self) -> None:
        # Owner/repo#N-Form gehört nicht ins selbe Repo — skip.
        body = "Closes etrox/other-repo#42\nCloses #43"
        assert extract_issue_refs(body) == [43]


class TestParseGherkinAc:
    def test_single_scenario_given_when_then(self) -> None:
        body = """## Description
Password reset needs to be solid.

## Acceptance Criteria

```gherkin
Scenario: User resets password via email
  Given a registered user with email "alice@example.com"
  When the user requests a password reset
  Then a reset email is sent within 30 seconds
```
"""
        acs = parse_gherkin_ac(body, issue_number=101)
        assert len(acs) == 1
        ac = acs[0]
        assert isinstance(ac, AcceptanceCriterion)
        assert ac.scenario == "User resets password via email"
        assert ac.given == ['a registered user with email "alice@example.com"']
        assert ac.when == ["the user requests a password reset"]
        assert ac.then == ["a reset email is sent within 30 seconds"]
        assert ac.issue_number == 101

    def test_multiple_scenarios(self) -> None:
        body = """## Acceptance Criteria

```gherkin
Scenario: Login succeeds with correct credentials
  Given a user with valid credentials
  When the user submits the login form
  Then they are redirected to the dashboard

Scenario: Login fails with wrong password
  Given a user with valid credentials
  When the user submits a wrong password
  Then an error "Invalid credentials" is shown
```
"""
        acs = parse_gherkin_ac(body, issue_number=202)
        assert len(acs) == 2
        assert acs[0].scenario == "Login succeeds with correct credentials"
        assert acs[1].scenario == "Login fails with wrong password"
        assert acs[1].then == ['an error "Invalid credentials" is shown']

    def test_and_clauses_attach_to_prior_keyword(self) -> None:
        body = """## Acceptance Criteria

```gherkin
Scenario: Checkout with discount
  Given an empty cart
  And the user is logged in
  When a product is added
  And a discount code is applied
  Then the total reflects the discount
  And a confirmation is shown
```
"""
        acs = parse_gherkin_ac(body, issue_number=303)
        assert len(acs) == 1
        ac = acs[0]
        assert ac.given == ["an empty cart", "the user is logged in"]
        assert ac.when == ["a product is added", "a discount code is applied"]
        assert ac.then == ["the total reflects the discount", "a confirmation is shown"]

    def test_no_ac_section_returns_empty(self) -> None:
        body = "## Description\nJust some text, no AC here."
        assert parse_gherkin_ac(body, issue_number=1) == []

    def test_ac_section_without_gherkin_block_returns_empty(self) -> None:
        body = """## Acceptance Criteria

- The user can log in.
- Errors show nicely.
"""
        # Strict-mode: ohne gherkin-Codefence wird nichts geparst.
        assert parse_gherkin_ac(body, issue_number=1) == []

    def test_gherkin_block_without_ac_header_still_parsed(self) -> None:
        # Issue-Form-Template könnte textarea ohne Header wrappen.
        body = """```gherkin
Scenario: A
  Given x
  When y
  Then z
```"""
        acs = parse_gherkin_ac(body, issue_number=5)
        assert len(acs) == 1
        assert acs[0].scenario == "A"

    def test_malformed_scenario_without_then_raises(self) -> None:
        body = """```gherkin
Scenario: Broken
  Given x
  When y
```"""
        acs = parse_gherkin_ac(body, issue_number=1)
        # Strict: ohne Then ist kein valides AC → verwerfen.
        assert acs == []

    def test_issue_form_template_shape(self) -> None:
        # Form-Template fügt üblicherweise "### Acceptance Criteria" als Header ein
        body = """### Summary
Feature.

### Acceptance Criteria

```gherkin
Scenario: Basic flow
  Given a precondition
  When an action happens
  Then the outcome is observed
```
"""
        acs = parse_gherkin_ac(body, issue_number=42)
        assert len(acs) == 1
        assert acs[0].given == ["a precondition"]
        assert acs[0].when == ["an action happens"]
        assert acs[0].then == ["the outcome is observed"]

    def test_multiple_gherkin_blocks(self) -> None:
        body = """```gherkin
Scenario: First
  Given a
  When b
  Then c
```

Some text between.

```gherkin
Scenario: Second
  Given d
  When e
  Then f
```
"""
        acs = parse_gherkin_ac(body, issue_number=9)
        assert len(acs) == 2
        assert acs[0].scenario == "First"
        assert acs[1].scenario == "Second"
