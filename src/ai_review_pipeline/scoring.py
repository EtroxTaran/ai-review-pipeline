"""Scoring-Parser + Role-Thresholds für den AI-Review-Consensus (Wave 2a).

Reviewer (Codex/Gemini/Claude) antworten ab Wave 2 mit einem strukturierten
JSON-Block wie:

    ```json
    {
      "score": 8,
      "verdict": "green",
      "summary": "…",
      "findings": [{"severity": "warn", "file": "…", "line": 42, "msg": "…"}]
    }
    ```

Dieser Parser extrahiert den Block (auch mit Prosa drumrum), validiert ihn
streng und gibt ein `ScoredVerdict` zurück. Jeder Fehlerpfad ist **fail-closed**:
bei Parse-Problem / fehlenden Keys / ungültigen Werten → `verdict="hard"`,
`score=0`, `parse_failed=True`. Das verhindert, dass ein schlampiger LLM-Output
accidentally als „green" durchgeht.

Role-Thresholds (`verdict_for_role`) kodieren die Locked-Decision vom
Pipeline-Reifung-Plan (hashed-wishing-hopper.md):
- `code` / `design`: ≥8 green · 5–7 soft · <5 hard
- `security`: ≥8 green · ≤7 hard (kein soft-band, Security-Veto)

Portiert aus ai-portal/scripts/ai-review/scoring.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Verdict = Literal["green", "soft", "hard"]
Severity = Literal["info", "warn", "error"]
Role = Literal["code", "security", "design"]

_VALID_VERDICTS: frozenset[str] = frozenset({"green", "soft", "hard"})
_VALID_SEVERITIES: frozenset[str] = frozenset({"info", "warn", "error"})


@dataclass(frozen=True)
class Finding:
    severity: Severity
    file: str
    line: int
    msg: str


@dataclass
class ScoredVerdict:
    """Normalisiertes Reviewer-Ergebnis.

    - `score`: 1–10 bei Erfolg, 0 bei Parse-Fail
    - `verdict`: 'green' | 'soft' | 'hard' — bei Parse-Fail immer 'hard'
    - `summary`: Human-readable (first-level Begründung für Sticky-Comment)
    - `findings`: Liste strukturierter Finds; leer bei Parse-Fail
    - `parse_failed`: True wenn Parser fail-closed geantwortet hat (Monitoring/
      Debug-Signal — der Reviewer hat kein valides JSON geliefert)
    """

    score: int
    verdict: Verdict
    summary: str
    findings: list[Finding] = field(default_factory=list)
    parse_failed: bool = False


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

# Fängt JSON-Block in Markdown-Code-Fence (```json … ```) ODER bare {…}.
# Nicht-greedy damit mehrere Blöcke korrekt zerlegt werden.
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_RE = re.compile(r"(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})", re.DOTALL)


def _extract_json(text: str) -> str | None:
    """Findet den ersten JSON-Block im Text (fenced preferred, dann bare)."""
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1)
    # Bare-Fallback: nur verwenden wenn kein fence-Block da — sonst könnte
    # der Parser versehentlich einen sub-JSON-Ausdruck aus der Prosa nehmen
    m = _BARE_RE.search(text)
    if m:
        return m.group(1)
    return None


def _fail_closed(reason: str) -> ScoredVerdict:
    return ScoredVerdict(
        score=0,
        verdict="hard",
        summary=f"parse-fail: {reason}",
        findings=[],
        parse_failed=True,
    )


def parse_scored_verdict(raw: str) -> ScoredVerdict:
    """Parst den strukturierten Review-Output eines LLM-Reviewers.

    Strenge Validation — jeder Pfad, der nicht sauber als ScoredVerdict
    interpretierbar ist, wird als `verdict="hard"` zurückgegeben.
    """
    block = _extract_json(raw)
    if block is None:
        return _fail_closed("no JSON block found in reviewer output")

    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        return _fail_closed(f"malformed JSON: {exc.msg}")

    if not isinstance(data, dict):
        return _fail_closed("JSON root must be an object")

    # Required keys
    for key in ("score", "verdict", "summary"):
        if key not in data:
            return _fail_closed(f"missing required key: {key}")

    # Score type + range
    score = data["score"]
    if not isinstance(score, int) or isinstance(score, bool):
        return _fail_closed("score must be an integer")
    if not 1 <= score <= 10:
        return _fail_closed(f"score out of range 1–10: {score}")

    # Verdict enum
    verdict = data["verdict"]
    if verdict not in _VALID_VERDICTS:
        return _fail_closed(f"invalid verdict: {verdict!r}")

    # Summary
    summary = data["summary"]
    if not isinstance(summary, str):
        return _fail_closed("summary must be a string")

    # Findings (optional, defaults to [])
    findings_raw = data.get("findings", [])
    if not isinstance(findings_raw, list):
        return _fail_closed("findings must be a list")

    findings: list[Finding] = []
    for idx, f in enumerate(findings_raw):
        if not isinstance(f, dict):
            return _fail_closed(f"finding[{idx}] must be an object")
        severity = f.get("severity", "info")
        if severity not in _VALID_SEVERITIES:
            return _fail_closed(f"finding[{idx}].severity invalid: {severity!r}")
        file_ = f.get("file", "")
        line = f.get("line", 0)
        msg = f.get("msg", "")
        if not isinstance(file_, str) or not isinstance(msg, str):
            return _fail_closed(f"finding[{idx}]: file/msg must be strings")
        if not isinstance(line, int) or isinstance(line, bool):
            return _fail_closed(f"finding[{idx}].line must be an int")
        findings.append(Finding(severity=severity, file=file_, line=line, msg=msg))  # type: ignore[arg-type]

    return ScoredVerdict(
        score=score,
        verdict=verdict,  # type: ignore[arg-type]
        summary=summary,
        findings=findings,
        parse_failed=False,
    )


# ---------------------------------------------------------------------------
# Role-Thresholds
# ---------------------------------------------------------------------------

def verdict_from_score(score: int) -> Verdict:
    """Default-Bands für Code + Design: ≥8 green · 5–7 soft · <5 hard."""
    if score >= 8:
        return "green"
    if score >= 5:
        return "soft"
    return "hard"


def verdict_for_role(score: int, *, role: Role) -> Verdict:
    """Role-aware Threshold.

    Security ist strenger: ≤7 = hard (kein soft-band), weil Security-Soft-Block
    bedeutet „ist wahrscheinlich OK aber knapp" — und das Risiko eines
    knapp-OK-Security-Miss ist asymmetrisch teuer.
    """
    if role == "security":
        return "green" if score >= 8 else "hard"
    return verdict_from_score(score)


# ---------------------------------------------------------------------------
# Re-export public API
# ---------------------------------------------------------------------------

__all__ = [
    "Verdict",
    "Severity",
    "Role",
    "Finding",
    "ScoredVerdict",
    "parse_scored_verdict",
    "verdict_from_score",
    "verdict_for_role",
]
