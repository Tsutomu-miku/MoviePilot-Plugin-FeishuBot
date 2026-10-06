"""飞书机器人插件 — 飞书 API 客户端"""

import json as _json
from datetime import datetime, timedelta

import requests
from app.log import logger


class _FeishuAPI:
    """飞书 Token 管理 & 消息发送"""

    def __init__(self, app_id, app_secret, is_active=None):
        self._is_active = is_active or (lambda: True)
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: str = ""
        self._token_expire: datetime = datetime.min

    @staticmethod
    def _token_ttl_seconds(expire_value) -> int:
        try:
            expire_seconds = int(expire_value)
        except (TypeError, ValueError):
            expire_seconds = 7200
        return max(expire_seconds - 60, 60)

    def _get_token(self) -> str:
        if self._token and datetime.now() < self._token_expire:
            return self._token
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code", 0) != 0:
                raise RuntimeError(
                    f"code={data.get('code')} msg={data.get('msg') or data.get('message') or 'unknown error'}"
                )

            token = data.get("tenant_access_token", "")
            if not token:
                raise RuntimeError("tenant_access_token missing in response")

            self._token = token
            self._token_expire = datetime.now() + timedelta(
                seconds=self._token_ttl_seconds(data.get("expire"))
            )
        except Exception as e:
            logger.error(f"获取飞书 Token 失败: {e}")
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def send_text(self, chat_id: str, text: str, reply_msg_id: str = None):
        """发送文本消息。提供 reply_msg_id 时使用回复 API。"""
        if not self._is_active():
            return {}
        content = _json.dumps({"text": text}, ensure_ascii=False)

        if reply_msg_id:
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{reply_msg_id}/reply"
            body = {"msg_type": "text", "content": content}
            params = {}
        else:
            url = "https://open.feishu.cn/open-apis/im/v1/messages"
            body = {"receive_id": chat_id, "msg_type": "text", "content": content}
            params = {"receive_id_type": "chat_id"}

        try:
            headers = self._headers()
            if not self._is_active():
                return {}
            resp = requests.post(
                url, params=params, headers=headers,
                json=body, timeout=10,
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.warning(f"飞书消息发送失败: {result}")
        except Exception as e:
            logger.error(f"飞书发送异常: {e}")

    def send_card(self, chat_id: str, card: dict, reply_msg_id: str = None):
        """发送卡片消息。支持 reply_msg_id 回复。"""
        if not self._is_active():
            return {}
        content = _json.dumps(card, ensure_ascii=False)

        if reply_msg_id:
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{reply_msg_id}/reply"
            body = {"msg_type": "interactive", "content": content}
            params = {}
        else:
            url = "https://open.feishu.cn/open-apis/im/v1/messages"
            body = {
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": content,
            }
            params = {"receive_id_type": "chat_id"}

        try:
            headers = self._headers()
            if not self._is_active():
                return {}
            resp = requests.post(
                url, params=params, headers=headers,
                json=body, timeout=10,
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.warning(f"飞书卡片发送失败: {result}")
            return result
        except Exception as e:
            logger.error(f"飞书发送卡片异常: {e}")
            return {}

    def update_card(self, message_id: str, card: dict):
        """更新已发送的卡片消息（用于状态更新）"""
        if not self._is_active():
            return {}
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
        body = {
            "msg_type": "interactive",
            "content": _json.dumps(card, ensure_ascii=False),
        }
        try:
            headers = self._headers()
            if not self._is_active():
                return {}
            resp = requests.patch(
                url, headers=headers, json=body, timeout=10,
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.warning(f"飞书卡片更新失败: {result}")
        except Exception as e:
            logger.error(f"飞书更新卡片异常: {e}")
