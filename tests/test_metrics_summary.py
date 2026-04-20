"""Tests für ai_review_pipeline.metrics_summary (Wave 7d).

Portiert aus ai-portal/scripts/ai-review/metrics_summary_test.py.
Import angepasst: from . import metrics_summary → from ai_review_pipeline import metrics_summary.

metrics_summary liest .ai-review/metrics.jsonl und aggregiert Trends
(auto-merge-rate, disagreement-rate, waiver-frequency, autofix-success-rate,
avg wall-clock per stage). Pure-function-Kern testbar ohne FS-I/O.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_review_pipeline import metrics_summary


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FilterByAgeTests(unittest.TestCase):
    """filter_by_age gibt nur Records zurück, deren timestamp >= cutoff ist."""

    def test_filters_older_records(self) -> None:
        now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
        records = [
            {"timestamp": _iso(now - timedelta(days=10)), "pr": 1},
            {"timestamp": _iso(now - timedelta(days=2)),  "pr": 2},
            {"timestamp": _iso(now - timedelta(hours=1)), "pr": 3},
        ]
        cutoff = now - timedelta(days=7)
        result = metrics_summary.filter_by_age(records, cutoff=cutoff)
        prs = [r["pr"] for r in result]
        self.assertEqual(prs, [2, 3])

    def test_missing_timestamp_is_skipped(self) -> None:
        # Records ohne timestamp werden ignoriert (defensive)
        records = [{"pr": 1}, {"timestamp": _iso(datetime.now(timezone.utc)), "pr": 2}]
        cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
        result = metrics_summary.filter_by_age(records, cutoff=cutoff)
        self.assertEqual([r["pr"] for r in result], [2])


class SummarizeTests(unittest.TestCase):
    """summarize aggregiert eine Liste Records zu einer Summary-Dict."""

    def test_counts_consensus_outcomes(self) -> None:
        records = [
            {"pr": 1, "consensus": "success", "merged": True},
            {"pr": 2, "consensus": "success", "merged": True},
            {"pr": 3, "consensus": "failure", "merged": False},
            {"pr": 4, "consensus": "soft",    "merged": False},
        ]
        summary = metrics_summary.summarize(records)
        self.assertEqual(summary["total_prs"], 4)
        self.assertEqual(summary["merged"], 2)
        self.assertEqual(summary["consensus_breakdown"]["success"], 2)
        self.assertEqual(summary["consensus_breakdown"]["failure"], 1)
        self.assertEqual(summary["consensus_breakdown"]["soft"], 1)
        # Auto-merge-Rate = merged / total
        self.assertEqual(summary["auto_merge_rate"], 0.5)

    def test_counts_security_waivers(self) -> None:
        records = [
            {"pr": 1, "consensus": "success"},
            {"pr": 2, "consensus": "success",
             "security_waiver_reason": "FP: Gemini misread line 29"},
            {"pr": 3, "consensus": "failure"},
        ]
        summary = metrics_summary.summarize(records)
        self.assertEqual(summary["security_waivers"]["count"], 1)
        self.assertEqual(
            summary["security_waivers"]["reasons"],
            ["FP: Gemini misread line 29"],
        )

    def test_counts_autofix_outcomes(self) -> None:
        records = [
            {"pr": 1, "autofix_triggered_by": "telegram-button",
             "autofix_files_changed": 2, "autofix_post_fix_checks": "pass"},
            {"pr": 2, "autofix_triggered_by": "pr-comment-retry",
             "autofix_files_changed": 0, "autofix_post_fix_checks": "pass"},
            {"pr": 3, "autofix_triggered_by": "telegram-button",
             "autofix_files_changed": 0, "autofix_post_fix_checks": "fail"},
        ]
        summary = metrics_summary.summarize(records)
        self.assertEqual(summary["autofix"]["total"], 3)
        self.assertEqual(summary["autofix"]["passed"], 2)
        self.assertEqual(summary["autofix"]["failed"], 1)
        self.assertAlmostEqual(summary["autofix"]["pass_rate"], 2/3, places=3)
        self.assertEqual(summary["autofix"]["by_trigger"]["telegram-button"], 2)
        self.assertEqual(summary["autofix"]["by_trigger"]["pr-comment-retry"], 1)

    def test_counts_disagreements(self) -> None:
        # Disagreement-Records haben stages.code + code-cursor mit
        # unterschiedlichen verdicts (green vs soft/hard).
        records = [
            {"pr": 1, "stages": {
                "code": {"verdict": "green", "score": 9},
                "code-cursor": {"verdict": "green", "score": 8},
            }},
            {"pr": 2, "stages": {
                "code": {"verdict": "green", "score": 9},
                "code-cursor": {"verdict": "hard", "score": 3},
            }},  # disagreement
            {"pr": 3, "stages": {
                "code": {"verdict": "soft", "score": 6},
                "code-cursor": {"verdict": "soft", "score": 6},
            }},
        ]
        summary = metrics_summary.summarize(records)
        self.assertEqual(summary["disagreements"], 1)

    def test_avg_wall_clock_per_stage(self) -> None:
        records = [
            {"pr": 1, "stages": {
                "code": {"wall_ms": 10000, "iterations": 1},
                "security": {"wall_ms": 5000, "iterations": 1},
            }},
            {"pr": 2, "stages": {
                "code": {"wall_ms": 20000, "iterations": 2},
                "security": {"wall_ms": 7000, "iterations": 1},
            }},
        ]
        summary = metrics_summary.summarize(records)
        self.assertEqual(summary["avg_wall_ms"]["code"], 15000)
        self.assertEqual(summary["avg_wall_ms"]["security"], 6000)

    def test_empty_records_safe(self) -> None:
        summary = metrics_summary.summarize([])
        self.assertEqual(summary["total_prs"], 0)
        self.assertEqual(summary["auto_merge_rate"], 0.0)
        self.assertEqual(summary["security_waivers"]["count"], 0)
        self.assertEqual(summary["autofix"]["total"], 0)
        self.assertEqual(summary["disagreements"], 0)


class ReadRecordsTests(unittest.TestCase):
    """read_records liest die jsonl-Datei und returnt List[dict]."""

    def test_reads_valid_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.jsonl"
            path.write_text(
                json.dumps({"pr": 1, "consensus": "success"}) + "\n"
                + json.dumps({"pr": 2, "consensus": "failure"}) + "\n",
                encoding="utf-8",
            )
            records = metrics_summary.read_records(path)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["pr"], 1)

    def test_missing_file_returns_empty(self) -> None:
        records = metrics_summary.read_records(Path("/tmp/does-not-exist-metrics.jsonl"))
        self.assertEqual(records, [])

    def test_skips_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.jsonl"
            path.write_text(
                '{"pr": 1}\nnot-json-at-all\n{"pr": 2}\n',
                encoding="utf-8",
            )
            records = metrics_summary.read_records(path)
            self.assertEqual([r["pr"] for r in records], [1, 2])


class ParseDurationTests(unittest.TestCase):
    """parse_duration akzeptiert '7d', '24h', '30m' etc."""

    def test_parses_days(self) -> None:
        self.assertEqual(metrics_summary.parse_duration("7d"), timedelta(days=7))

    def test_parses_hours(self) -> None:
        self.assertEqual(metrics_summary.parse_duration("24h"), timedelta(hours=24))

    def test_parses_minutes(self) -> None:
        self.assertEqual(metrics_summary.parse_duration("30m"), timedelta(minutes=30))

    def test_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            metrics_summary.parse_duration("7x")


class RenderHumanTests(unittest.TestCase):
    """render_human erzeugt lesbaren Report aus Summary-Dict."""

    def _base_summary(self) -> dict:
        """Minimales Summary fuer Render-Tests."""
        return {
            "total_prs": 5,
            "merged": 4,
            "auto_merge_rate": 0.8,
            "consensus_breakdown": {"success": 4, "failure": 1},
            "security_waivers": {"count": 0, "reasons": []},
            "autofix": {
                "total": 0, "passed": 0, "failed": 0,
                "pass_rate": 0.0, "by_trigger": {},
            },
            "disagreements": 0,
            "avg_wall_ms": {},
        }

    def test_contains_pr_count_and_merge_rate(self) -> None:
        summary = self._base_summary()
        output = metrics_summary.render_human(summary, since_label="7d")
        self.assertIn("5 PRs reviewed", output)
        self.assertIn("4 auto-merged", output)
        self.assertIn("80%", output)
        self.assertIn("7d", output)

    def test_no_autofix_shows_zero(self) -> None:
        summary = self._base_summary()
        output = metrics_summary.render_human(summary, since_label="7d")
        self.assertIn("0 Auto-Fix-Runs", output)

    def test_with_autofix_shows_stats(self) -> None:
        summary = self._base_summary()
        summary["autofix"] = {
            "total": 3, "passed": 2, "failed": 1,
            "pass_rate": 2/3, "by_trigger": {"telegram-button": 3},
        }
        output = metrics_summary.render_human(summary, since_label="24h")
        self.assertIn("3 Auto-Fix-Runs", output)
        self.assertIn("2/3 passed", output)

    def test_with_security_waivers_shows_reasons(self) -> None:
        summary = self._base_summary()
        summary["security_waivers"] = {
            "count": 1,
            "reasons": ["FP: Gemini misread line 29"],
        }
        output = metrics_summary.render_human(summary, since_label="7d")
        self.assertIn("1 Security-Waiver(s)", output)
        self.assertIn("FP: Gemini misread line 29", output)

    def test_no_waivers_shows_zero(self) -> None:
        summary = self._base_summary()
        output = metrics_summary.render_human(summary, since_label="7d")
        self.assertIn("0 Security-Waiver", output)

    def test_disagreements_shown(self) -> None:
        summary = self._base_summary()
        summary["disagreements"] = 2
        output = metrics_summary.render_human(summary, since_label="7d")
        self.assertIn("2 Disagreement(s)", output)

    def test_avg_wall_ms_shown_when_present(self) -> None:
        summary = self._base_summary()
        summary["avg_wall_ms"] = {"code": 14000, "security": 8000}
        output = metrics_summary.render_human(summary, since_label="7d")
        self.assertIn("Avg Wall-Clock", output)
        self.assertIn("code 14s", output)
        self.assertIn("security 8s", output)

    def test_no_wall_ms_section_when_empty(self) -> None:
        summary = self._base_summary()
        output = metrics_summary.render_human(summary, since_label="7d")
        self.assertNotIn("Avg Wall-Clock", output)


class MainCLITests(unittest.TestCase):
    """main() CLI integrations-Tests ohne echte Dateisystem-Nutzung."""

    def test_invalid_duration_returns_exit_2(self) -> None:
        rc = metrics_summary.main(["--since", "7x"])
        self.assertEqual(rc, 2)

    def test_missing_metrics_file_returns_0_with_empty_summary(self) -> None:
        # Nicht-existierende Datei -> leeres Summary, kein Crash
        rc = metrics_summary.main([
            "--since", "7d",
            "--path", "/tmp/does-not-exist-metrics-main-test.jsonl",
        ])
        self.assertEqual(rc, 0)

    def test_json_output_is_valid(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.jsonl"
            now = datetime.now(timezone.utc)
            record = {
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "consensus": "success",
                "merged": True,
            }
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = metrics_summary.main([
                    "--since", "7d",
                    "--path", str(path),
                    "--json",
                ])
            self.assertEqual(rc, 0)
            parsed = json.loads(buf.getvalue())
            self.assertEqual(parsed["total_prs"], 1)
            self.assertEqual(parsed["merged"], 1)

    def test_human_output_printed_by_default(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.jsonl"
            now = datetime.now(timezone.utc)
            record = {
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "consensus": "success",
                "merged": True,
            }
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = metrics_summary.main([
                    "--since", "7d",
                    "--path", str(path),
                ])
            self.assertEqual(rc, 0)
            self.assertIn("PRs reviewed", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
