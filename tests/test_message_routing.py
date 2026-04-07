import unittest
import threading

from tests.support import make_configured_bridge


class MessageRoutingTests(unittest.TestCase):
    def test_process_message_routes_plain_prompt_to_interactive_job(self) -> None:
        bridge = make_configured_bridge()
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
        bridge = make_configured_bridge()
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
        bridge = make_configured_bridge()
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
        bridge = make_configured_bridge()

        kind, reason = bridge.choose_job_kind("Iterate on this tuning prompt and generate multiple variants")

        self.assertEqual(kind, "background")
        self.assertIn("Auto-dispatch picked background", reason)

    def test_choose_job_kind_keeps_short_question_interactive(self) -> None:
        bridge = make_configured_bridge()

        kind, reason = bridge.choose_job_kind("Why is this test failing?")

        self.assertEqual(kind, "interactive")
        self.assertIn("interactive lane", reason)

    def test_process_message_lists_jobs(self) -> None:
        bridge = make_configured_bridge()
        sent_messages: list[str] = []
        bridge.log_event = lambda *args, **kwargs: None
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append(text) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None
        bridge.format_jobs_overview = lambda: "Bridge jobs\nActive: none"

        bridge.process_message("chat-1", {"text": "/jobs"})

        self.assertEqual(sent_messages, ["Bridge jobs\nActive: none"])

    def test_process_message_cancel_requests_active_job(self) -> None:
        bridge = make_configured_bridge()
        sent_messages: list[str] = []
        bridge.log_event = lambda *args, **kwargs: None
        bridge.send_message = lambda chat_id, text, reply_markup=None: sent_messages.append(text) or 1
        bridge.save_last_activity = lambda *args, **kwargs: None
        bridge.cancel_job = lambda job_id: job_id == "job-3"

        bridge.process_message("chat-1", {"text": "/cancel job-3"})

        self.assertEqual(sent_messages, ["Cancellation requested for job-3."])

    def test_start_codex_job_rejects_second_interactive_job(self) -> None:
        bridge = make_configured_bridge()
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

    def test_run_codex_job_worker_completes_without_jobs_lock_deadlock(self) -> None:
        bridge = make_configured_bridge()
        bridge.jobs["job-1"] = {
            "id": "job-1",
            "kind": "interactive",
            "status": "queued",
            "chat_id": "chat-1",
            "prompt_preview": "hello",
            "cancel_requested": False,
            "result_preview": "",
        }
        bridge.interactive_job_id = "job-1"
        bridge.send_message = lambda chat_id, text, reply_markup=None: 1
        bridge.load_thread_id = lambda: None
        bridge.run_codex = lambda *args, **kwargs: (
            "reply",
            {
                "model": "gpt-test",
                "input_tokens": 1,
                "output_tokens": 1,
                "exact_input_tokens": 1,
                "exact_output_tokens": 1,
                "estimated_input_tokens": 0,
                "estimated_output_tokens": 0,
                "input_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "outcome": "ok",
                "pricing_source": "manual",
                "pricing_url": "",
            },
        )
        bridge.record_usage = lambda usage: None
        bridge.log_event = lambda *args, **kwargs: None

        worker = threading.Thread(
            target=bridge.run_codex_job_worker,
            args=("job-1", "hello", None),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=0.5)

        self.assertFalse(worker.is_alive(), "worker thread deadlocked on jobs_lock")
        self.assertEqual(bridge.jobs["job-1"]["status"], "completed")
        self.assertEqual(bridge.interactive_job_id, None)


if __name__ == "__main__":
    unittest.main()
