import collections
import datetime
import random
import re
import traceback

import jieba
from langchain.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from nonebot.log import logger
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_orm import get_session
from sqlalchemy import Select, desc, extract, func

from nonebot_plugin_groupmate_agent.agent.optional_tools import AgentSkill, OptionalToolBundle, OptionalToolContext
from nonebot_plugin_groupmate_agent.model import ChatHistory, UserRelation
from nonebot_plugin_groupmate_agent.reply_guard import is_request_active, mark_request_sent

SKILL_PROMPT = """- 若用户提到“年度报告 / 个人总结 / 成分分析”，直接调用 `generate_and_send_annual_report`；
  工具完成后只回复“请查收~”，不要复述报告
"""


def create_report_tool(ctx: OptionalToolContext):
    """创建年度报告工具（限制在当前群聊 session_id 范围内）"""

    @tool("generate_and_send_annual_report")
    async def generate_and_send_annual_report() -> str:
        """
        生成并发送当前群聊的年度报告。
        包含：个人在本群的统计、性格分析、全群排行榜以及Bot的好感度回顾。
        """
        if ctx.request_id is not None and not await is_request_active(ctx.session_id, ctx.request_id):
            return "请求已过期，已取消发送。"

        try:
            logger.info(f"开始生成用户 {ctx.user_name} 在群 {ctx.session_id} 的年度报告...")
            now = datetime.datetime.now()
            current_year = now.year
            async with get_session() as db_session:
                stmt = Select(ChatHistory).where(
                    ChatHistory.user_id == ctx.user_id,
                    ChatHistory.session_id == ctx.session_id,
                    extract("year", ChatHistory.created_at) == current_year,
                )
                all_msgs = (await db_session.execute(stmt)).scalars().all()

                if not all_msgs:
                    await UniMessage.text("你今年在这个群好像没怎么说话，生成不了报告哦...").send()
                    if ctx.request_id is not None:
                        mark_request_sent(ctx.session_id, ctx.request_id)
                    return "用户本群无数据。"

                text_msgs = [m.content for m in all_msgs if m.content_type == "text" and m.content]
                total_count = len(all_msgs)

                samples = random.sample(text_msgs, min(len(text_msgs), 30)) if text_msgs else []
                longest_msg = max(text_msgs, key=len) if text_msgs else "无"
                if len(longest_msg) > 60:
                    longest_msg = longest_msg[:60] + "..."

                active_hour_desc = "潜水员"
                if all_msgs:
                    hours = [m.created_at.hour for m in all_msgs]
                    top_hour = collections.Counter(hours).most_common(1)[0][0]
                    active_hour_desc = f"{top_hour}点"

                async def get_rank_str(content_type=None, hour_limit=None):
                    stmt = Select(ChatHistory.user_id, func.count(ChatHistory.msg_id).label("c")).where(
                        extract("year", ChatHistory.created_at) == current_year,
                        ChatHistory.session_id == ctx.session_id,
                    )

                    if content_type:
                        stmt = stmt.where(ChatHistory.content_type == content_type)
                    if hour_limit:
                        stmt = stmt.where(extract("hour", ChatHistory.created_at) < hour_limit)

                    stmt = stmt.group_by(ChatHistory.user_id).order_by(desc("c")).limit(3)
                    rows = (await db_session.execute(stmt)).all()

                    if not rows:
                        return "虚位以待"

                    rank_items = []
                    for uid, count in rows:
                        name_stmt = (
                            Select(ChatHistory.user_name)
                            .where(ChatHistory.user_id == uid)
                            .order_by(desc(ChatHistory.created_at))
                            .limit(1)
                        )

                        latest_name = (await db_session.execute(name_stmt)).scalar()
                        display_name = latest_name if latest_name else f"用户{uid}"
                        rank_items.append(f"{display_name}({count})")
                    return ", ".join(rank_items)

                rank_talk = await get_rank_str()
                rank_img = await get_rank_str(content_type="image")
                rank_night = await get_rank_str(hour_limit=5)

                stmt_text = (
                    Select(ChatHistory.content)
                    .where(
                        ChatHistory.session_id == ctx.session_id,
                        extract("year", ChatHistory.created_at) == current_year,
                        ChatHistory.user_id == ctx.user_id,
                        ChatHistory.content_type == "text",
                    )
                    .order_by(desc(ChatHistory.created_at))
                    .limit(2000)
                )

                rows = (await db_session.execute(stmt_text)).all()
                sample_text = "\n".join([r[0] for r in rows if r[0]])

                clean_text = re.sub(r"[^\u4e00-\u9fa5]", "", sample_text)
                words = jieba.lcut(clean_text)
                filtered = [w for w in words if len(w) > 1 and w not in ctx.stop_words]
                hot_words_str = "、".join([x[0] for x in collections.Counter(filtered).most_common(8)])

                relation_stmt = Select(UserRelation).where(UserRelation.user_id == ctx.user_id)
                relation = (await db_session.execute(relation_stmt)).scalar_one_or_none()

                favorability = 0
                favorability_raw = 0
                relation_state = "normal"
                relation_state_desc = "陌生/普通"
                impression_tags = []
                if relation:
                    favorability = relation.favorability
                    favorability_raw = relation.favorability_raw
                    relation_state = relation.state or "normal"
                    relation_state_desc = relation.get_status_desc()
                    impression_tags = relation.tags if relation.tags else []

                relation_desc = (
                    f"关系状态: {relation_state} ({relation_state_desc}), "
                    f"分值(映射分/原始分): {favorability}/{favorability_raw}, "
                    f"印象标签: {', '.join(impression_tags)}"
                )

            report_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        """你是一个专业的年度报告撰写助手。
你的任务是阅读用户的聊天统计数据和发言样本，分析其性格，然后生成一份格式整洁、风格幽默的年度报告。

【语气控制指南 (非常重要)】
根据用户的“关系状态”调整你的语气：
- 状态为 happy / affectionate / enamored / love：语气更亲密、偏宠溺，可以适当煽情。
- 状态为 upset / distressed / broken：语气可傲娇、嫌弃、带吐槽，但不要失控辱骂。
- 状态为 normal：语气正常、友善、带一点调侃。
关系状态是主依据；分值只作参考。其中“映射分”用于展示，“原始分”才是核心变化依据。

【排版要求】
1. **绝对禁止使用 Markdown**（不要用 #, **, ##, - 等符号列表）。
2. 使用 Emoji 和 纯文本分隔符（如 ━━━━━━━━）来排版。
3. 语气要像老朋友一样，可以根据数据进行调侃或夸奖。

【必须包含的板块】
1. 📊 标题行 ({year}年度报告 | 用户名)
2. 📈 基础数据 (发言数、活跃时间、最长发言摘要)
3. 💌 我们的羁绊 (根据关系状态与标签，写一段话回顾你们的关系。正向关系可煽情，负向关系可吐槽。)
4. 🔥 年度热词 (列出数据中提供的热词)
5. 🏆 群内风云榜 (必须包含以下三个榜单)
   - 🗣️ 龙王榜 (发言最多)
   - 🎭 斗图榜 (发图最多)
   - 🦉 修仙榜 (熬夜最多)
6. 🧠 成分分析 (这是**重点**：请阅读提供的 `samples` 聊天记录，分析这个人的说话风格、
是不是复读机、是不是爱发疯。写一段100字左右的犀利点评)
7. 💡 {bot_name}寄语 (一句简短的祝福)
""",
                    ),
                    (
                        "user",
                        """
【用户数据】
用户名: {user_name}
年份: {year}
累计发言: {count}
活跃时间: {active_hour}
最长发言片段: {longest_msg}
年度热词: {hot_words}

【{bot_name}与用户的关系】
{relation_desc}

【全群排行参考】
龙王榜: {rank_talk}
斗图榜: {rank_img}
熬夜榜: {rank_night}

【用户发言样本 (用于性格分析)】
{samples}

请生成报告：""",
                    ),
                ]
            )

            prompt_input = {
                "user_name": ctx.user_name,
                "bot_name": ctx.config.bot_name,
                "year": current_year,
                "count": total_count,
                "active_hour": active_hour_desc,
                "longest_msg": longest_msg,
                "hot_words": hot_words_str,
                "relation_desc": relation_desc,
                "rank_talk": rank_talk,
                "rank_img": rank_img,
                "rank_night": rank_night,
                "samples": "\n".join(samples),
            }

            logger.info(
                f"内部 LLM 生成报告中，状态: {relation_state}, "
                f"分值(映射/原始): {favorability}/{favorability_raw}"
            )
            chain = report_prompt | ctx.model
            response_msg = await chain.ainvoke(prompt_input)
            final_report_text = response_msg.content
            if not isinstance(final_report_text, str):
                return "输出结果失败"

            if ctx.request_id is not None and not await is_request_active(ctx.session_id, ctx.request_id):
                return "请求已过期，已取消发送。"

            await UniMessage.text(final_report_text).send()
            if ctx.request_id is not None:
                mark_request_sent(ctx.session_id, ctx.request_id)

            return "报告已生成并发送。"

        except Exception as e:
            logger.error(f"内部 LLM 生成报告失败: {e}")
            traceback.print_exc()
            return f"生成过程出错: {e}"

    return generate_and_send_annual_report


async def build(ctx: OptionalToolContext) -> OptionalToolBundle:
    if ctx.is_cross_user_direct_reply:
        return OptionalToolBundle(name="report")
    return OptionalToolBundle(
        name="report",
        tools=[create_report_tool(ctx)],
        skills=[
            AgentSkill(
                name="annual_report",
                description="用户明确要求年度报告时生成报告。",
                prompt=SKILL_PROMPT,
                tool_names=("generate_and_send_annual_report",),
            )
        ],
    )
