"""Model-Resolution aus committed Registry + Env-Var-Override + Dev-Override.

Single-Source-of-Truth: `<repo>/registry/MODEL_REGISTRY.env` (committed).
Die Pipeline liest zur Laufzeit — keine hardcoded Modell-Defaults mehr.

Kaskade (höchste Priorität zuerst):
  1. Env-Var `AI_REVIEW_MODEL_<ROLE>` (lokales Testing, CI-Override)
  2. Dev-Override `~/.openclaw/workspace/MODEL_REGISTRY.md` (optional, nur lokal)
  3. Committed Registry `<repo>/registry/MODEL_REGISTRY.env` (SoT)
  4. RegistryMissingError / RegistryIncompleteError wenn SoT fehlt/korrupt

Policy (User-Entscheidung 2026-04-24):
  - Opus nur für Design + AC-Second-Opinion (hohe Stakes, 1 Call/PR)
  - Sonnet für Auto-Fix + Fix-Loop (Volume-Traffic, Opus overkill)
  - Codex / Cursor / AC-Judge: CLI-Default — `resolve_model()` gibt None zurück
    damit der Caller ohne --model-Flag startet.

Siehe: ~/.claude/plans/snuggly-wiggling-moler.md (Always-Latest-Models).
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RegistryMissingError(FileNotFoundError):
    """Registry-Datei existiert nicht. Echter Konfig-Bug — fail-closed."""


class RegistryIncompleteError(ValueError):
    """Registry existiert, aber ein Pflicht-Key fehlt. Fail-closed."""


class UnknownRoleError(ValueError):
    """`resolve_model()` mit nicht-registrierter Rolle aufgerufen."""


# ---------------------------------------------------------------------------
# Registry-Layout
# ---------------------------------------------------------------------------

# Pflicht-Keys in jeder Registry — fail-closed wenn einer fehlt.
# (CLI-Version-Pins sind soft — default "latest", also nicht Pflicht.)
REQUIRED_REGISTRY_KEYS: frozenset[str] = frozenset({
    "CLAUDE_OPUS", "CLAUDE_SONNET", "CLAUDE_HAIKU",
    "GEMINI_PRO", "GEMINI_FLASH",
    "OPENAI_MAIN",
})

# Role → Registry-Key. None bedeutet: CLI-Default, kein --model-Flag.
# Kann durch Env-Var AI_REVIEW_MODEL_<ROLE> überschrieben werden —
# dann passiert der explicit-Model-Pfad auch für None-Rollen.
ROLE_TO_REGISTRY_KEY: dict[str, str | None] = {
    # Reviewer-Stages
    "security":          "GEMINI_PRO",
    "design":            "CLAUDE_OPUS",
    # AC-Validation
    "ac_judge":          None,          # Codex-CLI default
    "ac_second_opinion": "CLAUDE_OPUS",
    # Auto-Fix / Fix-Loop (Volume → Sonnet)
    "auto_fix":          "CLAUDE_SONNET",
    "fix_loop":          "CLAUDE_SONNET",
    # CLI-Default-Reviewer (Codex + Cursor)
    "code":              None,          # Codex-CLI default
    "code-cursor":       None,          # cursor-agent CLI default
}

# Default-Pfad zur committed Registry. Liegt INNERHALB des Python-Packages,
# damit `pip install git+https://…@main` das File mitbringt (siehe
# hatchling wheel config: `packages = ["src/ai_review_pipeline"]`).
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent / "registry" / "MODEL_REGISTRY.env"

# Default-Pfad zum Dev-Override (nur wenn Path existiert — kein Error wenn nicht).
DEFAULT_DEV_OVERRIDE_PATH = Path.home() / ".openclaw" / "workspace" / "MODEL_REGISTRY.md"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


# Vendor-Prefixes die entfernt werden müssen (LiteLLM-Style-Names wie
# `anthropic/claude-opus-4-6` → die Vendor-CLIs akzeptieren nur den bare
# Suffix `claude-opus-4-6`). Prefixe ab Schema-Validation bewusst lax —
# jeder LiteLLM-konforme Prefix wird gestripped.
_KNOWN_VENDOR_PREFIXES: tuple[str, ...] = (
    "anthropic/",
    "openai/",
    "google/",
    "google-vertex-ai/",
    "gemini/",
    "xai/",
    "cursor/",
)

# Alias-Map für Key-Naming-Varianten. Der OpenClaw-Workspace-Registry nutzt
# `ANTHROPIC_*` (provider-prefixed); die ai-review-pipeline-committed-Registry
# nutzt historisch `CLAUDE_*` (model-family-prefixed). Beide sollen arbeiten.
# Beim Parsen wird jeder Alias auf die canonical Form (die in REQUIRED_REGISTRY_KEYS)
# gemapped, damit Dev-Override-Files mit entweder Konvention funktionieren.
_KEY_ALIASES: dict[str, str] = {
    "ANTHROPIC_OPUS": "CLAUDE_OPUS",
    "ANTHROPIC_SONNET": "CLAUDE_SONNET",
    "ANTHROPIC_HAIKU": "CLAUDE_HAIKU",
    # OpenAI bleibt konsistent (OPENAI_MAIN ist canonical)
    # Gemini bleibt konsistent (GEMINI_PRO / GEMINI_FLASH sind canonical)
}


def _strip_vendor_prefix(value: str) -> str:
    """LiteLLM-Style `anthropic/claude-opus-4-6` → `claude-opus-4-6`."""
    for prefix in _KNOWN_VENDOR_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parst ein einfaches `KEY=value`-File (keine YAML-Deps).

    Regeln:
      - Leer-Zeilen und Comments (`#`) werden ignoriert (auch indented)
      - Werte dürfen in `"..."` oder `'...'` quoted sein — Quotes werden gestripped
      - Zeilen ohne `=` werden ignoriert (Registry-History, Prose, etc.)
      - LiteLLM-Vendor-Prefixes (`anthropic/`, `openai/`, ...) werden entfernt,
        weil die Vendor-CLIs nur bare Modell-Namen akzeptieren

    Dev-Override ist ein .md-File mit gleichem Inline-Format — das Parsen
    ignoriert eh alle Zeilen ohne `=`, daher funktioniert der gleiche Parser.
    """
    result: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip quotes
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"')
            or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        # Strip LiteLLM-Vendor-Prefixes — Vendor-CLIs wollen bare Namen
        value = _strip_vendor_prefix(value)
        if not key:
            continue
        # Alias-Canonicalization: ANTHROPIC_OPUS → CLAUDE_OPUS etc.
        canonical = _KEY_ALIASES.get(key, key)
        result[canonical] = value
    return result


# ---------------------------------------------------------------------------
# Registry-Loading
# ---------------------------------------------------------------------------


def _load_registry(path: Path) -> dict[str, str]:
    """Lädt committed Registry und validiert Pflicht-Keys."""
    if not path.is_file():
        raise RegistryMissingError(
            f"Model registry not found at {path}. "
            f"Either the committed registry is missing (pipeline bug) "
            f"or pip install didn't include the registry directory "
            f"(build-config issue)."
        )
    parsed = _parse_env_file(path)
    missing = REQUIRED_REGISTRY_KEYS - set(parsed.keys())
    if missing:
        raise RegistryIncompleteError(
            f"Registry {path} missing required keys: {sorted(missing)}"
        )
    return parsed


def _load_dev_override(path: Path | None) -> dict[str, str]:
    """Lädt Dev-Override falls vorhanden — KEIN Error wenn File fehlt."""
    if path is None or not path.is_file():
        return {}
    try:
        return _parse_env_file(path)
    except OSError:
        return {}


def _env_var_for_role(role: str) -> str:
    """Rolle → Env-Var-Name. `code-cursor` → `AI_REVIEW_MODEL_CODE_CURSOR`."""
    return f"AI_REVIEW_MODEL_{role.upper().replace('-', '_')}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_model(
    role: str,
    *,
    registry_path: Path | None = None,
    dev_override_path: Path | None = None,
) -> str | None:
    """Löst die Rolle in einen konkreten Modell-Namen auf (oder None für CLI-Default).

    Parameters
    ----------
    role:
        Rollen-String aus ROLE_TO_REGISTRY_KEY (z.B. "security", "design").
    registry_path:
        Override für die committed Registry (für Tests). Default: repo-local.
    dev_override_path:
        Override für Dev-File. Default: ~/.openclaw/workspace/MODEL_REGISTRY.md.
        Wenn das File nicht existiert, wird es silent ignoriert.

    Returns
    -------
    str
        Der Modell-Name (z.B. "gemini-3.1-pro-preview"), wenn Rolle ein
        Registry-Key hat ODER Env-Var gesetzt ist.
    None
        Wenn Rolle ein CLI-Default-Reviewer ist (Codex/Cursor/AC-Judge) UND
        keine Env-Var die Rolle überschreibt. Caller MUSS dann ohne `--model`
        invoken.

    Raises
    ------
    UnknownRoleError
        Rolle ist nicht in ROLE_TO_REGISTRY_KEY registriert.
    RegistryMissingError
        Committed Registry existiert nicht (pipeline-bug).
    RegistryIncompleteError
        Registry fehlt ein Pflicht-Key.
    """
    if role not in ROLE_TO_REGISTRY_KEY:
        raise UnknownRoleError(
            f"Unknown role {role!r}. Registered roles: "
            f"{sorted(ROLE_TO_REGISTRY_KEY.keys())}"
        )

    # Pfad 1 — Env-Var (höchste Priorität, auch für CLI-Default-Rollen)
    env_var = _env_var_for_role(role)
    if env_var in os.environ:
        value = os.environ[env_var].strip()
        if value:
            return value

    # CLI-Default-Rolle und keine Env-Override → None (Caller weiß Bescheid)
    registry_key = ROLE_TO_REGISTRY_KEY[role]
    if registry_key is None:
        return None

    # Pfad 2 + 3 — Dev-Override + Committed Registry laden
    reg_path = registry_path if registry_path is not None else DEFAULT_REGISTRY_PATH
    dev_path = dev_override_path if dev_override_path is not None else DEFAULT_DEV_OVERRIDE_PATH

    registry = _load_registry(reg_path)       # wirft bei Missing/Incomplete
    dev_override = _load_dev_override(dev_path)  # leise leer bei fehlendem File

    # Dev-Override hat Vorrang wenn der Key dort definiert ist
    if registry_key in dev_override:
        return dev_override[registry_key]
    return registry[registry_key]


# ---------------------------------------------------------------------------
# Convenience: CLI-Version-Pins (für Installer-Workflows)
# ---------------------------------------------------------------------------


def get_cli_version_pin(cli_name: str, *, registry_path: Path | None = None) -> str:
    """CLI-Version-Pin aus Registry. Default `latest` wenn nicht gesetzt.

    Parameters
    ----------
    cli_name:
        Einer von `codex`, `cursor-agent`. Andere CLIs werfen UnknownRoleError.
    """
    mapping = {
        "codex": "CODEX_CLI_VERSION",
        "cursor-agent": "CURSOR_AGENT_CLI_VERSION",
    }
    if cli_name not in mapping:
        raise UnknownRoleError(
            f"Unknown CLI {cli_name!r}. Known: {list(mapping.keys())}"
        )
    reg_path = registry_path if registry_path is not None else DEFAULT_REGISTRY_PATH
    if not reg_path.is_file():
        return "latest"
    parsed = _parse_env_file(reg_path)
    return parsed.get(mapping[cli_name], "latest")


__all__ = [
    "resolve_model",
    "get_cli_version_pin",
    "RegistryMissingError",
    "RegistryIncompleteError",
    "UnknownRoleError",
    "ROLE_TO_REGISTRY_KEY",
    "REQUIRED_REGISTRY_KEYS",
]
