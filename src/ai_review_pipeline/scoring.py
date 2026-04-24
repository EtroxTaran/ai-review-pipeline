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


# ---------------------------------------------------------------------------
# Robustness: LLM-Quirks-Recovery
# ---------------------------------------------------------------------------

# Regex zum Entfernen von `// ... \n` line comments außerhalb von Strings.
# Konservativ: matcht `//` nur wenn nicht direkt nach einem String-Ende (d.h.
# wir ignorieren die Möglichkeit, dass `//` in einem String-Value steckt —
# das ist fail-safe: im schlimmsten Fall bleibt ein valider String unverändert,
# weil kein Match triggert, oder ein String-Content mit `//` wird verstümmelt,
# was dann auf den nächsten Recovery-Pass fällt oder fail-closed endet).
_LINE_COMMENT_RE = re.compile(r"(?<!:)//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
# Python-Literale an Wort-Grenzen. `\b` matcht nur True/False/None, nicht
# substrings wie "TrueStory".
_PY_TRUE_RE = re.compile(r"\bTrue\b")
_PY_FALSE_RE = re.compile(r"\bFalse\b")
_PY_NONE_RE = re.compile(r"\bNone\b")


def _strip_comments(block: str) -> str:
    """Entfernt `//` line + `/* */` block comments (häufig bei Cursor/Grok)."""
    block = _BLOCK_COMMENT_RE.sub("", block)
    block = _LINE_COMMENT_RE.sub("", block)
    return block


def _strip_trailing_commas(block: str) -> str:
    """`,}` → `}` und `,]` → `]` (JS-Style, Python dict-Style)."""
    return _TRAILING_COMMA_RE.sub(r"\1", block)


def _normalize_python_literals(block: str) -> str:
    """Python-Literale → JSON-Literale."""
    block = _PY_TRUE_RE.sub("true", block)
    block = _PY_FALSE_RE.sub("false", block)
    block = _PY_NONE_RE.sub("null", block)
    return block


def _swap_single_to_double_quotes(block: str) -> str:
    """Tauscht Single-Quotes gegen Double-Quotes — nur wenn der Block KEINE
    Double-Quotes enthält (sicheres Gesamtsignal: es ist Python-dict-Style).

    Wenn der Block bereits gemischte Quotes hat, lassen wir es — ein
    einfacher Global-Replace würde escaped Apostrophes zerstören.
    """
    if '"' in block:
        return block
    if "'" not in block:
        return block
    return block.replace("'", '"')


_RECOVERY_PASSES: tuple[tuple[str, "callable"], ...] = (  # type: ignore[name-defined]
    ("strip_comments", _strip_comments),
    ("strip_trailing_commas", _strip_trailing_commas),
    ("normalize_python_literals", _normalize_python_literals),
    ("single_to_double_quotes", _swap_single_to_double_quotes),
)


def _try_recover_json(block: str) -> tuple[dict | None, list[str]]:
    """Versucht den JSON-Block via bekannter LLM-Quirks-Normalisierungen zu
    reparieren. Gibt `(data, applied_passes)` zurück; `data` ist None wenn
    alle Passes gescheitert sind.

    Reihenfolge der Passes wichtig: Comments zuerst (damit Single-Quote-Swap
    keine Kommentar-Zeichen betrifft), dann trailing commas, dann Python-
    Literale, dann Single-Quote-Swap.
    """
    current = block
    applied: list[str] = []
    for name, fn in _RECOVERY_PASSES:
        new = fn(current)
        if new != current:
            applied.append(name)
            current = new
        try:
            data = json.loads(current)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data, applied
        # Non-dict (z.B. Array): weiter iterieren — vielleicht bringt der
        # nächste Pass doch noch Struktur.
    return None, applied


def parse_scored_verdict(raw: str) -> ScoredVerdict:
    """Parst den strukturierten Review-Output eines LLM-Reviewers.

    Strenge Validation — jeder Pfad, der nicht sauber als ScoredVerdict
    interpretierbar ist, wird als `verdict="hard"` zurückgegeben.

    Robustheit: Wenn strict `json.loads` fehlschlägt, versucht eine Kaskade
    bekannter LLM-Quirks-Recoveries (Single-Quotes, trailing commas, Python-
    Literale, Inline-Kommentare). Bei erfolgreicher Recovery wird das im
    summary vermerkt ("[recovered: …] …") — Audit-Trail für schlampige
    Reviewer, aber kein Merge-Blocker.
    """
    block = _extract_json(raw)
    if block is None:
        return _fail_closed("no JSON block found in reviewer output")

    recovery_note: str | None = None
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        recovered, applied = _try_recover_json(block)
        if recovered is None:
            return _fail_closed(f"malformed JSON: {exc.msg}")
        data = recovered
        recovery_note = ",".join(applied) if applied else "unknown"

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

    if recovery_note is not None:
        summary = f"[recovered: {recovery_note}] {summary}"

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
