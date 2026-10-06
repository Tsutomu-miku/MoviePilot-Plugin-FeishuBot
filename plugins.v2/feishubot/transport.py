"""Own the SDK loop and close it before another plugin instance starts.

The Lark SDK uses a module-level event loop and has no public stop method.
Its private disconnect operation is isolated here and covered by lifecycle tests.
"""

import asyncio
import sys
import threading
import types

from app.log import logger


# Keep ownership across plugin module reloads, rather than on each FeishuBot.
_registry = sys.modules.setdefault("_moviepilot_feishu_ws_owner", types.ModuleType("_moviepilot_feishu_ws_owner"))
if not hasattr(_registry, "lock"):
    _registry.lock = threading.RLock()
    _registry.owner = None


class WebSocketTransport:
    def __init__(self, client_factory, sdk_module, on_retire=None):
        self._factory = client_factory
        self._sdk_module = sdk_module
        self._on_retire = on_retire
        self._stop = threading.Event()
        self.client = None
        self.loop = None
        self.thread = None

    @property
    def connected(self):
        return not self._stop.is_set() and getattr(self.client, "_conn", None) is not None

    def start(self):
        with _registry.lock:
            previous = _registry.owner
            if previous is not None and previous is not self:
                if previous._on_retire:
                    previous._on_retire()
                previous.stop()
                if previous.thread is not None and previous.thread.is_alive():
                    raise RuntimeError("旧飞书连接尚未退出，请稍后重新加载插件")
            _registry.owner = self
            self.thread = threading.Thread(target=self._run, name="feishu-ws-client", daemon=True)
            self.thread.start()

    def stop(self):
        self._stop.set()
        client, loop = self.client, self.loop
        if client is not None:
            client._auto_reconnect = False
        if loop is not None and not loop.is_closed():
            def cancel_tasks():
                for task in asyncio.all_tasks(loop):
                    task.cancel()
            try:
                loop.call_soon_threadsafe(cancel_tasks)
            except RuntimeError:
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            loop = asyncio.new_event_loop()
            self.loop = loop
            asyncio.set_event_loop(loop)
            self._sdk_module.loop = loop
            client = None
            try:
                client = self._factory()
                self.client = client
                if not self._stop.is_set():
                    client.start()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                if not self._stop.is_set():
                    logger.error(f"飞书长连接退出: {exc}")
            finally:
                if client is not None:
                    client._auto_reconnect = False
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                try:
                    if client is not None:
                        loop.run_until_complete(asyncio.wait_for(client._disconnect(), timeout=3))
                except Exception as exc:
                    logger.warning(f"飞书连接关闭异常: {exc}")
                loop.close()
                self.loop = None
                self.client = None
            if self._stop.wait(10):
                break
