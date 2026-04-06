import importlib.util
import importlib
import sys
import threading
import types
import unittest
from pathlib import Path


def _install_app_stubs():
    app_module = types.ModuleType("app")
    core_module = types.ModuleType("app.core")
    config_module = types.ModuleType("app.core.config")
    config_module.settings = types.SimpleNamespace()

    event_module = types.ModuleType("app.core.event")

    class _EventManager:
        @staticmethod
        def register(_event_type):
            def decorator(func):
                return func
            return decorator

    class _Event:
        def __init__(self, event_data=None):
            self.event_data = event_data or {}

    event_module.eventmanager = _EventManager()
    event_module.Event = _Event

    log_module = types.ModuleType("app.log")
    log_module.logger = types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )

    plugins_module = types.ModuleType("app.plugins")

    class _PluginBase:
        pass

    plugins_module._PluginBase = _PluginBase

    schemas_module = types.ModuleType("app.schemas")
    schemas_module.MediaType = types.SimpleNamespace(MOVIE="movie", TV="tv")

    schemas_types_module = types.ModuleType("app.schemas.types")
    schemas_types_module.EventType = types.SimpleNamespace(
        TransferComplete="TransferComplete",
        DownloadAdded="DownloadAdded",
        SubscribeAdded="SubscribeAdded",
    )
    schemas_types_module.MediaType = schemas_module.MediaType

    sys.modules["app"] = app_module
    sys.modules["app.core"] = core_module
    sys.modules["app.core.config"] = config_module
    sys.modules["app.core.event"] = event_module
    sys.modules["app.log"] = log_module
    sys.modules["app.plugins"] = plugins_module
    sys.modules["app.schemas"] = schemas_module
    sys.modules["app.schemas.types"] = schemas_types_module


def _load_feishubot_module():
    _install_app_stubs()
    repo_root = Path(__file__).resolve().parents[1]
    plugin_dir = repo_root / "plugins.v2" / "feishubot"
    spec = importlib.util.spec_from_file_location(
        "feishubot_under_test",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PLUGIN_MODULE = _load_feishubot_module()
STATE_MODULE = importlib.import_module(f"{PLUGIN_MODULE.__name__}.state")
LLM_MODULE = importlib.import_module(f"{PLUGIN_MODULE.__name__}.ai.llm")
FEISHU_API_MODULE = importlib.import_module(f"{PLUGIN_MODULE.__name__}.feishu_api")
FeishuBot = PLUGIN_MODULE.FeishuBot


class _FakeHistory:
    def __init__(self):
        self.messages = []

    def append(self, message):
        self.messages.append(message)


class _FakeExecutor:
    def __init__(self, state):
        self.state = state

    def execute(self, fn_name, fn_args):
        if fn_name != "download_resource":
            raise AssertionError(f"unexpected tool call: {fn_name}")

        index = fn_args["index"]
        self.state.pending_download = {
            "index": index,
            "title": f"资源 {index + 1}",
            "site": "测试站点",
            "size": "1 GB",
        }
        return types.SimpleNamespace(
            success=True,
            data={
                "status": "pending_confirmation",
                "index": index,
                "title": f"资源 {index + 1}",
                "site": "测试站点",
                "size": "1 GB",
                "tags": {"resolution": "4K"},
                "message": f"资源 {index + 1} 等待确认下载。",
            },
            error=None,
        )


class _FakeEngine:
    def __init__(self, resource_count=12):
        self.state = types.SimpleNamespace(
            resource_cache=[object() for _ in range(resource_count)],
            search_cache=[],
            pending_download=None,
        )
        self.history = _FakeHistory()
        self.executor = _FakeExecutor(self.state)


class _FakeFeishu:
    def __init__(self):
        self.calls = []
        self.updated = []

    def send_card(self, chat_id, card, reply_msg_id=None):
        self.calls.append(
            {"chat_id": chat_id, "card": card, "reply_msg_id": reply_msg_id}
        )
        return {"data": {"message_id": "status_msg"}}

    def update_card(self, message_id, card):
        self.updated.append({"message_id": message_id, "card": card})


class FeishuBotRoutingTests(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("app.chain", None)
        sys.modules.pop("app.chain.download", None)
        sys.modules.pop("app.chain.search", None)

    def test_single_session_key_is_constant(self):
        self.assertEqual(
            FeishuBot._session_key("chat_a", "user_a"),
            FeishuBot._session_key("chat_b", "user_b"),
        )

    def test_record_message_once_rejects_duplicates(self):
        bot = FeishuBot()
        bot._seen_msg_ids = {}
        bot._seen_msg_ids_lock = threading.Lock()

        self.assertTrue(bot._record_message_once("msg-1"))
        self.assertFalse(bot._record_message_once("msg-1"))

    def test_dispatch_serial_task_respects_busy_gate(self):
        bot = FeishuBot()
        calls = []

        bot._try_acquire_global_processing = lambda session_key, token: False
        bot._release_global_processing = lambda session_key, token: calls.append(("release", session_key, token))

        dispatched = bot._dispatch_serial_task(
            "single_user",
            "token",
            "demo_task",
            lambda: calls.append(("run",)),
        )

        self.assertFalse(dispatched)
        self.assertEqual(calls, [])

    def test_extract_message_text_strips_mentions(self):
        msg = {
            "content": '{"text":"@_user_1 下载11号"}',
            "mentions": [{"key": "@_user_1"}],
        }
        self.assertEqual(FeishuBot._extract_message_text(msg), "下载11号")

    def test_state_helpers_sync_cache_counts(self):
        state = PLUGIN_MODULE.ChatState()
        synced = STATE_MODULE.sync_state_cache(
            state,
            search_cache=[1, 2],
            resource_cache=[1],
        )
        self.assertIs(synced, state)
        self.assertEqual(STATE_MODULE.cache_counts(state), (2, 1))

    def test_parse_cached_index_command_supports_download_and_subscribe(self):
        self.assertEqual(
            FeishuBot._parse_cached_index_command("下载11号"),
            ("download", 10),
        )
        self.assertEqual(
            FeishuBot._parse_cached_index_command("订阅2"),
            ("subscribe", 1),
        )
        self.assertEqual(
            FeishuBot._parse_cached_index_command("下载0号"),
            ("download", None),
        )

    def test_cached_download_reply_uses_same_state_machine(self):
        bot = FeishuBot()
        bot._feishu = _FakeFeishu()
        engine = _FakeEngine()

        handled = bot._try_handle_cached_index_action(
            "下载11号",
            engine,
            chat_id="chat-id",
            msg_id="msg-id",
            session_key="ignored",
        )

        self.assertTrue(handled)
        self.assertEqual(engine.state.pending_download["index"], 10)
        self.assertEqual(bot._feishu.calls[-1]["reply_msg_id"], "msg-id")
        self.assertEqual(
            bot._feishu.calls[-1]["card"]["header"]["title"]["content"],
            "⬇️ 下载确认",
        )
        self.assertEqual(engine.history.messages[0]["content"], "下载11号")

    def test_cached_download_without_resource_cache_returns_error_card(self):
        bot = FeishuBot()
        bot._feishu = _FakeFeishu()
        engine = _FakeEngine(resource_count=0)

        handled = bot._try_handle_cached_index_action(
            "下载1号",
            engine,
            chat_id="chat-id",
            msg_id="msg-id",
            session_key="ignored",
        )

        self.assertTrue(handled)
        self.assertEqual(
            bot._feishu.calls[-1]["card"]["header"]["title"]["content"],
            "⚠️ 出错了",
        )
        self.assertIn("当前没有可用的资源列表上下文", engine.history.messages[1]["content"])

    def test_legacy_search_populates_shared_engine_cache(self):
        class _SearchChain:
            @staticmethod
            def search_medias(title=""):
                return [
                    types.SimpleNamespace(
                        title=f"{title} A",
                        year="2024",
                        type=types.SimpleNamespace(value="movie"),
                        vote_average=8.5,
                        overview="overview",
                    )
                ]

            @staticmethod
            def search_torrents(title=""):
                torrent = types.SimpleNamespace(
                    title=f"{title} 4K",
                    site_name="站点",
                    size=1024**3,
                    seeders=88,
                )
                return [types.SimpleNamespace(torrent_info=torrent)]

        chain_module = types.ModuleType("app.chain")
        search_module = types.ModuleType("app.chain.search")
        search_module.SearchChain = _SearchChain
        sys.modules["app.chain"] = chain_module
        sys.modules["app.chain.search"] = search_module

        bot = FeishuBot()
        bot._shared_state = PLUGIN_MODULE.ChatState()

        media_result = bot._legacy_tool_search_media("测试电影", session_key="single_user")
        resource_result = bot._legacy_tool_search_resources("测试电影", session_key="single_user")

        self.assertEqual(media_result["results"][0]["title"], "测试电影 A")
        self.assertEqual(resource_result["results"][0]["title"], "测试电影 4K")
        self.assertEqual(len(bot._shared_state.search_cache), 1)
        self.assertEqual(len(bot._shared_state.resource_cache), 1)

    def test_cached_download_can_use_shared_state_without_engine(self):
        class _DownloadChain:
            @staticmethod
            def download_single(_ctx):
                return True

        chain_module = types.ModuleType("app.chain")
        download_module = types.ModuleType("app.chain.download")
        download_module.DownloadChain = _DownloadChain
        sys.modules["app.chain"] = chain_module
        sys.modules["app.chain.download"] = download_module

        bot = FeishuBot()
        bot._feishu = _FakeFeishu()
        bot._shared_state = PLUGIN_MODULE.ChatState()
        bot._shared_state.resource_cache = [
            types.SimpleNamespace(
                torrent_info=types.SimpleNamespace(
                    title="测试资源",
                    site_name="站点",
                    size=1024**3,
                    seeders=66,
                )
            )
        ]

        handled = bot._try_handle_cached_index_action(
            "下载1号",
            None,
            chat_id="chat-id",
            msg_id="msg-id",
            session_key="single_user",
        )

        self.assertTrue(handled)
        self.assertEqual(
            bot._feishu.calls[-1]["card"]["header"]["title"]["content"],
            "⬇️ 下载确认",
        )
        self.assertEqual(bot._shared_state.pending_download["index"], 0)

    def test_retired_qwen_free_model_is_normalized_in_model_chain(self):
        self.assertEqual(
            LLM_MODULE.normalize_model_name("qwen/qwen3-4b:free"),
            "qwen/qwen3-next-80b-a3b-instruct:free",
        )
        client = LLM_MODULE.LLMClient(
            api_key="test-key",
            model="qwen/qwen3-4b:free",
            fallback_models=["qwen/qwen3-4b:free", "openrouter/free"],
            auto_fallback=False,
        )
        self.assertEqual(
            client.model_chain,
            ["qwen/qwen3-next-80b-a3b-instruct:free", "openrouter/free"],
        )

    def test_feishu_token_fetch_sets_cached_expiry(self):
        class _FakeResponse:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {
                    "code": 0,
                    "tenant_access_token": "tenant-token",
                    "expire": "3600",
                }

        calls = []
        original_post = FEISHU_API_MODULE.requests.post

        def _fake_post(*args, **kwargs):
            calls.append((args, kwargs))
            return _FakeResponse()

        FEISHU_API_MODULE.requests.post = _fake_post
        try:
            api = FEISHU_API_MODULE._FeishuAPI("app-id", "app-secret")
            self.assertEqual(api._get_token(), "tenant-token")
            self.assertGreater(api._token_expire, FEISHU_API_MODULE.datetime.now())
            self.assertEqual(api._get_token(), "tenant-token")
            self.assertEqual(len(calls), 1)
        finally:
            FEISHU_API_MODULE.requests.post = original_post

    def test_agent_progress_callback_imports_ai_tools_from_parent_package(self):
        class _ProgressEngine:
            @staticmethod
            def chat_with_progress(text, on_tool_start=None):
                if on_tool_start:
                    on_tool_start("search_resources", {"keyword": text})
                return "处理完成", ["搜索资源"]

        bot = FeishuBot()
        bot._feishu = _FakeFeishu()
        bot._get_or_create_engine = lambda _session_key: _ProgressEngine()
        bot._release_global_processing = lambda _session_key, _msg_id: None
        bot._build_trace_id = lambda *_args: "trace"

        bot._agent_handle_v2("single_user", "流浪地球", "chat-id", "msg-id")

        self.assertEqual(len(bot._feishu.calls), 1)
        self.assertGreaterEqual(len(bot._feishu.updated), 2)
        self.assertEqual(bot._feishu.updated[0]["message_id"], "status_msg")
        self.assertEqual(bot._feishu.updated[-1]["message_id"], "status_msg")


if __name__ == "__main__":
    unittest.main()
