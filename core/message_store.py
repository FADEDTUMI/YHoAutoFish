"""
消息持久化存储。

将服务端推送的通知/广播消息持久化到 messages.json，
支持新增、查询、标记已读。
"""

import json
import os
import threading
import time

from core.paths import ensure_writable_file

_FILE_LOCK = threading.RLock()
_MAX_MESSAGES = 200


class MessageStore:
    """轻量消息持久化存储。"""

    def __init__(self, path=None):
        self._path = path or ensure_writable_file("messages.json")
        self._messages = []
        self._next_id = 1
        self._load()

    # ------------------------------------------------------------------
    #  公开接口
    # ------------------------------------------------------------------

    def add_message(self, text, msg_type="notification"):
        """追加一条消息并保存。返回新消息的 id。"""
        with _FILE_LOCK:
            msg = {
                "id": self._next_id,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "text": str(text),
                "read": False,
                "type": str(msg_type),
            }
            self._next_id += 1
            self._messages.append(msg)
            if len(self._messages) > _MAX_MESSAGES:
                self._messages = self._messages[-_MAX_MESSAGES:]
            self._save()
            return msg["id"]

    def get_messages(self):
        """返回所有消息（按时间正序）。"""
        with _FILE_LOCK:
            return list(self._messages)

    def has_unread(self):
        """是否存在未读消息。"""
        with _FILE_LOCK:
            return any(not m["read"] for m in self._messages)

    def unread_count(self):
        """未读消息数量。"""
        with _FILE_LOCK:
            return sum(1 for m in self._messages if not m["read"])

    def mark_all_read(self):
        """标记所有消息为已读并保存。"""
        with _FILE_LOCK:
            changed = False
            for m in self._messages:
                if not m["read"]:
                    m["read"] = True
                    changed = True
            if changed:
                self._save()

    # ------------------------------------------------------------------
    #  内部读写
    # ------------------------------------------------------------------

    def _load(self):
        try:
            if not os.path.exists(self._path):
                return
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            msgs = data.get("messages", [])
            if isinstance(msgs, list):
                self._messages = msgs
                self._next_id = data.get("next_id", 1)
                if self._messages:
                    max_id = max(int(m.get("id", 0)) for m in self._messages)
                    self._next_id = max(self._next_id, max_id + 1)
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    def _save(self):
        try:
            data = {
                "messages": self._messages,
                "next_id": self._next_id,
            }
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            if os.path.exists(self._path):
                os.replace(tmp, self._path)
            else:
                os.rename(tmp, self._path)
        except OSError:
            pass
