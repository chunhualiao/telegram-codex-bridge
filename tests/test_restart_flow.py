import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import make_configured_bridge


class RestartFlowTests(unittest.TestCase):
    def test_restart_intent_detection_matches_natural_language_and_script_requests(self) -> None:
        bridge = make_configured_bridge()

        self.assertTrue(bridge.is_restart_request("/restart"))
        self.assertTrue(bridge.is_restart_request("restart the bridge"))
        self.assertTrue(bridge.is_restart_request("run restart-bridge.sh"))
        self.assertTrue(bridge.is_restart_request("run the restart sh"))
        self.assertFalse(bridge.is_restart_request("please review restart behavior in this repo"))

    def test_process_message_routes_restart_request_to_internal_restart_handler(self) -> None:
        bridge = make_configured_bridge()
        sent_messages: list[str] = []
        restart_calls: list[str] = []
        starts: list[tuple[str, str, str]] = []
        bridge.log_event = lambda *args, **kwargs: None
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append(text) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None
        bridge.schedule_restart = lambda: restart_calls.append("scheduled")
        bridge.start_codex_job = (
            lambda *, chat_id, prompt, kind, image_message=None: (
                starts.append((chat_id, prompt, kind)) or ("job-1", "unexpected")
            )
        )

        bridge.process_message("chat-1", {"text": "run the restart sh"})

        self.assertEqual(restart_calls, ["scheduled"])
        self.assertEqual(starts, [])
        self.assertEqual(
            sent_messages,
            ["Scheduling a detached bridge restart. Expect a fresh 'Telegram Codex bridge is online.' message shortly."],
        )

    def test_handle_restart_request_reports_schedule_failure(self) -> None:
        bridge = make_configured_bridge()
        sent_messages: list[str] = []
        logged: list[tuple[tuple[object, ...], dict]] = []
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append(text) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None
        bridge.log_event = lambda *args, **kwargs: logged.append((args, kwargs))

        def fail_restart() -> None:
            raise RuntimeError("boom")

        bridge.schedule_restart = fail_restart

        bridge.handle_restart_request("chat-1")

        self.assertEqual(
            sent_messages,
            [
                "Scheduling a detached bridge restart. Expect a fresh 'Telegram Codex bridge is online.' message shortly.",
                "Failed to schedule restart.\n\nboom",
            ],
        )
        self.assertEqual(logged[0][0], ("ERROR", "Failed to schedule detached restart: boom"))

    def test_schedule_restart_launches_detached_helper_and_writes_restart_log(self) -> None:
        bridge = make_configured_bridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            restart_script = state_dir / "restart-bridge.sh"
            restart_script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            restart_log = state_dir / "restart.log"
            bridge.restart_log_file = restart_log

            captured: dict[str, object] = {}

            def fake_popen(args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                return object()

            with mock.patch("bridge.BASE_DIR", state_dir):
                with mock.patch("bridge.subprocess.Popen", side_effect=fake_popen):
                    bridge.schedule_restart()

            self.assertEqual(captured["args"], ["/bin/bash", str(restart_script)])
            kwargs = captured["kwargs"]
            self.assertEqual(kwargs["cwd"], str(state_dir))
            self.assertEqual(kwargs["stdin"], mock.ANY)
            self.assertEqual(kwargs["stderr"], mock.ANY)
            self.assertTrue(kwargs["start_new_session"])
            self.assertIn("/usr/bin:/bin", kwargs["env"]["PATH"])
            self.assertTrue(restart_log.exists())
            self.assertEqual(stat.S_IMODE(restart_log.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
