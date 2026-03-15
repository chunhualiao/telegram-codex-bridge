import unittest

from tests.support import make_bridge


class ProgressUpdateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
