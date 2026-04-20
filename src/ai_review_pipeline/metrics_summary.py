"""Metrics-Aggregator — woechentlicher Trend-Check (Wave 7d).

Liest `.ai-review/metrics.jsonl` und gibt ein Summary aus:

    $ python3 -m ai_review_pipeline.metrics_summary --since 7d
    # Output:
    #   15 PRs reviewed, 12 auto-merged (80%)
    #   2 Auto-Fix-Runs, beide erfolgreich
    #   1 Security-Waiver: "FP: Gemini misread line 29"
    #   0 Disagreements (Codex != Cursor)
    #   Avg Wall-Clock: code 14s, security 8s, design 5s

Portiert aus ai-portal/scripts/ai-review/metrics_summary.py.

Design-Prinzipien:
  - Pure-function-Kern (filter_by_age, summarize, parse_duration) — testbar
    ohne FS-I/O.
  - Read-only CLI. Keine side-effects. Kein Netz, keine DB.
  - Eingabe-tolerant: malformed lines werden uebersprungen, missing-fields
    mit Defaults ersetzt. Die Datei wird vom Runner appended, also reale
    Corruption via concurrent-write ist denkbar.

Nutzung als Woechentliches-Review-Tool:
  $ python3 -m ai_review_pipeline.metrics_summary --since 7d --json
  # -> maschinenlesbar fuer Telegram-Weekly-Digest-Workflow
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_METRICS_PATH = Path(".ai-review") / "metrics.jsonl"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(r"^(\d+)([dhm])$")


def parse_duration(s: str) -> timedelta:
    """Parst '7d', '24h', '30m' zu einem timedelta.

    Unterstuetzt d (days), h (hours), m (minutes). Invalid -> ValueError.
    """
    m = _DURATION_RE.match(s.strip())
    if not m:
        raise ValueError(f"Invalid duration: {s!r} — expected <int>(d|h|m)")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return timedelta(days=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "m":
        return timedelta(minutes=n)
    raise ValueError(f"Unknown unit: {unit!r}")


def _parse_ts(ts: str) -> datetime | None:
    """ISO-8601 mit Z-Suffix -> datetime. None bei parse-error."""
    if not ts:
        return None
    try:
        # Python <3.11 fromisoformat kommt mit Z-Suffix nicht klar — replacen.
        normalized = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def filter_by_age(
    records: list[dict[str, Any]],
    *,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    """Returnt nur Records, deren timestamp >= cutoff ist.

    Records ohne/mit malformed timestamp werden uebersprungen — defensive
    Haltung, damit ein einzelner Corruptor nicht das ganze Summary kippt.
    """
    out: list[dict[str, Any]] = []
    for r in records:
        ts = _parse_ts(r.get("timestamp", ""))
        if ts is None:
            continue
        if ts >= cutoff:
            out.append(r)
    return out


def read_records(path: Path) -> list[dict[str, Any]]:
    """Liest die jsonl-Datei zeilenweise. Fehlende Datei -> []."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # Malformed — skip, nicht crashen.
            continue
    return records


# ---------------------------------------------------------------------------
# Summary aggregation
# ---------------------------------------------------------------------------


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregiert Records zu einem Summary-Dict mit stabilen Keys."""
    total = len(records)

    consensus_breakdown: dict[str, int] = {}
    merged = 0
    waiver_reasons: list[str] = []
    autofix_total = 0
    autofix_passed = 0
    autofix_by_trigger: dict[str, int] = {}
    disagreements = 0
    # Wall-clock per stage — wir sammeln Listen, mitteln am Ende
    wall_samples: dict[str, list[int]] = {}

    for r in records:
        c = r.get("consensus") or "unknown"
        consensus_breakdown[c] = consensus_breakdown.get(c, 0) + 1
        if r.get("merged"):
            merged += 1

        waiver = r.get("security_waiver_reason")
        if waiver:
            waiver_reasons.append(str(waiver))

        trigger = r.get("autofix_triggered_by")
        if trigger:
            autofix_total += 1
            autofix_by_trigger[trigger] = autofix_by_trigger.get(trigger, 0) + 1
            if r.get("autofix_post_fix_checks") == "pass":
                autofix_passed += 1

        stages = r.get("stages") or {}
        for name, s in stages.items():
            if not isinstance(s, dict):
                continue
            wall = s.get("wall_ms")
            if isinstance(wall, int) and wall > 0:
                wall_samples.setdefault(name, []).append(wall)

        # Disagreement-Detection: code vs code-cursor verdict mismatch
        code = stages.get("code") if isinstance(stages, dict) else None
        cursor = stages.get("code-cursor") if isinstance(stages, dict) else None
        if (
            isinstance(code, dict)
            and isinstance(cursor, dict)
            and code.get("verdict")
            and cursor.get("verdict")
            and code["verdict"] != cursor["verdict"]
        ):
            disagreements += 1

    avg_wall_ms = {
        stage: int(sum(samples) / len(samples))
        for stage, samples in wall_samples.items()
        if samples
    }

    return {
        "total_prs": total,
        "merged": merged,
        "auto_merge_rate": (merged / total) if total > 0 else 0.0,
        "consensus_breakdown": consensus_breakdown,
        "security_waivers": {
            "count": len(waiver_reasons),
            "reasons": waiver_reasons,
        },
        "autofix": {
            "total": autofix_total,
            "passed": autofix_passed,
            "failed": autofix_total - autofix_passed,
            "pass_rate": (autofix_passed / autofix_total) if autofix_total > 0 else 0.0,
            "by_trigger": autofix_by_trigger,
        },
        "disagreements": disagreements,
        "avg_wall_ms": avg_wall_ms,
    }


# ---------------------------------------------------------------------------
# Human-readable render
# ---------------------------------------------------------------------------


def render_human(summary: dict[str, Any], *, since_label: str) -> str:
    """Baut einen 5-Zeilen-Report fuer Nicos woechentlichen Check."""
    lines: list[str] = [
        f"AI-Review Summary ({since_label})",
        "",
    ]
    total = summary["total_prs"]
    merged = summary["merged"]
    rate = round(summary["auto_merge_rate"] * 100)
    lines.append(f"- {total} PRs reviewed, {merged} auto-merged ({rate}%)")

    autofix = summary["autofix"]
    if autofix["total"] > 0:
        pass_rate = round(autofix["pass_rate"] * 100)
        lines.append(
            f"- {autofix['total']} Auto-Fix-Runs "
            f"({autofix['passed']}/{autofix['total']} passed, {pass_rate}%)",
        )
    else:
        lines.append("- 0 Auto-Fix-Runs")

    waivers = summary["security_waivers"]
    if waivers["count"] > 0:
        lines.append(f"- {waivers['count']} Security-Waiver(s):")
        for r in waivers["reasons"][:5]:
            lines.append(f"    - {r[:80]}")
    else:
        lines.append("- 0 Security-Waiver")

    lines.append(
        f"- {summary['disagreements']} Disagreement(s) (Codex != Cursor)",
    )

    if summary["avg_wall_ms"]:
        stage_parts = [
            f"{stage} {int(ms/1000)}s"
            for stage, ms in sorted(summary["avg_wall_ms"].items())
        ]
        lines.append(f"- Avg Wall-Clock: {', '.join(stage_parts)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Summarize .ai-review/metrics.jsonl trends.",
    )
    ap.add_argument(
        "--since", default="7d",
        help="Lookback window: 7d, 24h, 30m, etc. (default 7d)",
    )
    ap.add_argument(
        "--path", type=Path, default=DEFAULT_METRICS_PATH,
        help="Pfad zur metrics.jsonl (default .ai-review/metrics.jsonl)",
    )
    ap.add_argument(
        "--json", action="store_true",
        help="Ausgabe als JSON (maschinenlesbar fuer n8n-weekly-digest)",
    )
    args = ap.parse_args(argv)

    try:
        window = parse_duration(args.since)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    records = read_records(args.path)
    cutoff = datetime.now(timezone.utc) - window
    filtered = filter_by_age(records, cutoff=cutoff)
    summary = summarize(filtered)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(render_human(summary, since_label=args.since))

    return 0


if __name__ == "__main__":
    sys.exit(main())
