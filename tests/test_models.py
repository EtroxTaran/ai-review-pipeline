"""Tests für ai_review_pipeline.models.resolve_model().

Resolve-Kaskade:
  1. Env-Var `AI_REVIEW_MODEL_<ROLE>` (für lokales Testing + Override)
  2. Dev-Override-File `~/.openclaw/workspace/MODEL_REGISTRY.md`
  3. Committed Registry `<repo>/registry/MODEL_REGISTRY.env`
  4. Fail-safe mit last-known-good aus Registry (die IST last-known-good)
  5. Fail-closed nur wenn Registry selbst fehlt/korrupt

Policy:
- Cursor + Codex brauchen kein `--model`-Flag (CLI-Default vertraut).
  Für diese Rollen returnt `resolve_model()` `None` — Caller baut dann ohne Flag.
- Gemini, Claude: Model-Name wird aus Registry geresolved.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ai_review_pipeline import models


class ResolveModelFromRegistryTests(unittest.TestCase):
    """Happy-Path: Registry-File wird gelesen, Rolle → Modell aufgelöst."""

    def setUp(self) -> None:
        # Isolierte Test-Registry
        self.tmp = TemporaryDirectory()
        self.registry_path = Path(self.tmp.name) / "MODEL_REGISTRY.env"
        self.registry_path.write_text(
            "# test registry\n"
            "CLAUDE_OPUS=claude-opus-9-9\n"
            "CLAUDE_SONNET=claude-sonnet-9-9\n"
            "CLAUDE_HAIKU=claude-haiku-9-9\n"
            "GEMINI_PRO=gemini-9.9-pro\n"
            "GEMINI_FLASH=gemini-9-flash\n"
            "OPENAI_MAIN=gpt-9-codex\n"
            "CODEX_CLI_VERSION=latest\n"
            "CURSOR_AGENT_CLI_VERSION=latest\n"
        )
        # Kein Dev-Override — explizit auf nicht-existentes File zeigen,
        # damit User's real ~/.openclaw/workspace/MODEL_REGISTRY.md nicht gelesen wird
        self.no_dev_override = Path(self.tmp.name) / "no-dev-override.md"
        # Isolierte Env — keine echten AI_REVIEW_MODEL_*-Vars reinfunken
        self.env_patcher = patch.dict(os.environ, {}, clear=False)
        self.env_patcher.start()
        for key in list(os.environ.keys()):
            if key.startswith("AI_REVIEW_MODEL_"):
                del os.environ[key]

    def tearDown(self) -> None:
        self.env_patcher.stop()
        self.tmp.cleanup()

    def _resolve(self, role: str) -> str | None:
        return models.resolve_model(
            role,
            registry_path=self.registry_path,
            dev_override_path=self.no_dev_override,
        )

    def test_resolves_security_role_to_gemini_pro(self) -> None:
        self.assertEqual(self._resolve("security"), "gemini-9.9-pro")

    def test_resolves_design_role_to_claude_opus(self) -> None:
        self.assertEqual(self._resolve("design"), "claude-opus-9-9")

    def test_resolves_ac_second_opinion_to_claude_opus(self) -> None:
        self.assertEqual(self._resolve("ac_second_opinion"), "claude-opus-9-9")

    def test_auto_fix_uses_sonnet_not_opus(self) -> None:
        # User-Policy: Volume-Traffic auf Sonnet, Opus nur Design + AC-Second-Opinion
        self.assertEqual(self._resolve("auto_fix"), "claude-sonnet-9-9")

    def test_fix_loop_uses_sonnet(self) -> None:
        self.assertEqual(self._resolve("fix_loop"), "claude-sonnet-9-9")

    def test_cursor_role_returns_none(self) -> None:
        # Cursor-CLI nutzt eigenen Default → kein --model-Flag → None
        self.assertIsNone(self._resolve("code-cursor"))

    def test_codex_role_returns_none(self) -> None:
        self.assertIsNone(self._resolve("code"))

    def test_ac_judge_returns_none(self) -> None:
        # Judge ist Codex-CLI → CLI-Default
        self.assertIsNone(self._resolve("ac_judge"))


class EnvVarOverrideTests(unittest.TestCase):
    """Env-Var hat höchste Priorität für lokales Testen."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.registry_path = Path(self.tmp.name) / "MODEL_REGISTRY.env"
        self.registry_path.write_text(
            "GEMINI_PRO=gemini-from-registry\n"
            "CLAUDE_OPUS=claude-from-registry\n"
            "CLAUDE_SONNET=sonnet-from-registry\n"
            "CLAUDE_HAIKU=haiku-from-registry\n"
            "GEMINI_FLASH=flash-from-registry\n"
            "OPENAI_MAIN=codex-from-registry\n"
            "CODEX_CLI_VERSION=latest\n"
            "CURSOR_AGENT_CLI_VERSION=latest\n"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _resolve(self, role: str) -> str | None:
        return models.resolve_model(
            role,
            registry_path=self.registry_path,
            dev_override_path=Path(self.tmp.name) / "no-dev-override.md",
        )

    def test_env_var_overrides_registry(self) -> None:
        with patch.dict(os.environ, {"AI_REVIEW_MODEL_SECURITY": "gemini-env-override"}):
            self.assertEqual(self._resolve("security"), "gemini-env-override")

    def test_env_var_can_force_model_for_cli_default_role(self) -> None:
        # Wenn Dev will: auch für Codex-CLI-Role einen expliziten Model-Flag setzen
        with patch.dict(os.environ, {"AI_REVIEW_MODEL_CODE": "gpt-experimental"}):
            self.assertEqual(self._resolve("code"), "gpt-experimental")

    def test_env_var_case_insensitive_role_name(self) -> None:
        # "code-cursor" → AI_REVIEW_MODEL_CODE_CURSOR (hyphen → underscore, uppercase)
        with patch.dict(os.environ, {"AI_REVIEW_MODEL_CODE_CURSOR": "composer-experimental"}):
            self.assertEqual(self._resolve("code-cursor"), "composer-experimental")


class DevOverrideFileTests(unittest.TestCase):
    """~/.openclaw/workspace/MODEL_REGISTRY.md hat Priorität zwischen Env und committed Registry."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.committed = Path(self.tmp.name) / "MODEL_REGISTRY.env"
        self.committed.write_text(
            "GEMINI_PRO=gemini-committed\n"
            "CLAUDE_OPUS=claude-committed\n"
            "CLAUDE_SONNET=sonnet-committed\n"
            "CLAUDE_HAIKU=haiku-committed\n"
            "GEMINI_FLASH=flash-committed\n"
            "OPENAI_MAIN=codex-committed\n"
            "CODEX_CLI_VERSION=latest\n"
            "CURSOR_AGENT_CLI_VERSION=latest\n"
        )
        self.dev_override = Path(self.tmp.name) / "dev_override.md"
        self.dev_override.write_text(
            "# Dev override\n"
            "GEMINI_PRO=gemini-dev-override\n"
        )
        # Keine AI_REVIEW_MODEL_*-Vars
        for key in list(os.environ.keys()):
            if key.startswith("AI_REVIEW_MODEL_"):
                del os.environ[key]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dev_override_beats_committed_registry(self) -> None:
        model = models.resolve_model(
            "security",
            registry_path=self.committed,
            dev_override_path=self.dev_override,
        )
        self.assertEqual(model, "gemini-dev-override")

    def test_dev_override_missing_falls_back_to_registry(self) -> None:
        missing_override = Path(self.tmp.name) / "does-not-exist.md"
        model = models.resolve_model(
            "security",
            registry_path=self.committed,
            dev_override_path=missing_override,
        )
        self.assertEqual(model, "gemini-committed")

    def test_dev_override_partial_keys_fall_back_to_registry(self) -> None:
        # Dev-Override hat nur GEMINI_PRO — Claude-Werte kommen aus committed Registry
        model = models.resolve_model(
            "design",
            registry_path=self.committed,
            dev_override_path=self.dev_override,
        )
        self.assertEqual(model, "claude-committed")


class FailSafeTests(unittest.TestCase):
    """Registry korrupt / fehlt — fail-closed (echter Konfig-Bug)."""

    def test_missing_registry_raises(self) -> None:
        missing = Path("/tmp/definitely-does-not-exist/MODEL_REGISTRY.env")
        with self.assertRaises(models.RegistryMissingError):
            models.resolve_model("security", registry_path=missing)

    def test_missing_required_key_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.env"
            broken.write_text(
                "# CLAUDE_OPUS fehlt\n"
                "CLAUDE_SONNET=sonnet\n"
                "CLAUDE_HAIKU=haiku\n"
                "GEMINI_PRO=gemini\n"
                "GEMINI_FLASH=flash\n"
                "OPENAI_MAIN=codex\n"
                "CODEX_CLI_VERSION=latest\n"
                "CURSOR_AGENT_CLI_VERSION=latest\n"
            )
            with self.assertRaises(models.RegistryIncompleteError) as ctx:
                models.resolve_model("design", registry_path=broken)
            self.assertIn("CLAUDE_OPUS", str(ctx.exception))

    def test_unknown_role_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            reg = Path(tmp) / "ok.env"
            reg.write_text(
                "CLAUDE_OPUS=x\nCLAUDE_SONNET=x\nCLAUDE_HAIKU=x\n"
                "GEMINI_PRO=x\nGEMINI_FLASH=x\nOPENAI_MAIN=x\n"
                "CODEX_CLI_VERSION=latest\nCURSOR_AGENT_CLI_VERSION=latest\n"
            )
            with self.assertRaises(models.UnknownRoleError):
                models.resolve_model("not-a-real-role", registry_path=reg)


class RegistryParserTests(unittest.TestCase):
    """Isolierter Test für den Env-File-Parser."""

    def test_parses_simple_key_value(self) -> None:
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "r.env"
            f.write_text("KEY=value\n")
            parsed = models._parse_env_file(f)
            self.assertEqual(parsed, {"KEY": "value"})

    def test_strips_comments_and_blanks(self) -> None:
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "r.env"
            f.write_text(
                "# top comment\n"
                "\n"
                "   # indented comment\n"
                "KEY=value\n"
                "\n"
                "OTHER=thing\n"
            )
            parsed = models._parse_env_file(f)
            self.assertEqual(parsed, {"KEY": "value", "OTHER": "thing"})

    def test_handles_quoted_values(self) -> None:
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "r.env"
            f.write_text('KEY="with spaces"\nOTHER=\'also ok\'\n')
            parsed = models._parse_env_file(f)
            self.assertEqual(parsed["KEY"], "with spaces")
            self.assertEqual(parsed["OTHER"], "also ok")

    def test_ignores_lines_without_equals(self) -> None:
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "r.env"
            f.write_text("KEY=value\nthis is garbage\nOTHER=thing\n")
            parsed = models._parse_env_file(f)
            self.assertEqual(parsed, {"KEY": "value", "OTHER": "thing"})

    def test_canonicalizes_anthropic_aliases(self) -> None:
        # Regression: OpenClaw-Workspace-Registry nutzt ANTHROPIC_*, die Pipeline
        # erwartet CLAUDE_*. Parser muss aliasen beim Einlesen.
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "r.env"
            f.write_text(
                "ANTHROPIC_OPUS=claude-opus-4-7\n"
                "ANTHROPIC_SONNET=claude-sonnet-4-6\n"
                "ANTHROPIC_HAIKU=claude-haiku-4-5\n"
            )
            parsed = models._parse_env_file(f)
            self.assertEqual(parsed["CLAUDE_OPUS"], "claude-opus-4-7")
            self.assertEqual(parsed["CLAUDE_SONNET"], "claude-sonnet-4-6")
            self.assertEqual(parsed["CLAUDE_HAIKU"], "claude-haiku-4-5")
            # Canonical keys nicht doppelt (alias ersetzt den Original-Namen)
            self.assertNotIn("ANTHROPIC_OPUS", parsed)

    def test_dev_override_with_anthropic_keys_works(self) -> None:
        # End-to-End: Dev-Override nutzt ANTHROPIC_*, resolve_model findet's
        with TemporaryDirectory() as tmp:
            registry = Path(tmp) / "committed.env"
            registry.write_text(
                "CLAUDE_OPUS=committed-opus\nCLAUDE_SONNET=s\nCLAUDE_HAIKU=h\n"
                "GEMINI_PRO=g\nGEMINI_FLASH=gf\nOPENAI_MAIN=om\n"
                "CODEX_CLI_VERSION=latest\nCURSOR_AGENT_CLI_VERSION=latest\n"
            )
            override = Path(tmp) / "override.md"
            override.write_text("ANTHROPIC_OPUS=dev-override-opus-9-9\n")

            for key in list(os.environ.keys()):
                if key.startswith("AI_REVIEW_MODEL_"):
                    del os.environ[key]

            model = models.resolve_model(
                "design",
                registry_path=registry,
                dev_override_path=override,
            )
            self.assertEqual(model, "dev-override-opus-9-9")

    def test_strips_litellm_vendor_prefixes(self) -> None:
        # LiteLLM-Style wie `anthropic/claude-opus-4-6` → `claude-opus-4-6`
        # weil Anthropic's CLI nur bare Modell-Namen akzeptiert.
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "r.env"
            f.write_text(
                "CLAUDE=anthropic/claude-opus-4-7\n"
                "OPENAI=openai/gpt-5.3-codex\n"
                "GOOGLE=google/gemini-3-pro\n"
                "GEMINI=gemini/gemini-3.1-pro-preview\n"
                "BARE=claude-opus-4-7\n"
            )
            parsed = models._parse_env_file(f)
            self.assertEqual(parsed["CLAUDE"], "claude-opus-4-7")
            self.assertEqual(parsed["OPENAI"], "gpt-5.3-codex")
            self.assertEqual(parsed["GOOGLE"], "gemini-3-pro")
            self.assertEqual(parsed["GEMINI"], "gemini-3.1-pro-preview")
            self.assertEqual(parsed["BARE"], "claude-opus-4-7")  # bare unverändert


if __name__ == "__main__":
    unittest.main()
