import asyncio
import datetime
import json
from typing import Any
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from .models.OneBotV11Message import OneBotV11Message


async def _maybe_await(value: Any) -> Any:
    # 兼容同步与异步两种返回值
    if asyncio.iscoroutine(value):
        return await value
    return value


async def _call_onebot_action(client: Any, action: str, params: dict[str, Any]) -> Any:
    # 适配不同 OneBot 客户端实现，按常见调用方式依次尝试
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
            result = await _maybe_await(candidate())
            if result is not None:
                return result
        except Exception as e:
            last_error = e
            continue

    if last_error:
        raise last_error
    raise RuntimeError(f"无法调用 OneBot 动作: {action}")


def _extract_message_batch(payload: Any) -> list[dict[str, Any]]:
    # 兼容不同适配器的返回结构
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


def _calc_next_message_seq(batch: list[dict[str, Any]], current: int | None) -> int | None:
    # 使用当前批次最小 message_seq 作为下一页锚点
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

    next_seq = min(seq_values)
    if current is not None and next_seq >= current:
        next_seq = current - 1
    if next_seq <= 0:
        return None
    return next_seq


def _is_message_not_exists_error(error: Exception) -> bool:
    # 分页越界时，Napcat 常返回 retcode=1200 或“消息不存在”
    retcode = getattr(error, "retcode", None)
    message_text = str(getattr(error, "message", "")) + str(getattr(error, "wording", ""))
    raw_text = str(error)
    return retcode == 1200 or ("不存在" in message_text) or ("不存在" in raw_text)


async def _fetch_messages_in_range(
    client: Any,
    group_id: str,
    start_timestamp: int,
    end_timestamp: int,
    logger_instance: Any | None = None,
) -> list[dict[str, Any]]:
    # 分页拉取群历史消息，并按时间窗口筛选
    raw_messages: list[dict[str, Any]] = []
    seen_raw_ids: set[str] = set()
    message_seq: int | None = None
    missing_seq_retry_count = 0
    group_id_for_api: Any = int(group_id) if group_id.isdigit() else group_id

    while True:
        params: dict[str, Any] = {"group_id": group_id_for_api, "count": 100}
        if message_seq is not None:
            params["message_seq"] = message_seq

        try:
            response = await _call_onebot_action(client, "get_group_msg_history", params)
        except Exception as e:
            if _is_message_not_exists_error(e):
                # 遇到不存在序号时回退重试，避免空洞序号导致提前终止
                if message_seq is None or message_seq <= 1:
                    if logger_instance:
                        logger_instance.info(f"分页到历史边界，停止继续拉取: {e}")
                    break
                missing_seq_retry_count += 1
                if missing_seq_retry_count > 100:
                    if logger_instance:
                        logger_instance.info(f"连续命中不存在的 message_seq，停止继续拉取: {e}")
                    break
                message_seq -= 1
                continue
            raise
        missing_seq_retry_count = 0

        batch = _extract_message_batch(response)
        if not batch:
            break

        stop_paging = False
        for item in batch:
            msg_group_id = str(item.get("group_id", ""))
            if msg_group_id and msg_group_id != group_id:
                continue

            msg_time = int(item.get("time") or item.get("timestamp") or 0)
            if msg_time <= 0:
                continue

            if msg_time < start_timestamp:
                # 批次已进入目标区间之前，可以结束翻页
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

        next_seq = _calc_next_message_seq(batch, message_seq)
        if next_seq is None:
            break
        message_seq = next_seq

    return raw_messages


async def get_msg_list(
    groupID: str,
    days: int,
    client: Any,
    logger_instance: Any | None = None,
    now: datetime.datetime | None = None,
) -> list[OneBotV11Message]:
    # 获取“当前时刻回溯 days 天”内的群消息，并完成去重与排序
    if days < 0:
        raise ValueError("<days> 需要是大于等于 0 的整数。")
    if not groupID:
        raise ValueError("<groupID> 不能为空。")

    now_dt = now or datetime.datetime.now()
    end_datetime = now_dt
    start_datetime = now_dt - datetime.timedelta(days=days)
    start_timestamp = int(start_datetime.timestamp())
    end_timestamp = int(end_datetime.timestamp())

    raw_messages = await _fetch_messages_in_range(
        client=client,
        group_id=groupID,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        logger_instance=logger_instance,
    )

    pre_messages = [OneBotV11Message.from_raw(item) for item in raw_messages]
    content_seen: set[str] = set()
    messages: list[OneBotV11Message] = []
    for msg in pre_messages:
        # 按消息内容去重，而非 message_id 去重
        content_key = msg.content_key()
        if content_key in content_seen:
            continue
        content_seen.add(content_key)
        messages.append(msg)

    # 按发送时间从早到晚排序
    messages.sort(key=lambda item: item.time)
    return messages

async def process_msg(
    messages: list[OneBotV11Message],
    context: Context,
    event: AstrMessageEvent,
    logger_instance: Any | None = None,
) -> list[str]:
    message_json_list: list[str] = []
    for message_item in sorted(messages, key=lambda item: item.time):
        message_json_list.append(
            json.dumps(
                {
                    "time": message_item.time,
                    "user_id": message_item.user_id,
                    "message": message_item.message,
                },
                ensure_ascii=False,
                default=str,
            )
        )
    
    #TODO: 完善 prompt
    prompt_text = ""
    step_size = 10
    window_size = 50
    llm_response_list: list[str] = []
    provider_id = await context.get_current_chat_provider_id(umo=event.unified_msg_origin)

    if step_size <= 0:
        step_size = 1
    if window_size <= 0:
        window_size = 1
    if not message_json_list:
        return llm_response_list

    # 滑动窗口：先处理前 window_size 条，再每轮向后滑动 step_size 条
    # 例如窗口大小 50、步长 10：第 1 轮 1~50，第 2 轮 11~60，第 3 轮 21~70
    for window_start in range(0, len(message_json_list), step_size):
        window_messages = message_json_list[window_start : window_start + window_size]
        if not window_messages:
            break

        llm_prompt = f"{prompt_text}\n\n消息列表：\n" + "\n".join(window_messages)
        llm_response = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=llm_prompt,
        )
        completion_text = getattr(llm_response, "completion_text", str(llm_response))
        llm_response_list.append(completion_text)
        if logger_instance:
            logger_instance.info(completion_text)
    
    #TODO: 对中间结果进一步处理
