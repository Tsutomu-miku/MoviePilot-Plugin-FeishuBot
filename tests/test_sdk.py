"""Optional integration checks using the official SDK, with all network I/O replaced."""

import asyncio
import json
import threading
import unittest

from test_conversation import FeishuBot, Runtime, Transport, context, message
from test_feishubot_routing import _FakeFeishu


@unittest.skipUnless(Runtime._HAS_LARK_SDK, "lark-oapi not installed")
class NativeSDKTests(unittest.TestCase):
    def setUp(self):
        self.bot = FeishuBot()
        self.bot.init_plugin({"enabled": True, "use_ws": False})
        self.bot._feishu = _FakeFeishu()

    def tearDown(self):
        self.bot.stop_service()

    def test_native_message_callback_enqueues_and_deduplicates(self):
        received, done = [], threading.Event()
        def handle(event):
            received.append(self.bot._extract_message_text(event["message"]))
            done.set()
        self.bot._handle_message = handle
        handler = self.bot._build_event_handler()
        payload = {"schema": "2.0", "header": {"event_id": "event", "event_type": "im.message.receive_v1"},
                   "event": message("下载A", "native-message")}
        handler.do_without_validation(json.dumps(payload).encode())
        handler.do_without_validation(json.dumps(payload).encode())
        self.assertTrue(done.wait(2))
        self.assertEqual(received, ["下载A"])

    def test_native_card_callback_can_cancel_the_current_selection(self):
        state = self.bot._get_session_state(self.bot._session_key("chat", "user"))
        state.replace_resources([context("A")])
        selected = state.select_resource(0)
        handler = self.bot._build_event_handler()
        done = threading.Event()
        payload = {"schema": "2.0", "header": {"event_id": "card", "event_type": "card.action.trigger"},
                   "event": {"operator": {"open_id": "user"}, "context": {"open_chat_id": "chat"},
                             "action": {"value": {"action": "cancel_download", "selection_id": selected["selection_id"]}}}}
        response = handler.do_without_validation(json.dumps(payload).encode())
        self.bot._enqueue_task(done.set)
        self.assertTrue(done.wait(2))
        self.assertIsNone(state.pending_download)
        self.assertEqual(type(response).__name__, "P2CardActionTriggerResponse")

    def test_native_client_start_is_cancelled_and_disconnected(self):
        import lark_oapi.ws.client as sdk
        entered, closed = threading.Event(), threading.Event()
        class Connection:
            async def close(self):
                closed.set()
        class Client(sdk.Client):
            async def _connect(self):
                self._conn = Connection()
                entered.set()
            async def _ping_loop(self):
                await asyncio.Event().wait()
        transport = Transport(lambda: Client("test-app", "test-secret"), sdk)
        try:
            transport.start()
            self.assertTrue(entered.wait(2))
            transport.stop()
            self.assertTrue(closed.is_set())
            self.assertFalse(transport.thread.is_alive())
        finally:
            transport.stop()

