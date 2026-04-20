"""Issue-Context-Extractor + Task-Block-Builder (Wave 3).

Portiert aus ai-portal/scripts/ai-review/issue_context.py.

Zweck: Die AI-Reviewer sehen heute nur den Diff. Das reicht um „ist der
Code korrekt?" zu beantworten, aber nicht „ist das auch das, was gefordert
war?". Wave 3 scrapt den PR-Body nach GitHub-Closing-Keywords, fetcht die
verlinkten Issues via `gh`-CLI, extrahiert Acceptance-Criteria-Checkboxes
und prependet den Reviewer-Prompts einen klaren `## Task Context`-Block.

Strikt Read-Only — kein Issue wird mutiert. Bei fehlendem Link oder
gelöschtem Issue fällt der Block still auf PR-Body-only zurück (kein
Crash-Pfad).

Dedup-Entscheidung:
- extract_issue_numbers() ist NICHT durch issue_parser.extract_issue_refs()
  ersetzbar: issue_context nutzt eine umfassendere Keyword-Liste
  (closed, fixed, resolved), einen Cap von 3 Issues und `Ref` (Singular).
  issue_parser.extract_issue_refs() hat keinen Cap und dient dem
  AC-Validation-Pfad (alle Refs zählen). Beide Funktionen bleiben parallel
  erhalten — unterschiedliche Semantik.
- extract_acceptance_criteria() (Checkbox-Pattern `- [ ]`) ist NICHT
  redundant zu issue_parser.parse_gherkin_ac() (Gherkin-Code-Blöcke).
  Erstere ist für klassische GitHub-Checkboxen, letztere für strukturierte
  Gherkin-Szenarien. Beide werden in der Pipeline benötigt.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable


# GitHub-Closing-Keywords (offizielle Liste, case-insensitive).
# Quelle: https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue
_CLOSING_KEYWORD = r"(?:closes?|closed|fix(?:es|ed)?|resolv(?:es|ed)?|refs?)"

# Matcht `Closes #42`, `Fix #13`, `Refs: #100` usw. auf Wort-Grenze, damit
# `#123` in einem Code-Block oder Prosa-Satz NICHT misclassified wird.
_ISSUE_LINK_RE = re.compile(
    rf"\b{_CLOSING_KEYWORD}\s*:?\s*#(\d+)",
    re.IGNORECASE,
)

# Markdown-Checkbox-Pattern in Issue-Bodies. `- [ ]` oder `- [x]`/`- [X]`.
_CHECKBOX_RE = re.compile(r"^\s*-\s*\[[ xX]\]\s+(.+?)\s*$", re.MULTILINE)

# Prompt-Limits — damit der Task-Context-Block die LLM-Kontext-Fenster
# nicht sprengt. 2 KB pro Issue-Body ist genug für Titel + AC + Überblick.
_MAX_ISSUES = 3
_MAX_PR_BODY_CHARS = 4_000
_MAX_ISSUE_BODY_CHARS = 2_000


# ---------------------------------------------------------------------------
# Extraction (pure)
# ---------------------------------------------------------------------------

def extract_issue_numbers(pr_body: str | None) -> list[int]:
    """Scrapt PR-Body nach Closing-Keywords, dedupliziert, cappt bei 3."""
    if not pr_body:
        return []
    seen: set[int] = set()
    ordered: list[int] = []
    for match in _ISSUE_LINK_RE.finditer(pr_body):
        n = int(match.group(1))
        if n not in seen:
            seen.add(n)
            ordered.append(n)
        if len(ordered) >= _MAX_ISSUES:
            break
    return ordered


def extract_acceptance_criteria(issue_body: str | None) -> list[str]:
    """Extrahiert Checkbox-Zeilen (`- [ ]` oder `- [x]`) als Criteria-Liste.

    Unterscheidet sich bewusst von issue_parser.parse_gherkin_ac():
    Hier werden klassische GitHub-Markdown-Checkboxen extrahiert (nicht
    Gherkin-Code-Blöcke). Beide Patterns werden in der Pipeline benötigt.
    """
    if not issue_body:
        return []
    return [m.group(1).strip() for m in _CHECKBOX_RE.finditer(issue_body)]


# ---------------------------------------------------------------------------
# Fetching (gh CLI)
# ---------------------------------------------------------------------------

def _gh_view_issue(n: int) -> dict | None:
    """Default-Fetcher: ruft `gh issue view N --json title,body,labels,state`."""
    try:
        result = subprocess.run(  # noqa: S603 — cmd is a trusted list[str]
            [
                "gh", "issue", "view", str(n),
                "--json", "number,title,body,labels,state",
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    # `labels` kommt als [{"name":"foo"}, …] — auf Strings mappen
    labels_raw = data.get("labels", [])
    if isinstance(labels_raw, list):
        labels = [lb.get("name", "") if isinstance(lb, dict) else str(lb) for lb in labels_raw]
    else:
        labels = []
    return {
        "number": data.get("number", n),
        "title": data.get("title", ""),
        "body": data.get("body", ""),
        "labels": labels,
        "state": data.get("state", "open"),
    }


FetchFn = Callable[[int], dict[str, Any] | None]


def fetch_issues(
    numbers: list[int],
    *,
    fetch_fn: FetchFn = _gh_view_issue,
) -> list[dict[str, Any]]:
    """Fetcht jede Issue-Nummer; skippt fehlerhafte Calls still."""
    issues: list[dict[str, Any]] = []
    for n in numbers:
        issue = fetch_fn(n)
        if issue is not None:
            issues.append(issue)
    return issues


# ---------------------------------------------------------------------------
# Prompt-Block-Builder
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… (truncated at {limit} chars)"


def build_task_context_block(
    *,
    pr_title: str,
    pr_body: str,
    linked_issues: list[dict[str, Any]],
) -> str:
    """Rendert den `## Task Context`-Block für den Reviewer-Prompt.

    Leerer String wenn weder PR-Inhalt noch Issues vorliegen — dann wird
    der Block NICHT prependet (kein Noise im Prompt).
    """
    if not pr_title and not (pr_body or "").strip() and not linked_issues:
        return ""

    parts: list[str] = ["## Task Context", ""]
    if pr_title:
        parts.append(f"**PR Title:** {pr_title}")
    if pr_body and pr_body.strip():
        parts += [
            "",
            "**PR Body:**",
            "",
            _truncate(pr_body.strip(), _MAX_PR_BODY_CHARS),
        ]

    if linked_issues:
        parts += ["", "**Linked Issues:**", ""]
        for issue in linked_issues:
            n = issue.get("number", "?")
            title = issue.get("title", "(untitled)")
            labels = issue.get("labels", [])
            state = issue.get("state", "?")
            labels_str = ", ".join(labels) if labels else ""
            head = f"- **#{n}** [{state}]"
            if labels_str:
                head += f" [labels: {labels_str}]"
            head += f" — {title}"
            parts.append(head)

            body = issue.get("body", "") or ""
            if body.strip():
                parts += [
                    "",
                    _truncate(body.strip(), _MAX_ISSUE_BODY_CHARS),
                    "",
                ]

            criteria = extract_acceptance_criteria(body)
            if criteria:
                parts += ["", "**Acceptance Criteria:**", ""]
                for ac in criteria:
                    parts.append(f"- [ ] {ac}")

    parts += [
        "",
        "---",
        "",
        "**Review charter:** Beurteile NICHT nur ob der Code technisch korrekt ist, "
        "sondern auch ob die Umsetzung der oben genannten Anforderung entspricht. "
        "Bei einer klaren Diskrepanz zwischen Task und Umsetzung: `verdict: \"hard\"` "
        "+ finding mit severity `error` und msg beginnend mit `scope-mismatch:`.",
        "",
    ]
    return "\n".join(parts)


def build_task_context(
    *,
    pr_title: str,
    pr_body: str,
    fetch_fn: FetchFn = _gh_view_issue,
) -> str:
    """Ein-Aufruf-Wrapper: Extract → Fetch → Render."""
    numbers = extract_issue_numbers(pr_body)
    issues = fetch_issues(numbers, fetch_fn=fetch_fn)
    return build_task_context_block(
        pr_title=pr_title, pr_body=pr_body, linked_issues=issues,
    )


# ---------------------------------------------------------------------------
# Re-export public API — explicit allowlist
# ---------------------------------------------------------------------------

__all__ = [
    "FetchFn",
    "extract_issue_numbers",
    "extract_acceptance_criteria",
    "fetch_issues",
    "build_task_context_block",
    "build_task_context",
]
