import tempfile
import unittest
from pathlib import Path

from tests.support import make_configured_bridge


class RuntimeConfigTests(unittest.TestCase):
    def test_set_runtime_config_value_updates_model_and_persists_override(self) -> None:
        bridge = make_configured_bridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge.runtime_config_file = Path(temp_dir) / "runtime_config.json"

            key, value = bridge.set_runtime_config_value("model", "gpt-5.4")

            self.assertEqual(key, "codex.model")
            self.assertEqual(value, "gpt-5.4")
            self.assertEqual(bridge.codex_model_override, "gpt-5.4")
            self.assertEqual(bridge.detected_model_name, "gpt-5.4")
            self.assertEqual(bridge.runtime_config_overrides["codex.model"], "gpt-5.4")

    def test_unset_runtime_config_value_restores_default(self) -> None:
        bridge = make_configured_bridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge.runtime_config_file = Path(temp_dir) / "runtime_config.json"
            bridge.set_runtime_config_value("bridge.progress_interval_seconds", "45")

            bridge.unset_runtime_config_value("bridge.progress_interval_seconds")

            self.assertEqual(bridge.progress_interval, 15)
            self.assertNotIn("bridge.progress_interval_seconds", bridge.runtime_config_overrides)

    def test_effective_codex_flags_drops_model_flags_when_override_present(self) -> None:
        bridge = make_configured_bridge()
        bridge.codex_flags = ["--full-auto", "--model", "old-model", "--json", "--model=other-old-model"]
        bridge.codex_model_override = "new-model"

        flags = bridge.effective_codex_flags()

        self.assertEqual(flags, ["--full-auto", "--json"])

    def test_current_idle_timeout_uses_command_timeout_for_active_command_execution(self) -> None:
        bridge = make_configured_bridge()

        self.assertEqual(bridge.current_idle_timeout_seconds("command_execution"), 600)
        self.assertEqual(bridge.current_idle_timeout_seconds("agent_message"), 120)
        self.assertEqual(bridge.current_idle_timeout_seconds(None), 120)

    def test_infer_active_item_type_recovers_command_execution_from_last_event(self) -> None:
        bridge = make_configured_bridge()

        inferred = bridge.infer_active_item_type(
            active_item_type=None,
            last_event_type="item.started",
            last_event_item_type="command_execution",
        )

        self.assertEqual(inferred, "command_execution")
        self.assertEqual(bridge.current_idle_timeout_seconds(inferred), 600)

    def test_infer_active_item_type_does_not_extend_completed_command_execution(self) -> None:
        bridge = make_configured_bridge()

        inferred = bridge.infer_active_item_type(
            active_item_type=None,
            last_event_type="item.completed",
            last_event_item_type="command_execution",
        )

        self.assertIsNone(inferred)
        self.assertEqual(bridge.current_idle_timeout_seconds(inferred), 120)

    def test_handle_config_command_reports_unknown_key(self) -> None:
        bridge = make_configured_bridge()
        sent_messages: list[str] = []
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append(text) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None

        bridge.handle_config_command("chat-1", "/config show not-a-key")

        self.assertEqual(sent_messages, ["Unknown config key: not-a-key"])

    def test_config_menu_markup_exposes_clickable_commands(self) -> None:
        bridge = make_configured_bridge()

        markup = bridge.config_menu_markup("bridge")

        self.assertTrue(markup["resize_keyboard"])
        labels = [button["text"] for row in markup["keyboard"] for button in row]
        self.assertIn("/config show bridge.auth_required", labels)
        self.assertIn("/config set bridge.auth_required false", labels)
        self.assertIn("/config show bridge.progress_mode", labels)
        self.assertIn("/config set bridge.progress_mode append", labels)
        self.assertIn("/config hide", labels)

    def test_set_runtime_config_value_updates_progress_mode(self) -> None:
        bridge = make_configured_bridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge.runtime_config_file = Path(temp_dir) / "runtime_config.json"

            key, value = bridge.set_runtime_config_value("bridge.progress_mode", "append")

            self.assertEqual(key, "bridge.progress_mode")
            self.assertEqual(value, "append")
            self.assertEqual(bridge.progress_mode, "append")

    def test_auth_disabled_skips_unlock_gate(self) -> None:
        bridge = make_configured_bridge()
        bridge.auth_required = False
        bridge.load_last_activity = lambda: None

        self.assertFalse(bridge.is_unlock_required())

    def test_passphrase_is_masked_in_config_output(self) -> None:
        bridge = make_configured_bridge()

        details = bridge.format_config_key_details("bridge.passphrase")

        self.assertIn("Config key: bridge.passphrase", details)
        self.assertIn("se*************se", details)
        self.assertNotIn("secret-passphrase", details)

    def test_handle_config_set_sends_confirmation(self) -> None:
        bridge = make_configured_bridge()
        sent_messages: list[tuple[str, dict | None]] = []
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append((text, reply_markup)) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge.runtime_config_file = Path(temp_dir) / "runtime_config.json"

            bridge.handle_config_command("chat-1", "/config set bridge.auth_required false")

        self.assertEqual(sent_messages[0][0], "Updated bridge.auth_required = false")
        self.assertIsInstance(sent_messages[0][1], dict)
        self.assertFalse(bridge.auth_required)

    def test_passphrase_override_is_not_persisted_to_disk(self) -> None:
        bridge = make_configured_bridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_config_file = Path(temp_dir) / "runtime_config.json"
            bridge.runtime_config_file = runtime_config_file

            bridge.set_runtime_config_value("bridge.passphrase", "new-secret")

            self.assertEqual(bridge.passphrase, "new-secret")
            self.assertEqual(bridge.runtime_config_overrides["bridge.passphrase"], "new-secret")
            self.assertEqual(runtime_config_file.read_text(encoding="utf-8").strip(), "{}")

    def test_handle_config_unset_sends_confirmation(self) -> None:
        bridge = make_configured_bridge()
        sent_messages: list[tuple[str, dict | None]] = []
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append((text, reply_markup)) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge.runtime_config_file = Path(temp_dir) / "runtime_config.json"
            bridge.set_runtime_config_value("bridge.auth_required", "false")

            bridge.handle_config_command("chat-1", "/config unset bridge.auth_required")

        self.assertEqual(sent_messages[0][0], "Removed override for bridge.auth_required")
        self.assertTrue(bridge.auth_required)

    def test_runtime_config_round_trip_loads_into_fresh_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_config_file = Path(temp_dir) / "runtime_config.json"
            writer = make_configured_bridge()
            writer.runtime_config_file = runtime_config_file
            writer.set_runtime_config_value("bridge.auth_required", "false")
            writer.set_runtime_config_value("codex.model", "synthetic-model")

            reader = make_configured_bridge()
            reader.runtime_config_file = runtime_config_file
            reader.runtime_config_overrides = reader.load_runtime_config()
            reader.apply_runtime_config(reader.runtime_config_overrides)

        self.assertFalse(reader.auth_required)
        self.assertEqual(reader.codex_model_override, "synthetic-model")
        self.assertEqual(reader.detected_model_name, "synthetic-model")

    def test_runtime_config_round_trip_preserves_long_string_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_config_file = Path(temp_dir) / "runtime_config.json"
            long_prompt = "prompt-" + ("x" * 180)
            writer = make_configured_bridge()
            writer.runtime_config_file = runtime_config_file
            writer.set_runtime_config_value("voice.transcribe_prompt", long_prompt)

            reader = make_configured_bridge()
            reader.runtime_config_file = runtime_config_file
            reader.runtime_config_overrides = reader.load_runtime_config()
            reader.apply_runtime_config(reader.runtime_config_overrides)

        self.assertEqual(reader.transcribe_prompt, long_prompt)
        self.assertEqual(reader.runtime_config_overrides["voice.transcribe_prompt"], long_prompt)

    def test_load_runtime_config_ignores_persisted_passphrase_override(self) -> None:
        bridge = make_configured_bridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_config_file = Path(temp_dir) / "runtime_config.json"
            runtime_config_file.write_text(
                '{"bridge.passphrase":"persisted-secret","bridge.auth_required":false}',
                encoding="utf-8",
            )
            bridge.runtime_config_file = runtime_config_file

            overrides = bridge.load_runtime_config()

        self.assertEqual(overrides, {"bridge.auth_required": False})

    def test_config_root_command_shows_menu_message(self) -> None:
        bridge = make_configured_bridge()
        sent_messages: list[tuple[str, dict | None]] = []
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append((text, reply_markup)) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None

        bridge.handle_config_command("chat-1", "/config")

        self.assertIn("Bridge config menu", sent_messages[0][0])
        self.assertIsInstance(sent_messages[0][1], dict)

    def test_config_menu_hide_removes_keyboard(self) -> None:
        bridge = make_configured_bridge()
        sent_messages: list[tuple[str, dict | None]] = []
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append((text, reply_markup)) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None

        bridge.handle_config_command("chat-1", "/config hide")

        self.assertEqual(sent_messages[0][0], "Config menu hidden.")
        self.assertEqual(sent_messages[0][1], {"remove_keyboard": True})


if __name__ == "__main__":
    unittest.main()
