from __future__ import annotations

import asyncio
import http.server
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.browser_computer import (
    BrowserPolicyError,
    BrowserSession,
    execute_action,
    install_loopback_routes,
    remove_browser_assets,
    risky_action_reason,
    validate_loopback_url,
    validate_loopback_websocket_url,
)


class ValidateLoopbackUrlTests(unittest.TestCase):
    def test_accepts_http_loopback_hosts(self) -> None:
        with patch("app.browser_computer.socket.getaddrinfo") as getaddrinfo:
            getaddrinfo.side_effect = lambda host, *_args, **_kwargs: [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))
            ] if host == "::1" else [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
            ]

            self.assertEqual(validate_loopback_url("http://localhost:5199/app"), "http://localhost:5199/app")
            self.assertEqual(validate_loopback_url("https://127.0.0.1/test"), "https://127.0.0.1/test")
            self.assertEqual(validate_loopback_url("http://[::1]:8770/"), "http://[::1]:8770/")

    def test_rejects_non_loopback_and_non_http_urls(self) -> None:
        with patch("app.browser_computer.socket.getaddrinfo") as getaddrinfo:
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
            ]

            for url in (
                "https://example.com",
                "http://192.168.1.20",
                "file:///etc/passwd",
                "javascript:alert(1)",
                "http://user:pass@localhost:8770",
            ):
                with self.subTest(url=url), self.assertRaises(BrowserPolicyError):
                    validate_loopback_url(url)

    def test_rejects_dns_rebinding_to_non_loopback(self) -> None:
        with patch("app.browser_computer.socket.getaddrinfo") as getaddrinfo:
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ]
            with self.assertRaises(BrowserPolicyError):
                validate_loopback_url("http://localhost:8770")

    def test_websocket_policy_accepts_only_loopback_ws_schemes(self) -> None:
        with patch("app.browser_computer.socket.getaddrinfo") as getaddrinfo:
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
            ]
            self.assertEqual(
                validate_loopback_websocket_url("ws://localhost:8770/events"),
                "ws://localhost:8770/events",
            )
            with self.assertRaises(BrowserPolicyError):
                validate_loopback_websocket_url("wss://example.com/events")
            with self.assertRaises(BrowserPolicyError):
                validate_loopback_websocket_url("http://localhost:8770/events")


class NetworkPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_installs_http_and_websocket_guards(self) -> None:
        context = AsyncMock()
        await install_loopback_routes(context)

        context.route.assert_awaited_once()
        context.route_web_socket.assert_awaited_once()
        http_guard = context.route.await_args.args[1]
        websocket_guard = context.route_web_socket.await_args.args[1]

        blocked_http = AsyncMock()
        blocked_http.request.url = "https://example.com/data"
        await http_guard(blocked_http)
        blocked_http.abort.assert_awaited_once_with("blockedbyclient")

        blocked_websocket = AsyncMock()
        blocked_websocket.url = "wss://example.com/events"
        await websocket_guard(blocked_websocket)
        blocked_websocket.close.assert_awaited_once_with(code=1008, reason="Browser loopback policy")

        allowed_websocket = AsyncMock()
        allowed_websocket.url = "ws://127.0.0.1:8770/events"
        allowed_websocket.connect_to_server = Mock()
        await websocket_guard(allowed_websocket)
        allowed_websocket.connect_to_server.assert_called_once_with()


class ExecuteActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.page = AsyncMock()
        self.page.mouse = AsyncMock()
        self.page.keyboard = AsyncMock()

    async def test_executes_allowed_pointer_keyboard_and_scroll_actions(self) -> None:
        await execute_action(self.page, {"type": "click", "x": 12, "y": 34, "button": "left"})
        self.page.mouse.click.assert_awaited_once_with(12, 34, button="left")

        await execute_action(self.page, {"type": "type", "text": "hello"})
        self.page.keyboard.type.assert_awaited_once_with("hello")

        await execute_action(self.page, {"type": "keypress", "keys": ["CTRL", "L"]})
        self.page.keyboard.press.assert_awaited_once_with("Control+L")

        await execute_action(self.page, {"type": "scroll", "x": 10, "y": 20, "scroll_x": 0, "scroll_y": 420})
        self.page.mouse.move.assert_awaited_once_with(10, 20)
        self.page.mouse.wheel.assert_awaited_once_with(0, 420)

    async def test_rejects_unknown_or_file_actions(self) -> None:
        for action in (
            {"type": "upload_file", "path": "/tmp/a"},
            {"type": "download"},
            {"type": "clipboard_read"},
            {"type": "launch_app"},
        ):
            with self.subTest(action=action), self.assertRaises(BrowserPolicyError):
                await execute_action(self.page, action)

    async def test_wait_is_bounded(self) -> None:
        with patch("app.browser_computer.asyncio.sleep", new=AsyncMock()) as sleep:
            await execute_action(self.page, {"type": "wait"})
            sleep.assert_awaited_once_with(2)

            with self.assertRaises(BrowserPolicyError):
                await execute_action(self.page, {"type": "wait", "seconds": 30})

    async def test_flags_destructive_click_and_sensitive_typing(self) -> None:
        self.page.evaluate = AsyncMock(return_value={"label": "删除账户", "isSubmit": False})
        reason = await risky_action_reason(self.page, {"type": "click", "x": 10, "y": 10})
        self.assertIn("删除账户", reason or "")

        self.page.evaluate = AsyncMock(return_value="password current-password")
        reason = await risky_action_reason(self.page, {"type": "type", "text": "hidden"})
        self.assertEqual(reason, "即将向敏感输入框填写内容")

        self.page.evaluate = AsyncMock(return_value={"label": "查看详情", "isSubmit": False})
        self.assertIsNone(await risky_action_reason(self.page, {"type": "click", "x": 10, "y": 10}))

    async def test_flags_form_submission_and_enter_keypress(self) -> None:
        self.page.evaluate = AsyncMock(return_value={"label": "继续", "isSubmit": True})
        self.assertEqual(
            await risky_action_reason(self.page, {"type": "click", "x": 10, "y": 10}),
            "即将提交表单：继续",
        )
        self.assertEqual(
            await risky_action_reason(self.page, {"type": "keypress", "keys": ["ENTER"]}),
            "即将按 Enter，可能提交当前表单",
        )


class BrowserAssetTests(unittest.TestCase):
    def test_remove_browser_assets_deletes_only_the_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir, patch(
            "app.browser_computer.DATA_DIR", Path(data_dir)
        ):
            target = Path(data_dir) / "browser-runs/42"
            sibling = Path(data_dir) / "browser-runs/43"
            target.mkdir(parents=True)
            sibling.mkdir(parents=True)
            (target / "step-0001.png").write_bytes(b"target")
            (sibling / "step-0001.png").write_bytes(b"sibling")

            remove_browser_assets(42)

            self.assertFalse(target.exists())
            self.assertTrue(sibling.is_dir())

    def test_scheduler_deletion_cleans_browser_assets(self) -> None:
        from app import scheduler

        with patch.object(
            scheduler.db,
            "query_one",
            return_value={"engine": "browser", "log_path": None, "status": "done"},
        ), patch.object(scheduler.db, "execute"), patch.object(
            scheduler.browser_computer, "remove_browser_assets"
        ) as remove_assets, patch.object(scheduler.events, "emit"), patch.object(
            scheduler, "_cancel_active_children"
        ), patch.object(scheduler, "tick"):
            scheduler.delete(42)

        remove_assets.assert_called_once_with(42)

    def test_project_deletion_cleans_only_browser_task_assets(self) -> None:
        from app import scheduler

        rows = [
            {"id": 42, "engine": "browser", "log_path": None},
            {"id": 43, "engine": "codex", "log_path": None},
        ]
        with patch.object(scheduler.db, "query", return_value=rows), patch.object(
            scheduler.db, "execute"
        ), patch.object(
            scheduler.browser_computer, "remove_browser_assets"
        ) as remove_assets, patch.object(scheduler.events, "emit"), patch.object(scheduler, "tick"):
            scheduler.delete_project_tasks(7)

        remove_assets.assert_called_once_with(42)


class BrowserSessionLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_liveness_contract_tracks_termination(self) -> None:
        session = BrowserSession(
            task_id=1,
            prompt="打开 http://localhost:8770",
            log_path="/tmp/browser-session-contract.log",
            loop=asyncio.get_running_loop(),
            on_status=lambda *_args: None,
            on_exit=lambda *_args: None,
            on_tokens=lambda *_args: None,
            on_permission=lambda *_args: None,
        )
        self.assertTrue(session.is_alive())
        session.terminate()
        self.assertFalse(session.is_alive())

    async def test_runs_computer_loop_and_persists_screenshot(self) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = (
                    b"<!doctype html><button style='position:fixed;inset:0' "
                    b"onclick=\"document.body.dataset.clicked='yes'\">Click</button>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]

        computer_call = SimpleNamespace(
            type="computer_call",
            call_id="call-1",
            actions=[SimpleNamespace(type="click", x=20, y=20, button="left", keys=[])],
            pending_safety_checks=[
                SimpleNamespace(id="safe-1", code="state_change", message="The click may change state")
            ],
        )
        final_message = SimpleNamespace(
            type="message",
            content=[SimpleNamespace(text="页面点击验收通过")],
        )
        first = SimpleNamespace(
            id="resp-1",
            output=[computer_call],
            usage=SimpleNamespace(total_tokens=18),
        )
        second = SimpleNamespace(
            id="resp-2",
            output=[final_message],
            usage=SimpleNamespace(total_tokens=42),
        )
        create = AsyncMock(side_effect=[first, second])
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        events: list[dict] = []
        token_updates: list[tuple[int, int]] = []

        with tempfile.TemporaryDirectory() as data_dir, patch.dict(
            "os.environ", {"BOSUN_OPENAI_API_KEY": "test-key"}, clear=False
        ), patch("app.browser_computer.DATA_DIR", Path(data_dir)), patch(
            "app.browser_computer.risky_action_reason", new=AsyncMock(return_value="本地风险")
        ):
            session = BrowserSession(
                task_id=999,
                prompt=f"点击按钮并确认结果 http://127.0.0.1:{port}",
                log_path=str(Path(data_dir) / "task.log"),
                loop=asyncio.get_running_loop(),
                on_status=lambda *_args: None,
                on_exit=lambda *_args: None,
                on_tokens=lambda task_id, tokens: token_updates.append((task_id, tokens)),
                on_permission=lambda *_args: None,
                client_factory=lambda _key: client,
                availability_fn=lambda: {"available": True, "missing": [], "model": "gpt-5.6"},
            )
            session._event = events.append  # type: ignore[method-assign]
            session._ask_permission = AsyncMock(return_value=True)  # type: ignore[method-assign]
            try:
                await session._run()
            finally:
                await session._close_browser()

            self.assertTrue((Path(data_dir) / "browser-runs/999/step-0001.png").is_file())
            self.assertIn("screenshot", [event["t"] for event in events])
            self.assertIn("result", [event["t"] for event in events])
            self.assertEqual(create.await_count, 2)
            followup = create.await_args_list[1].kwargs["input"][0]
            self.assertEqual(followup["type"], "computer_call_output")
            self.assertEqual(followup["call_id"], "call-1")
            self.assertEqual(followup["acknowledged_safety_checks"][0]["id"], "safe-1")
            self.assertEqual(session._ask_permission.await_count, 2)  # type: ignore[attr-defined]
            self.assertEqual(token_updates, [(999, 60)])
            self.assertEqual(next(event for event in events if event["t"] == "result")["tokens"], 60)

        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    unittest.main()
