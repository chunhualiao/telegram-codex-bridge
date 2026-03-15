import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
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


class PollingErrorClassificationTests(unittest.TestCase):
    def test_transient_polling_error_detects_connection_reset(self) -> None:
        bridge = make_bridge()

        self.assertTrue(
            bridge.is_transient_polling_error(
                urllib.error.URLError("[Errno 54] Connection reset by peer")
            )
        )

    def test_transient_polling_error_detects_no_route_to_host(self) -> None:
        bridge = make_bridge()

        self.assertTrue(
            bridge.is_transient_polling_error(
                urllib.error.URLError("[Errno 65] No route to host")
            )
        )

    def test_transient_polling_error_rejects_non_network_messages(self) -> None:
        bridge = make_bridge()

        self.assertFalse(
            bridge.is_transient_polling_error(
                urllib.error.URLError("certificate verify failed")
            )
        )


class OpsecTests(unittest.TestCase):
    def test_sanitize_sensitive_text_redacts_tokens_and_bearer_headers(self) -> None:
        bridge = make_bridge()
        bridge.bot_token = "123456:telegram-secret"
        bridge.openai_api_key = "sk-secret-openai"
        text = (
            "Telegram failed for https://api.telegram.org/bot123456:telegram-secret/getUpdates "
            "with Authorization: Bearer sk-secret-openai"
        )

        sanitized = bridge.sanitize_sensitive_text(text)

        self.assertNotIn("123456:telegram-secret", sanitized)
        self.assertNotIn("sk-secret-openai", sanitized)
        self.assertIn("[redacted-telegram-token]", sanitized)
        self.assertIn("Bearer [redacted]", sanitized)

    def test_write_private_text_uses_private_file_permissions(self) -> None:
        bridge = make_bridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "secret.txt"

            bridge.write_private_text(path, "secret-value")

            mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_transcribe_audio_uses_in_process_http_request(self) -> None:
        bridge = make_bridge()
        bridge.openai_api_key = "sk-unit-test"
        bridge.transcribe_model = "whisper-1"
        bridge.transcribe_prompt = "hello"
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"text": "transcribed text"}).encode("utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "voice.wav"
            source.write_bytes(b"fake-wav")

            import bridge as bridge_module

            original_urlopen = bridge_module.urllib.request.urlopen

            def fake_urlopen(req, timeout=0):
                captured["url"] = req.full_url
                captured["headers"] = dict(req.header_items())
                captured["data"] = req.data
                captured["timeout"] = timeout
                return FakeResponse()

            bridge_module.urllib.request.urlopen = fake_urlopen
            try:
                text = bridge.transcribe_audio(source)
            finally:
                bridge_module.urllib.request.urlopen = original_urlopen

        self.assertEqual(text, "transcribed text")
        self.assertEqual(captured["url"], "https://api.openai.com/v1/audio/transcriptions")
        headers = {str(key).lower(): str(value) for key, value in captured["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer sk-unit-test")
        self.assertIn("multipart/form-data; boundary=", headers["content-type"])
        self.assertIsInstance(captured["data"], bytes)
        self.assertIn(b'name="model"', captured["data"])
        self.assertIn(b'name="prompt"', captured["data"])
        self.assertIn(b'filename="voice.wav"', captured["data"])


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
        bridge.progress_mode = "edit"
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
        bridge.jobs_lock = threading.Lock()
        bridge.jobs = {}
        bridge.recent_job_ids = []
        bridge.next_job_number = 0
        bridge.interactive_job_id = None
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

    def test_infer_active_item_type_recovers_command_execution_from_last_event(self) -> None:
        bridge = self.make_configured_bridge()

        inferred = bridge.infer_active_item_type(
            active_item_type=None,
            last_event_type="item.started",
            last_event_item_type="command_execution",
        )

        self.assertEqual(inferred, "command_execution")
        self.assertEqual(bridge.current_idle_timeout_seconds(inferred), 600)

    def test_infer_active_item_type_does_not_extend_completed_command_execution(self) -> None:
        bridge = self.make_configured_bridge()

        inferred = bridge.infer_active_item_type(
            active_item_type=None,
            last_event_type="item.completed",
            last_event_item_type="command_execution",
        )

        self.assertIsNone(inferred)
        self.assertEqual(bridge.current_idle_timeout_seconds(inferred), 120)

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
        self.assertIn("/config show bridge.progress_mode", labels)
        self.assertIn("/config set bridge.progress_mode append", labels)
        self.assertIn("/config hide", labels)

    def test_set_runtime_config_value_updates_progress_mode(self) -> None:
        bridge = self.make_configured_bridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge.runtime_config_file = Path(temp_dir) / "runtime_config.json"

            key, value = bridge.set_runtime_config_value("bridge.progress_mode", "append")

            self.assertEqual(key, "bridge.progress_mode")
            self.assertEqual(value, "append")
            self.assertEqual(bridge.progress_mode, "append")


class ProgressUpdateTests(unittest.TestCase):
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
        bridge.progress_mode = "edit"
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
        bridge.jobs_lock = threading.Lock()
        bridge.jobs = {}
        bridge.recent_job_ids = []
        bridge.next_job_number = 0
        bridge.interactive_job_id = None
        bridge.config_defaults = bridge.capture_runtime_config_defaults()
        bridge.runtime_config_overrides = {}
        bridge.detected_model_name = bridge.detect_model_name()
        return bridge

    def test_maybe_update_progress_edits_status_message_in_edit_mode(self) -> None:
        bridge = make_bridge()
        bridge.progress_mode = "edit"
        bridge.progress_edit_interval = 2.0
        edits: list[tuple[str, int, str]] = []
        bridge.edit_message = lambda chat_id, message_id, text: edits.append((chat_id, message_id, text))
        state = {"last_text": "", "last_edit_at": 0.0}

        bridge.maybe_update_progress("chat-1", 123, "Working...", force=True, state=state)

        self.assertEqual(edits, [("chat-1", 123, "Working...")])
        self.assertEqual(state["last_text"], "Working...")

    def test_maybe_update_progress_sends_new_messages_in_append_mode(self) -> None:
        bridge = make_bridge()
        bridge.progress_mode = "append"
        bridge.progress_interval = 15
        bridge.progress_edit_interval = 2.0
        sent_messages: list[tuple[str, str]] = []
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append((chat_id, text)) or 1
        state = {"last_text": "", "last_edit_at": 0.0}

        bridge.maybe_update_progress("chat-1", 123, "Working...", force=True, state=state)
        bridge.maybe_update_progress("chat-1", 123, "Still working...", state=state)

        self.assertEqual(
            sent_messages,
            [("chat-1", "Working..."), ("chat-1", "Still working...")],
        )
        self.assertEqual(state["last_text"], "Still working...")

    def test_maybe_update_progress_throttles_append_mode_messages(self) -> None:
        bridge = make_bridge()
        bridge.progress_mode = "append"
        bridge.progress_interval = 15
        bridge.progress_edit_interval = 2.0
        sent_messages: list[tuple[str, str]] = []
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append((chat_id, text)) or 1
        state = {"last_text": "", "last_edit_at": 100.0}

        import bridge as bridge_module

        original_time = bridge_module.time.time
        bridge_module.time.time = lambda: 105.0
        try:
            bridge.maybe_update_progress(
                "chat-1",
                123,
                "Running Codex...\n\nStill working... 60s elapsed.",
                state=state,
            )
        finally:
            bridge_module.time.time = original_time

        self.assertEqual(sent_messages, [])

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

    def test_process_message_routes_plain_prompt_to_interactive_job(self) -> None:
        bridge = self.make_configured_bridge()
        sent_messages: list[str] = []
        starts: list[tuple[str, str, str, object | None]] = []
        bridge.log_event = lambda *args, **kwargs: None
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append(text) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None
        bridge.start_codex_job = (
            lambda *, chat_id, prompt, kind, image_message=None: (
                starts.append((chat_id, prompt, kind, image_message)) or ("job-1", "Started interactive job job-1.")
            )
        )
        bridge.choose_job_kind = lambda prompt, has_image=False, has_voice=False: (
            "interactive",
            "Auto-dispatch kept this in the interactive lane.",
        )

        bridge.process_message("chat-1", {"text": "hello bridge"})

        self.assertEqual(starts, [("chat-1", "hello bridge", "interactive", None)])
        self.assertEqual(
            sent_messages,
            ["Auto-dispatch kept this in the interactive lane.\n\nStarted interactive job job-1."],
        )

    def test_process_message_routes_spawn_command_to_background_job(self) -> None:
        bridge = self.make_configured_bridge()
        sent_messages: list[str] = []
        starts: list[tuple[str, str, str]] = []
        bridge.log_event = lambda *args, **kwargs: None
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append(text) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None
        bridge.start_codex_job = (
            lambda *, chat_id, prompt, kind, image_message=None: (
                starts.append((chat_id, prompt, kind)) or ("job-2", "Started background job job-2.")
            )
        )

        bridge.process_message("chat-1", {"text": "/spawn tune this prompt"})

        self.assertEqual(starts, [("chat-1", "tune this prompt", "background")])
        self.assertEqual(sent_messages, ["Started background job job-2."])

    def test_process_message_auto_dispatches_long_running_prompt_to_background_job(self) -> None:
        bridge = self.make_configured_bridge()
        sent_messages: list[str] = []
        starts: list[tuple[str, str, str]] = []
        bridge.log_event = lambda *args, **kwargs: None
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append(text) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None
        bridge.start_codex_job = (
            lambda *, chat_id, prompt, kind, image_message=None: (
                starts.append((chat_id, prompt, kind)) or ("job-9", "Started background job job-9.")
            )
        )

        bridge.process_message("chat-1", {"text": "please tune this prompt until it gets better"})

        self.assertEqual(starts, [("chat-1", "please tune this prompt until it gets better", "background")])
        self.assertIn("Auto-dispatch picked background", sent_messages[0])
        self.assertIn("Started background job job-9.", sent_messages[0])

    def test_choose_job_kind_marks_tuning_prompt_as_background(self) -> None:
        bridge = self.make_configured_bridge()

        kind, reason = bridge.choose_job_kind("Iterate on this tuning prompt and generate multiple variants")

        self.assertEqual(kind, "background")
        self.assertIn("Auto-dispatch picked background", reason)

    def test_choose_job_kind_keeps_short_question_interactive(self) -> None:
        bridge = self.make_configured_bridge()

        kind, reason = bridge.choose_job_kind("Why is this test failing?")

        self.assertEqual(kind, "interactive")
        self.assertIn("interactive lane", reason)

    def test_process_message_lists_jobs(self) -> None:
        bridge = self.make_configured_bridge()
        sent_messages: list[str] = []
        bridge.log_event = lambda *args, **kwargs: None
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append(text) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None
        bridge.format_jobs_overview = lambda: "Bridge jobs\nActive: none"

        bridge.process_message("chat-1", {"text": "/jobs"})

        self.assertEqual(sent_messages, ["Bridge jobs\nActive: none"])

    def test_process_message_cancel_requests_active_job(self) -> None:
        bridge = self.make_configured_bridge()
        sent_messages: list[str] = []
        bridge.log_event = lambda *args, **kwargs: None
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append(text) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None
        bridge.cancel_job = lambda job_id: job_id == "job-3"

        bridge.process_message("chat-1", {"text": "/cancel job-3"})

        self.assertEqual(sent_messages, ["Cancellation requested for job-3."])

    def test_start_codex_job_rejects_second_interactive_job(self) -> None:
        bridge = self.make_configured_bridge()
        bridge.jobs["job-1"] = {
            "id": "job-1",
            "kind": "interactive",
            "status": "running",
            "chat_id": "chat-1",
            "prompt_preview": "existing job",
            "cancel_requested": False,
        }
        bridge.interactive_job_id = "job-1"

        job_id, message = bridge.start_codex_job(chat_id="chat-1", prompt="next job", kind="interactive")

        self.assertIsNone(job_id)
        self.assertIn("Interactive lane is busy with job-1", message)


if __name__ == "__main__":
    unittest.main()
