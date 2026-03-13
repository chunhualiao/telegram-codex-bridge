import unittest

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


if __name__ == "__main__":
    unittest.main()
