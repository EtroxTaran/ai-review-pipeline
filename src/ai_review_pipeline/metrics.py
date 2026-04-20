"""Pipeline-Metrics: einfacher jsonl-Writer pro Consensus-Run (Wave 4).

Portiert aus ai-portal/scripts/ai-review/metrics.py.

Eine Zeile pro PR-Entscheidung in `.ai-review/metrics.jsonl` (Repo-lokal,
gitignored). Jede Zeile ist ein eigenständiges JSON-Objekt mit den Keys:

    timestamp, pr, head_sha, consensus, stages, merged

`stages` ist ein dict pro Reviewer mit `score`, `verdict`, `wall_ms`,
`iterations` (oder `skipped: "<reason>"` wenn die Stage übersprungen wurde).

Wave 7d: Optionale Top-Level-Felder (werden von `metrics_summary.py`
aggregiert, aber nicht von emit_metrics_line erzwungen):
  - `security_waiver_reason`: str — wenn PR-Author einen Waiver gesetzt hat
  - `autofix_triggered_by`: "telegram-button" | "pr-comment-retry" | "direct"
  - `autofix_files_changed`: int
  - `autofix_post_fix_checks`: "pass" | "fail"

Zweck: Baseline für „wie oft mergen wir autonom, wo läuft Zeit drauf, wie
oft trifft der Circuit-Breaker?" ohne separates Monitoring-System.
`metrics_summary.py` analysiert die Datei → Trend-Report für Nicos
wöchentlichen Check.

Observability-Prinzip: write-only, append-only, keine Reads während der
Pipeline läuft. Liest nur das Aggregator-Skript separat.

JSONL-Format-Notiz (Kompatibilität mit discord_notify.py):
  discord_notify._log_failure schreibt ebenfalls in `.ai-review/metrics.jsonl`,
  jedoch mit einem Schema-Subset das discord-spezifische Felder enthält
  (`module`, `status`, `error`, optional `status_code`). Der `timestamp`-Key
  ist kompatibel — beide schreiben UTC, aber mit unterschiedlichem Suffix:
  dieses Modul: „Z"-Suffix via strftime; discord_notify: „+00:00"-Suffix via
  isoformat(). Beide Formate sind ISO-8601-konform und werden von Standard-
  Tools (Python datetime.fromisoformat, jq, spreadsheets) korrekt geparst.
  Die Datei ist ein Schema-union-Log: Aggregator-Skripte müssen optionale
  Felder tolerieren (MUST ignore unknown keys).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Stage-level Timer + Metric Record
# ---------------------------------------------------------------------------

@dataclass
class StageMetric:
    """Pro-Reviewer-Record. Startet den Clock beim `start()`, misst `wall_ms`.

    Wave 6a: `iter_trend` trackt Scores pro Iteration für Konvergenz-Analyse.
    Wave 6c: `nachfrage_outcome` trackt soft-consensus-Antwort (wenn applicable).
    """

    stage: str
    score: int | None = None
    verdict: str | None = None
    iterations: int = 0
    wall_ms: int = 0
    skipped: str | None = None
    # Wave 6a: Score-Verlauf über alle Iterationen (für Konvergenz-Analyse)
    iter_trend: list[int] = field(default_factory=list)
    # Wave 6a: True wenn das Score-Trend-Gate Iter 3 getriggert hat
    reached_iter_3: bool = False
    # Wave 6c: soft-consensus-Antwort ("approved" | "retry" | "timeout" | None)
    nachfrage_outcome: str | None = None
    _t0: float | None = field(default=None, repr=False)

    def start(self) -> None:
        self._t0 = time.monotonic()

    def _stop_clock(self) -> None:
        if self._t0 is not None:
            self.wall_ms = int((time.monotonic() - self._t0) * 1000)

    def record_iter_score(self, score: int) -> None:
        """Wave 6a: fügt den Score der aktuellen Iteration zum Trend hinzu."""
        self.iter_trend.append(score)

    def finish(self, *, score: int, verdict: str, iterations: int) -> None:
        self.score = score
        self.verdict = verdict
        self.iterations = iterations
        # Wave 6a: wenn >2 Iter gelaufen sind, war das Score-Trend-Gate aktiv
        if iterations > 2:
            self.reached_iter_3 = True
        self._stop_clock()

    def skip(self, *, reason: str) -> None:
        self.skipped = reason
        self._stop_clock()

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view. None/leere Felder werden weggelassen."""
        d: dict[str, Any] = {"wall_ms": self.wall_ms, "iterations": self.iterations}
        if self.score is not None:
            d["score"] = self.score
        if self.verdict is not None:
            d["verdict"] = self.verdict
        if self.skipped is not None:
            d["skipped"] = self.skipped
        if self.iter_trend:
            d["iter_trend"] = list(self.iter_trend)
        if self.reached_iter_3:
            d["reached_iter_3"] = True
        if self.nachfrage_outcome is not None:
            d["nachfrage_outcome"] = self.nachfrage_outcome
        return d


# ---------------------------------------------------------------------------
# Line writer
# ---------------------------------------------------------------------------

DEFAULT_METRICS_PATH = Path(".ai-review") / "metrics.jsonl"


def emit_metrics_line(
    record: dict[str, Any],
    *,
    path: Path | None = None,
) -> None:
    """Appendet eine JSON-Zeile in die Metrics-Datei. Idempotent beim Ordner-anlegen."""
    target = Path(path) if path is not None else DEFAULT_METRICS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    # ISO-8601 UTC mit Z-Suffix (stabil serialisierbar, sortierbar, zone-eindeutig)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    full = {"timestamp": ts, **record}

    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(full, separators=(",", ":"), sort_keys=True))
        fh.write("\n")
