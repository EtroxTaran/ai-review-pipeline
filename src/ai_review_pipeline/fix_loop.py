"""Shared iterative fix loop — calls Claude Code CLI to resolve review findings.

Flow:
  iteration 1: review() → green? done. Otherwise fix().
  iteration 2: review() → green? done. Otherwise fix().
  ...
  iteration N (max): review() → green? done. Otherwise ESCALATE to human.

The loop is invoked by each stage script (code/security/design) AFTER the
initial review surfaces failures. We keep everything in one GitHub Actions
job — no round-trips through workflow_run just to re-run the reviewer —
because that would both double the wall-clock and double CLI rate-limit
consumption.

Fix-step is always Claude Code CLI (user explicit choice): claude knows the
repo context best because it developed it. Reviewer per stage differs
(codex/gemini/claude) but FIXER is always claude.

No-API-keys: `claude` CLI auth comes from `~/.claude/.credentials.json` on
the r2d2 runner host, mounted into the job environment.

Portiert aus ai-portal/scripts/ai-review/fix_loop.py.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ai_review_pipeline import common, scoring


# ---------------------------------------------------------------------------
# Öffentliche Typen
# ---------------------------------------------------------------------------

ReviewFn = Callable[[], tuple[bool, str]]
"""() -> (success, summary_for_human). summary wird im Escalation-Comment angezeigt."""

FixFn = Callable[..., bool]
"""
Signatur: fix_fn(stage=..., iteration=..., summary=..., pr_number=...)
Gibt True bei erfolgreichem Fix-Commit+Push zurück, False bei CLI/Commit/Push-Fehler.
"""


@dataclass
class LoopOutcome:
    stage: str
    pr_number: int
    success: bool
    iterations: int
    escalated: bool
    summaries: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------

# Wave 6a: Score-Trend-Gate für bedingte Iter 3.
# Nur wenn Score(iter2) - Score(iter1) >= MIN_SCORE_IMPROVEMENT, lassen wir
# eine dritte Iteration zu. Das ist ein expliziter Kompromiss zwischen
# hart-cap bei 2 (starr) und naiv-cap bei 3 (Budget-Verschwendung bei
# Stagnation). Empirie arXiv:2603.26458: 86% first-pass, minimaler Gewinn
# ab Iter 3 — also nur bei echtem Konvergenz-Signal lohnt Iter 3.
MIN_SCORE_IMPROVEMENT = 2
HARD_CAP_ITERATIONS = 3


def run_fix_loop(
    *,
    stage: str,
    pr_number: int,
    review_fn: ReviewFn,
    fix_fn: FixFn,
    max_iterations: int = 2,  # reifung-v2: 4→2 (Circuit-Breaker, Industry-Median)
) -> LoopOutcome:
    """Führt review → (wenn red) fix → repeat aus, gedeckelt bei max_iterations.

    Wave 6a: Soft-cap bis HARD_CAP_ITERATIONS (=3), erlaubt aber nur wenn
    Score-Trend Konvergenz beweist (Δ ≥ MIN_SCORE_IMPROVEMENT zwischen
    aufeinanderfolgenden Iterationen). Ohne Score-Signal bleibt max_iterations
    der hart cap (backward-compat mit Legacy-Pfad ohne JSON-Output).

    Bricht früh ab wenn fix_fn False zurückgibt (CLI-Fehler, Commit-Hook-Fehler,
    Push-Rejection) — nochmals auf einem defekten Tool zu laufen verschwendet
    Rate-Limit-Budget.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be ≥ 1")

    summaries: list[str] = []
    # Wave 2b: Score-Regression-Guard. `last_score` bleibt None solange der
    # Reviewer kein JSON-Block liefert (legacy-Pfad). Sobald ein Score da ist,
    # vergleichen wir ihn mit dem vorigen — bei Regression abbrechen.
    last_score: int | None = None
    # Wave 6a: Effektives Limit. Startet beim user-übergebenen max_iterations
    # und kann EINMAL auf HARD_CAP_ITERATIONS hochgesetzt werden wenn Score
    # sich signifikant verbessert (Konvergenz-Gate). Nie über HARD_CAP hinaus.
    effective_max = max_iterations
    iteration = 0

    while iteration < effective_max:
        iteration += 1
        success, summary = review_fn()
        summaries.append(summary)
        if success:
            return LoopOutcome(
                stage=stage, pr_number=pr_number, success=True,
                iterations=iteration, escalated=False, summaries=summaries,
            )

        # Score-Regression-Check: nur aktiv wenn BEIDE (vorige + aktuelle) Iter
        # einen parsbaren Score lieferten. Sonst keine Aussage möglich → weiter.
        current_verdict = scoring.parse_scored_verdict(summary)
        current_score: int | None = None
        if not current_verdict.parse_failed:
            current_score = current_verdict.score
            if last_score is not None and current_score < last_score:
                # Regression detected — abort loop with escalation
                summaries.append(
                    f"⚠️ score-regression: {last_score} → {current_score} — aborting"
                )
                return LoopOutcome(
                    stage=stage, pr_number=pr_number, success=False,
                    iterations=iteration, escalated=True, summaries=summaries,
                )

            # Wave 6a: Score-Trend-Gate — nach vorletzter Iter prüfen, ob
            # Score um ≥ MIN_SCORE_IMPROVEMENT gestiegen ist. Wenn ja UND
            # wir sind sonst am default-cap, erlauben wir eine Extra-Iter
            # bis HARD_CAP.
            if (
                last_score is not None
                and iteration == effective_max  # letzte Iter nach user-cap
                and effective_max < HARD_CAP_ITERATIONS
                and (current_score - last_score) >= MIN_SCORE_IMPROVEMENT
            ):
                summaries.append(
                    f"📈 score-trend-gate: {last_score} → {current_score} "
                    f"(Δ={current_score - last_score}, ≥{MIN_SCORE_IMPROVEMENT}) "
                    f"— granting iter {effective_max + 1}"
                )
                effective_max = min(HARD_CAP_ITERATIONS, effective_max + 1)

            last_score = current_score

        # Fix nur ausführen wenn noch eine weitere Runde folgt
        if iteration < effective_max:
            fixed = fix_fn(
                stage=stage, iteration=iteration,
                summary=summary, pr_number=pr_number,
            )
            if not fixed:
                return LoopOutcome(
                    stage=stage, pr_number=pr_number, success=False,
                    iterations=iteration, escalated=True, summaries=summaries,
                )

    return LoopOutcome(
        stage=stage, pr_number=pr_number, success=False,
        iterations=iteration, escalated=True, summaries=summaries,
    )


def build_escalation_comment(
    *,
    stage: str,
    iterations: int,
    summaries: list[str],
    pr_number: int,
    human: str = "@EtroxTaran",
) -> str:
    """Menschenlesbarer Escalation-Kommentar, der gepostet wird wenn der Loop aufgibt."""
    lines = [
        f"⚠️ **AI-Review escalation — {stage}**",
        "",
        f"Die `{stage}`-Review-Stage konvergierte nach **{iterations} Iterationen** "
        "nicht zu einem grünen Status.",
        "",
        f"Human-in-the-Loop nötig: {human}",
        "",
        "### Verlauf",
        "",
    ]
    for i, summary in enumerate(summaries, start=1):
        short = summary.strip().splitlines()[0][:180] if summary.strip() else "(no summary)"
        lines.append(f"- **Iter {i}:** {short}")
    lines += [
        "",
        f"_PR #{pr_number} · gestoppt durch `src/ai_review_pipeline/fix_loop.py`_",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Standard Fix-Implementierung — Claude Code CLI, commit, push
# ---------------------------------------------------------------------------

_FIX_PROMPT_TEMPLATE = """You are fixing review findings on Nexus Portal PR #{pr_number}.

Stage: **{stage}**
Iteration: {iteration} / {max_iterations}

## Findings from the {stage}-reviewer

```
{summary}
```

## Your task

1. Apply the minimal edits needed to resolve the findings above.
2. Preserve every existing test — do not weaken assertions.
3. Run `pnpm typecheck` and `pnpm test --changed origin/{base_branch}` locally.
4. If either command fails, STOP and return — do NOT commit a broken state.
5. Otherwise, stage and commit with message:
   `[ai-fix] {stage}: iter {iteration} — <your concise summary>`
6. Push to the PR branch.

Constraints (Nexus Portal coding rules):
- TypeScript strict, never `any`, explicit return types.
- Tailwind tokens only (no raw hex / no text-green-*).
- No raw HTML in plugin code (no <table>, <button>, <select>, <textarea>).
- Deutsche Kommentare, englischer Code.
- TDD: jeder neue Logik-Block braucht einen Test vorher.

Return a concise summary of what you changed. If you cannot fix the
findings (contradictory/unclear), explain why and abort without committing.
"""


@dataclass
class ClaudeFixer:
    """Production fix-callable — ruft `claude` CLI + git commit/push auf."""

    worktree: Path
    base_branch: str
    branch: str
    runner: common.Runner = common.default_runner
    # Policy: fix_loop = Volume-Traffic → Sonnet. Default aus Registry
    # (resolve_model('fix_loop')). Field-Default ist Legacy-Fallback;
    # __post_init__ ersetzt None durch aktuellen Registry-Wert.
    model: str | None = None
    max_iterations: int = 2  # reifung-v2: 4→2 (Circuit-Breaker)

    def __post_init__(self) -> None:
        if self.model is None:
            from ai_review_pipeline import models
            self.model = models.resolve_model("fix_loop")

    def _head_sha(self) -> str | None:
        """Liest den aktuellen HEAD-SHA (oder None wenn git fehlschlägt)."""
        proc = self.runner(
            ["git", "rev-parse", "HEAD"],
            cwd=self.worktree, timeout=15,
        )
        if proc.returncode != 0:
            return None
        return (proc.stdout or "").strip() or None

    def _abort_without_push(self, head_before: str | None) -> None:
        """Rollt die Worktree-HEAD zurück auf den letzten gepushten SHA.

        Hintergrund: Wenn Claude lokal committet, aber typecheck/tests/push
        danach scheitern, darf HEAD nicht auf dem unpushed Commit stehen
        bleiben. stage.run_stage() liest nach der Fix-Loop
        `current_head_sha(worktree)` und postet den Terminal-Status auf
        genau diese SHA. Ohne Reset würde der Status auf eine SHA zeigen,
        die GitHub nie gesehen hat — aus einer sauberen Eskalation würde
        ein Stage-Crash.

        Best-effort: wenn HEAD schon stimmt (kein Commit erzeugt) oder
        `git rev-parse` fehlschlägt, passiert nichts.
        """
        if not head_before:
            return
        current = self._head_sha()
        if current is None or current == head_before:
            return
        self.runner(
            ["git", "reset", "--hard", head_before],
            cwd=self.worktree, timeout=30,
        )

    def __call__(self, *, stage: str, iteration: int, summary: str,
                 pr_number: int) -> bool:
        prompt = _FIX_PROMPT_TEMPLATE.format(
            pr_number=pr_number,
            stage=stage,
            iteration=iteration,
            max_iterations=self.max_iterations,
            summary=summary,
            base_branch=self.base_branch,
        )

        # 0) HEAD-Snapshot — später vergleichen wir, ob Claude einen neuen
        #    Commit erzeugt hat. `git push` exited 0 bei "Everything up-to-date",
        #    was sonst fälschlich als Erfolg durchgeht.
        head_before = self._head_sha()

        # 1) Claude Code CLI wendet Edits an + committet
        try:
            proc = self.runner(
                ["claude", "--model", self.model, "--permission-mode", "acceptEdits",
                 "-p", prompt],
                cwd=self.worktree,
                timeout=common.CLI_FIX_TIMEOUT,
                env={**os.environ, "NO_COLOR": "1"},
            )
        except subprocess.TimeoutExpired:
            print(f"❌ Claude fix timeout ({common.CLI_FIX_TIMEOUT}s)", file=sys.stderr)
            # Timeout kann mitten in einem Commit aufgeschlagen haben → roll back,
            # damit kein unpushed Commit als HEAD verbleibt.
            self._abort_without_push(head_before)
            return False
        if proc.returncode != 0:
            print(
                f"❌ Claude fix CLI exit {proc.returncode}:\n"
                f"{(proc.stderr or '')[:800]}",
                file=sys.stderr,
            )
            self._abort_without_push(head_before)
            return False

        # 1b) Verifizieren dass Claude tatsächlich committet hat — sonst würde
        #     `git push` still gegen einen unveränderten Branch erfolgreich sein
        #     und die Stage würde green melden ohne einen Fix zu publizieren.
        head_after = self._head_sha()
        if head_before is None or head_after is None or head_before == head_after:
            print(
                "❌ Claude fix produced no new commit — aborting without push. "
                f"(HEAD before={head_before!r}, after={head_after!r})",
                file=sys.stderr,
            )
            return False

        # 2) Typecheck + Tests müssen noch passen bevor wir pushen.
        tc = self.runner(
            ["pnpm", "-w", "typecheck"], cwd=self.worktree,
            timeout=common.PREFLIGHT_TYPECHECK_TIMEOUT,
        )
        if tc.returncode != 0:
            print(
                f"❌ Post-fix typecheck failed — aborting without push:\n"
                f"{(tc.stderr or '')[:800]}",
                file=sys.stderr,
            )
            self._abort_without_push(head_before)
            return False

        ts = self.runner(
            ["pnpm", "-w", "test", "--", "--run", "--changed",
             f"origin/{self.base_branch}"],
            cwd=self.worktree,
            timeout=common.PREFLIGHT_TEST_TIMEOUT,
        )
        if ts.returncode != 0:
            print(
                f"❌ Post-fix tests failed — aborting without push:\n"
                f"{(ts.stderr or '')[:800]}",
                file=sys.stderr,
            )
            self._abort_without_push(head_before)
            return False

        # 3) Push. Claude Code committet; wir pushen hier damit Push-Fehler
        #    sichtbar werden.
        #    Zwei nicht-offensichtliche Flags:
        #    - HEAD:refs/heads/<branch> refspec: stage.run_stage() erstellt den
        #      Worktree von einem rohen SHA, sodass HEAD detached ist. Normales
        #      `git push origin <branch>` sucht nach einer lokalen
        #      refs/heads/<branch> und schlägt fehl mit "src refspec ...
        #      does not match any". Der explizite Refspec pusht den detached
        #      Commit direkt auf den Remote-Branch-Ref.
        #    - --no-verify: Husky pre-push hook läuft das vollständige
        #      `pnpm typecheck + test + test:e2e` Suite (~3-5min), was sowohl
        #      die oben durchgeführte Verifikation dupliziert ALS AUCH den
        #      Subprocess-Timeout überschreitet. Fix-Loop hat typecheck +
        #      `pnpm test --changed` bereits zertifiziert; Hook zu umgehen
        #      ist hier sicher (aber NICHT für menschliche Pushes).
        push = self.runner(
            ["git", "push", "--no-verify", "origin",
             f"HEAD:refs/heads/{self.branch}"],
            cwd=self.worktree, timeout=180,
        )
        if push.returncode != 0:
            print(
                f"❌ git push failed:\n{(push.stderr or '')[:800]}",
                file=sys.stderr,
            )
            self._abort_without_push(head_before)
            return False
        return True


# ---------------------------------------------------------------------------
# CLI-Einstiegspunkt (wird normalerweise nicht direkt aufgerufen — Stage-Scripts
# orchestrieren via run_fix_loop())
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Führt einen einzelnen claude-fix-Schritt für eine Stage aus. "
                    "Stage-Scripts rufen run_fix_loop() normalerweise direkt auf.",
    )
    parser.add_argument("--stage", required=True,
                        choices=["code", "security", "design"])
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--base-branch", required=True)
    parser.add_argument("--branch", required=True)
    args = parser.parse_args(argv)

    fixer = ClaudeFixer(
        worktree=args.worktree,
        base_branch=args.base_branch,
        branch=args.branch,
    )
    ok = fixer(
        stage=args.stage, iteration=args.iteration,
        summary=args.summary, pr_number=args.pr_number,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
