"""飞书 API 客户端 — Token 管理 & 消息发送"""

import json
from datetime import datetime, timedelta
from typing import Optional

import requests
from app.log import logger


class FeishuAPI:
    """封装飞书 Token 获取和消息发送"""

    def __init__(self, app_id: str, app_secret: str, default_chat_id: str = ""):
        self.app_id = app_id
        self.app_secret = app_secret
        self.default_chat_id = default_chat_id
        self._token: str = ""
        self._token_expire: datetime = datetime.min

    # ── Token 管理 ──

    def _get_token(self) -> str:
        if self._token and datetime.now() < self._token_expire:
            return self._token
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            )
            data = resp.json()
            self._token = data.get("tenant_access_token", "")
            self._token_expire = datetime.now() + timedelta(
                seconds=data.get("expire", 7200) - 60
            )
        except Exception as e:
            logger.error(f"获取飞书 Token 失败: {e}")
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # ── 消息发送 ──

    def send_text(self, chat_id: str, text: str, reply_msg_id: Optional[str] = None):
        """发送文本消息。如果提供 reply_msg_id，则作为回复发送。"""
        content = json.dumps({"text": text}, ensure_ascii=False)

        if reply_msg_id:
            # 使用回复 API
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{reply_msg_id}/reply"
            body = {"msg_type": "text", "content": content}
        else:
            url = "https://open.feishu.cn/open-apis/im/v1/messages"
            body = {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": content,
            }

        try:
            params = {} if reply_msg_id else {"receive_id_type": "chat_id"}
            resp = requests.post(
                url, params=params, headers=self._headers(),
                json=body, timeout=10,
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.warning(f"飞书消息发送失败: {result}")
        except Exception as e:
            logger.error(f"飞书发送异常: {e}")

    def send_card(self, chat_id: str, card: dict):
        """发送交互卡片消息"""
        body = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers=self._headers(),
                json=body, timeout=10,
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.warning(f"飞书卡片发送失败: {result}")
        except Exception as e:
            logger.error(f"飞书发送卡片异常: {e}")
