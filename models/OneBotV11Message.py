import json
from dataclasses import dataclass
from typing import Any


@dataclass
class OneBotV11Message:
    # OneBot v11 历史消息统一数据结构
    message_id: str
    group_id: str
    user_id: str
    time: int
    message: Any
    raw: dict[str, Any]

    @classmethod
    def from_raw(cls, raw_message: dict[str, Any]) -> "OneBotV11Message":
        # 兼容不同适配器的时间字段命名
        return cls(
            message_id=str(raw_message.get("message_id", "")),
            group_id=str(raw_message.get("group_id", "")),
            user_id=str(raw_message.get("user_id", "")),
            time=int(raw_message.get("time") or raw_message.get("timestamp") or 0),
            message=raw_message.get("message"),
            raw=raw_message,
        )

    def content_key(self) -> str:
        # 用消息内容生成稳定键，用于跨批次去重
        if isinstance(self.message, (list, dict)):
            return json.dumps(self.message, ensure_ascii=False, sort_keys=True)
        return str(self.message)
