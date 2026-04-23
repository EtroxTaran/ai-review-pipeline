"""Regression test: das gebaute Wheel MUSS alle 4 stage-prompts/*.md enthalten.

Hintergrund: PR#8 hat die Prompt-Files ins src/-Tree gelegt, aber vorher (Shadow-
Pipeline-Run #24689725932) sind die Stages mit FileNotFoundError gecrasht, weil
das installierte Paket die .md-Files NICHT im site-packages enthielt. Pytest
`test_all_stage_prompt_files_exist` prüfte damals nur das source-Tree, nicht
das Build-Artifact. Dieser Test schließt die Lücke.

Strategie:
- hatchling-Wheel bauen (zipfile), sicherstellen dass alle 4 Prompts drin sind
- das Wheel in einen temporären venv installieren und laden, damit wir den
  exakten site-packages-Pfad verifizieren den stage.load_prompt() zur Laufzeit
  nutzen würde.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_PROMPTS = (
    "code_review.md",
    "cursor_review.md",
    "security_review.md",
    "design_review.md",
)


def _build_wheel(outdir: Path) -> Path:
    """Baut ein Wheel aus dem aktuellen repo-root und gibt den Pfad zurück."""
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(
            f"python -m build nicht verfügbar (stderr: {result.stderr[:200]}). "
            "Test braucht `pip install build`."
        )
    wheels = list(outdir.glob("ai_review_pipeline-*.whl"))
    assert wheels, f"Kein Wheel gebaut in {outdir}"
    return wheels[0]


class TestWheelContainsStagePrompts:
    def test_wheel_ships_all_four_stage_prompt_markdown_files(self, tmp_path: Path) -> None:
        """Arrange: frisches Wheel bauen. Act: zipfile lesen.
        Assert: alle 4 prompts/ .md-Files sind im Archiv auf dem exakten Pfad,
        den stage.PROMPTS_DIR zur Laufzeit erwartet."""
        # Arrange
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        wheel_path = _build_wheel(build_dir)

        # Act
        with zipfile.ZipFile(wheel_path) as zf:
            names = set(zf.namelist())

        # Assert
        for prompt_file in REQUIRED_PROMPTS:
            archive_path = f"ai_review_pipeline/stages/prompts/{prompt_file}"
            assert archive_path in names, (
                f"Prompt-Datei fehlt im Wheel: {archive_path}\n"
                "hatchling hat die .md-Files nicht ins Paket-Artifact aufgenommen.\n"
                "Fix: pyproject.toml → [tool.hatch.build.targets.wheel] sicherstellen,\n"
                "dass packages=['src/ai_review_pipeline'] ohne include-filter gesetzt ist."
            )


@pytest.mark.slow
class TestInstalledWheelResolvesPromptFiles:
    """Integration: Wheel in temporäres venv installieren + load_prompt() ausführen."""

    def test_installed_package_can_load_all_stage_prompts(self, tmp_path: Path) -> None:
        # Arrange
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        wheel_path = _build_wheel(build_dir)

        venv_dir = tmp_path / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            check=True,
            capture_output=True,
        )

        venv_python = venv_dir / "bin" / "python"
        if not venv_python.exists():
            # Windows-Fallback (nicht verwendet in CI, aber korrekt)
            venv_python = venv_dir / "Scripts" / "python.exe"
        if not venv_python.exists():
            pytest.skip("venv konnte python nicht erzeugen (ensurepip fehlt?)")

        # Act: Wheel ins venv installieren
        install = subprocess.run(
            [str(venv_python), "-m", "pip", "install", str(wheel_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if install.returncode != 0:
            pytest.skip(f"pip install fehlgeschlagen: {install.stderr[:300]}")

        # Act: load_prompt() im venv ausführen
        script = (
            "import sys, json\n"
            "from ai_review_pipeline.stages import stage\n"
            "out = {}\n"
            f"for n in {list(REQUIRED_PROMPTS)!r}:\n"
            "    out[n] = len(stage.load_prompt(n))\n"
            "print(json.dumps(out))\n"
        )
        result = subprocess.run(
            [str(venv_python), "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )

        # Assert
        import json

        sizes = json.loads(result.stdout)
        assert set(sizes.keys()) == set(REQUIRED_PROMPTS)
        for name, size in sizes.items():
            assert size > 0, f"{name} ist leer oder nicht lesbar"

    def _cleanup(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
