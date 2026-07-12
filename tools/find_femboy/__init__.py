from __future__ import annotations

import asyncio
import random
import re
import traceback
from dataclasses import dataclass, field
from typing import Any

from langchain.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from nonebot.log import logger
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_orm import get_session

from nonebot_plugin_groupmate_agent.agent.optional_tools import (
    AgentSkill,
    OptionalToolBundle,
    OptionalToolContext,
    ToolLimitSpec,
)
from nonebot_plugin_groupmate_agent.model import ChatHistory, UserRelation
from nonebot_plugin_groupmate_agent.reply_guard import is_request_active, mark_request_sent
from sqlalchemy import Select

SKILL_PROMPT = """- 找男娘：当用户明确要求“找男娘 / 抓男娘 / 谁是男娘 / 群里的男娘”等群聊整活时，调用 `find_femboy_in_recent_chat`
  - 工具会从主插件传入的最近 20 条聊天记录里的非 bot 发言者中纯随机抽一个人
  - 工具发送结果时会实际艾特被抽中的人
  - 工具会把“男娘”当群聊玩笑标签处理，不作真实身份判断
  - 工具会自行发送结果；调用成功后不要复述结果，直接 `finish`
"""

RECENT_HISTORY_LIMIT = 20
MAX_TARGET_SAMPLES = 5
MAX_RAG_CHARS = 1200
MAX_REPLY_CHARS = 180
DIRECT_IDENTIFICATION_PREFIX = "群里的男娘就是：{name}。"


@dataclass
class Candidate:
    user_id: str
    user_name: str
    samples: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RelationProfile:
    user_name: str
    state: str
    status_desc: str
    favorability: int
    favorability_raw: int
    tags: tuple[str, ...] = ()


def _strip_role_prefix(name: str) -> str:
    if name.startswith("群主-"):
        return name[3:]
    if name.startswith("管理员-"):
        return name[4:]
    return name


def _parse_msg_body(content: str | None) -> str:
    lines = str(content or "").splitlines()
    if not lines:
        return ""

    body_start = 0
    if lines[0].startswith("id:"):
        body_start = 1
        if len(lines) > 1 and lines[1].startswith("回复id:"):
            body_start = 2
    return "\n".join(lines[body_start:]).strip()


def _normalize_sample(text: str, *, content_type: str) -> str:
    if content_type == "image":
        body = _parse_msg_body(text)
        return f"[图片] {body}".strip()
    return re.sub(r"\s+", " ", _parse_msg_body(text)).strip()


def _safe_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _collect_candidates(history: list[Any]) -> list[Candidate]:
    candidates: dict[str, Candidate] = {}

    for msg in list(history or [])[-RECENT_HISTORY_LIMIT:]:
        content_type = _safe_text(getattr(msg, "content_type", ""))
        if content_type == "bot":
            continue

        user_id = _safe_text(getattr(msg, "user_id", ""))
        if not user_id:
            continue

        user_name = _strip_role_prefix(_safe_text(getattr(msg, "user_name", ""))) or user_id
        candidate = candidates.setdefault(user_id, Candidate(user_id=user_id, user_name=user_name))
        candidate.user_name = user_name

        sample = _normalize_sample(str(getattr(msg, "content", "") or ""), content_type=content_type)
        if sample:
            candidate.samples.append(sample)

    return list(candidates.values())


def _normalize_tags(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        tags = [value]
    elif isinstance(value, list | tuple | set):
        tags = list(value)
    else:
        return ()

    normalized: list[str] = []
    seen: set[str] = set()
    for item in tags:
        tag = _safe_text(item)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
    return tuple(normalized)


async def _load_relation_profile(candidate: Candidate) -> RelationProfile | None:
    """Read only the current v2 UserRelation table via the host ORM model."""
    try:
        async with get_session() as db_session:
            result = await db_session.execute(
                Select(UserRelation).where(UserRelation.user_id == candidate.user_id)
            )
            relation = result.scalar_one_or_none()
            if relation is None:
                return None

            try:
                status_desc = relation.get_status_desc()
            except Exception:
                status_desc = relation.state or "normal"

            return RelationProfile(
                user_name=_strip_role_prefix(_safe_text(relation.user_name)) or candidate.user_name,
                state=_safe_text(relation.state) or "normal",
                status_desc=_safe_text(status_desc) or "陌生/普通",
                favorability=int(relation.favorability or 0),
                favorability_raw=int(relation.favorability_raw or 0),
                tags=_normalize_tags(relation.tags),
            )
    except Exception as e:
        logger.warning(f"找男娘工具读取 v2 用户画像失败，降级使用聊天素材: {e}")
        return None


def _format_relation_profile(profile: RelationProfile | None) -> str:
    if profile is None:
        return "（v2 画像表中暂无此人记录）"

    tags = "、".join(profile.tags) if profile.tags else "无"
    return (
        f"画像昵称：{profile.user_name}\n"
        f"关系状态：{profile.state} ({profile.status_desc})\n"
        f"好感度：映射分 {profile.favorability} / 原始分 {profile.favorability_raw}\n"
        f"标签：{tags}"
    )


def _build_rag_query(candidate: Candidate, profile: RelationProfile | None) -> str:
    parts = [candidate.user_name]
    if profile is not None:
        if profile.user_name and profile.user_name != candidate.user_name:
            parts.append(profile.user_name)
        parts.extend(profile.tags[:6])

    sample_text = " ".join(candidate.samples[-3:])
    if sample_text:
        parts.append(sample_text[:180])
    return " ".join(part for part in parts if part).strip() or candidate.user_name


async def _search_rag_context(
    ctx: OptionalToolContext,
    candidate: Candidate,
    profile: RelationProfile | None,
) -> str:
    try:
        from nonebot_plugin_groupmate_agent.memory import DB
    except Exception as e:
        logger.info(f"找男娘工具无法加载 RAG: {type(e).__name__}: {e}")
        return ""

    if not getattr(DB, "enabled", False):
        return ""

    query = _build_rag_query(candidate, profile)
    try:
        result = await asyncio.wait_for(DB.search_chat(query, ctx.session_id), timeout=15.0)
    except asyncio.TimeoutError:
        logger.warning("找男娘工具 RAG 搜索超时，降级使用最近聊天记录")
        return ""
    except Exception as e:
        logger.warning(f"找男娘工具 RAG 搜索失败，降级使用最近聊天记录: {e}")
        return ""

    return str(result or "").strip()[:MAX_RAG_CHARS]


def _clip_reply(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(normalized) <= MAX_REPLY_CHARS:
        return normalized
    return normalized[: MAX_REPLY_CHARS - 1].rstrip() + "…"


def _has_direct_identification(candidate: Candidate, text: str) -> bool:
    compact_text = re.sub(r"\s+", "", str(text or ""))
    compact_name = re.sub(r"\s+", "", candidate.user_name)
    if not compact_name:
        return False

    patterns = (
        f"群里的男娘就是：{compact_name}",
        f"群里的男娘就是{compact_name}",
        f"群里男娘就是：{compact_name}",
        f"群里男娘就是{compact_name}",
        f"本群男娘就是：{compact_name}",
        f"本群男娘就是{compact_name}",
        f"{compact_name}就是群里的男娘",
        f"{compact_name}是群里的男娘",
        f"{compact_name}就是群里男娘",
        f"{compact_name}是群里男娘",
        f"{compact_name}就是本群男娘",
        f"{compact_name}是本群男娘",
        f"{compact_name}就是男娘",
        f"{compact_name}是男娘",
        f"男娘就是：{compact_name}",
        f"男娘就是{compact_name}",
        f"男娘是{compact_name}",
        f"男娘：{compact_name}",
    )
    return any(pattern in compact_text for pattern in patterns)


def _ensure_playful_frame(candidate: Candidate, text: str, profile: RelationProfile | None = None) -> str:
    text = str(text or "").replace("今日" + "男娘", "群里的男娘")
    reply = _clip_reply(text)
    if not reply:
        return _fallback_reply(candidate, profile)

    if not _has_direct_identification(candidate, reply):
        reply = DIRECT_IDENTIFICATION_PREFIX.format(name=candidate.user_name) + reply

    return _clip_reply(reply)


def _fallback_reply(candidate: Candidate, profile: RelationProfile | None = None) -> str:
    reasons = [
        "发言气质太会拐弯",
        "语气里有可疑可爱波动",
        "最近存在高浓度抽象行为",
        "聊天轨迹自带拐弯特效",
        "句尾精神状态略显精致",
    ]
    if profile is not None and profile.tags:
        tag = random.choice(profile.tags)
        reasons.append(f"画像标签“{tag}”提供了可疑素材")

    picked = random.sample(reasons, 3)
    return (
        DIRECT_IDENTIFICATION_PREFIX.format(name=candidate.user_name)
        + f"理由：{picked[0]}、{picked[1]}、{picked[2]}。"
    )


async def _generate_reply(
    ctx: OptionalToolContext,
    candidate: Candidate,
    rag_context: str,
    profile: RelationProfile | None,
) -> str:
    if not ctx.model:
        return _fallback_reply(candidate, profile)

    recent_samples = "\n".join(f"- {item}" for item in candidate.samples[-MAX_TARGET_SAMPLES:])
    if not recent_samples:
        recent_samples = "- （最近 20 条里只有存在感，没有有效文本）"
    profile_text = _format_relation_profile(profile)
    rag_text = rag_context or "（没有检索到额外历史素材）"

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """你是QQ群里的整活文案代写。现在要生成“找男娘”工具的输出。
这里的“男娘”只是群聊玩笑标签，不是真实性别、性取向、性别认同或现实身份判断。
你可以基于材料胡编理由。
画像标签只能作为玩笑素材，不是现实身份判断依据。
输出中文纯文本，不用 Markdown，不要提数据库、RAG、模型或工具。
控制在 120 字以内，1 到 3 句。
第一句必须明确指出被抽中的人是群里的男娘，优先使用这个句式：群里的男娘就是：{user_name}。
后面再给 2 到 3 个荒诞但轻量的理由。""",
            ),
            (
                "user",
                """被抽中的人：{user_name}

【最近 20 条内此人的发言素材】
{recent_samples}

【bot 对此人的 v2 画像】
{profile_text}

【可选历史素材】
{rag_context}

请生成最终群聊消息：""",
            ),
        ]
    )

    try:
        chain = prompt | ctx.model
        response = await asyncio.wait_for(
            chain.ainvoke(
                {
                    "user_name": candidate.user_name,
                    "recent_samples": recent_samples,
                    "profile_text": profile_text,
                    "rag_context": rag_text,
                }
            ),
            timeout=45.0,
        )
        content = response.content if isinstance(response.content, str) else ""
        return _ensure_playful_frame(candidate, content, profile)
    except Exception as e:
        logger.warning(f"找男娘工具调用模型失败，使用兜底文案: {e}")
        return _fallback_reply(candidate, profile)


async def _is_current_request_active(ctx: OptionalToolContext) -> bool:
    if ctx.request_id is None:
        return True
    return await is_request_active(ctx.session_id, ctx.request_id)


def _build_send_message(
    content: str,
    *,
    mention_user_id: str | None = None,
) -> UniMessage:
    if mention_user_id:
        return UniMessage.at(mention_user_id).text(f" {content}")
    return UniMessage.text(content)


def _build_record_content(
    content: str,
    *,
    mention_user_id: str | None = None,
    mention_name: str | None = None,
) -> str:
    if not mention_user_id:
        return content
    display_name = _safe_text(mention_name) or mention_user_id
    return f"@{display_name} {content}".strip()


async def _send_and_record(
    ctx: OptionalToolContext,
    content: str,
    *,
    mention_user_id: str | None = None,
    mention_name: str | None = None,
) -> str:
    if not await _is_current_request_active(ctx):
        return "请求已过期，已取消发送。"

    message = _build_send_message(content, mention_user_id=mention_user_id)
    result = await message.send()
    if ctx.request_id is not None:
        mark_request_sent(ctx.session_id, ctx.request_id)

    msg_id = result.msg_ids[-1]["message_id"] if result.msg_ids else "unknown"
    bot_name = getattr(ctx.config, "bot_name", None) or "bot"
    bot_user_id = str(ctx.bot_id or bot_name)
    record_content = _build_record_content(
        content,
        mention_user_id=mention_user_id,
        mention_name=mention_name,
    )
    async with get_session() as db_session:
        chat_history = ChatHistory(
            session_id=ctx.session_id,
            user_id=bot_user_id,
            content_type="bot",
            content=f"id: {msg_id}\n{record_content}",
            user_name=bot_name,
        )
        db_session.add(chat_history)
        await db_session.commit()

    logger.info(f"找男娘工具已发送: {record_content}")
    return "已随机找出并发送。"


def create_find_femboy_tool(ctx: OptionalToolContext):
    @tool("find_femboy_in_recent_chat")
    async def find_femboy_in_recent_chat() -> str:
        """
        从最近 20 条聊天记录中的非 bot 发言者里纯随机抽一个人，指认为“群里的男娘”，并发送一条群聊整活文案。
        只有用户明确要求“找男娘 / 抓男娘 / 谁是男娘 / 群里的男娘”等玩笑场景时才使用。
        """
        if not await _is_current_request_active(ctx):
            return "请求已过期，已取消发送。"

        try:
            candidates = _collect_candidates(ctx.history or [])
            if not candidates:
                return await _send_and_record(ctx, "最近 20 条里没抓到可随机点名的人，群里的男娘暂时缺席。")

            candidate = random.choice(candidates)
            profile = await _load_relation_profile(candidate)
            rag_context = await _search_rag_context(ctx, candidate, profile)

            if not await _is_current_request_active(ctx):
                return "请求已过期，已取消发送。"

            reply = await _generate_reply(ctx, candidate, rag_context, profile)

            if not await _is_current_request_active(ctx):
                return "请求已过期，已取消发送。"

            return await _send_and_record(
                ctx,
                reply,
                mention_user_id=candidate.user_id,
                mention_name=candidate.user_name,
            )
        except Exception as e:
            logger.error(f"找男娘工具执行失败: {e}")
            traceback.print_exc()
            return f"找男娘失败: {type(e).__name__}: {e}"

    return find_femboy_in_recent_chat


async def healthcheck(ctx: OptionalToolContext) -> tuple[bool, str]:
    return True, "ok"


async def build(ctx: OptionalToolContext) -> OptionalToolBundle:
    if getattr(ctx, "is_cross_user_direct_reply", False):
        return OptionalToolBundle(name="find_femboy")

    return OptionalToolBundle(
        name="find_femboy",
        tools=[create_find_femboy_tool(ctx)],
        skills=[
            AgentSkill(
                name="find_femboy",
                description="用户明确要求在群聊中找男娘、抓男娘或进行相关整活时使用。",
                prompt=SKILL_PROMPT,
                tool_names=("find_femboy_in_recent_chat",),
            )
        ],
        tool_limits=[ToolLimitSpec(tool_name="find_femboy_in_recent_chat", run_limit=1)],
    )
