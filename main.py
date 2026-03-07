import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from .models.windowProcessOutputJson import windowProcessOutputJson
from .util import get_msg_list, process_msg


@register("qq_group_msg_info_gathering", "YourName", "QQ群消息收集插件", "1.0.0")
class QQGroupMsgInfoGatheringPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def initialize(self):
        return None

    @filter.command_group("gathering")
    def gathering(self):
        pass

    @gathering.command("from")
    async def gathering_from(self, event: AstrMessageEvent, groupID: str, days: int):
        # 仅支持 OneBot v11 平台
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("❌ 此功能仅支持 OneBot V11 (QQ) 平台。")
            return

        # 基础参数校验
        if days < 0:
            yield event.plain_result("❌ <days> 需要是大于等于 0 的整数。")
            return

        if not groupID:
            yield event.plain_result("❌ 正确格式为：/gathering from <groupID> <days>")
            return

        client = event.bot
        # 将 days 转换为“当前时刻回溯 N 天”的展示区间
        now = datetime.datetime.now()
        end_datetime = now
        start_datetime = now - datetime.timedelta(days=days)

        await event.send(
            event.plain_result(
                f"⏳ 正在拉取群 {groupID} 从 {start_datetime.strftime('%Y-%m-%d %H:%M:%S')} 到 {end_datetime.strftime('%Y-%m-%d %H:%M:%S')} 的消息，请稍候..."
            )
        )

        try:
            # 通过 util 层统一完成拉取、筛选、去重、排序
            messages = await get_msg_list(
                groupID=groupID,
                days=days,
                client=client,
                logger_instance=logger,
                now=now,
            )
        except Exception as e:
            logger.error(f"拉取群历史消息失败: {e}")
            yield event.plain_result(f"❌ 拉取失败：{e}")
            return

        # 返回最终统计结果
        if not messages:
            yield event.plain_result("✅ 未找到符合条件的消息。")
            return


        # 处理消息
        window_process_llm_response_list: list[windowProcessOutputJson] = await process_msg(
            messages=messages,
            context=self.context,
            event=event,
            logger_instance=logger,
        )
        
        earliest = datetime.datetime.fromtimestamp(messages[0].time).strftime("%Y-%m-%d %H:%M:%S")
        latest = datetime.datetime.fromtimestamp(messages[-1].time).strftime("%Y-%m-%d %H:%M:%S")
        yield event.plain_result(
            f"✅ 已收集 {len(messages)} 条消息（去重后），时间范围：{earliest} ~ {latest}"
        )

        # 发送 LLM 处理结果
        for response in window_process_llm_response_list:
            await event.send(event.plain_result(response.to_json()))
        
    async def terminate(self):
        return None
