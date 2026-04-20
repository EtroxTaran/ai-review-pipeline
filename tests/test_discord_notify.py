"""TDD-Tests für discord_notify.py (Phase 3.4 Pipeline-Extraktion).

Design-Entscheidungen (aus Plan §§269-399):
- discord_notify.py postet NICHT direkt an Discord-API (Rate-Limits, Components-v2-Komplexität).
- Stattdessen: HTTP-POST an ops-n8n Webhook `http://127.0.0.1:5679/webhook/ai-review-dispatch`.
- ops-n8n übernimmt Components-v2-Rendering, Inline-Buttons, Sticky-Messages.
- Fail-Open (Plan S6): Wenn ops-n8n down → logge in metrics.jsonl, kein Exception-Raise.

Discord vs. Telegram Unterschiede:
- `channel_id` statt `chat_id` (Discord nutzt Snowflake-IDs)
- `custom_id` statt `callback_data` für Button-Interaktionen (Discord Components v2)
- `mention_role` statt Telegram-@mention-Semantik
- Kein direkter Bot-Token im Python-Modul (Token lebt exklusiv in ops-n8n)
- `sticky_message`-Flag für Update-statt-Neu-Posten (Discord Edit-Message-Pattern)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_review_pipeline.discord_notify import (
    DiscordNotifyPayload,
    notify_discord,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def base_config() -> dict:
    """Minimale config wie aus .ai-review/config.yaml gelesen."""
    return {
        "notifications": {
            "target": "discord",
            "discord": {
                "channel_id": "123456789012345678",
                "mention_role": "@here",
                "sticky_message": True,
            },
        }
    }


@pytest.fixture()
def base_payload() -> DiscordNotifyPayload:
    """Minimaler gültiger Payload für notify_discord."""
    return DiscordNotifyPayload(
        event_type="escalation",
        pr_url="https://github.com/EtroxTaran/ai-review-pipeline/pull/42",
        repo="EtroxTaran/ai-review-pipeline",
        pr_number=42,
        consensus_score=4.5,
        stage_scores={"code_review": 5, "security": 4, "ac_validation": 4},
        findings=["Fix-Loop nach 3 Iterationen ohne Konvergenz beendet"],
        button_actions=[
            {"action": "approve", "text": "Approve", "custom_id": "approve:42"},
            {"action": "fix", "text": "Auto-Fix", "custom_id": "fix:42"},
            {"action": "manual", "text": "Manual Review", "custom_id": "manual:42"},
        ],
        channel_id=None,  # aus config gelesen
        mention_role=None,
        sticky_message=None,
    )


# ---------------------------------------------------------------------------
# 1. test_payload_shape_matches_contract
# ---------------------------------------------------------------------------


class TestPayloadShapeMatchesContract:
    """requests.post wird mit korrektem JSON-Body aufgerufen."""

    def test_posts_to_ops_n8n_webhook_url(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """Arrange: Payload + Config. Act: notify_discord. Assert: requests.post URL stimmt."""
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config)

        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert call_url == "http://127.0.0.1:5679/webhook/ai-review-dispatch"

    def test_json_body_contains_required_fields(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """Arrange: Payload. Act: notify_discord. Assert: alle Pflichtfelder im JSON-Body."""
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config)

        body = mock_post.call_args[1]["json"]
        assert body["event_type"] == "escalation"
        assert body["pr_url"] == "https://github.com/EtroxTaran/ai-review-pipeline/pull/42"
        assert body["repo"] == "EtroxTaran/ai-review-pipeline"
        assert body["pr_number"] == 42
        assert body["consensus_score"] == 4.5
        assert "stage_scores" in body
        assert "findings" in body
        assert "channel_id" in body
        assert "button_actions" in body

    def test_channel_id_filled_from_config_when_payload_has_none(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """channel_id=None im Payload → aus config.notifications.discord.channel_id befüllt."""
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config)

        body = mock_post.call_args[1]["json"]
        assert body["channel_id"] == "123456789012345678"

    def test_timeout_is_five_seconds(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """requests.post wird mit timeout=5 aufgerufen (Plan-Vorgabe)."""
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config)

        assert mock_post.call_args[1]["timeout"] == 5

    def test_returns_true_on_2xx(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            result = notify_discord(base_payload, base_config)

        assert result is True

    def test_returns_false_on_4xx(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=400)
            result = notify_discord(base_payload, base_config)

        assert result is False


# ---------------------------------------------------------------------------
# 2. test_fail_open_on_connection_error
# ---------------------------------------------------------------------------


class TestFailOpenOnConnectionError:
    """ConnectionError → kein Exception-Raise, nur Return False + Metrics-Log."""

    def test_connection_error_does_not_raise(
        self, base_payload: DiscordNotifyPayload, base_config: dict, tmp_path: Path
    ) -> None:
        """Arrange: requests.post wirft ConnectionError. Act: notify_discord.
        Assert: kein Raise, gibt False zurück."""
        import requests

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.side_effect = requests.ConnectionError("ops-n8n unreachable")

            metrics = tmp_path / "metrics.jsonl"
            result = notify_discord(base_payload, base_config, metrics_path=metrics)

        assert result is False

    def test_timeout_error_does_not_raise(
        self, base_payload: DiscordNotifyPayload, base_config: dict, tmp_path: Path
    ) -> None:
        """Timeout ist auch ein RequestException → Fail-Open."""
        import requests

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.side_effect = requests.Timeout("timeout after 5s")

            metrics = tmp_path / "metrics.jsonl"
            result = notify_discord(base_payload, base_config, metrics_path=metrics)

        assert result is False

    def test_unexpected_exception_does_not_raise(
        self, base_payload: DiscordNotifyPayload, base_config: dict, tmp_path: Path
    ) -> None:
        """Auch nicht-RequestException-Fehler dürfen die Pipeline nicht crashen."""
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.side_effect = RuntimeError("unexpected")

            metrics = tmp_path / "metrics.jsonl"
            result = notify_discord(base_payload, base_config, metrics_path=metrics)

        assert result is False


# ---------------------------------------------------------------------------
# 3. test_button_actions_structure
# ---------------------------------------------------------------------------


class TestButtonActionsStructure:
    """Approve/Fix/Manual Buttons als custom_id-Array korrekt gerendert."""

    def test_button_actions_forwarded_in_payload(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """button_actions-Liste wird 1:1 in den Webhook-Body übernommen."""
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config)

        body = mock_post.call_args[1]["json"]
        actions = body["button_actions"]
        assert len(actions) == 3

        action_names = {a["action"] for a in actions}
        assert action_names == {"approve", "fix", "manual"}

    def test_custom_id_contains_pr_number(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """Jeder Button-custom_id muss die PR-Nummer enthalten (Discord-Callback-Pattern)."""
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config)

        body = mock_post.call_args[1]["json"]
        for action in body["button_actions"]:
            assert "42" in action["custom_id"], f"custom_id fehlt PR-Nummer: {action['custom_id']}"
            assert "text" in action

    def test_custom_id_uses_discord_format(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """custom_id-Format: `<action>:<pr_number>` — Discord-Standard, kein callback_data."""
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config)

        body = mock_post.call_args[1]["json"]
        approve_action = next(a for a in body["button_actions"] if a["action"] == "approve")
        # Discord: `approve:42`, nicht Telegram: `approve:42` (gleiche Form, andere Semantik)
        assert approve_action["custom_id"] == "approve:42"

    def test_no_callback_data_field_in_discord_payload(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """Discord nutzt `custom_id`, nicht `callback_data` (Telegram-Begriff)."""
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config)

        body = mock_post.call_args[1]["json"]
        for action in body["button_actions"]:
            # callback_data ist Telegram-Begriff, Discord nutzt custom_id
            assert "callback_data" not in action


# ---------------------------------------------------------------------------
# 4. test_channel_id_from_config
# ---------------------------------------------------------------------------


class TestChannelIdFromConfig:
    """Default vs. Override-Pfad für channel_id."""

    def test_payload_channel_id_overrides_config(self, base_config: dict) -> None:
        """Wenn Payload eine channel_id hat, wird sie verwendet (Override-Pfad)."""
        payload = DiscordNotifyPayload(
            event_type="escalation",
            pr_url="https://github.com/x/y/pull/1",
            repo="x/y",
            pr_number=1,
            consensus_score=9.0,
            stage_scores={},
            findings=[],
            button_actions=[],
            channel_id="999888777666555444",  # Override
            mention_role=None,
            sticky_message=None,
        )

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(payload, base_config)

        body = mock_post.call_args[1]["json"]
        assert body["channel_id"] == "999888777666555444"

    def test_config_channel_id_used_when_payload_is_none(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """Wenn Payload channel_id=None, kommt sie aus der Config."""
        # base_payload hat channel_id=None
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config)

        body = mock_post.call_args[1]["json"]
        assert body["channel_id"] == "123456789012345678"

    def test_missing_discord_config_returns_false(self) -> None:
        """Wenn keine discord-Config vorhanden, kein Absturz, aber False."""
        payload = DiscordNotifyPayload(
            event_type="escalation",
            pr_url="https://github.com/x/y/pull/1",
            repo="x/y",
            pr_number=1,
            consensus_score=4.0,
            stage_scores={},
            findings=[],
            button_actions=[],
            channel_id=None,
            mention_role=None,
            sticky_message=None,
        )
        config_no_discord: dict = {"notifications": {"target": "none"}}

        result = notify_discord(payload, config_no_discord)
        assert result is False


# ---------------------------------------------------------------------------
# 5. test_waiver_notification
# ---------------------------------------------------------------------------


class TestWaiverNotification:
    """waived=True ändert Payload-Struktur: keine Buttons, event_type angepasst."""

    def test_waived_true_has_no_button_actions(self, base_config: dict) -> None:
        """Waivers haben keine Interaction-Buttons (keine Entscheidung ausstehend)."""
        payload = DiscordNotifyPayload(
            event_type="ac_waiver",
            pr_url="https://github.com/x/y/pull/10",
            repo="x/y",
            pr_number=10,
            consensus_score=10.0,
            stage_scores={"ac_validation": 10},
            findings=[],
            button_actions=[],  # leere Liste bei Waivern
            channel_id=None,
            mention_role=None,
            sticky_message=None,
            waived=True,
        )

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(payload, base_config)

        body = mock_post.call_args[1]["json"]
        # Waiver: keine Buttons in der Nachricht
        assert body["button_actions"] == []

    def test_waived_flag_present_in_payload(self, base_config: dict) -> None:
        """waived=True wird explizit in den Webhook-Body geschrieben."""
        payload = DiscordNotifyPayload(
            event_type="ac_waiver",
            pr_url="https://github.com/x/y/pull/10",
            repo="x/y",
            pr_number=10,
            consensus_score=10.0,
            stage_scores={},
            findings=["Waiver gesetzt: bootstrap-phase"],
            button_actions=[],
            channel_id=None,
            mention_role=None,
            sticky_message=None,
            waived=True,
        )

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(payload, base_config)

        body = mock_post.call_args[1]["json"]
        assert body.get("waived") is True

    def test_waived_false_is_default(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """Standard-Payload ohne waived= hat waived=False im Body."""
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config)

        body = mock_post.call_args[1]["json"]
        assert body.get("waived") is False


# ---------------------------------------------------------------------------
# 6. test_escalation_event_type
# ---------------------------------------------------------------------------


class TestEscalationEventType:
    """event_type='escalation' setzt mention_role aus Config."""

    def test_escalation_includes_mention_role_from_config(self, base_config: dict) -> None:
        """Bei escalation: mention_role aus config.notifications.discord.mention_role."""
        payload = DiscordNotifyPayload(
            event_type="escalation",
            pr_url="https://github.com/x/y/pull/99",
            repo="x/y",
            pr_number=99,
            consensus_score=3.0,
            stage_scores={"code_review": 3},
            findings=["Fix-Loop divergiert"],
            button_actions=[
                {"action": "approve", "text": "Approve", "custom_id": "approve:99"},
            ],
            channel_id=None,
            mention_role=None,  # aus Config befüllen
            sticky_message=None,
        )

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(payload, base_config)

        body = mock_post.call_args[1]["json"]
        assert body["mention_role"] == "@here"

    def test_payload_mention_role_overrides_config(self, base_config: dict) -> None:
        """Wenn Payload mention_role setzt, überschreibt das die Config."""
        payload = DiscordNotifyPayload(
            event_type="escalation",
            pr_url="https://github.com/x/y/pull/99",
            repo="x/y",
            pr_number=99,
            consensus_score=3.0,
            stage_scores={},
            findings=[],
            button_actions=[],
            channel_id=None,
            mention_role="<@&987654321>",  # Role-Override
            sticky_message=None,
        )

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(payload, base_config)

        body = mock_post.call_args[1]["json"]
        assert body["mention_role"] == "<@&987654321>"

    def test_non_escalation_has_no_mention_role(self, base_config: dict) -> None:
        """Nicht-Eskalations-Events nutzen keinen @mention (kein unnötiges Pingen)."""
        payload = DiscordNotifyPayload(
            event_type="review_success",
            pr_url="https://github.com/x/y/pull/5",
            repo="x/y",
            pr_number=5,
            consensus_score=9.5,
            stage_scores={"code_review": 9, "security": 10},
            findings=[],
            button_actions=[],
            channel_id=None,
            mention_role=None,
            sticky_message=None,
        )

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(payload, base_config)

        body = mock_post.call_args[1]["json"]
        # Kein @mention bei Erfolg
        assert body.get("mention_role") is None


# ---------------------------------------------------------------------------
# 7. test_sticky_message_flag_passed_through
# ---------------------------------------------------------------------------


class TestStickyMessageFlagPassedThrough:
    """sticky_message-Flag wird korrekt in Webhook-Body weitergeleitet."""

    def test_sticky_message_true_from_config(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """sticky_message=None im Payload → True aus Config."""
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config)

        body = mock_post.call_args[1]["json"]
        assert body["sticky_message"] is True

    def test_sticky_message_false_override(self, base_config: dict) -> None:
        """Payload sticky_message=False überschreibt Config-Default."""
        payload = DiscordNotifyPayload(
            event_type="escalation",
            pr_url="https://github.com/x/y/pull/1",
            repo="x/y",
            pr_number=1,
            consensus_score=4.0,
            stage_scores={},
            findings=[],
            button_actions=[],
            channel_id=None,
            mention_role=None,
            sticky_message=False,
        )

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(payload, base_config)

        body = mock_post.call_args[1]["json"]
        assert body["sticky_message"] is False

    def test_sticky_message_in_body_when_config_has_it(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """sticky_message-Feld ist immer im Body (auch wenn False)."""
        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config)

        body = mock_post.call_args[1]["json"]
        assert "sticky_message" in body


# ---------------------------------------------------------------------------
# 8. test_metrics_logged_on_failure
# ---------------------------------------------------------------------------


class TestMetricsLoggedOnFailure:
    """Bei HTTP-Fehler oder Exception: Eintrag in .ai-review/metrics.jsonl schreiben."""

    def test_metrics_jsonl_written_on_connection_error(
        self, base_payload: DiscordNotifyPayload, base_config: dict, tmp_path: Path
    ) -> None:
        """Arrange: requests.post ConnectionError. Act: notify_discord.
        Assert: metrics.jsonl existiert + enthält failure-Eintrag."""
        import requests

        metrics_file = tmp_path / "metrics.jsonl"

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.side_effect = requests.ConnectionError("ops-n8n unreachable")

            notify_discord(base_payload, base_config, metrics_path=metrics_file)

        assert metrics_file.exists(), "metrics.jsonl wurde nicht erstellt"
        lines = metrics_file.read_text().strip().splitlines()
        assert len(lines) >= 1

        entry = json.loads(lines[-1])
        assert entry["status"] == "failure"
        assert "discord_notify" in entry.get("module", "discord_notify")
        assert "timestamp" in entry

    def test_metrics_jsonl_written_on_4xx_response(
        self, base_payload: DiscordNotifyPayload, base_config: dict, tmp_path: Path
    ) -> None:
        """4xx-Antwort von ops-n8n → auch als failure loggen."""
        metrics_file = tmp_path / "metrics.jsonl"

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=503)

            notify_discord(base_payload, base_config, metrics_path=metrics_file)

        assert metrics_file.exists()
        entry = json.loads(metrics_file.read_text().strip().splitlines()[-1])
        assert entry["status"] == "failure"

    def test_metrics_appended_not_overwritten(
        self, base_payload: DiscordNotifyPayload, base_config: dict, tmp_path: Path
    ) -> None:
        """Metrics-Einträge werden angehängt (append-only JSONL), nicht überschrieben."""
        import requests

        metrics_file = tmp_path / "metrics.jsonl"

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.side_effect = requests.ConnectionError("down")

            notify_discord(base_payload, base_config, metrics_path=metrics_file)
            notify_discord(base_payload, base_config, metrics_path=metrics_file)

        lines = metrics_file.read_text().strip().splitlines()
        assert len(lines) == 2, f"Erwartet 2 Einträge, aber {len(lines)} gefunden"

    def test_metrics_path_via_env_var(
        self, base_payload: DiscordNotifyPayload, base_config: dict, tmp_path: Path
    ) -> None:
        """AI_REVIEW_METRICS_PATH env-var setzt Standard-Metrics-Pfad."""
        import requests

        metrics_file = tmp_path / "env_metrics.jsonl"

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.side_effect = requests.ConnectionError("down")

            with patch.dict(os.environ, {"AI_REVIEW_METRICS_PATH": str(metrics_file)}):
                # metrics_path nicht explizit übergeben → via env var
                notify_discord(base_payload, base_config)

        assert metrics_file.exists()

    def test_no_metrics_written_on_success(
        self, base_payload: DiscordNotifyPayload, base_config: dict, tmp_path: Path
    ) -> None:
        """Bei HTTP 200 wird KEIN Metrics-Eintrag geschrieben (nur Fehler protokollieren)."""
        metrics_file = tmp_path / "metrics.jsonl"

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            notify_discord(base_payload, base_config, metrics_path=metrics_file)

        assert not metrics_file.exists(), "Bei Erfolg darf kein Metrics-Eintrag geschrieben werden"


# ---------------------------------------------------------------------------
# 9. Bonus: Dataclass-Validierung
# ---------------------------------------------------------------------------


class TestDiscordNotifyPayloadDataclass:
    """Grundlegende Dataclass-Semantik des DiscordNotifyPayload."""

    def test_dataclass_fields_accessible(self, base_payload: DiscordNotifyPayload) -> None:
        assert base_payload.event_type == "escalation"
        assert base_payload.pr_number == 42
        assert base_payload.waived is False

    def test_waived_defaults_to_false(self) -> None:
        payload = DiscordNotifyPayload(
            event_type="escalation",
            pr_url="u",
            repo="x/y",
            pr_number=1,
            consensus_score=5.0,
            stage_scores={},
            findings=[],
            button_actions=[],
            channel_id=None,
            mention_role=None,
            sticky_message=None,
        )
        assert payload.waived is False


# ---------------------------------------------------------------------------
# 10. test_default_webhook_url
# ---------------------------------------------------------------------------


class TestDefaultWebhookUrl:
    """ops-n8n Default-URL kann via env var überschrieben werden."""

    def test_custom_webhook_url_via_env(
        self, base_payload: DiscordNotifyPayload, base_config: dict
    ) -> None:
        """AI_REVIEW_DISPATCH_URL env var überschreibt Default-Webhook-URL."""
        custom_url = "http://192.168.1.100:5679/webhook/ai-review-dispatch"

        with patch("ai_review_pipeline.discord_notify.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)

            with patch.dict(os.environ, {"AI_REVIEW_DISPATCH_URL": custom_url}):
                notify_discord(base_payload, base_config)

        call_url = mock_post.call_args[0][0]
        assert call_url == custom_url
