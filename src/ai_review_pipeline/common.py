"""Shared primitives for the AI review pipeline.

Portiert aus ai-portal/scripts/ai-review/common.py.

All subprocess-driven functions accept a `runner` callable (default: a thin
wrapper around `subprocess.run`). Tests inject a `FakeRunner` that records
calls and returns canned responses — no real CLI or network I/O in tests.

This module is intentionally dependency-light (stdlib only) so it runs
unchanged on the r2d2 self-hosted runner, inside the n8n-portal container,
and in any ad-hoc developer shell.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol


# ---------------------------------------------------------------------------
# Repo constants
# ---------------------------------------------------------------------------

# Default-Repo — kann per GhClient(repo=...) überschrieben werden.
REPO = "EtroxTaran/ai-portal"

# REPO_ROOT: im Gegensatz zur ai-portal-Version kennen wir hier keinen
# fixen Repo-Root. Default ist das aktuelle Working Directory; Aufrufer können
# default_runner mit explizitem cwd aufrufen oder REPO_ROOT überschreiben.
REPO_ROOT = Path.cwd()

# Sticky-comment markers — one per pipeline stage + one for the consensus view.
MARKER_CODE_REVIEW = "<!-- nexus-ai-review-code -->"
MARKER_SECURITY_REVIEW = "<!-- nexus-ai-review-security -->"
MARKER_DESIGN_REVIEW = "<!-- nexus-ai-review-design -->"
MARKER_CODE_CURSOR_REVIEW = "<!-- nexus-ai-review-code-cursor -->"
MARKER_CONSENSUS = "<!-- nexus-ai-review-consensus -->"

# Commit-status contexts (consumed by Branch Protection as required checks).
# Wave 5a: STATUS_CODE_CURSOR added — zweiter Code-Reviewer via Cursor CLI.
# Consensus-Logik aggregiert STATUS_CODE + STATUS_CODE_CURSOR zu einem
# virtuellen "code-consensus" vor dem overall 2-of-3-Check.
STATUS_CODE = "ai-review/code"
STATUS_CODE_CURSOR = "ai-review/code-cursor"
STATUS_SECURITY = "ai-review/security"
STATUS_DESIGN = "ai-review/design"
STATUS_CONSENSUS = "ai-review/consensus"
# Wave 7a: Security-Waiver — separater status, damit Audit-Trail getrennt
# vom Security-Review-Ergebnis ist. Security-Report bleibt rot (failure),
# Waiver überschreibt nur den Consensus.
STATUS_SECURITY_WAIVER = "ai-review/security-waiver"

STAGE_STATUS_CONTEXTS = (
    STATUS_CODE, STATUS_CODE_CURSOR, STATUS_SECURITY, STATUS_DESIGN,
)

# GitHub commit-status allowed states (API contract)
VALID_STATES = frozenset({"success", "failure", "pending", "error"})

# Size limits — GitHub comment body is capped at 65_536 chars
MAX_SECTION_CHARS = 28_000
MAX_DIFF_CHARS = 100_000
MAX_PREFLIGHT_OUTPUT_CHARS = 4_000

# CLI-invocation timeouts (seconds)
CLI_REVIEW_TIMEOUT = 300     # 5 min per LLM call
# reifung-v2 (2026-04-19): 1500s → 480s pro Fix-Invocation.
# Rationale: Circuit-Breaker. 8 min ist genug Kopfraum für 1–3 Edits +
# Typecheck im ClaudeFixer-Subprocess; darüber ist die Iteration fast nie
# konvergent (Industry-Median laut Perplexity/LangGraph-Telemetrie). Kombiniert
# mit max_iterations=2 ergibt 2×(480+300)=1560s worst-case pro Stage — passt
# unter den neuen 20-min Job-Timeout in den Workflow-Files.
CLI_FIX_TIMEOUT = 480
PREFLIGHT_TYPECHECK_TIMEOUT = 240
PREFLIGHT_TEST_TIMEOUT = 300


# ---------------------------------------------------------------------------
# Runner protocol — dependency-injection seam for all subprocess calls
# ---------------------------------------------------------------------------

class Runner(Protocol):
    """Callable that runs a command and returns a CompletedProcess-like object.

    Must return an object with .returncode, .stdout, .stderr attrs.
    """

    def __call__(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        stdin_data: str | None = None,
    ) -> Any: ...  # noqa: E501


def default_runner(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    stdin_data: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Production Runner: calls `subprocess.run` with text-mode + stderr capture."""
    return subprocess.run(  # noqa: S603 — cmd is a trusted list[str], never a shell string
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd or REPO_ROOT,
        timeout=timeout,
        env=env,
        input=stdin_data,
        check=False,
    )


# ---------------------------------------------------------------------------
# Git helpers (used across stage + fix_loop)
# ---------------------------------------------------------------------------

def current_head_sha(
    worktree: Path,
    *,
    fallback: str,
    runner: Runner = default_runner,
) -> str:
    """Resolve the worktree's current HEAD sha; return `fallback` on failure.

    Wird nach der Fix-Loop in stage.run_stage() benutzt: ClaudeFixer pushed
    `[ai-fix]`-Commits, dadurch ist der ursprünglich aus der GitHub-API
    gelesene `head_sha` veraltet. Der finale `set_commit_status` muss am
    aktuellen Tip hängen, sonst sieht Branch Protection den grünen Status
    nicht auf dem PR-Head.
    """
    try:
        proc = runner(
            ["git", "rev-parse", "HEAD"], cwd=worktree, timeout=15,
        )
    except Exception:
        return fallback
    if getattr(proc, "returncode", 1) != 0:
        return fallback
    sha = (getattr(proc, "stdout", "") or "").strip()
    return sha or fallback


# ---------------------------------------------------------------------------
# Source-reference regex (findings parser)
# ---------------------------------------------------------------------------

# Two alternations, BOTH requiring an explicit colon between path and line.
# Without the colon `foo.ts42` (no separator) would match and false-positive.
SOURCE_FILE_RE = re.compile(
    r'`([A-Za-z0-9_./\-]+\.(?:ts|tsx|js|jsx|mjs|cjs|css|scss|json|md|yml|yaml|sh|py|surql|sql))`?:(\d+)\b|'
    r'`([A-Za-z0-9_./\-]+\.(?:ts|tsx|js|jsx|mjs|cjs|css|scss|json|md|yml|yaml|sh|py|surql|sql)):(\d+)`'
)


# ---------------------------------------------------------------------------
# Pure helpers (string formatting)
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def strip_ansi(text: str) -> str:
    """Remove ANSI color / cursor escape sequences from CLI output."""
    return _ANSI_RE.sub("", text)


def truncate(text: str, max_chars: int) -> str:
    """Truncate from the TAIL (keep the beginning) with a visible marker."""
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + "\n\n_(Output gekürzt — lokal ausführen für vollständigen Report)_"
    )


def tail(text: str, max_chars: int) -> str:
    """Truncate from the HEAD (keep the end) — useful for test/typecheck output."""
    if len(text) <= max_chars:
        return text
    return (
        f"_(Anfang gekürzt — letzte {max_chars} Zeichen)_\n\n"
        + text[-max_chars:]
    )


# ---------------------------------------------------------------------------
# Findings parsing + Multi-Model Consensus
# ---------------------------------------------------------------------------

Finding = dict  # TypedDict-lite: {"path": str, "line": int, "snippet": str, "model": str}


def parse_findings(model_name: str, output: str) -> list[Finding]:
    """Extract backtick-quoted `path:line` references from CLI output.

    Dedupes within a single model call (same path+line only counted once).
    """
    seen: set[tuple[str, int]] = set()
    findings: list[Finding] = []

    for line in output.split("\n"):
        for match in SOURCE_FILE_RE.finditer(line):
            path = match.group(1) or match.group(3)
            lineno_str = match.group(2) or match.group(4)
            if not path or not lineno_str:
                continue
            try:
                lineno = int(lineno_str)
            except ValueError:
                continue

            key = (path, lineno)
            if key in seen:
                continue
            seen.add(key)

            snippet = line.strip().lstrip("-•*").strip()[:300]
            findings.append({
                "path": path,
                "line": lineno,
                "snippet": snippet,
                "model": model_name,
            })
    return findings


def find_consensus(all_findings: Iterable[Finding]) -> list[dict]:
    """Group findings by (path, line) and return those flagged by ≥2 distinct models.

    Same model emitting the same path:line twice does NOT count as consensus —
    consensus requires multiple independent model perspectives.
    """
    grouped: dict[tuple[str, int], list[Finding]] = defaultdict(list)
    for f in all_findings:
        grouped[(f["path"], f["line"])].append(f)

    consensus: list[dict] = []
    for (path, line), items in grouped.items():
        models = sorted({i["model"] for i in items})
        if len(models) >= 2:
            consensus.append({
                "path": path,
                "line": line,
                "snippets": [i["snippet"] for i in items],
                "models": models,
            })
    consensus.sort(key=lambda c: (c["path"], c["line"]))
    return consensus


# ---------------------------------------------------------------------------
# Consensus-status computation (for ai-review/consensus)
# ---------------------------------------------------------------------------

# Wave 6b: Confidence-weighted thresholds für Code-Consensus avg-score.
# Empirisch begründet via O'Reilly Radar 2026-04 (confidence-weighted triage
# schlägt strict-agreement) — strict-binary produziert zu viele False-
# Positives bei Reviewer-Disagreement.
CODE_CONSENSUS_GREEN_THRESHOLD = 8.0  # avg >= 8 → success
CODE_CONSENSUS_SOFT_THRESHOLD = 5.0   # 5 <= avg < 8 → soft (Nachfrage-Pfad)


def resolve_code_consensus(
    code_state: str | None,
    code_cursor_state: str | None,
    *,
    code_score: int | None = None,
    cursor_score: int | None = None,
) -> str:
    """Fügt Codex + Cursor zu einem virtuellen code-consensus zusammen.

    Wave 5b (binär): state-basierte Regel (success/failure/skipped/pending).
    Wave 6b (weighted): wenn BEIDE Scores vorliegen, nutzen wir avg-score mit
    drei-Zonen-Mapping (success/soft/failure) statt der strikten Binär-Regel.
    Das gibt uns ein differenziertes „soft"-Verdict für den Nachfrage-Pfad.

    Regeln (state-Teil, wenn Scores nicht beide verfügbar):
      - Beide success                  → success (Agreement green)
      - Beide skipped (Rate-Limit o.ä.)→ skipped
      - Beide failure                  → failure (Agreement hard)
      - Ein success + ein skipped      → success (Quorum reicht)
      - Ein failure + ein skipped      → failure
      - Ein success + ein failure      → failure (strict fail-safe)
      - Irgendwas pending              → pending
      - code-cursor None / missing     → treat als skipped (backward-compat)

    Regeln (weighted-Teil, wenn beide Scores vorhanden + keine pending):
      - avg >= 8                       → success
      - 5 <= avg < 8                   → soft (neu! triggert Nachfrage)
      - avg < 5                        → failure

    Returns: "success" | "soft" | "failure" | "skipped" | "pending"
    """
    a = code_state or "skipped"
    b = code_cursor_state or "skipped"

    if "pending" in (a, b):
        return "pending"

    # Wave 6b: Weighted-score path — nur aktiv wenn BEIDE Scores vorliegen
    # (sonst bleibt die Aussagekraft schwach, Fallback auf Binär).
    if code_score is not None and cursor_score is not None:
        avg = (code_score + cursor_score) / 2.0
        if avg >= CODE_CONSENSUS_GREEN_THRESHOLD:
            return "success"
        if avg >= CODE_CONSENSUS_SOFT_THRESHOLD:
            return "soft"
        return "failure"

    # Binäre Fallback-Logik (Wave 5b bleibt erhalten):
    active = [v for v in (a, b) if v != "skipped"]
    if not active:
        return "skipped"
    if all(v == "success" for v in active):
        return "success"
    return "failure"


def consensus_status(
    stage_states: dict[str, str],
    *,
    code_score: int | None = None,
    cursor_score: int | None = None,
) -> tuple[str, str]:
    """Compute (state, description) for the `ai-review/consensus` commit-status.

    Rules:
      - Code-Stufe (Codex + Cursor) wird via `resolve_code_consensus` zu
        einem virtuellen `code-consensus` zusammengeführt (Wave 5b).
      - Wave 6b: Wenn beide Code-Scores gesetzt sind, nutzt die Code-Sub-
        Logik weighted-avg (kann "soft"-State zurückgeben).
      - Code-consensus "soft" → overall consensus "pending" mit Nachfrage-
        Description (triggert den Nachfrage-Pfad).
      - Triple daraus: {code-consensus, security, design}.
      - Any pending → consensus pending.
      - Skipped stages drop out of numerator + denominator.
      - ≥2 success (bei denom=3) ODER denom=1 + 1 success → success.
      - Alles skipped/pending → pending.
      - Sonst → failure.
    """
    code_state = stage_states.get(STATUS_CODE)
    code_cursor_state = stage_states.get(STATUS_CODE_CURSOR)

    # Wave 5b/6b: Code-Sub-Consensus (Codex + Cursor)
    code_consensus = resolve_code_consensus(
        code_state, code_cursor_state,
        code_score=code_score,
        cursor_score=cursor_score,
    )

    # Wave 6b: soft-state → pending mit Nachfrage-Description.
    # Der nachgelagerte Workflow (ai-review-nachfrage.yml) erkennt das an
    # der Description und postet den Sticky-Comment mit den 3 Optionen.
    if code_consensus == "soft":
        if code_score is not None and cursor_score is not None:
            avg = (code_score + cursor_score) / 2.0
            desc = (
                f"Code-review needs human ACK — avg score {avg:.1f}/10 "
                f"(codex={code_score}, cursor={cursor_score})"
            )
        else:
            desc = "Code-review needs human ACK — borderline verdict"
        return "pending", desc

    # Falls code-cursor nicht existiert und code explicit pending ist,
    # sollen wir pending bleiben (Backward-Compat: alte PRs ohne Cursor-stage
    # zeigen nur `code` state).
    security_state = stage_states.get(STATUS_SECURITY, "pending")
    design_state = stage_states.get(STATUS_DESIGN, "pending")
    # Wave 7a: Security-Waiver kann ein failing Security-Review überschreiben
    # (mit Audit-Trail). security_waiver_state == "success" bedeutet Nico hat
    # `/ai-review security-waiver <reason>` mit valider Begründung gepostet.
    security_waiver_state = stage_states.get(STATUS_SECURITY_WAIVER, "pending")

    triple = {
        "code-consensus": code_consensus,
        STATUS_SECURITY: security_state,
        STATUS_DESIGN: design_state,
    }

    if any(s == "pending" for s in triple.values()):
        return "pending", "Waiting for stages to complete"

    # Security-Veto (Wave 2b / Wave 5b): Security ist ein Hard-Gate. Wenn der
    # Security-Reviewer failure meldet, blockt das Consensus unabhängig von
    # den anderen Stimmen. Rationale: Security-Miss ist asymmetrisch teuer,
    # und die Pipeline-Rolle ist explizit als „kein soft-band" designt.
    #
    # Wave 7a: Waiver-Ausnahme — nur wenn Nico einen dokumentierten
    # Security-Waiver mit ≥30-Zeichen-Begründung gepostet hat, darf consensus
    # trotzdem grün werden. Security-Status selbst bleibt failure (audit-trail).
    if security_state == "failure" and security_waiver_state != "success":
        return "failure", "Security-Veto: ai-review/security = failure"

    completed = {k: v for k, v in triple.items() if v != "skipped"}
    if not completed:
        return "pending", "All stages skipped — no review performed yet"

    success_count = sum(1 for v in completed.values() if v == "success")
    total = len(completed)

    # Bau eine Description, die Doppel-Code-Review explizit macht
    code_detail = ""
    if code_state and code_cursor_state:
        code_detail = (
            f" [codex={code_state[:4]}, cursor={code_cursor_state[:4]}"
            f" → {code_consensus}]"
        )
    desc = f"{success_count}/{total} AI reviewers green{code_detail}"

    if success_count >= 2 or (total == 1 and success_count == 1):
        return "success", desc
    return "failure", desc


# ---------------------------------------------------------------------------
# Sticky-comment builder
# ---------------------------------------------------------------------------

def build_sticky_comment(
    *,
    marker: str,
    title: str,
    head_sha: str,
    sections: list[tuple[str, str]],
) -> str:
    """Build a collapsible-sections sticky comment body.

    Each section is a (label, content) pair rendered inside a <details> block.
    Individual sections are truncated to MAX_SECTION_CHARS so one verbose LLM
    can't blow the 65_536 GitHub comment cap.
    """
    parts: list[str] = [
        marker,
        f"## {title}",
        "",
        f"> Commit `{head_sha[:8]}`",
        "",
    ]
    for label, content in sections:
        parts += [
            "<details>",
            f"<summary><strong>{label}</strong></summary>",
            "",
            "```",
            truncate(content, MAX_SECTION_CHARS),
            "```",
            "",
            "</details>",
            "",
        ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI wrappers (Codex, Gemini, Claude) — all accept injected `runner`
# ---------------------------------------------------------------------------

def _safe_stdout(proc: Any) -> str:
    out = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    return strip_ansi(out).strip()


# Wave 4: Rate-Limit-Detection
# Matcht gängige Phrasen aus OpenAI/Anthropic/Google-APIs + CLI-Fehlermeldungen.
# Wenn der Output eines CLI-Reviewers einem dieser Muster entspricht, skippt
# die Stage sich sauber (state=success, description='skipped: rate-limit') und
# der Consensus nimmt 2-of-N Reviewer statt als failure zu werten.
_RATE_LIMIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b429\b"),
    re.compile(r"\brate[\s_-]?limit", re.IGNORECASE),
    re.compile(r"\bquota\s+exceeded", re.IGNORECASE),
    re.compile(r"\busage\s+limit", re.IGNORECASE),
    re.compile(r"too\s+many\s+requests", re.IGNORECASE),
)


def detect_rate_limit(output: str) -> bool:
    """True wenn der CLI-Output wie ein Rate-Limit aussieht.

    Wir greifen sowohl stdout als auch stderr ab (in _safe_stdout kombiniert),
    und Matchen gegen eine kleine Liste expliziter Muster. Falsche Positiv
    (zB ein PR, der das Wort 'rate limit' in einem Commit-Message erwähnt)
    sind hier akzeptabel: der Stage skippt dann, Consensus nimmt 2-of-N,
    und Nico kann per Comment-Kommando re-triggern.
    """
    if not output:
        return False
    return any(p.search(output) for p in _RATE_LIMIT_PATTERNS)


def run_codex(
    *,
    prompt: str,
    worktree: Path,
    base_branch: str,
    pr_title: str,
    runner: Runner = default_runner,
    timeout: int = CLI_REVIEW_TIMEOUT,
) -> str:
    """Invoke the Codex CLI (`codex review` subcommand, GPT-5).

    Der `codex review`-Parser behandelt `--base` und den positionalen
    `[PROMPT]` als gegenseitig exklusiv ("cannot be used with"). Wir wählen
    den `--base`-Scope und reichen unsere Review-Instruktionen via stdin
    durch (`--` + stdin-Pipe fällt aus — der Parser kennt nur den
    positionalen Prompt). Konsequenz: Codex nutzt seinen eigenen Default-
    Review-Prompt, während Claude/Gemini weiterhin unseren REVIEW_PROMPT
    konsumieren. Der `prompt`-Parameter bleibt Teil der Signatur für API-
    Kompatibilität, wird aber nicht mehr an die CLI durchgereicht.
    """
    del prompt  # bewusst verworfen — siehe Docstring
    try:
        proc = runner(
            [
                "codex", "review",
                "--base", f"origin/{base_branch}",
                "--title", pr_title,
            ],
            cwd=worktree,
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        return f"_(Codex Timeout nach {timeout}s — lokal erneut laufen lassen)_"
    return _safe_stdout(proc) or "_(Codex hat keinen Output produziert)_"


def run_gemini(
    *,
    prompt: str,
    worktree: Path,
    base_branch: str,
    runner: Runner = default_runner,
    model: str = "gemini-2.5-pro",
    timeout: int = CLI_REVIEW_TIMEOUT,
) -> str:
    """Invoke Gemini CLI. -m MUST come before -p (yargs ordering bug)."""
    try:
        proc = runner(
            ["gemini", "-m", model, "-p", prompt],
            cwd=worktree,
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        return f"_(Gemini Timeout nach {timeout}s — lokal erneut laufen lassen)_"
    return _safe_stdout(proc) or "_(Gemini hat keinen Output produziert)_"


def run_claude(
    *,
    prompt: str,
    worktree: Path,
    base_branch: str,
    runner: Runner = default_runner,
    model: str = "claude-opus-4-7",
    timeout: int = CLI_REVIEW_TIMEOUT,
) -> str:
    """Invoke Claude Code CLI in print-mode (-p)."""
    try:
        proc = runner(
            ["claude", "--model", model, "-p", prompt],
            cwd=worktree,
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        return f"_(Claude Timeout nach {timeout}s — lokal erneut laufen lassen)_"
    return _safe_stdout(proc) or "_(Claude hat keinen Output produziert)_"


def _extract_cursor_result(raw: str) -> str:
    """Extrahiert das `result`-Feld aus Cursor-agent JSON-Output.

    Cursor-agent `--output-format json` produziert pro Aufruf genau ein
    JSON-Objekt mit `{type: "result", subtype: "success", result: "<text>",
    session_id, usage, duration_ms}`. Bei parse-failure (z.B. bei Errors
    die stderr-Text mitbringen) fallen wir auf den raw-string zurück, damit
    der Review-Sticky-Comment nicht leer bleibt.
    """
    try:
        data = json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        return raw
    if not isinstance(data, dict):
        return raw
    result = data.get("result")
    if isinstance(result, str) and result.strip():
        return result
    # Fallback — manchmal landet der Text in `messages[-1].content`
    messages = data.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            content = last.get("content") or last.get("text")
            if isinstance(content, str):
                return content
    return raw


def run_cursor(
    *,
    prompt: str,
    worktree: Path,
    base_branch: str,
    runner: Runner = default_runner,
    model: str = "composer-2",
    timeout: int = CLI_REVIEW_TIMEOUT,
) -> str:
    """Invoke Cursor Agent CLI (zweiter Code-Reviewer, Wave 5a).

    Cursor akzeptiert den prompt als **positionales** Argument (nicht -p wie
    Gemini/Claude). `--print` macht's non-interactive, `--force` skippt den
    Workspace-Trust-Prompt (wir laufen auf einem trusted self-hosted Runner).
    `--output-format json` damit wir `result` deterministisch extrahieren.

    Model-Default: `composer-2` — Cursor's eigenes Modell, maximale Vendor-
    Diversität gegenüber Codex (GPT-5). Fallback-Optionen: `auto`,
    `claude-4.6-sonnet-medium`, `gpt-5.3-codex`.
    """
    try:
        proc = runner(
            [
                "cursor-agent",
                "--print",
                "--force",
                "--output-format", "json",
                "--model", model,
                prompt,
            ],
            cwd=worktree,
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        return f"_(Cursor Timeout nach {timeout}s — lokal erneut laufen lassen)_"
    raw = _safe_stdout(proc)
    if not raw:
        return "_(Cursor hat keinen Output produziert)_"
    return _extract_cursor_result(raw)


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def git_diff_stat(
    worktree: Path, base_branch: str, *, runner: Runner = default_runner,
) -> str:
    proc = runner(
        ["git", "diff", f"origin/{base_branch}...HEAD", "--stat"],
        cwd=worktree, timeout=30,
    )
    return _safe_stdout(proc)


def git_diff_full(
    worktree: Path, base_branch: str, *, runner: Runner = default_runner,
    max_chars: int = MAX_DIFF_CHARS,
) -> str:
    proc = runner(
        ["git", "diff", f"origin/{base_branch}...HEAD"],
        cwd=worktree, timeout=60,
    )
    out = _safe_stdout(proc)
    if len(out) > max_chars:
        return out[:max_chars] + "\n... (Diff gekürzt)"
    return out


def git_changed_files(
    worktree: Path, base_branch: str, *, runner: Runner = default_runner,
) -> list[str]:
    proc = runner(
        ["git", "diff", "--name-only", f"origin/{base_branch}...HEAD"],
        cwd=worktree, timeout=30,
    )
    out = _safe_stdout(proc)
    return [line for line in out.split("\n") if line.strip()]


# ---------------------------------------------------------------------------
# GitHub API client (via `gh` CLI — auth comes from `gh auth login`)
# ---------------------------------------------------------------------------

@dataclass
class GhClient:
    """Thin wrapper around the `gh` CLI with a DI-friendly runner.

    All methods raise RuntimeError on non-zero exit (except the sticky-comment
    lookup, which tolerates "no existing comment" as a normal case).
    """

    runner: Runner = field(default=default_runner)
    repo: str = REPO

    def _gh(self, *args: str, stdin_data: str | None = None,
            timeout: int = 60) -> Any:
        proc = self.runner(
            ["gh", *args],
            cwd=REPO_ROOT,
            timeout=timeout,
            stdin_data=stdin_data,
        )
        return proc

    # --- PR meta ---------------------------------------------------------

    def get_pr(self, pr_number: int) -> dict:
        # `body` (Wave 3) wird für Issue-Context-Extraction gebraucht.
        proc = self._gh(
            "pr", "view", str(pr_number),
            "--json", "title,body,baseRefName,headRefOid,isDraft,headRefName",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"gh pr view {pr_number} failed: {proc.stderr}")
        return json.loads(proc.stdout)

    def get_pr_for_current_branch(self) -> int:
        proc = self._gh("pr", "view", "--json", "number", "-q", ".number")
        if proc.returncode != 0 or not proc.stdout.strip().isdigit():
            raise RuntimeError("No open PR for current branch")
        return int(proc.stdout.strip())

    # --- Commit statuses -------------------------------------------------

    def set_commit_status(
        self, *, sha: str, context: str, state: str, description: str,
        target_url: str | None = None,
    ) -> None:
        if state not in VALID_STATES:
            raise ValueError(
                f"state must be one of {sorted(VALID_STATES)}, got {state!r}"
            )
        # Description is capped at 140 chars by the GitHub API
        description = description[:140]
        fields = [
            "-f", f"state={state}",
            "-f", f"context={context}",
            "-f", f"description={description}",
        ]
        if target_url:
            fields += ["-f", f"target_url={target_url}"]

        proc = self._gh(
            "api", "-X", "POST",
            f"repos/{self.repo}/statuses/{sha}",
            *fields,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"set_commit_status({context}={state}) failed: {proc.stderr}"
            )

    def get_commit_status_details(self, sha: str) -> dict[str, dict]:
        """Wave 6b: Liefert `{context: {state, description}}` — volle Details.

        Wird vom consensus-aggregator genutzt, um Scores aus Status-
        Descriptions zu parsen (Format: `"score: N/10 (verdict): ..."`).
        """
        proc = self._gh(
            "api", f"repos/{self.repo}/commits/{sha}/status",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"get_commit_status_details failed: {proc.stderr}")
        data = json.loads(proc.stdout or "{}")
        result: dict[str, dict] = {}
        for s in data.get("statuses", []):
            ctx = s.get("context")
            if not ctx or ctx in result:
                continue
            state = s.get("state", "pending")
            description = s.get("description") or ""
            # Skipped-Normalisierung (siehe get_commit_statuses)
            if state == "success" and description.strip().lower().startswith("skipped"):
                state = "skipped"
            result[ctx] = {"state": state, "description": description}
        return result

    def get_commit_statuses(self, sha: str) -> dict[str, str]:
        """Return a dict of {context: state} for the most-recent status per context.

        Normalization: GitHub's status API only supports
        success/failure/pending/error. Our stage.py encodes a *skipped* stage
        as `state=success` with a description beginning with `skipped`
        (see stage.py — skipped path). We detect that convention here and
        surface the pseudo-state `skipped` so `consensus_status` can drop
        the stage from the denominator instead of counting it as green.
        """
        proc = self._gh(
            "api", f"repos/{self.repo}/commits/{sha}/status",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"get_commit_statuses failed: {proc.stderr}")
        data = json.loads(proc.stdout or "{}")
        result: dict[str, str] = {}
        # The API returns statuses sorted newest-first — first occurrence wins
        for s in data.get("statuses", []):
            ctx = s.get("context")
            if not ctx or ctx in result:
                continue
            state = s.get("state", "pending")
            description = (s.get("description") or "").strip().lower()
            if state == "success" and description.startswith("skipped"):
                state = "skipped"
            result[ctx] = state
        return result

    # --- Sticky comments -------------------------------------------------

    def _find_sticky_comment_id(self, pr_number: int, marker: str) -> str | None:
        proc = self._gh(
            "api",
            f"repos/{self.repo}/issues/{pr_number}/comments",
            "--jq",
            f'[.[] | select(.body | contains("{marker}"))] | first | .id',
        )
        val = (proc.stdout or "").strip()
        return val if val and val != "null" else None

    def post_sticky_comment(
        self, *, pr_number: int, marker: str, body: str,
    ) -> None:
        """Upsert a comment identified by `marker`."""
        existing = self._find_sticky_comment_id(pr_number, marker)
        if existing:
            proc = self._gh(
                "api", "-X", "PATCH",
                f"repos/{self.repo}/issues/comments/{existing}",
                "-f", f"body={body}",
            )
        else:
            proc = self._gh(
                "pr", "comment", str(pr_number), "--body", body,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"post_sticky_comment failed: {proc.stderr}")

    # --- Reviews ---------------------------------------------------------

    def post_review(
        self, *, pr_number: int, head_sha: str, body: str, event: str,
        line_comments: list[dict] | None = None,
    ) -> None:
        """Post a review (optionally with inline line comments).

        `event` ∈ {APPROVE, REQUEST_CHANGES, COMMENT}.
        """
        payload = {
            "commit_id": head_sha,
            "body": body,
            "event": event,
            "comments": line_comments or [],
        }
        proc = self._gh(
            "api", "-X", "POST",
            f"repos/{self.repo}/pulls/{pr_number}/reviews",
            "--input", "-",
            stdin_data=json.dumps(payload),
        )
        if proc.returncode != 0:
            # Log-only: REQUEST_CHANGES on own PR is denied; line not-in-diff is denied.
            print(
                f"⚠️ post_review({event}) failed — häufig: self-review oder line out-of-diff:\n"
                f"   {(proc.stderr or '')[:500]}",
                file=sys.stderr,
            )

    def dismiss_stale_reviews(self, *, pr_number: int, marker: str) -> None:
        """Dismiss all previous CHANGES_REQUESTED reviews whose body contains `marker`."""
        proc = self._gh(
            "api",
            f"repos/{self.repo}/pulls/{pr_number}/reviews",
            "--jq",
            (
                f'[.[] | select(.state == "CHANGES_REQUESTED" '
                f'and (.body // "" | contains("{marker}"))) | .id]'
            ),
        )
        try:
            ids = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            ids = []

        for rid in ids:
            self._gh(
                "api", "-X", "PUT",
                f"repos/{self.repo}/pulls/{pr_number}/reviews/{rid}/dismissals",
                "-f", "message=Superseded by newer review",
                "-f", "event=DISMISS",
            )


# ---------------------------------------------------------------------------
# Re-export public API — explicit allowlist
# ---------------------------------------------------------------------------

__all__ = [
    "REPO", "REPO_ROOT",
    "MARKER_CODE_REVIEW", "MARKER_SECURITY_REVIEW", "MARKER_DESIGN_REVIEW",
    "MARKER_CONSENSUS",
    "STATUS_CODE", "STATUS_SECURITY", "STATUS_DESIGN", "STATUS_CONSENSUS",
    "STAGE_STATUS_CONTEXTS", "VALID_STATES",
    "MAX_SECTION_CHARS", "MAX_DIFF_CHARS", "MAX_PREFLIGHT_OUTPUT_CHARS",
    "CLI_REVIEW_TIMEOUT", "CLI_FIX_TIMEOUT",
    "PREFLIGHT_TYPECHECK_TIMEOUT", "PREFLIGHT_TEST_TIMEOUT",
    "Runner", "default_runner",
    "strip_ansi", "truncate", "tail",
    "SOURCE_FILE_RE", "parse_findings", "find_consensus",
    "consensus_status",
    "build_sticky_comment",
    "run_codex", "run_gemini", "run_claude",
    "git_diff_stat", "git_diff_full", "git_changed_files",
    "GhClient",
]
