import tempfile
import unittest
from pathlib import Path

from bridge import TelegramCodexBridge


def make_bridge() -> TelegramCodexBridge:
    return TelegramCodexBridge.__new__(TelegramCodexBridge)


class UsageSnapshotExtractionTests(unittest.TestCase):
    def test_extracts_usage_from_explicit_usage_container(self) -> None:
        bridge = make_bridge()
        event = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 340,
                "total_tokens": 1540,
            },
        }

        snapshot = bridge.extract_usage_snapshot(event)

        self.assertEqual(
            snapshot,
            {
                "input_tokens": 1200,
                "output_tokens": 340,
                "total_tokens": 1540,
            },
        )

    def test_rejects_obviously_bogus_usage_values(self) -> None:
        bridge = make_bridge()
        event = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 81_709_876,
                "output_tokens": 120,
                "total_tokens": 81_709_996,
            },
        }

        snapshot = bridge.extract_usage_snapshot(event)

        self.assertIsNone(snapshot)

    def test_prefers_more_complete_usage_snapshot(self) -> None:
        bridge = make_bridge()
        event = {
            "type": "turn.completed",
            "usage": {"total_tokens": 600},
            "result": {
                "usage": {
                    "input_tokens": 420,
                    "output_tokens": 180,
                    "total_tokens": 600,
                }
            },
        }

        snapshot = bridge.extract_usage_snapshot(event)

        self.assertEqual(
            snapshot,
            {
                "input_tokens": 420,
                "output_tokens": 180,
                "total_tokens": 600,
            },
        )

    def test_ignores_non_usage_token_fields_in_event_and_item(self) -> None:
        bridge = make_bridge()
        event = {
            "type": "item.completed",
            "input_tokens": 83_430_095,
            "output_tokens": 293_617,
            "result": {
                "type": "command_execution",
                "input_tokens": 7_654_321,
                "output_tokens": 12_345,
            },
            "item": {
                "type": "agent_message",
                "text": "synthetic reply",
                "input_tokens": 6_543_210,
                "output_tokens": 54_321,
            },
        }

        snapshot = bridge.extract_usage_snapshot(event)

        self.assertIsNone(snapshot)


class FinalizeUsageTests(unittest.TestCase):
    def test_finalize_usage_uses_exact_counts_when_present(self) -> None:
        bridge = make_bridge()
        bridge.estimate_tokens = lambda text: 9999
        bridge.get_pricing_model_name = lambda: "anonymized-model"
        bridge.resolve_pricing = lambda model: {
            "input_per_million": 2.0,
            "output_per_million": 8.0,
            "source": "unit-test",
            "url": "https://example.invalid/pricing",
        }

        usage = bridge.finalize_usage(
            prompt="synthetic prompt",
            response_text="synthetic response",
            outcome="ok",
            exact_usage={"input_tokens": 250, "output_tokens": 125},
        )

        self.assertEqual(usage["input_tokens"], 250)
        self.assertEqual(usage["output_tokens"], 125)
        self.assertEqual(usage["exact_input_tokens"], 250)
        self.assertEqual(usage["exact_output_tokens"], 125)
        self.assertEqual(usage["estimated_input_tokens"], 0)
        self.assertEqual(usage["estimated_output_tokens"], 0)
        self.assertAlmostEqual(usage["input_cost_usd"], 0.0005)
        self.assertAlmostEqual(usage["output_cost_usd"], 0.001)
        self.assertEqual(usage["pricing_source"], "unit-test")

    def test_finalize_usage_falls_back_to_estimates_when_exact_counts_missing(self) -> None:
        bridge = make_bridge()

        def fake_estimate(text: str) -> int:
            return {
                "synthetic prompt": 111,
                "synthetic response": 222,
            }[text]

        bridge.estimate_tokens = fake_estimate
        bridge.get_pricing_model_name = lambda: "anonymized-model"
        bridge.resolve_pricing = lambda model: {
            "input_per_million": 1.5,
            "output_per_million": 6.0,
            "source": "unit-test",
            "url": "",
        }

        usage = bridge.finalize_usage(
            prompt="synthetic prompt",
            response_text="synthetic response",
            outcome="ok",
            exact_usage=None,
        )

        self.assertEqual(usage["input_tokens"], 111)
        self.assertEqual(usage["output_tokens"], 222)
        self.assertEqual(usage["exact_input_tokens"], 0)
        self.assertEqual(usage["exact_output_tokens"], 0)
        self.assertEqual(usage["estimated_input_tokens"], 111)
        self.assertEqual(usage["estimated_output_tokens"], 222)
        self.assertAlmostEqual(usage["input_cost_usd"], 0.0001665)
        self.assertAlmostEqual(usage["output_cost_usd"], 0.001332)


class TimeoutDiagnosticsTests(unittest.TestCase):
    def test_format_recent_output_tail_trims_and_skips_blank_lines(self) -> None:
        bridge = make_bridge()
        long_line = "x" * 300

        tail = bridge.format_recent_output_tail(["\n", " short line \n", long_line])

        self.assertEqual(tail, f"short line | {'x' * 237}...")

    def test_build_timeout_diagnostics_includes_last_event_and_tail(self) -> None:
        bridge = make_bridge()
        diagnostics = bridge.build_timeout_diagnostics(
            elapsed=142,
            quiet_for=120,
            idle_timeout=600,
            last_event_at=1_773_407_547.0,
            last_event_summary="item.completed (command_execution)",
            recent_output_tail=['{"type":"item.completed"}\n', "plain text line\n"],
        )

        self.assertIn("Elapsed: 142s", diagnostics)
        self.assertIn("Silent for: 120s", diagnostics)
        self.assertIn("Idle timeout: 600s", diagnostics)
        self.assertIn("Last Codex event:", diagnostics)
        self.assertIn("item.completed (command_execution)", diagnostics)
        self.assertIn('{"type":"item.completed"} | plain text line', diagnostics)

    def test_build_timeout_diagnostics_handles_missing_event_context(self) -> None:
        bridge = make_bridge()
        diagnostics = bridge.build_timeout_diagnostics(
            elapsed=30,
            quiet_for=30,
            idle_timeout=None,
            last_event_at=None,
            last_event_summary=None,
            recent_output_tail=[],
        )

        self.assertIn("Last Codex event: none captured", diagnostics)
        self.assertIn("Recent output tail: none", diagnostics)


class RuntimeConfigTests(unittest.TestCase):
    def make_configured_bridge(self) -> TelegramCodexBridge:
        bridge = make_bridge()
        bridge.workdir = "/tmp/workdir"
        bridge.codex_flags = ["--full-auto", "--json"]
        bridge.codex_model_override = ""
        bridge.system_prompt = "default prompt"
        bridge.codex_max_runtime = 900
        bridge.codex_idle_timeout = 120
        bridge.codex_command_idle_timeout = 600
        bridge.poll_timeout = 30
        bridge.progress_interval = 15
        bridge.progress_edit_interval = 2.0
        bridge.auth_required = True
        bridge.passphrase = "secret-passphrase"
        bridge.inactivity_timeout = 3600
        bridge.conflict_exit_threshold = 3
        bridge.transcribe_model = "whisper-1"
        bridge.transcribe_prompt = "transcribe prompt"
        bridge.meter_price_model = ""
        bridge.meter_price_input_per_million = 0.0
        bridge.meter_price_output_per_million = 0.0
        bridge.pricing_lookup_preference = "auto"
        bridge.pricing_cache_ttl_seconds = 86400
        bridge.config_defaults = bridge.capture_runtime_config_defaults()
        bridge.runtime_config_overrides = {}
        bridge.detected_model_name = bridge.detect_model_name()
        return bridge

    def test_set_runtime_config_value_updates_model_and_persists_override(self) -> None:
        bridge = self.make_configured_bridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge.runtime_config_file = Path(temp_dir) / "runtime_config.json"

            key, value = bridge.set_runtime_config_value("model", "gpt-5.4")

            self.assertEqual(key, "codex.model")
            self.assertEqual(value, "gpt-5.4")
            self.assertEqual(bridge.codex_model_override, "gpt-5.4")
            self.assertEqual(bridge.detected_model_name, "gpt-5.4")
            self.assertEqual(bridge.runtime_config_overrides["codex.model"], "gpt-5.4")

    def test_unset_runtime_config_value_restores_default(self) -> None:
        bridge = self.make_configured_bridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge.runtime_config_file = Path(temp_dir) / "runtime_config.json"
            bridge.set_runtime_config_value("bridge.progress_interval_seconds", "45")

            bridge.unset_runtime_config_value("bridge.progress_interval_seconds")

            self.assertEqual(bridge.progress_interval, 15)
            self.assertNotIn("bridge.progress_interval_seconds", bridge.runtime_config_overrides)

    def test_effective_codex_flags_drops_model_flags_when_override_present(self) -> None:
        bridge = self.make_configured_bridge()
        bridge.codex_flags = ["--full-auto", "--model", "old-model", "--json", "--model=other-old-model"]
        bridge.codex_model_override = "new-model"

        flags = bridge.effective_codex_flags()

        self.assertEqual(flags, ["--full-auto", "--json"])

    def test_current_idle_timeout_uses_command_timeout_for_active_command_execution(self) -> None:
        bridge = self.make_configured_bridge()

        self.assertEqual(bridge.current_idle_timeout_seconds("command_execution"), 600)
        self.assertEqual(bridge.current_idle_timeout_seconds("agent_message"), 120)
        self.assertEqual(bridge.current_idle_timeout_seconds(None), 120)

    def test_handle_config_command_reports_unknown_key(self) -> None:
        bridge = self.make_configured_bridge()
        sent_messages: list[str] = []
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append(text) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None

        bridge.handle_config_command("chat-1", "/config show not-a-key")

        self.assertEqual(sent_messages, ["Unknown config key: not-a-key"])

    def test_config_menu_markup_exposes_clickable_commands(self) -> None:
        bridge = self.make_configured_bridge()

        markup = bridge.config_menu_markup("bridge")

        self.assertTrue(markup["resize_keyboard"])
        labels = [button["text"] for row in markup["keyboard"] for button in row]
        self.assertIn("/config show bridge.auth_required", labels)
        self.assertIn("/config set bridge.auth_required false", labels)
        self.assertIn("/config hide", labels)

    def test_auth_disabled_skips_unlock_gate(self) -> None:
        bridge = self.make_configured_bridge()
        bridge.auth_required = False
        bridge.load_last_activity = lambda: None

        self.assertFalse(bridge.is_unlock_required())

    def test_passphrase_is_masked_in_config_output(self) -> None:
        bridge = self.make_configured_bridge()

        details = bridge.format_config_key_details("bridge.passphrase")

        self.assertIn("Config key: bridge.passphrase", details)
        self.assertIn("se*************se", details)
        self.assertNotIn("secret-passphrase", details)

    def test_handle_config_set_sends_confirmation(self) -> None:
        bridge = self.make_configured_bridge()
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
        bridge = self.make_configured_bridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_config_file = Path(temp_dir) / "runtime_config.json"
            bridge.runtime_config_file = runtime_config_file

            bridge.set_runtime_config_value("bridge.passphrase", "new-secret")

            self.assertEqual(bridge.passphrase, "new-secret")
            self.assertEqual(bridge.runtime_config_overrides["bridge.passphrase"], "new-secret")
            self.assertEqual(runtime_config_file.read_text(encoding="utf-8").strip(), "{}")

    def test_handle_config_unset_sends_confirmation(self) -> None:
        bridge = self.make_configured_bridge()
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
            writer = self.make_configured_bridge()
            writer.runtime_config_file = runtime_config_file
            writer.set_runtime_config_value("bridge.auth_required", "false")
            writer.set_runtime_config_value("codex.model", "synthetic-model")

            reader = self.make_configured_bridge()
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
            writer = self.make_configured_bridge()
            writer.runtime_config_file = runtime_config_file
            writer.set_runtime_config_value("voice.transcribe_prompt", long_prompt)

            reader = self.make_configured_bridge()
            reader.runtime_config_file = runtime_config_file
            reader.runtime_config_overrides = reader.load_runtime_config()
            reader.apply_runtime_config(reader.runtime_config_overrides)

        self.assertEqual(reader.transcribe_prompt, long_prompt)
        self.assertEqual(reader.runtime_config_overrides["voice.transcribe_prompt"], long_prompt)

    def test_load_runtime_config_ignores_persisted_passphrase_override(self) -> None:
        bridge = self.make_configured_bridge()
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
        bridge = self.make_configured_bridge()
        sent_messages: list[tuple[str, dict | None]] = []
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append((text, reply_markup)) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None

        bridge.handle_config_command("chat-1", "/config")

        self.assertIn("Bridge config menu", sent_messages[0][0])
        self.assertIsInstance(sent_messages[0][1], dict)

    def test_config_menu_hide_removes_keyboard(self) -> None:
        bridge = self.make_configured_bridge()
        sent_messages: list[tuple[str, dict | None]] = []
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append((text, reply_markup)) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None

        bridge.handle_config_command("chat-1", "/config hide")

        self.assertEqual(sent_messages[0][0], "Config menu hidden.")
        self.assertEqual(sent_messages[0][1], {"remove_keyboard": True})


if __name__ == "__main__":
    unittest.main()
