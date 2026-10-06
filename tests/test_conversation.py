import asyncio
import importlib
import json
import sys
import threading
import types
import unittest
from unittest.mock import patch

from test_feishubot_routing import PLUGIN_MODULE, FeishuBot, _FakeFeishu

SESSION = FeishuBot._session_key("chat", "user")
pkg = PLUGIN_MODULE.__name__
Executor = importlib.import_module(pkg + ".ai.executor").ToolExecutor
Engine = importlib.import_module(pkg + ".ai.engine").ChatEngine
History = importlib.import_module(pkg + ".ai.history").ChatHistory
Transport = importlib.import_module(pkg + ".transport").WebSocketTransport
Runtime = importlib.import_module(pkg + ".mixins.runtime")
LLM = importlib.import_module(pkg + ".ai.llm")


def context(title):
    return types.SimpleNamespace(
        torrent_info=types.SimpleNamespace(title=title, size=1024**3, site_name="test"),
        media_info=True,
    )


def message(text, ident, chat="chat", user="user", **extra):
    return {"message": dict(content=json.dumps({"text": text}), message_type="text",
                            message_id=ident, chat_id=chat, **extra),
            "sender": {"sender_id": {"open_id": user}}}


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.downloaded = []
        self.results = [context("A 4K")]
        media = types.ModuleType("app.chain.media")
        media.MediaChain = lambda: types.SimpleNamespace(search=lambda **kw: (None, []))
        search = types.ModuleType("app.chain.search")
        search.SearchChain = lambda: types.SimpleNamespace(search_by_title=lambda **kw: self.results)
        download = types.ModuleType("app.chain.download")
        download.DownloadChain = lambda: types.SimpleNamespace(
            download_single=lambda **kw: self.downloaded.append(kw["context"]) or True)
        self.modules = patch.dict(sys.modules, {"app.chain": types.ModuleType("app.chain"),
                                               "app.chain.media": media, "app.chain.search": search,
                                               "app.chain.download": download})
        self.modules.start()
        self.bot = FeishuBot()
        self.bot.init_plugin({"enabled": True, "use_ws": False, "llm_enabled": True,
                              "openrouter_key": "test-key", "openrouter_model": "vendor/model"})
        self.bot._feishu = _FakeFeishu()

    def tearDown(self):
        self.bot.stop_service()
        self.modules.stop()

    def executor(self):
        return Executor(self.bot._get_session_state(SESSION))

    def test_research_cannot_confirm_a_different_movie(self):
        executor = self.executor()
        executor.execute("search_resources", {"keyword": "A"})
        executor.execute("download_resource", {"index": 0, "confirmed": False})
        self.results = [context("B")]
        executor.execute("search_resources", {"keyword": "B"})
        result = executor.execute("download_resource", {"index": -1, "confirmed": True})
        self.assertFalse(result.success)
        self.assertEqual(self.downloaded, [])

    def test_empty_search_clears_old_results_and_pending(self):
        executor = self.executor()
        executor.execute("search_resources", {"keyword": "A"})
        executor.execute("download_resource", {"index": 0, "confirmed": False})
        self.results = []
        executor.execute("search_resources", {"keyword": "B"})
        self.assertEqual(executor.state.resource_cache, [])
        self.assertIsNone(executor.state.pending_download)

    def test_failed_search_clears_old_results(self):
        executor = self.executor()
        executor.state.replace_resources(self.results)
        broken = types.SimpleNamespace(search_by_title=lambda **kw: (_ for _ in ()).throw(RuntimeError("unavailable")))
        with patch.object(sys.modules["app.chain.search"], "SearchChain", return_value=broken):
            self.assertFalse(executor.execute("search_resources", {"keyword": "B"}).success)
        self.assertEqual(executor.state.resource_cache, [])

    def test_confirm_requires_an_existing_preview(self):
        executor = self.executor()
        executor.state.replace_resources(self.results)
        self.assertFalse(executor.execute("download_resource", {"index": 0, "confirmed": True}).success)
        self.assertEqual(self.downloaded, [])

    def test_preview_cannot_be_confirmed_by_the_same_model_turn(self):
        executor = self.executor()
        executor.state.replace_resources(self.results)
        executor.begin_turn()
        executor.execute("download_resource", {"index": 0, "confirmed": False})
        self.assertFalse(executor.execute("download_resource", {"index": -1, "confirmed": True}).success)
        self.assertEqual(self.downloaded, [])
        executor.begin_turn()
        self.assertTrue(executor.execute("download_resource", {"index": -1, "confirmed": True}).success)

    def test_confirm_rejects_a_different_selection(self):
        executor = self.executor()
        executor.state.replace_resources([context("A"), context("B")])
        executor.execute("download_resource", {"index": 0, "confirmed": False})
        self.assertFalse(executor.execute("download_resource", {"index": 1, "confirmed": True}).success)
        self.assertEqual(self.downloaded, [])

    def test_pending_uses_exact_resource_not_mutable_list_index(self):
        executor = self.executor()
        original = context("A")
        executor.state.replace_resources([original])
        executor.execute("download_resource", {"index": 0, "confirmed": False})
        executor.state.resource_cache[0] = context("B")
        self.assertTrue(executor.execute("download_resource", {"index": -1, "confirmed": True}).success)
        self.assertIs(self.downloaded[0], original)

    def test_old_card_cannot_confirm_a_new_preview(self):
        executor = self.executor()
        executor.state.replace_resources(self.results)
        first = executor.execute("download_resource", {"index": 0, "confirmed": False}).data
        second = executor.execute("download_resource", {"index": 0, "confirmed": False}).data
        self.bot._card_download_confirmed(0, "user", "chat", first["selection_id"])
        self.assertEqual(self.downloaded, [])
        self.bot._card_download_confirmed(0, "user", "chat", second["selection_id"])
        self.bot._card_download_confirmed(0, "user", "chat", second["selection_id"])
        self.assertEqual(len(self.downloaded), 1)

    def test_old_resource_card_cannot_select_from_new_list(self):
        state = self.bot._get_session_state(SESSION)
        state.replace_resources([context("A")])
        old_id = state.resource_search_id
        state.replace_resources([context("B")])
        self.bot._card_download(0, "user", "chat", old_id)
        self.assertIsNone(state.pending_download)

    def test_cancel_card_clears_only_its_own_selection_and_records_history(self):
        state = self.bot._get_session_state(SESSION)
        state.replace_resources(self.results)
        selection = state.select_resource(0)
        self.bot._card_cancel("user", "chat", "stale-id")
        self.assertIsNotNone(state.pending_download)
        self.bot._card_cancel("user", "chat", selection["selection_id"])
        self.assertIsNone(state.pending_download)
        engine = self.bot._get_or_create_engine(SESSION)
        self.assertEqual(engine.history.to_api_messages()[-2]["content"], "取消下载")

    def test_card_selection_and_text_confirmation_share_history(self):
        state = self.bot._get_session_state(SESSION)
        state.replace_resources(self.results)
        self.bot._card_download(0, "user", "chat", state.resource_search_id)
        engine = self.bot._get_or_create_engine(SESSION)
        self.assertIn("A 4K", engine.history.to_api_messages()[-1]["content"])
        self.bot._try_handle_direct_pending_action("确认", engine, "chat", "confirm", SESSION)
        self.assertEqual(len(self.downloaded), 1)
        self.assertIn("已添加下载", engine.history.to_api_messages()[-1]["content"])

    def test_two_users_and_two_chats_have_independent_history_and_state(self):
        a = self.bot._get_or_create_engine("chat::alice")
        b = self.bot._get_or_create_engine("chat::bob")
        c = self.bot._get_or_create_engine("other::alice")
        a.history.append({"role": "user", "content": "A"})
        a.state.replace_resources(self.results)
        self.assertEqual(b.history.count, 0)
        self.assertEqual(c.history.count, 0)
        self.assertEqual(b.state.resource_cache, [])
        self.assertIs(a, self.bot._get_or_create_engine("chat::alice"))

    def test_continuous_messages_and_quote_replies_are_processed_fifo(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def handle(event):
            text = self.bot._extract_message_text(event["message"])
            if text == "下载A":
                entered.set()
                self.assertTrue(release.wait(2))
            calls.append(text)
            if len(calls) == 3:
                done.set()
        self.bot._handle_message = handle
        self.assertTrue(self.bot._enqueue_message(message("下载A", "1")))
        self.assertTrue(entered.wait(2))
        self.assertTrue(self.bot._enqueue_message(message("4K 5.1声道", "2", root_id="thread-root")))
        self.assertTrue(self.bot._enqueue_message(message("下载1号", "3", parent_id="quoted-message")))
        self.assertFalse(self.bot._enqueue_message(message("下载1号", "3")))
        release.set()
        self.assertTrue(done.wait(2))
        self.assertEqual(calls, ["下载A", "4K 5.1声道", "下载1号"])

    def test_download_conversation_keeps_preferences_selection_and_confirmation(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        engine = self.bot._get_or_create_engine(SESSION)
        def chat(messages, **kwargs):
            last = messages[-1]
            if last["role"] == "user" and last["content"] == "下载A":
                entered.set()
                self.assertTrue(release.wait(2))
                return {"choices": [{"message": {"tool_calls": [{"id": "search", "function": {
                    "name": "search_resources", "arguments": json.dumps({"keyword": "A"})}}]}}]}
            return {"choices": [{"message": {"content": "找到 A 4K，请选择资源。"}}]}
        engine.llm.chat = chat
        self.bot._enqueue_message(message("下载A", "1"))
        self.assertTrue(entered.wait(2))
        self.bot._enqueue_message(message("需要4K 5.1声道", "2", parent_id="quote"))
        self.bot._enqueue_message(message("下载1号", "3", root_id="thread"))
        self.bot._enqueue_message(message("确认", "4"))
        self.bot._enqueue_task(done.set)
        release.set()
        self.assertTrue(done.wait(2))
        users = [msg["content"] for msg in engine.history.to_api_messages() if msg["role"] == "user"]
        self.assertEqual(users, ["下载A", "需要4K 5.1声道", "下载1号", "确认"])
        self.assertEqual([item.torrent_info.title for item in self.downloaded], ["A 4K"])
        self.assertIsNone(engine.state.pending_download)

    def test_clear_affects_only_the_current_session(self):
        engine = self.bot._get_or_create_engine(SESSION)
        other = self.bot._get_or_create_engine("other-session")
        engine.history.append({"role": "user", "content": "A"})
        other.history.append({"role": "user", "content": "B"})
        self.bot._handle_system_text_command("重新开始", engine, "chat", "clear")
        self.assertEqual(engine.history.count, 0)
        self.assertEqual(other.history.count, 1)

    def test_legacy_clear_also_clears_the_current_selection(self):
        state = self.bot._get_session_state(SESSION)
        state.replace_resources(self.results)
        state.select_resource(0)
        self.bot._handle_system_text_command("重新开始", None, "chat", "clear", SESSION)
        self.assertEqual(state.resource_cache, [])
        self.assertIsNone(state.pending_download)

    def test_downloading_tool_uses_current_moviepilot_method(self):
        download = types.SimpleNamespace(downloading=lambda: [types.SimpleNamespace(title="A", progress=50)])
        with patch.object(sys.modules["app.chain.download"], "DownloadChain", return_value=download):
            result = self.executor().execute("get_downloading", {})
        self.assertTrue(result.success)
        self.assertEqual(result.data["tasks"][0]["title"], "A")

    def test_stopped_instance_cannot_restore_or_receive_messages(self):
        self.bot.stop_service()
        self.assertFalse(self.bot._ensure_runtime_ready())
        self.assertFalse(self.bot._enqueue_message(message("确认", "late")))
        self.assertIsNone(self.bot._feishu)

    def test_reinitialization_invalidates_old_generation(self):
        old = self.bot._generation
        self.bot.init_plugin({"enabled": True, "use_ws": False})
        self.assertFalse(self.bot._is_dispatch_active(old))

    def test_explicit_model_migration_discards_fallback_pool(self):
        captured = []
        self.bot.update_config = captured.append
        self.bot.init_plugin({"use_ws": False, "openrouter_free_model": "previous/model",
                              "openrouter_fallback_models": ["other"], "openrouter_auto_fallback": True})
        self.assertEqual(self.bot._openrouter_model, "previous/model")
        self.assertEqual(captured[0]["openrouter_model"], "previous/model")
        self.assertNotIn("openrouter_free_model", captured[0])
        self.assertNotIn("openrouter_fallback_models", captured[0])


class HistoryAndModelTests(unittest.TestCase):
    def test_tool_round_is_appended_atomically_before_trimming(self):
        engine = Engine("test-key", "vendor/model")
        for i in range(14):
            engine.history.extend([{"role": "user", "content": str(i)},
                                   {"role": "assistant", "content": "ok"}])
        responses = iter([
            {"choices": [{"message": {"tool_calls": [
                {"id": "tool-id", "function": {"name": "get_downloading", "arguments": "{}"}}]}}]},
            {"choices": [{"message": {"content": "done"}}]},
        ])
        snapshots = []
        def chat(**kw):
            snapshots.append(kw["messages"])
            return next(responses)
        engine.llm.chat = chat
        engine.executor.execute = lambda *args: importlib.import_module(pkg + ".ai.types").ToolResult(success=True, data={"tasks": []})
        self.assertEqual(engine.chat("状态"), "done")
        for snapshot in snapshots:
            previous = None
            for msg in snapshot:
                if msg["role"] == "tool":
                    self.assertIsNotNone(previous)
                    self.assertEqual(previous["role"], "assistant")
                    self.assertEqual(previous["tool_calls"][0]["id"], msg["tool_call_id"])
                previous = msg

    def test_a_long_current_turn_keeps_all_its_tool_results(self):
        history = History("system")
        history.append({"role": "user", "content": "request"})
        for i in range(20):
            history.extend([{"role": "assistant", "tool_calls": [{"id": str(i)}]},
                            {"role": "tool", "tool_call_id": str(i), "content": "ok"}])
        self.assertEqual(history.to_api_messages()[1]["role"], "user")
        self.assertEqual(history.count, 41)
        history.append({"role": "user", "content": "next"})
        self.assertEqual(history.count, 1)

    def test_cancelled_engine_cannot_run_a_late_tool(self):
        engine = Engine("test-key", "vendor/model")
        def chat(**kw):
            engine.cancel()
            return {"choices": [{"message": {"tool_calls": [{"id": "id", "function": {"name": "download_resource", "arguments": "{}"}}]}}]}
        engine.llm.chat = chat
        with patch.object(engine.executor, "execute") as execute:
            engine.chat("request")
            execute.assert_not_called()

    def test_model_request_uses_only_configured_model(self):
        client = LLM.LLMClient("key", "vendor/model")
        response = types.SimpleNamespace(raise_for_status=lambda: None,
                                         json=lambda: {"model": "vendor/model", "choices": []})
        with patch.object(LLM.requests, "post", return_value=response) as post:
            client.chat([{"role": "user", "content": "hello"}])
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs["json"]["model"], "vendor/model")

    def test_empty_model_does_not_make_a_network_request(self):
        with patch.object(LLM.requests, "post") as post:
            with self.assertRaises(RuntimeError):
                LLM.LLMClient("key").chat([])
            post.assert_not_called()


class TransportTests(unittest.TestCase):
    def create_transport(self):
        sdk = types.SimpleNamespace(loop=None)
        entered, closed = threading.Event(), threading.Event()
        class Client:
            _conn = None
            _auto_reconnect = True
            def start(self):
                async def serve():
                    self._conn = object()
                    entered.set()
                    await asyncio.Event().wait()
                sdk.loop.run_until_complete(serve())
            async def _disconnect(self):
                self._conn = None
                closed.set()
        return Transport(Client, sdk), entered, closed

    def test_stop_closes_connection_loop_and_thread(self):
        transport, entered, closed = self.create_transport()
        try:
            transport.start()
            self.assertTrue(entered.wait(2))
            loop = transport.loop
            self.assertTrue(transport.connected)
            transport.stop()
            self.assertTrue(closed.is_set())
            self.assertFalse(transport.thread.is_alive())
            self.assertTrue(loop.is_closed())
            self.assertFalse(transport.connected)
        finally:
            transport.stop()

    def test_new_transport_retires_old_transport_before_connecting(self):
        old, old_entered, old_closed = self.create_transport()
        new, new_entered, _ = self.create_transport()
        try:
            old.start()
            self.assertTrue(old_entered.wait(2))
            new.start()
            self.assertTrue(new_entered.wait(2))
            self.assertTrue(old_closed.is_set())
            self.assertFalse(old.thread.is_alive())
        finally:
            old.stop()
            new.stop()
