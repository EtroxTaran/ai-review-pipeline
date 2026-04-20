"""Parser für PR-Issue-Referenzen und Gherkin Acceptance Criteria.

Zwei Kern-APIs:

    extract_issue_refs(pr_body) -> list[int]
        findet `Closes #N`, `Fixes #N`, `Resolves #N`, `Refs #N` im PR-Body,
        case-insensitive, deduped, order-preserved, ignoriert cross-repo-Refs
        (`owner/repo#N`).

    parse_gherkin_ac(issue_body, issue_number) -> list[AcceptanceCriterion]
        parst ```gherkin```-Code-Blocks aus dem Issue-Body, clustert Given/When/Then
        mit And/But als Folgesteps. Verwirft AC ohne Then (strict).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Stichwort + mind. 1 whitespace + #N; Wort-Grenzen + Negative lookbehind gegen `/`
# sorgen dafür, dass `etrox/other-repo#42` nicht matcht.
_ISSUE_REF_RE = re.compile(
    r"(?<![\w/])(?:closes|fixes|resolves|refs)\s+#(\d+)\b",
    re.IGNORECASE,
)

_GHERKIN_BLOCK_RE = re.compile(r"```gherkin\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_SCENARIO_HEADER_RE = re.compile(r"^\s*Scenario(?:\s+Outline)?\s*:\s*(.+?)\s*$", re.IGNORECASE)
_STEP_RE = re.compile(r"^\s*(Given|When|Then|And|But)\s+(.+?)\s*$", re.IGNORECASE)


@dataclass
class AcceptanceCriterion:
    """Ein geparstes Gherkin-Scenario, tied to its source-Issue."""

    scenario: str
    given: list[str] = field(default_factory=list)
    when: list[str] = field(default_factory=list)
    then: list[str] = field(default_factory=list)
    issue_number: int = 0
    raw: str = ""


def extract_issue_refs(pr_body: str | None) -> list[int]:
    """Alle Issue-Nummern aus Closes/Fixes/Resolves/Refs, deduped, order-preserved."""
    if not pr_body:
        return []
    seen: set[int] = set()
    result: list[int] = []
    for match in _ISSUE_REF_RE.finditer(pr_body):
        n = int(match.group(1))
        if n not in seen:
            seen.add(n)
            result.append(n)
    return result


def parse_gherkin_ac(issue_body: str | None, issue_number: int) -> list[AcceptanceCriterion]:
    """Extrahiert Acceptance Criteria aus allen ```gherkin```-Blocks.

    Strict: Scenarios ohne Then werden verworfen. `And`/`But` erben das
    vorherige Given/When/Then-Keyword.
    """
    if not issue_body:
        return []

    acs: list[AcceptanceCriterion] = []
    for block_match in _GHERKIN_BLOCK_RE.finditer(issue_body):
        block = block_match.group(1)
        acs.extend(_parse_block(block, issue_number))
    return acs


def _parse_block(block: str, issue_number: int) -> list[AcceptanceCriterion]:
    current: AcceptanceCriterion | None = None
    current_keyword: str | None = None
    acs: list[AcceptanceCriterion] = []

    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        scenario_match = _SCENARIO_HEADER_RE.match(line)
        if scenario_match:
            if current is not None:
                _append_if_valid(acs, current)
            current = AcceptanceCriterion(
                scenario=scenario_match.group(1).strip(),
                issue_number=issue_number,
            )
            current_keyword = None
            continue

        step_match = _STEP_RE.match(line)
        if not step_match or current is None:
            continue

        keyword = step_match.group(1).capitalize()
        text = step_match.group(2).strip()

        if keyword in ("And", "But"):
            target = current_keyword
        else:
            target = keyword
            current_keyword = keyword

        if target == "Given":
            current.given.append(text)
        elif target == "When":
            current.when.append(text)
        elif target == "Then":
            current.then.append(text)

    if current is not None:
        _append_if_valid(acs, current)
    return acs


def _append_if_valid(acs: list[AcceptanceCriterion], ac: AcceptanceCriterion) -> None:
    if ac.then:
        acs.append(ac)
