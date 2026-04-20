"""Shared stage scaffold — each of the review stages is a thin wrapper.

A stage run:
 1. Resolves the PR + base + head SHA.
 2. Creates an isolated git worktree at the PR head.
 3. (Optional) Runs preflight (typecheck + changed tests) for LLM context.
 4. Invokes the stage's reviewer CLI (codex / gemini / claude) with the
    stage-specific prompt template.
 5. Parses the output for "is-clean" sentinels OR finding patterns.
 6. Posts a sticky advisory comment with the full reviewer output.
 7. Writes a commit-status (ai-review/{code|security|design}) = success/failure.
 8. If failure → delegates to fix_loop.run_fix_loop() with the stage's
    reviewer callback for re-checks and ClaudeFixer for the edits.
 9. Cleans up the worktree.

Stages are CLI-invoked by `.github/workflows/ai-{code,security,design}-review.yml`
and can also be run locally:
    python3 -m ai_review_pipeline.stages.code_review --pr 42 --skip-preflight

Portiert aus ai-portal/scripts/ai-review/stage.py (Wave 4b).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ai_review_pipeline import common, discord_notify, fix_loop, issue_context, preflight, scoring


# Codex' Default-Review-Prompt emittiert Findings im Format
# `- [P1] Titel — path:line` — ohne Backticks um den Pfad. common.SOURCE_FILE_RE
# matched NUR backtickte Pfade, also gehen Codex-Findings sonst an
# `treat_no_findings_as_clean=True` vorbei. Diese beiden Muster schließen die
# Lücke, ohne die Consensus-Pipeline zu berühren (parse_findings bleibt
# unverändert — Consensus verlangt weiterhin backtickte Referenzen).
_CODEX_SEVERITY_TAG_RE = re.compile(r"\[P[0-3]\]")
_CODEX_UNTICKED_FILE_LINE_RE = re.compile(
    r"(?<![`\w])"  # nicht von einem Backtick/Wortzeichen präfigiert (Anti-URL)
    r"[A-Za-z0-9_./\-]+"
    r"\.(?:ts|tsx|js|jsx|mjs|cjs|css|scss|json|md|yml|yaml|sh|py|surql|sql)"
    r":\d+\b"
)


def _has_codex_finding_markers(output: str) -> bool:
    """Erkennt Codex-Findings, die common.parse_findings (backtick-only) verpasst.

    True sobald eine `[P*]`-Severity-Tag ODER ein unbackticktes `path.ext:line`
    im Output vorkommt. Genutzt vom Code-Stage, wo wir den Reviewer-Prompt
    nicht durchreichen können (codex CLI lehnt `--base` + [PROMPT] ab).
    """
    if _CODEX_SEVERITY_TAG_RE.search(output):
        return True
    if _CODEX_UNTICKED_FILE_LINE_RE.search(output):
        return True
    return False


ReviewerCLI = Callable[[str, Path, str], str]
"""(prompt, worktree, base_branch) → reviewer output string.

Wraps one of run_codex / run_gemini / run_claude with its stage-specific
argument defaults. We let each stage choose since codex needs --title.
"""


@dataclass
class StageConfig:
    """Per-stage configuration plumbed through run_stage()."""

    name: str                       # "code" | "security" | "design"
    status_context: str             # common.STATUS_CODE etc.
    sticky_marker: str              # common.MARKER_CODE_REVIEW etc.
    title_prefix: str               # Sticky-comment title prefix
    prompt_file: str                # filename under prompts/ (e.g. "code_review.md")
    reviewer_label: str             # human-readable (e.g. "Codex GPT-5")
    ok_sentinels: tuple[str, ...]   # if the whole output matches one of these → green
    reviewer_fn: Callable[..., str] # common.run_codex / run_gemini / run_claude
    path_filter: Callable[[list[str]], bool] | None = None
    """Returns True if this stage should run given the list of changed files.
    None → always run. Used by the design stage to skip non-UI PRs."""

    treat_no_findings_as_clean: bool = False
    """Akzeptiere "keine geparste Finding-Line" als clean, auch ohne Sentinel.

    Notwendig für den Code-Stage: `codex review` lehnt `--base` + [PROMPT]
    als mutually exclusive ab, also können wir unseren REVIEW_PROMPT (der
    Codex anweisen würde, "LGTM" zu emittieren) nicht durchreichen. Codex
    nutzt stattdessen seinen Default-Review-Prompt — ohne dass wir den
    Wortlaut des "alles grün"-Outputs kontrollieren. Statt an einem fragilen
    Sentinel zu hängen, betrachten wir die Abwesenheit jeglicher
    parsebarer `path:line`-Findings als Freigabe (Fail-Safe bleibt: sobald
    ein Finding erscheint, ist der Output nicht clean).

    Security/Design bleiben bei False — dort steuern wir den Prompt selbst
    und verlangen explizit ein Sentinel.
    """


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Review prompt assembly (rebuilt on every iteration)
# ---------------------------------------------------------------------------

def _build_review_prompt(
    *,
    worktree: Path,
    base_branch: str,
    preflight_ctx: str,
    base_prompt: str,
    task_context: str = "",
    runner: common.Runner | None = None,
) -> str:
    """Build the full reviewer prompt from the CURRENT worktree state.

    WICHTIG: Diese Funktion MUSS bei jeder Review-Iteration frisch
    aufgerufen werden — der ClaudeFixer edit die Worktree zwischen
    Iterationen; ein once-computed `full_prompt` würde dem Reviewer immer
    den Original-Diff zeigen und sowohl gefixte Findings als auch neue
    Regressions unsichtbar machen.

    `task_context` (Wave 3): Wird VOR dem Diff prependet, damit der
    Reviewer weiß was die Task-Anforderung war bevor er Code urteilt.
    Leer-String = kein Block (legacy-kompatibel).
    """
    diff_kwargs = {"runner": runner} if runner is not None else {}
    diff_stat = common.git_diff_stat(worktree, base_branch, **diff_kwargs)
    diff_full = common.git_diff_full(worktree, base_branch, **diff_kwargs)
    parts: list[str] = []
    if task_context:
        parts.append(task_context)
    if preflight_ctx:
        parts.append(preflight_ctx)
    parts += [
        f"## Changed files summary\n\n```\n{diff_stat}\n```",
        f"## Full diff\n\n```diff\n{diff_full}\n```",
        base_prompt,
    ]
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Output classification
# ---------------------------------------------------------------------------

def is_clean_output(
    output: str,
    sentinels: tuple[str, ...],
    *,
    treat_no_findings_as_clean: bool = False,
) -> bool:
    """A reviewer returned one of the stage's OK sentinels and nothing else.

    We check the stripped output exactly matches one of the sentinels to avoid
    false-greens when a reviewer says 'LGTM, but see foo.ts:42 — wait scratch that'.

    `treat_no_findings_as_clean` (code-Stage only): Wenn der Reviewer-Prompt
    nicht gesteuert werden kann (Codex-CLI akzeptiert `--base` XOR [PROMPT]),
    nutzen wir die Abwesenheit parsebarer `path:line`-Findings als Freigabe.
    Sobald auch nur eine Finding-Line im Output auftaucht, ist der Output
    wieder nicht clean — Fail-Safe bleibt erhalten.
    """
    stripped = output.strip()
    # Also accept the sentinel appearing on a line by itself anywhere in the output,
    # as long as there are no findings (no backtick:line patterns detected).
    if any(stripped == s for s in sentinels):
        return True
    findings = common.parse_findings("check", output)
    if not findings:
        for line in stripped.splitlines():
            if line.strip() in sentinels:
                return True
        if treat_no_findings_as_clean:
            # Codex' Default-Prompt liefert Findings im Format `- [P1] … — path:line`
            # ohne Backticks. common.parse_findings (backtick-only) übersieht sie,
            # also greifen hier zusätzlich die Codex-spezifischen Marker.
            if _has_codex_finding_markers(output):
                return False
            return True
    return False


# ---------------------------------------------------------------------------
# Scoring-aware classification (Wave 2b)
# ---------------------------------------------------------------------------

# Role-Map: StageConfig.name → scoring.Role. Ändert Threshold-Regel für den
# Security-Veto-Pfad (≤7 = hard statt soft-band 5-7).
_NAME_TO_ROLE: dict[str, scoring.Role] = {
    "code": "code",
    "security": "security",
    "design": "design",
}


def classify_output(
    raw_output: str,
    cfg: "StageConfig",
) -> tuple[str, str, scoring.ScoredVerdict | None]:
    """Klassifiziert den Reviewer-Output in (state, description, scoring).

    Zwei Pfade — scoring-aware first, Sentinel-Fallback wenn kein JSON:

    1. **Scoring-Pfad**: Wenn der Reviewer einen validen JSON-Block liefert
       (via `parse_scored_verdict`), wendet `verdict_for_role` die role-
       abhängigen Thresholds an. Gibt (state, desc, sv) zurück mit sv.parse_failed
       = False bei Erfolg, True bei Schema-Drift (= failure, fail-closed).

    2. **Sentinel-Fallback** (legacy, backward-compat): Wenn kein parsbares JSON
       vorhanden ist, greift `is_clean_output()` — bestehende Sentinel-Logik.
       Returnt (state, desc, None).

    Der Sticky-Comment + Fix-Loop sehen am `raw_output` weiterhin den vollen
    LLM-Text. Scoring ist additive, kein Info-Verlust.
    """
    sv = scoring.parse_scored_verdict(raw_output)

    # Pfad 1: Scoring hat parse_failed=False ODER es gibt IRGENDEINEN JSON-Block
    # (auch einen fehlerhaften → parse_failed=True = fail-closed). Der einzige Fall
    # wo wir auf Sentinel-Fallback gehen ist "kein JSON-Block überhaupt gefunden".
    # Heuristik: `parse_scored_verdict` returnt summary="parse-fail: no JSON block
    # found in reviewer output" wenn kein JSON da war.
    has_json_block = not (sv.parse_failed and sv.summary.startswith("parse-fail: no JSON"))

    if has_json_block:
        if sv.parse_failed:
            # Schema-Drift: JSON war da, aber Schema invalide. Fail-closed.
            return "failure", f"score: 0/10 ({sv.summary})", sv

        role = _NAME_TO_ROLE.get(cfg.name, "code")
        effective_verdict = scoring.verdict_for_role(sv.score, role=role)
        desc = f"score: {sv.score}/10 ({effective_verdict}): {sv.summary[:100]}"
        if effective_verdict == "green":
            return "success", desc, sv
        return "failure", desc, sv

    # Pfad 2: Sentinel-Fallback
    is_clean = is_clean_output(
        raw_output, cfg.ok_sentinels,
        treat_no_findings_as_clean=cfg.treat_no_findings_as_clean,
    )
    if is_clean:
        return "success", f"{cfg.reviewer_label} clean (sentinel)", None
    return "failure", f"{cfg.reviewer_label} flagged findings", None


# ---------------------------------------------------------------------------
# Stage orchestration
# ---------------------------------------------------------------------------

def run_stage(
    cfg: StageConfig,
    *,
    pr_number: int,
    skip_preflight: bool = False,
    skip_fix_loop: bool = False,
    max_iterations: int = 2,  # reifung-v2: 4→2 (Circuit-Breaker)
    gh: common.GhClient | None = None,
    config: dict | None = None,
) -> int:
    """Return exit code: 0 green, 1 red-unresolved (escalation), 2 error."""
    gh = gh or common.GhClient()

    try:
        pr = gh.get_pr(pr_number)
    except Exception as exc:
        print(f"❌ Failed to fetch PR #{pr_number}: {exc}", file=sys.stderr)
        return 2

    if pr.get("isDraft"):
        print(f"⏭️  PR #{pr_number} is a draft — skipping {cfg.name} review.")
        return 0

    pr_title = pr["title"]
    pr_body = pr.get("body", "") or ""
    base_branch = pr["baseRefName"]
    head_sha = pr["headRefOid"]
    head_branch = pr["headRefName"]

    # Wave 3: Task-Context-Block bauen (PR-Body + gelinkte Issues + AC).
    # Fehler beim gh-Issue-Fetch führen zu leerem Block (kein Crash) —
    # Reviewer sieht dann nur PR-Title+Body, das ist weiterhin mehr als vorher.
    try:
        task_context = issue_context.build_task_context(
            pr_title=pr_title, pr_body=pr_body,
        )
    except Exception as exc:
        print(f"⚠️ issue_context.build_task_context failed: {exc}", file=sys.stderr)
        task_context = ""

    target_url = (
        os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        + f"/{common.REPO}/actions/runs/"
        + os.environ.get("GITHUB_RUN_ID", "")
    )

    # Mark as pending before we start — users see progress in the PR UI.
    try:
        gh.set_commit_status(
            sha=head_sha, context=cfg.status_context, state="pending",
            description=f"{cfg.reviewer_label} review in progress",
            target_url=target_url,
        )
    except Exception as exc:
        print(f"⚠️ Could not set pending status: {exc}", file=sys.stderr)

    tmp_parent = Path(tempfile.mkdtemp(prefix=f"ai-review-{cfg.name}-"))
    worktree = tmp_parent / f"pr{pr_number}"

    try:
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=common.REPO_ROOT, check=True, timeout=120,
        )
        subprocess.run(
            ["git", "worktree", "add", "--force", str(worktree), head_sha],
            cwd=common.REPO_ROOT, check=True, timeout=120,
        )

        # Stage-specific path filter (e.g. design-review skips non-UI PRs)
        if cfg.path_filter is not None:
            changed = common.git_changed_files(worktree, base_branch)
            if not cfg.path_filter(changed):
                print(f"⏭️  {cfg.name}: no relevant files changed — marking skipped.")
                gh.set_commit_status(
                    sha=head_sha, context=cfg.status_context, state="success",
                    # "skipped" state isn't valid in GitHub's API vocabulary (only
                    # success/failure/pending/error). We use success + "skipped"
                    # in the description; consensus.py reads the description to
                    # detect the skip and adjusts the denominator.
                    description=f"skipped — no {cfg.name}-relevant files changed",
                    target_url=target_url,
                )
                return 0

        base_prompt = load_prompt(cfg.prompt_file)

        preflight_ctx = ""
        if not skip_preflight:
            try:
                preflight_ctx = preflight.run_preflight(worktree, base_branch)
            except Exception as exc:
                preflight_ctx = f"## Pre-Flight CI Context\n\n### ⚠️ Error: {exc}"

        # Holds the last classification result for status-writing after
        # the do_review() closure runs. Tupel (state, desc, scoring) —
        # updated in-place jede Iteration, damit Escalation-Comment den
        # richtigen Score sieht.
        last_classification: dict[str, tuple[str, str, scoring.ScoredVerdict | None]] = {}

        def do_review() -> tuple[bool, str]:
            """One reviewer invocation. Returns (success, raw_output).

            Der Prompt wird hier — innerhalb der Closure — gebaut, damit
            jede Iteration den aktuellen Worktree-Stand sieht (nach Claudes
            Fix-Commits). Ein außerhalb der Fix-Loop berechneter Prompt
            würde den Reviewer gegen den Original-Diff urteilen lassen.

            Wave 2b: Scoring-aware Klassifikation via classify_output().
            Reviewer können jetzt strukturierten JSON-Block liefern; Fallback
            auf Sentinel-Logik bleibt für legacy-Pfad erhalten.
            """
            full_prompt = _build_review_prompt(
                worktree=worktree,
                base_branch=base_branch,
                preflight_ctx=preflight_ctx,
                base_prompt=base_prompt,
                task_context=task_context,
            )
            output = cfg.reviewer_fn(
                prompt=full_prompt,
                worktree=worktree,
                base_branch=base_branch,
                **({"pr_title": pr_title} if cfg.name == "code" else {}),
            )
            state, desc, sv = classify_output(output, cfg)
            last_classification["result"] = (state, desc, sv)
            return state == "success", output

        # Initial review
        initial_success, initial_output = do_review()

        # Wave 4: Rate-Limit-Detection → Stage skippt sich sauber.
        # Consensus-Logik erkennt description="skipped: …" und nimmt 2-of-N
        # Reviewer. Kein False-Failure mehr bei transienten API-Problemen.
        if not initial_success and common.detect_rate_limit(initial_output):
            print(f"⚠️  Rate-limit detected in {cfg.name} reviewer — skipping stage.")
            try:
                gh.set_commit_status(
                    sha=head_sha, context=cfg.status_context, state="success",
                    description="skipped: rate-limit — consensus uses other stages",
                    target_url=target_url,
                )
            except Exception as exc:
                print(f"⚠️ Failed to post skip status: {exc}", file=sys.stderr)
            return 0

        # Sticky comment with the raw reviewer output
        sticky_body = common.build_sticky_comment(
            marker=cfg.sticky_marker,
            title=f"{cfg.title_prefix} · PR #{pr_number}",
            head_sha=head_sha,
            sections=(
                [("🧪 Pre-Flight", preflight_ctx)] if preflight_ctx else []
            ) + [
                (f"🤖 {cfg.reviewer_label}", initial_output),
            ],
        )
        try:
            gh.post_sticky_comment(
                pr_number=pr_number, marker=cfg.sticky_marker, body=sticky_body,
            )
        except Exception as exc:
            print(f"⚠️ Sticky comment failed: {exc}", file=sys.stderr)

        # Wave 2b: Scoring-aware Status-Description (falls vom Reviewer geliefert).
        # Fallback auf legacy-Wording wenn kein JSON-Block geparst werden konnte.
        _classification = last_classification.get("result")
        scoring_desc = _classification[1] if _classification else None

        if initial_success:
            gh.set_commit_status(
                sha=head_sha, context=cfg.status_context, state="success",
                description=scoring_desc or f"{cfg.reviewer_label} clean",
                target_url=target_url,
            )
            print(f"✅ {cfg.name} review clean on first pass. ({scoring_desc or 'sentinel'})")
            return 0

        if skip_fix_loop:
            gh.set_commit_status(
                sha=head_sha, context=cfg.status_context, state="failure",
                description=scoring_desc or f"{cfg.reviewer_label} flagged findings (fix-loop skipped)",
                target_url=target_url,
            )
            return 1

        # Enter the fix loop — ClaudeFixer edits files, commits, pushes;
        # we re-run the reviewer between iterations.
        fixer = fix_loop.ClaudeFixer(
            worktree=worktree, base_branch=base_branch, branch=head_branch,
            max_iterations=max_iterations,
        )

        outcome = fix_loop.run_fix_loop(
            stage=cfg.name, pr_number=pr_number,
            review_fn=do_review, fix_fn=fixer,
            max_iterations=max_iterations,
        )

        # Re-resolve HEAD: ClaudeFixer may have pushed `[ai-fix]` commits to
        # the PR branch, in which case `head_sha` (read once before the loop)
        # is stale. The terminal status must attach to the current tip so
        # Branch Protection sees the green/red result on the latest commit.
        final_sha = common.current_head_sha(worktree, fallback=head_sha)

        if outcome.success:
            gh.set_commit_status(
                sha=final_sha, context=cfg.status_context, state="success",
                description=f"{cfg.reviewer_label} green after {outcome.iterations} iterations",
                target_url=target_url,
            )
            return 0

        # Escalation path
        escalation = fix_loop.build_escalation_comment(
            stage=cfg.name, iterations=outcome.iterations,
            summaries=outcome.summaries, pr_number=pr_number,
        )
        try:
            gh.post_sticky_comment(
                pr_number=pr_number,
                marker=f"<!-- nexus-ai-review-{cfg.name}-escalation -->",
                body=escalation,
            )
        except Exception as exc:
            print(f"⚠️ Escalation comment failed: {exc}", file=sys.stderr)

        # Wave 4b (Phase 5 Cutover): Discord-Alert an Nico via ops-n8n Webhook.
        # Wenn Discord nicht konfiguriert → notify_discord gibt False zurück,
        # Pipeline läuft trotzdem durch — PR-Comment ist primärer Alert-Kanal.
        try:
            last_score = None
            # Letzte Summary parsen um den finalen Score zu extrahieren
            if outcome.summaries:
                sv = scoring.parse_scored_verdict(outcome.summaries[-1])
                if not sv.parse_failed:
                    last_score = sv.score
            pr_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{common.REPO}/pull/{pr_number}"
            last_summary = (outcome.summaries[-1] if outcome.summaries else "")[:500]
            alert_ok = discord_notify.notify_discord(
                discord_notify.DiscordNotifyPayload(
                    event_type="escalation",
                    pr_url=pr_url,
                    repo=common.REPO,
                    pr_number=pr_number,
                    consensus_score=float(last_score) if last_score is not None else 0.0,
                    stage_scores={
                        "stage": cfg.name,
                        "iterations": outcome.iterations,
                        "last_score": last_score,
                    },
                    findings=[last_summary] if last_summary else [],
                    button_actions=[],
                    channel_id=None,
                    mention_role="@here",
                    sticky_message=None,
                ),
                config or {},
            )
            if alert_ok:
                print(f"📨 Discord-Alert gesendet für {cfg.name}-Escalation auf PR #{pr_number}")
        except Exception as exc:
            # Alert-Fehler NIEMALS blockieren — wir haben den PR-Comment als Fallback.
            print(f"⚠️ Discord-Alert failed: {exc}", file=sys.stderr)

        gh.set_commit_status(
            sha=final_sha, context=cfg.status_context, state="failure",
            description=f"{cfg.reviewer_label} unresolved after {outcome.iterations} iterations — human review",
            target_url=target_url,
        )
        return 1

    except Exception as exc:
        print(f"💥 Stage {cfg.name} crashed: {exc}", file=sys.stderr)
        try:
            gh.set_commit_status(
                sha=head_sha if "head_sha" in locals() else "HEAD",
                context=cfg.status_context, state="error",
                description=f"Stage crashed: {str(exc)[:80]}",
                target_url=target_url,
            )
        except Exception:
            pass
        return 2
    finally:
        try:
            subprocess.run(
                ["git", "worktree", "remove", str(worktree), "--force"],
                cwd=common.REPO_ROOT, timeout=60, check=False,
            )
            shutil.rmtree(tmp_parent, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared CLI argument wiring (each stage script re-uses this)
# ---------------------------------------------------------------------------

def build_arg_parser(stage_name: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=f"AI {stage_name} review stage")
    ap.add_argument("--pr", type=int, required=True, help="PR number")
    ap.add_argument("--skip-preflight", action="store_true")
    ap.add_argument("--skip-fix-loop", action="store_true",
                    help="Only run the initial review — don't enter claude-fix loop.")
    ap.add_argument("--max-iterations", type=int, default=2,
                    help="reifung-v2: Default 2 (Circuit-Breaker); ältere Workflows "
                         "überschreiben via CLI-Arg.")
    return ap
