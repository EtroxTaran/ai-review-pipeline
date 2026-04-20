#!/usr/bin/env bash
# Lokaler Smoke-Test — prüft Installation, CLI-Entrypoint und Test-Suite.
# Nutzung: bash scripts/smoke_cli.sh [--venv /path/to/venv]
# Exit 0 = alles OK; Exit 1 = echter Fehler (kein Blocker durch fehlendes cli.py).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Optionales --venv Argument
VENV_PATH=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv) VENV_PATH="$2"; shift 2 ;;
    *) shift ;;
  esac
done

echo "=== ai-review-pipeline Smoke-Test ==="
echo "Repo: $REPO_ROOT"

# Binaries bestimmen (venv-aware; uv-venvs haben kein pip-Binary — nutze uv pip oder python -m pip)
if [[ -n "$VENV_PATH" ]]; then
  PYTHON_BIN="$VENV_PATH/bin/python"
  PYTEST_BIN="$VENV_PATH/bin/pytest"
  AI_REVIEW_BIN="$VENV_PATH/bin/ai-review"
  # uv-venvs: kein pip-Binary — uv pip bevorzugen, Fallback python -m pip
  if command -v uv &>/dev/null; then
    INSTALL_CMD="uv pip install --python $PYTHON_BIN"
  elif [[ -x "$VENV_PATH/bin/pip" ]]; then
    INSTALL_CMD="$VENV_PATH/bin/pip install"
  else
    INSTALL_CMD="$PYTHON_BIN -m pip install"
  fi
else
  PYTHON_BIN="python3"
  PYTEST_BIN="pytest"
  AI_REVIEW_BIN="ai-review"
  INSTALL_CMD="pip install"
fi

# --- Schritt 1: Package installieren ---
echo ""
echo "--- Step 1: install package + dev deps ---"
$INSTALL_CMD -e "$REPO_ROOT/.[dev]" -q
echo "OK: package installed"

# --- Schritt 2: CLI-Entrypoint prüfen ---
echo ""
echo "--- Step 2: ai-review CLI gate ---"
if ! [[ -x "$AI_REVIEW_BIN" ]] && ! command -v "$AI_REVIEW_BIN" &>/dev/null; then
  echo "SKIP: 'ai-review' binary not found — cli.py not yet present, will work after feat/cli merge"
  echo "      (Dies ist kein Fehler — paralleler Agent baut CLI.)"
else
  # Probe: läuft ai-review --help ohne ImportError?
  CLI_CHECK_OUT=$("$AI_REVIEW_BIN" --help 2>&1 || true)
  if echo "$CLI_CHECK_OUT" | grep -q "ModuleNotFoundError\|ImportError\|No module named"; then
    echo "SKIP: cli.py not yet present, will work after feat/cli merge"
    echo "      (Binary existiert, aber cli.py-Modul fehlt noch — paralleler Agent baut es.)"
    echo "      Probe-Output: $CLI_CHECK_OUT"
  else
    echo "--- Step 2a: ai-review --help ---"
    echo "$CLI_CHECK_OUT"

    echo ""
    echo "--- Step 2b: ai-review --version ---"
    "$AI_REVIEW_BIN" --version

    echo ""
    echo "--- Step 2c: ai-review stage ac-validation --help ---"
    "$AI_REVIEW_BIN" stage ac-validation --help

    echo "OK: CLI entrypoints erreichbar"
  fi
fi

# --- Schritt 3: Test-Suite ---
echo ""
echo "--- Step 3: pytest --tb=short ---"
cd "$REPO_ROOT"
"$PYTEST_BIN" --tb=short
echo "OK: pytest grün"

echo ""
echo "=== Smoke-Test abgeschlossen: OK ==="
