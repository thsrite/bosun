"""多端同看一个任务时的 PTY 尺寸仲裁（WebSocket 层）。

回归的是这个现象：手机和电脑同时打开同一个任务的终端，手机端一连上/一输入，
电脑端的终端就被重排成手机宽度。
"""
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import auth
from backend.app.routers import ws as ws_router


class FakeBacklog:
    truncated = False
    data = b""


class FakeSession:
    """只实现 ws 层用到的接口；记录 PTY 收到的尺寸调用。"""

    def __init__(self):
        self.sizes: list[tuple[int, int]] = []
        self.queues: list = []

    def read_backlog(self):
        return FakeBacklog()

    def subscribe(self):
        import asyncio

        q = asyncio.Queue()
        self.queues.append(q)
        return q

    def unsubscribe(self, q):
        if q in self.queues:
            self.queues.remove(q)

    def resize(self, rows, cols):
        self.sizes.append((rows, cols))

    def redraw(self, rows, cols):
        # 真实实现是「改一次尺寸再恢复」强制 TUI 补画，最终停在 (rows, cols)
        self.sizes.append((rows, cols))

    def write(self, data):
        pass


def _size_metas(messages):
    prefix = ws_router.SIZE_META_PREFIX
    return [m[len(prefix):] for m in messages if isinstance(m, str) and m.startswith(prefix)]


class WsViewportTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(ws_router.router)
        self.session = FakeSession()
        self.enter = patch.object(auth, "is_enabled", return_value=False)
        self.enter.start()
        self.addCleanup(self.enter.stop)
        get_session = patch.object(ws_router.scheduler, "get_session", return_value=self.session)
        get_session.start()
        self.addCleanup(get_session.stop)
        self.client = TestClient(app)
        # 注册表是模块级的，逐个用例之间必须清干净
        ws_router._viewports = ws_router.ViewportRegistry()
        ws_router._task_clients = {}

    def _drain_text(self, socket, count):
        return [socket.receive_text() for _ in range(count)]

    def test_narrow_client_does_not_shrink_the_wide_one(self):
        with self.client.websocket_connect("/ws/session/1") as desktop:
            desktop.send_text("\x00resize:40,200")
            self.assertEqual(_size_metas(self._drain_text(desktop, 1)), ["40,200"])
            self.assertEqual(self.session.sizes[-1], (40, 200))

            with self.client.websocket_connect("/ws/session/1") as phone:
                phone.send_text("\x00resize:20,45")
                # 手机端拿回的是仲裁结果（电脑端的尺寸），不是它自己上报的 20,45
                self.assertEqual(_size_metas(self._drain_text(phone, 1)), ["40,200"])
                # PTY 没有被压窄
                self.assertEqual(self.session.sizes[-1], (40, 200))

                # 手机端继续输入也不会改变 PTY 尺寸
                phone.send_text("hello")
                phone.send_text("\x00resize:20,45")
                self.assertEqual(self.session.sizes[-1], (40, 200))

    def test_wide_client_leaving_shrinks_back_to_the_remaining_one(self):
        with self.client.websocket_connect("/ws/session/1") as phone:
            phone.send_text("\x00resize:20,45")
            self._drain_text(phone, 1)
            with self.client.websocket_connect("/ws/session/1") as desktop:
                desktop.send_text("\x00resize:40,200")
                self._drain_text(desktop, 1)
                # 电脑端接管后，手机端收到广播的新网格
                self.assertEqual(_size_metas(self._drain_text(phone, 1)), ["40,200"])
                self.assertEqual(self.session.sizes[-1], (40, 200))
            # 电脑端断开 → PTY 缩回手机端自己的尺寸，并广播给手机端
            self.assertEqual(_size_metas(self._drain_text(phone, 1)), ["20,45"])
            self.assertEqual(self.session.sizes[-1], (20, 45))

    def test_single_client_still_gets_its_own_size(self):
        with self.client.websocket_connect("/ws/session/1") as phone:
            phone.send_text("\x00resize:20,45")
            self.assertEqual(_size_metas(self._drain_text(phone, 1)), ["20,45"])
            self.assertEqual(self.session.sizes[-1], (20, 45))

    def test_malformed_size_frame_is_ignored_not_typed_into_the_pty(self):
        with patch.object(self.session, "write") as write:
            with self.client.websocket_connect("/ws/session/1") as sock:
                sock.send_text("\x00resize:abc,200")
                sock.send_text("\x00resize:40,200")
                self.assertEqual(_size_metas(self._drain_text(sock, 1)), ["40,200"])
        write.assert_not_called()
        self.assertEqual(self.session.sizes, [(40, 200)])

    def test_registry_is_emptied_after_everyone_leaves(self):
        with self.client.websocket_connect("/ws/session/1") as sock:
            sock.send_text("\x00resize:40,200")
            self._drain_text(sock, 1)
        self.assertIsNone(ws_router._viewports.effective(1))
        self.assertNotIn(1, ws_router._task_clients)


if __name__ == "__main__":
    unittest.main()
