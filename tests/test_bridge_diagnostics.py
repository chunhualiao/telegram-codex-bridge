import unittest
import urllib.error

from tests.support import make_bridge


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


if __name__ == "__main__":
    unittest.main()
