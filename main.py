import asyncio
import datetime
import json
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@dataclass
class OneBotV11Message:
    # 统一封装 OneBot v11 历史消息结构，便于后续处理与维护
    message_id: str
    group_id: str
    user_id: str
    time: int
    message: Any
    raw: dict[str, Any]

    @classmethod
    def from_raw(cls, raw_message: dict[str, Any]) -> "OneBotV11Message":
        # 兼容不同适配器返回字段：time/timestamp
        return cls(
            message_id=str(raw_message.get("message_id", "")),
            group_id=str(raw_message.get("group_id", "")),
            user_id=str(raw_message.get("user_id", "")),
            time=int(raw_message.get("time") or raw_message.get("timestamp") or 0),
            message=raw_message.get("message"),
            raw=raw_message,
        )

    def content_key(self) -> str:
        # 用于按“消息内容”去重：列表/字典做稳定序列化，其他类型直接转字符串
        if isinstance(self.message, (list, dict)):
            return json.dumps(self.message, ensure_ascii=False, sort_keys=True)
        return str(self.message)


@register("qq_group_msg_info_gathering", "YourName", "QQ群消息收集插件", "1.0.0")
class QQGroupMsgInfoGatheringPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def initialize(self):
        return None

    @filter.command_group("gathering")
    def gathering(self):
        pass

    async def _maybe_await(self, value: Any) -> Any:
        # 兼容同步返回值与协程返回值
        if asyncio.iscoroutine(value):
            return await value
        return value

    async def _call_onebot_action(self, client: Any, action: str, params: dict[str, Any]) -> Any:
        # 尝试多种常见调用方式，适配不同版本客户端封装
        candidates = []

        call_action = getattr(client, "call_action", None)
        if callable(call_action):
            candidates.extend(
                [
                    lambda: call_action(action, **params),
                    lambda: call_action(action=action, **params),
                ]
            )

        call_api = getattr(client, "call_api", None)
        if callable(call_api):
            candidates.extend(
                [
                    lambda: call_api(action, **params),
                    lambda: call_api(action=action, **params),
                ]
            )

        direct_action = getattr(client, action, None)
        if callable(direct_action):
            candidates.extend(
                [
                    lambda: direct_action(**params),
                ]
            )

        last_error: Exception | None = None
        for candidate in candidates:
            try:
                result = await self._maybe_await(candidate())
                if result is not None:
                    return result
            except Exception as e:
                last_error = e
                continue

        if last_error:
            raise last_error
        raise RuntimeError(f"无法调用 OneBot 动作: {action}")

    def _extract_message_batch(self, payload: Any) -> list[dict[str, Any]]:
        # 兼容不同返回结构：list / {messages} / {message_list} / {data: ...}
        if isinstance(payload, list):
            return [m for m in payload if isinstance(m, dict)]
        if not isinstance(payload, dict):
            return []

        if isinstance(payload.get("messages"), list):
            return [m for m in payload["messages"] if isinstance(m, dict)]
        if isinstance(payload.get("message_list"), list):
            return [m for m in payload["message_list"] if isinstance(m, dict)]

        data = payload.get("data")
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict)]
        if isinstance(data, dict):
            if isinstance(data.get("messages"), list):
                return [m for m in data["messages"] if isinstance(m, dict)]
            if isinstance(data.get("message_list"), list):
                return [m for m in data["message_list"] if isinstance(m, dict)]

        return []

    def _calc_next_message_seq(self, batch: list[dict[str, Any]], current: int | None) -> int | None:
        # 取当前批次最小 message_seq 再减一，向更早消息翻页
        seq_values: list[int] = []
        for item in batch:
            seq = item.get("message_seq")
            if seq is None:
                continue
            try:
                seq_values.append(int(seq))
            except Exception:
                continue

        if not seq_values:
            return None

        next_seq = min(seq_values) - 1
        if next_seq < 0:
            return None
        if current is not None and next_seq >= current:
            return None
        return next_seq

    async def _fetch_day_group_messages(
        self,
        client: Any,
        group_id: str,
        start_timestamp: int,
        end_timestamp: int,
    ) -> list[dict[str, Any]]:
        # 拉取指定群在目标时间窗口内的原始消息
        raw_messages: list[dict[str, Any]] = []
        seen_raw_ids: set[str] = set()
        message_seq: int | None = None
        group_id_for_api: Any = int(group_id) if group_id.isdigit() else group_id

        while True:
            params: dict[str, Any] = {"group_id": group_id_for_api, "count": 100}
            if message_seq is not None:
                params["message_seq"] = message_seq

            response = await self._call_onebot_action(client, "get_group_msg_history", params)
            batch = self._extract_message_batch(response)
            if not batch:
                break

            stop_paging = False
            for item in batch:
                # 双重保障：即使接口参数已指定 group_id，仍做一次群号过滤
                msg_group_id = str(item.get("group_id", ""))
                if msg_group_id and msg_group_id != group_id:
                    continue

                msg_time = int(item.get("time") or item.get("timestamp") or 0)
                if msg_time <= 0:
                    continue

                if msg_time < start_timestamp:
                    # 批次已进入目标时间窗口之前，可终止翻页
                    stop_paging = True

                if start_timestamp <= msg_time < end_timestamp:
                    message_id = str(item.get("message_id", ""))
                    if not message_id:
                        content = item.get("message")
                        content_text = json.dumps(content, ensure_ascii=False, sort_keys=True)
                        message_id = f"{msg_time}:{item.get('user_id', '')}:{content_text}"
                    if message_id in seen_raw_ids:
                        continue
                    seen_raw_ids.add(message_id)
                    raw_messages.append(item)

            if stop_paging:
                break

            next_seq = self._calc_next_message_seq(batch, message_seq)
            if next_seq is None:
                break
            message_seq = next_seq

        return raw_messages

    @gathering.command("from")
    async def gathering_from(self, event: AstrMessageEvent, groupID: str, days: int):
        # 仅支持 OneBot v11（aiocqhttp）
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("❌ 此功能仅支持 OneBot V11 (QQ) 平台。")
            return

        if days < 0:
            yield event.plain_result("❌ <days> 需要是大于等于 0 的整数。")
            return

        if not groupID:
            yield event.plain_result("❌ 正确格式为：/gathering from <groupID> <days>")
            return

        client = event.bot
        # 将 days 转换为“目标自然日”的闭开区间 [00:00:00, 次日00:00:00)
        now = datetime.datetime.now()
        target_date = now - datetime.timedelta(days=days)
        start_datetime = datetime.datetime(target_date.year, target_date.month, target_date.day)
        end_datetime = start_datetime + datetime.timedelta(days=1)
        start_timestamp = int(start_datetime.timestamp())
        end_timestamp = int(end_datetime.timestamp())

        await event.send(
            event.plain_result(
                f"⏳ 正在拉取群 {groupID} 在 {target_date.strftime('%Y-%m-%d')} 的消息，请稍候..."
            )
        )

        try:
            raw_messages = await self._fetch_day_group_messages(
                client=client,
                group_id=groupID,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
        except Exception as e:
            logger.error(f"拉取群历史消息失败: {e}")
            yield event.plain_result(f"❌ 拉取失败：{e}")
            return

        # 先映射为结构化消息对象，再按消息内容去重
        pre_messages = [OneBotV11Message.from_raw(item) for item in raw_messages]
        content_seen: set[str] = set()
        messages: list[OneBotV11Message] = []
        for msg in pre_messages:
            content_key = msg.content_key()
            if content_key in content_seen:
                continue
            content_seen.add(content_key)
            messages.append(msg)

        # 按发送时间从早到晚排序
        messages.sort(key=lambda item: item.time)

        if not messages:
            yield event.plain_result("✅ 未找到符合条件的消息。")
            return

        earliest = datetime.datetime.fromtimestamp(messages[0].time).strftime("%Y-%m-%d %H:%M:%S")
        latest = datetime.datetime.fromtimestamp(messages[-1].time).strftime("%Y-%m-%d %H:%M:%S")
        yield event.plain_result(
            f"✅ 已收集 {len(messages)} 条消息（去重后），时间范围：{earliest} ~ {latest}"
        )

    async def terminate(self):
        return None
