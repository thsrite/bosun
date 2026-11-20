"""Consumer-visible privacy and uncertain-correlation boundaries; run on server 49."""
import json
import unittest

from analytics import enrich_calls, log_event, session_event, usage_event


class AnalyticsTests(unittest.TestCase):
    def call(self, **changes):
        value = {"id": "call1", "task_id": 1585, "start": 1790309400.0,
                 "end": 1790309500.0, "duration_s": 100, "ttft_s": 10,
                 "input_tokens": 170000, "cache_tokens": 169000,
                 "output_tokens": 600, "model": "gpt-6-astra", "tool_names": ["edit"]}
        value.update(changes)
        return value

    def usage(self, **changes):
        value = {"id": 1, "start": 1790309400.5, "duration_s": 99.5,
                 "ttft_s": 9.5, "input_tokens": 170000, "cache_tokens": 169000,
                 "output_tokens": 600, "model": "gpt-6-astra", "account": "account-123456789abc",
                 "failed": False}
        value.update(changes)
        return value

    def test_correlated_round_preserves_task_and_stream_rate(self):
        call = enrich_calls([self.call()], [self.usage()], [])[0]
        self.assertEqual(call["task_id"], 1585)
        self.assertEqual(call["correlation"], "matched")
        self.assertAlmostEqual(call["output_rate"], 600 / 90)
        self.assertIsNone(call["retry_count"])

    def test_two_calls_cannot_claim_same_gateway_request(self):
        calls = enrich_calls([self.call(), self.call(id="call2", task_id=1587)], [self.usage()], [])
        self.assertTrue(all(c["correlation"] == "ambiguous" and c["gateway"] is None for c in calls))

    def test_missing_telemetry_is_not_zero_or_slow_stream(self):
        call = enrich_calls([self.call(ttft_s=None, cache_tokens=None)], [self.usage()], [])[0]
        self.assertIsNone(call["gateway"])
        self.assertIsNone(call["output_rate"])
        self.assertIsNone(call["retry_count"])
        self.assertNotIn("slow_stream_observed", call["diagnosis"])

    def test_long_first_token_does_not_mean_slow_stream(self):
        call = enrich_calls([self.call(ttft_s=99, output_tokens=600)], [], [])[0]
        self.assertIn("high_ttft", call["diagnosis"])
        self.assertNotIn("slow_stream_observed", call["diagnosis"])

    def test_retry_account_switch_stays_on_unique_stream(self):
        usage = self.usage(start=1790309410.0, duration_s=90)
        logs = [
            {"kind": "selection", "at": 1790309400.0, "stream_id": "38d84fba", "model": "gpt-6-astra", "account": "account-aaaaaaaaaaaa"},
            {"kind": "failure", "at": 1790309405.0, "stream_id": "38d84fba", "model": "gpt-6-astra", "error_kind": "tls_eof", "duration_s": 5},
            {"kind": "selection", "at": 1790309410.0, "stream_id": "38d84fba", "model": "gpt-6-astra", "account": usage["account"]},
        ]
        call = enrich_calls([self.call()], [usage], logs)[0]
        self.assertEqual(call["retry_count"], 1)
        self.assertEqual(call["retry_seconds"], 5)

    def test_prompt_credentials_and_account_never_survive_normalization(self):
        secret = "private-content-do-not-persist"
        record = {"id": "abc123", "timestamp": "2026-09-25T04:00:02Z", "message": {
            "role": "assistant", "timestamp": 1790308800000, "model": "gpt-6-astra",
            "content": [{"type": "text", "text": secret}, {"type": "toolCall", "name": "write", "arguments": {"content": secret}}],
            "providerPayload": {"api_key": secret}, "usage": {"input": 100, "cacheRead": 50, "cacheWrite": 0, "output": 10}}}
        normalized = session_event(record)
        account = usage_event({"id": 1, "source": "alice@example.com", "timestamp": "2026-09-25T12:00:00+08:00"}, b"test-salt")
        line = '[2026-09-25 12:00:00] [38d84fba] [debug] [conductor_execution.go:1839] Use OAuth provider=codex auth_file=codex-12345678-alice@example.com-pro.json for model gpt-6-astra'
        selection = log_event(line, b"test-salt")
        self.assertEqual(account["account"], selection["account"])
        encoded = json.dumps([normalized, account, selection])
        self.assertNotIn(secret, encoded)
        self.assertNotIn("alice@example.com", encoded)
        self.assertNotIn("auth_file", encoded)
        self.assertEqual(normalized["input_tokens"], 150)

    def test_real_tool_ids_do_not_collapse(self):
        ids = ["call_ABC|ctc_" + "a" * 70, "call_DEF|ctc_" + "b" * 70]
        pairs = []
        for tool_id in ids:
            start = session_event({"type": "custom", "customType": "tool_execution_start", "timestamp": "2026-09-25T04:00:00Z", "data": {"toolCallId": tool_id, "toolName": "edit"}})
            end = session_event({"timestamp": "2026-09-25T04:00:01Z", "message": {"role": "toolResult", "toolCallId": tool_id, "toolName": "edit", "isError": True}})
            self.assertIsNotNone(start["id"])
            self.assertEqual(start["id"], end["id"])
            self.assertTrue(end["error"])
            pairs.append(start["id"])
        self.assertNotEqual(*pairs)


if __name__ == "__main__":
    unittest.main()
