import asyncio
import unittest
from unittest.mock import AsyncMock, call, patch

from backend.app import main, quota


class QuotaRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_idle_backend_refreshes_both_providers_immediately_and_hourly(self):
        with (
            patch.object(quota, "_cache", {}),
            patch.object(quota, "_fetch_claude", return_value={"available": True}) as claude,
            patch.object(quota, "_fetch_codex", return_value={"available": True}) as codex,
            patch.object(quota, "time", **{
                "monotonic.side_effect": [0, 12, 3600, 3607],
                "time.side_effect": [0, 0, 3600, 3600],
            }),
            patch("asyncio.sleep", new_callable=AsyncMock,
                  side_effect=[None, asyncio.CancelledError]) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await quota.refresh_in_background()

        self.assertEqual(claude.call_count, 2)
        self.assertEqual(codex.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(3588), call(3593)])

    async def test_provider_failure_does_not_stop_other_provider_or_next_round(self):
        with (
            patch.object(quota, "_provider_usage", side_effect=[RuntimeError("failed"), {}, {}, {}]) as usage,
            patch("asyncio.sleep", new_callable=AsyncMock, side_effect=[None, asyncio.CancelledError]),
            self.assertLogs("bosun.quota", level="ERROR") as logs,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await quota.refresh_in_background()

        self.assertEqual(usage.call_args_list, [call("claude"), call("codex")] * 2)
        self.assertIn("claude", logs.output[0])

    async def test_backend_lifecycle_starts_and_cancels_refresh(self):
        self.assertIn(main._start_quota_refresh, main.app.router.on_startup)
        self.assertIn(main._stop_quota_refresh, main.app.router.on_shutdown)
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def refresh():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with patch.object(quota, "refresh_in_background", side_effect=refresh):
            await main._start_quota_refresh()
            await asyncio.wait_for(entered.wait(), timeout=1)
            await main._stop_quota_refresh()

        self.assertTrue(stopped.is_set())
        self.assertTrue(main.app.state.quota_refresh_task.done())


if __name__ == "__main__":
    unittest.main()
