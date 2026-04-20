"""Tests für emit_metrics_line + StageMetric (Wave 4).

Portiert aus ai-portal/scripts/ai-review/metrics_test.py.
Imports angepasst auf ai_review_pipeline.metrics.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_review_pipeline import metrics


class EmitMetricsLineTests(unittest.TestCase):
    def test_appends_jsonl_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.jsonl"

            # Arrange
            record = {
                "pr": 42,
                "head_sha": "abc1234",
                "consensus": "success",
                "stages": {
                    "code": {"score": 9, "wall_ms": 12000, "iterations": 1},
                    "security": {"score": 9, "wall_ms": 9000, "iterations": 1},
                    "design": {"score": None, "wall_ms": 0, "iterations": 0, "skipped": "rate-limit"},
                },
                "merged": False,
            }

            # Act
            metrics.emit_metrics_line(record, path=path)

            # Assert
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            parsed = json.loads(lines[0])
            self.assertEqual(parsed["pr"], 42)
            self.assertEqual(parsed["stages"]["code"]["score"], 9)

    def test_appends_multiple_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.jsonl"
            metrics.emit_metrics_line({"pr": 1, "consensus": "success"}, path=path)
            metrics.emit_metrics_line({"pr": 2, "consensus": "failure"}, path=path)

            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["pr"], 1)
            self.assertEqual(json.loads(lines[1])["pr"], 2)

    def test_creates_parent_dir_if_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".ai-review" / "metrics.jsonl"
            self.assertFalse(path.parent.exists())

            metrics.emit_metrics_line({"pr": 1}, path=path)

            self.assertTrue(path.exists())

    def test_includes_iso_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.jsonl"
            metrics.emit_metrics_line({"pr": 1}, path=path)

            parsed = json.loads(path.read_text(encoding="utf-8").strip())
            # ISO-8601 UTC format mit Z-Suffix
            self.assertIn("T", parsed["timestamp"])
            self.assertTrue(parsed["timestamp"].endswith("Z"))


class StageMetricTests(unittest.TestCase):
    def test_records_success_with_score(self) -> None:
        sm = metrics.StageMetric(stage="code")
        sm.start()
        sm.finish(score=9, verdict="green", iterations=1)

        self.assertEqual(sm.score, 9)
        self.assertEqual(sm.verdict, "green")
        self.assertGreaterEqual(sm.wall_ms, 0)

    def test_records_skipped_reason(self) -> None:
        sm = metrics.StageMetric(stage="security")
        sm.start()
        sm.skip(reason="rate-limit")

        self.assertEqual(sm.skipped, "rate-limit")
        self.assertIsNone(sm.score)

    def test_to_dict_omits_none_fields(self) -> None:
        sm = metrics.StageMetric(stage="code")
        sm.start()
        sm.finish(score=8, verdict="green", iterations=1)
        d = sm.to_dict()
        self.assertIn("score", d)
        self.assertIn("wall_ms", d)
        self.assertNotIn("skipped", d)


class StageMetricWave6Tests(unittest.TestCase):
    """Wave 6a + 6c: iter_trend, reached_iter_3, nachfrage_outcome."""

    def test_iter_trend_records_scores_per_iteration(self) -> None:
        sm = metrics.StageMetric(stage="code")
        sm.start()
        sm.record_iter_score(4)
        sm.record_iter_score(6)
        sm.record_iter_score(8)
        sm.finish(score=8, verdict="green", iterations=3)

        self.assertEqual(sm.iter_trend, [4, 6, 8])
        self.assertTrue(sm.reached_iter_3)

    def test_reached_iter_3_false_when_only_2_iterations(self) -> None:
        sm = metrics.StageMetric(stage="code")
        sm.start()
        sm.record_iter_score(5)
        sm.record_iter_score(8)
        sm.finish(score=8, verdict="green", iterations=2)

        self.assertFalse(sm.reached_iter_3)

    def test_nachfrage_outcome_serialized_when_set(self) -> None:
        sm = metrics.StageMetric(stage="code")
        sm.start()
        sm.finish(score=6, verdict="soft", iterations=2)
        sm.nachfrage_outcome = "approved"
        d = sm.to_dict()
        self.assertEqual(d["nachfrage_outcome"], "approved")

    def test_to_dict_omits_wave6_fields_when_default(self) -> None:
        # Backward-compat: alte Felder fehlen im JSON wenn nicht gesetzt
        sm = metrics.StageMetric(stage="code")
        sm.start()
        sm.finish(score=9, verdict="green", iterations=1)
        d = sm.to_dict()
        self.assertNotIn("iter_trend", d)
        self.assertNotIn("reached_iter_3", d)
        self.assertNotIn("nachfrage_outcome", d)

    def test_to_dict_includes_iter_trend_when_nonempty(self) -> None:
        sm = metrics.StageMetric(stage="code")
        sm.start()
        sm.record_iter_score(5)
        sm.record_iter_score(7)
        sm.finish(score=7, verdict="soft", iterations=2)
        d = sm.to_dict()
        self.assertEqual(d["iter_trend"], [5, 7])


if __name__ == "__main__":
    unittest.main()
