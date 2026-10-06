"""One FIFO worker for inbound messages and card actions."""

import queue
import threading

from app.log import logger


class SerialDispatcher:
    def __init__(self, capacity=100):
        self._queue = queue.Queue(maxsize=capacity)
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="feishu-dialogue", daemon=True)
        self._thread.start()

    @property
    def pending_count(self):
        return self._queue.qsize()

    def submit(self, function, *args):
        if self._stopped.is_set():
            return False
        try:
            self._queue.put_nowait((function, args))
            return True
        except queue.Full:
            return False

    def stop(self, timeout=5):
        self._stopped.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    def _run(self):
        while not self._stopped.is_set():
            try:
                function, args = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if not self._stopped.is_set():
                    function(*args)
            except Exception:
                logger.error("飞书会话任务异常", exc_info=True)
            finally:
                self._queue.task_done()

