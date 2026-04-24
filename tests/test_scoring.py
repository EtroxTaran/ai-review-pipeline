"""Tests für parse_scored_verdict() — Wave 2a.

Parser muss:
- JSON-Block aus LLM-Output extrahieren (auch wenn Prosa drumrum steht)
- Score 1–10 + verdict (green/soft/hard) + findings normalisieren
- Bei Parse-Fehler fail-closed: verdict="hard", score=0 (statt silent success)
- Bei fehlenden Keys oder Out-of-Range-Werten: ebenfalls fail-closed

Portiert aus ai-portal/scripts/ai-review/scoring_test.py.
Import-Anpassung: `from . import scoring` → `from ai_review_pipeline import scoring`
"""

from __future__ import annotations

import unittest

from ai_review_pipeline import scoring


class ParseScoredVerdictTests(unittest.TestCase):
    # --- Happy Paths ---------------------------------------------------------

    def test_extracts_json_block_with_surrounding_prose(self) -> None:
        # Arrange: LLM-Output wie er typisch kommt: Prosa + JSON-Block + mehr Prosa
        raw = """
Here is my review:

```json
{
  "score": 9,
  "verdict": "green",
  "summary": "Looks good, tests cover edge cases.",
  "findings": []
}
```

Nothing else to add.
"""
        # Act
        verdict = scoring.parse_scored_verdict(raw)

        # Assert
        self.assertEqual(verdict.score, 9)
        self.assertEqual(verdict.verdict, "green")
        self.assertEqual(verdict.summary, "Looks good, tests cover edge cases.")
        self.assertEqual(verdict.findings, [])
        self.assertFalse(verdict.parse_failed)

    def test_extracts_bare_json_without_code_fence(self) -> None:
        raw = '{"score": 7, "verdict": "soft", "summary": "minor concerns", "findings": []}'
        verdict = scoring.parse_scored_verdict(raw)

        self.assertEqual(verdict.score, 7)
        self.assertEqual(verdict.verdict, "soft")

    def test_preserves_findings_list(self) -> None:
        raw = """```json
{
  "score": 5,
  "verdict": "soft",
  "summary": "a few issues",
  "findings": [
    {"severity": "warn", "file": "src/a.ts", "line": 42, "msg": "unused var"},
    {"severity": "error", "file": "src/b.ts", "line": 1, "msg": "missing auth"}
  ]
}
```"""
        verdict = scoring.parse_scored_verdict(raw)
        self.assertEqual(len(verdict.findings), 2)
        self.assertEqual(verdict.findings[0].severity, "warn")
        self.assertEqual(verdict.findings[1].file, "src/b.ts")
        self.assertEqual(verdict.findings[1].line, 1)

    # --- Fail-Closed Paths ---------------------------------------------------

    def test_fails_closed_when_no_json_found(self) -> None:
        # Reiner Prosa-Output, keine JSON → fail-closed
        raw = "The code looks fine, no issues found."
        verdict = scoring.parse_scored_verdict(raw)

        self.assertEqual(verdict.verdict, "hard")
        self.assertEqual(verdict.score, 0)
        self.assertTrue(verdict.parse_failed)
        self.assertIn("no json", verdict.summary.lower())

    def test_fails_closed_on_malformed_json(self) -> None:
        raw = '```json\n{"score": 8, "verdict": "green"  # trailing comma not valid\n```'
        verdict = scoring.parse_scored_verdict(raw)

        self.assertEqual(verdict.verdict, "hard")
        self.assertTrue(verdict.parse_failed)

    def test_fails_closed_when_required_key_missing(self) -> None:
        # verdict fehlt — Parser muss fail-closed
        raw = '{"score": 9, "summary": "fine", "findings": []}'
        verdict = scoring.parse_scored_verdict(raw)

        self.assertEqual(verdict.verdict, "hard")
        self.assertTrue(verdict.parse_failed)

    def test_fails_closed_when_score_out_of_range(self) -> None:
        raw = '{"score": 11, "verdict": "green", "summary": "x", "findings": []}'
        verdict = scoring.parse_scored_verdict(raw)

        self.assertEqual(verdict.verdict, "hard")
        self.assertTrue(verdict.parse_failed)

    def test_fails_closed_when_score_not_int(self) -> None:
        raw = '{"score": "nine", "verdict": "green", "summary": "x", "findings": []}'
        verdict = scoring.parse_scored_verdict(raw)

        self.assertEqual(verdict.verdict, "hard")
        self.assertTrue(verdict.parse_failed)

    def test_fails_closed_on_invalid_verdict_value(self) -> None:
        raw = '{"score": 8, "verdict": "maybe", "summary": "x", "findings": []}'
        verdict = scoring.parse_scored_verdict(raw)

        self.assertEqual(verdict.verdict, "hard")
        self.assertTrue(verdict.parse_failed)


class MalformedJsonRecoveryTests(unittest.TestCase):
    """Regression: LLM-Reviewer (insbesondere Cursor composer-2) liefern
    gelegentlich JSON mit typischen Quirks — Single-Quotes, trailing commas,
    Python-Booleans, Inline-Kommentare. Der Parser muss robust genug sein,
    diese häufigen Fälle zu erholen, statt fail-closed zu gehen.

    Entdeckt beim ai-portal Phase-5-Cutover (PR#44): cursor-review lieferte
    Output mit 'Expecting property name enclosed in double quotes', was das
    cursor-review-Gate hart failte, obwohl der Reviewer inhaltlich ein
    normales Verdict abliefern wollte.

    Fail-Closed-Kontrakt bleibt bestehen: wenn ALLE Recovery-Pfade scheitern
    ODER die required Keys nach Recovery fehlen, ist es weiterhin `parse_failed`.
    """

    def test_recovers_single_quotes_in_keys_and_values(self) -> None:
        # Python-Dict-Style: häufigstes Cursor-Quirk
        raw = "```json\n{'score': 8, 'verdict': 'green', 'summary': 'looks fine', 'findings': []}\n```"
        verdict = scoring.parse_scored_verdict(raw)

        self.assertFalse(verdict.parse_failed, f"should recover, got: {verdict.summary}")
        self.assertEqual(verdict.score, 8)
        self.assertEqual(verdict.verdict, "green")

    def test_recovers_trailing_comma_in_object(self) -> None:
        raw = '{"score": 9, "verdict": "green", "summary": "all good", "findings": [],}'
        verdict = scoring.parse_scored_verdict(raw)

        self.assertFalse(verdict.parse_failed, f"should recover, got: {verdict.summary}")
        self.assertEqual(verdict.score, 9)

    def test_recovers_trailing_comma_in_array(self) -> None:
        raw = (
            '{"score": 8, "verdict": "green", "summary": "ok", '
            '"findings": [{"severity": "info", "file": "a.py", "line": 1, "msg": "note",},]}'
        )
        verdict = scoring.parse_scored_verdict(raw)

        self.assertFalse(verdict.parse_failed, f"should recover, got: {verdict.summary}")
        self.assertEqual(len(verdict.findings), 1)

    def test_recovers_python_booleans_and_none(self) -> None:
        # Einige LLMs leaken Python-Literale in "JSON"
        raw = (
            '{"score": 8, "verdict": "green", "summary": "x", '
            '"findings": [], "extra_field": None, "flag": True, "other": False}'
        )
        verdict = scoring.parse_scored_verdict(raw)

        self.assertFalse(verdict.parse_failed, f"should recover, got: {verdict.summary}")
        self.assertEqual(verdict.score, 8)

    def test_recovers_line_comments(self) -> None:
        raw = """```json
{
  // Cursor's internal reasoning
  "score": 7,
  "verdict": "soft", // needs attention
  "summary": "edge case",
  "findings": []
}
```"""
        verdict = scoring.parse_scored_verdict(raw)

        self.assertFalse(verdict.parse_failed, f"should recover, got: {verdict.summary}")
        self.assertEqual(verdict.score, 7)
        self.assertEqual(verdict.verdict, "soft")

    def test_recovery_summary_prefixed_with_recovery_note(self) -> None:
        # Wenn Recovery applied wurde, MUSS das im summary sichtbar sein
        # (Audit-Trail — man sieht am Sticky-Comment, dass der Reviewer
        # schlampiges JSON geliefert hat, ohne dass der PR blockiert wird).
        raw = "{'score': 8, 'verdict': 'green', 'summary': 'ok', 'findings': []}"
        verdict = scoring.parse_scored_verdict(raw)

        self.assertFalse(verdict.parse_failed)
        self.assertIn("recovered", verdict.summary.lower())

    def test_still_fails_when_truly_broken(self) -> None:
        # Fail-Closed bleibt erhalten für echte Garbage
        raw = "```json\n{this is not json at all [[[ }}}\n```"
        verdict = scoring.parse_scored_verdict(raw)

        self.assertTrue(verdict.parse_failed)
        self.assertEqual(verdict.score, 0)

    def test_still_fails_when_recovered_but_missing_required_keys(self) -> None:
        # Recovery kann den JSON-Parser zufriedenstellen, aber die Schema-
        # Validation greift weiterhin — fehlt `verdict`, bleibt es fail-closed.
        raw = "{'score': 8, 'summary': 'no verdict key'}"
        verdict = scoring.parse_scored_verdict(raw)

        self.assertTrue(verdict.parse_failed)
        self.assertIn("missing required key", verdict.summary)


class VerdictHelpersTests(unittest.TestCase):
    """verdict_from_score + role-specific threshold rules."""

    def test_verdict_from_score_green_threshold(self) -> None:
        # >=8 = green
        self.assertEqual(scoring.verdict_from_score(8), "green")
        self.assertEqual(scoring.verdict_from_score(10), "green")

    def test_verdict_from_score_soft_band(self) -> None:
        # 5-7 = soft
        self.assertEqual(scoring.verdict_from_score(5), "soft")
        self.assertEqual(scoring.verdict_from_score(7), "soft")

    def test_verdict_from_score_hard_below_five(self) -> None:
        # <5 = hard
        self.assertEqual(scoring.verdict_from_score(4), "hard")
        self.assertEqual(scoring.verdict_from_score(0), "hard")

    def test_security_verdict_stricter(self) -> None:
        # Security: <=7 = hard (kein soft-band)
        self.assertEqual(scoring.verdict_for_role(7, role="security"), "hard")
        self.assertEqual(scoring.verdict_for_role(8, role="security"), "green")

    def test_code_and_design_use_default_bands(self) -> None:
        # Code und Design folgen der green/soft/hard-Regel
        self.assertEqual(scoring.verdict_for_role(7, role="code"), "soft")
        self.assertEqual(scoring.verdict_for_role(7, role="design"), "soft")
        self.assertEqual(scoring.verdict_for_role(8, role="code"), "green")


if __name__ == "__main__":
    unittest.main()
