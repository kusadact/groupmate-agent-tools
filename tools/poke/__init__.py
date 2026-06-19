from __future__ import annotations

import re
import sys
import traceback
from dataclasses import dataclass
from typing import Any

from langchain.tools import tool
from nonebot import get_bot
from nonebot.log import logger
from pydantic import BaseModel, Field

_optional_tools_module = sys.modules.get("nonebot_plugin_groupmate_agent.agent.optional_tools")
_optional_types_module = sys.modules.get("nonebot_plugin_groupmate_agent.agent.optional_tools.types")
if _optional_tools_module is not None:
    OptionalToolBundle = _optional_tools_module.OptionalToolBundle
    OptionalToolContext = _optional_tools_module.OptionalToolContext
    ToolLimitSpec = _optional_tools_module.ToolLimitSpec
elif _optional_types_module is not None:
    OptionalToolBundle = _optional_types_module.OptionalToolBundle
    OptionalToolContext = _optional_types_module.OptionalToolContext
    ToolLimitSpec = _optional_types_module.ToolLimitSpec
else:

    @dataclass(frozen=True)
    class ToolLimitSpec:
        tool_name: str | None
        run_limit: int

    @dataclass
    class OptionalToolBundle:
        name: str
        tools: list[Any] | None = None
        prompt: str = ""
        tool_limits: list[ToolLimitSpec] | None = None

    @dataclass
    class OptionalToolContext:
        session_id: str
        request_id: str | None = None
        user_id: str | None = None
        user_name: str | None = None
        interface: Any = None
        bot_id: str | None = None
        history: list[Any] | None = None
        direct_targets: list[dict[str, Any]] | None = None
        emoji_like_candidate_ids: set[str] | None = None
        has_direct_targets: bool = False
        is_multi_direct_reply: bool = False
        is_cross_user_direct_reply: bool = False
        has_admin_permission: bool = False
        config: Any = None
        model: Any = None
        stop_words: list[str] | None = None
        detach_request: Any = None
        can_continue: Any = None
        mark_sent: Any = None
        clear_detached: Any = None
        create_detached_task: Any = None
        bot: Any = None
        event: Any = None


_reply_guard_module = sys.modules.get("nonebot_plugin_groupmate_agent.reply_guard")
if _reply_guard_module is not None:
    _can_request_continue = _reply_guard_module.can_request_continue
else:

    async def _can_request_continue(session_id: str, request_id: str) -> bool:
        return True


def _scene_type_group() -> Any | None:
    uninfo_module = sys.modules.get("nonebot_plugin_uninfo")
    if uninfo_module is not None:
        return getattr(getattr(uninfo_module, "SceneType", None), "GROUP", None)
    return None


PROMPT = """- 戳一戳：当用户明确要求 bot 戳一下 / poke / 拍一拍某个群友时，可以调用 `poke_user`
  - `target_user_name` 填目标昵称、QQ 号或用户说出的称呼；用户要求“戳我/戳自己”时填“我”
  - 一次只戳一个人；需要戳多人时分别调用，最多调用 3 次
  - 工具会真的发送戳一戳动作，不会发送文字；成功后直接调用 `finish`，不要复述“已戳”
  - 如果工具返回失败，只能用 `reply_user` 简短说明失败原因，不要假装成功
"""

SELF_TARGET_WORDS = {"我", "我自己", "自己", "me", "self"}
REPLIED_TARGET_WORDS = {
    "他",
    "她",
    "它",
    "ta",
    "TA",
    "这个人",
    "这人",
    "那个人",
    "那人",
    "对方",
    "楼上",
    "被回复的人",
    "回复的人",
}


class PokeArgs(BaseModel):
    target_user_name: str = Field(description="要戳的目标昵称、QQ 号，或“我/自己”等明确称呼")


@dataclass(frozen=True)
class ResolvedTarget:
    user_id: str
    display_name: str
    from_group_member: bool = False


def _safe_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_target_name(value: str) -> str:
    text = _safe_text(value).strip()
    cq_at = re.search(r"\[CQ:at,qq=(\d+)", text)
    if cq_at:
        return cq_at.group(1)

    text = re.sub(r"^@+", "", text).strip()
    if text.startswith("qq="):
        text = text[3:].strip()
    return text


def _extract_numeric_id(raw: Any) -> int | None:
    match = re.search(r"(\d+)$", str(raw or ""))
    return int(match.group(1)) if match else None


def _member_aliases(member: Any) -> set[str]:
    values = {
        getattr(member, "id", None),
        getattr(member, "name", None),
        getattr(member, "nick", None),
        getattr(member, "card", None),
        getattr(member, "remark", None),
        getattr(getattr(member, "user", None), "id", None),
        getattr(getattr(member, "user", None), "name", None),
        getattr(getattr(member, "user", None), "nick", None),
    }
    return {_normalize_target_name(_safe_text(value)) for value in values if _safe_text(value)}


def _member_display_name(member: Any) -> str:
    for value in (
        getattr(member, "nick", None),
        getattr(member, "card", None),
        getattr(member, "name", None),
        getattr(getattr(member, "user", None), "nick", None),
        getattr(getattr(member, "user", None), "name", None),
        getattr(member, "id", None),
    ):
        text = _safe_text(value)
        if text:
            return text
    return "未知用户"


async def _get_group_members(ctx: OptionalToolContext) -> list[Any]:
    if ctx.interface is None:
        return []
    scene_type_group = _scene_type_group()
    if scene_type_group is None:
        return []
    try:
        return list(await ctx.interface.get_members(scene_type_group, ctx.session_id))
    except Exception as e:
        logger.debug(f"poke 工具获取群成员失败: {type(e).__name__}: {e}")
        return []


def _target_from_user_id(
    ctx: OptionalToolContext,
    user_id: str,
    *,
    display_name: str | None = None,
) -> ResolvedTarget | None:
    target_id = _safe_text(user_id)
    if not target_id:
        return None
    return ResolvedTarget(
        user_id=target_id,
        display_name=_safe_text(display_name) or target_id,
        from_group_member=False,
    )


def _target_from_member(member: Any) -> ResolvedTarget | None:
    user_id = _safe_text(getattr(member, "id", None))
    if not user_id:
        user_id = _safe_text(getattr(getattr(member, "user", None), "id", None))
    if not user_id:
        return None
    return ResolvedTarget(user_id=user_id, display_name=_member_display_name(member), from_group_member=True)


def _parse_history_platform_msg_id(content: str) -> str:
    first_line = str(content or "").splitlines()[0:1]
    if not first_line:
        return ""
    match = re.match(r"id:\s*(.+)", first_line[0])
    return match.group(1).strip() if match else ""


def _event_reply_id(event: Any) -> str:
    reply = getattr(event, "reply", None)
    if reply is None:
        return ""
    if isinstance(reply, dict):
        return _safe_text(reply.get("message_id") or reply.get("id"))
    for attr in ("message_id", "id"):
        text = _safe_text(getattr(reply, attr, None))
        if text:
            return text
    return ""


def _find_replied_target(ctx: OptionalToolContext) -> ResolvedTarget | None:
    reply_ids: list[str] = []
    for target in ctx.direct_targets or []:
        reply_id = _safe_text(target.get("reply_to_message_id"))
        if reply_id:
            reply_ids.append(reply_id)

    event = getattr(ctx, "event", None)
    event_reply = _event_reply_id(event)
    if event_reply:
        reply_ids.append(event_reply)

    seen_reply_ids = {item for item in reply_ids if item}
    if not seen_reply_ids:
        return None

    for msg in reversed(ctx.history or []):
        content = str(getattr(msg, "content", "") or "")
        platform_msg_id = _parse_history_platform_msg_id(content)
        db_msg_id = _safe_text(getattr(msg, "msg_id", None))
        if platform_msg_id not in seen_reply_ids and db_msg_id not in seen_reply_ids:
            continue
        if _safe_text(getattr(msg, "content_type", "")) == "bot":
            continue
        return _target_from_user_id(
            ctx,
            _safe_text(getattr(msg, "user_id", "")),
            display_name=_safe_text(getattr(msg, "user_name", "")),
        )
    return None


def _match_direct_target(ctx: OptionalToolContext, normalized: str) -> ResolvedTarget | None:
    matches: list[ResolvedTarget] = []
    for target in ctx.direct_targets or []:
        target_id = _safe_text(target.get("user_id"))
        target_name = _safe_text(target.get("user_name"))
        if not target_id:
            continue
        aliases = {_normalize_target_name(target_id), _normalize_target_name(target_name)}
        if normalized in aliases:
            matches.append(ResolvedTarget(user_id=target_id, display_name=target_name or target_id))
    return matches[0] if len(matches) == 1 else None


def _match_group_member(members: list[Any], normalized: str) -> tuple[ResolvedTarget | None, str | None]:
    exact_matches: list[Any] = []
    for member in members:
        if normalized in _member_aliases(member):
            exact_matches.append(member)

    if len(exact_matches) == 1:
        return _target_from_member(exact_matches[0]), None
    if len(exact_matches) > 1:
        names = "、".join(_member_display_name(member) for member in exact_matches[:5])
        return None, f"目标“{normalized}”匹配到多人：{names}，请说得更具体一点。"

    if len(normalized) < 2:
        return None, None

    fuzzy_matches: list[Any] = []
    normalized_lower = normalized.lower()
    for member in members:
        aliases = _member_aliases(member)
        if any(normalized_lower in alias.lower() for alias in aliases):
            fuzzy_matches.append(member)

    if len(fuzzy_matches) == 1:
        return _target_from_member(fuzzy_matches[0]), None
    if len(fuzzy_matches) > 1:
        names = "、".join(_member_display_name(member) for member in fuzzy_matches[:5])
        return None, f"目标“{normalized}”有多个可能：{names}，请换成完整昵称或 QQ 号。"
    return None, None


async def _resolve_target(ctx: OptionalToolContext, raw_target: str) -> tuple[ResolvedTarget | None, str | None]:
    normalized = _normalize_target_name(raw_target)
    if not normalized:
        return None, "目标不能为空。"

    if normalized in SELF_TARGET_WORDS:
        if ctx.user_id:
            return _target_from_user_id(ctx, ctx.user_id, display_name=ctx.user_name), None
        if len(ctx.direct_targets or []) == 1:
            target = ctx.direct_targets[0]
            return _target_from_user_id(ctx, target.get("user_id"), display_name=target.get("user_name")), None
        return None, "本轮没有单一当前用户，无法判断要戳谁。"

    if normalized in REPLIED_TARGET_WORDS:
        replied_target = _find_replied_target(ctx)
        if replied_target is not None:
            return replied_target, None
        return None, "没有找到被回复消息对应的用户，无法判断要戳谁。"

    direct_target = _match_direct_target(ctx, normalized)
    if direct_target is not None:
        return direct_target, None

    members = await _get_group_members(ctx)
    group_target, group_error = _match_group_member(members, normalized)
    if group_target is not None or group_error:
        return group_target, group_error

    if normalized.isdigit():
        return ResolvedTarget(user_id=normalized, display_name=normalized), None

    return None, f"未找到用户“{normalized}”，请换成完整昵称或 QQ 号。"


def _get_context_bot(ctx: OptionalToolContext) -> Any | None:
    bot = getattr(ctx, "bot", None)
    if bot is not None:
        return bot
    if ctx.bot_id:
        try:
            return get_bot(ctx.bot_id)
        except Exception as e:
            logger.warning(f"poke 工具获取 bot 失败: {type(e).__name__}: {e}")
    return None


def _context_group_id(ctx: OptionalToolContext) -> int | None:
    event = getattr(ctx, "event", None)
    event_group_id = _extract_numeric_id(getattr(event, "group_id", None))
    if event_group_id is not None:
        return event_group_id
    message_type = _safe_text(getattr(event, "message_type", None))
    detail_type = _safe_text(getattr(event, "detail_type", None))
    if message_type == "private" or detail_type == "private":
        return None
    return _extract_numeric_id(ctx.session_id)


async def _try_api_variants(bot: Any, api_names: tuple[str, ...], payloads: list[dict[str, int]]) -> str | None:
    if not hasattr(bot, "call_api"):
        return None

    last_error: Exception | None = None
    for api_name in api_names:
        for payload in payloads:
            try:
                await bot.call_api(api_name, **payload)
                return api_name
            except Exception as e:
                last_error = e
                logger.debug(f"poke API 尝试失败 api={api_name} payload={payload}: {type(e).__name__}: {e}")
    if last_error is not None:
        logger.debug(f"所有 poke API 尝试均失败，准备 fallback: {type(last_error).__name__}: {last_error}")
    return None


async def _send_poke_segment(bot: Any, *, target_id: int, group_id: int | None) -> None:
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    poke = MessageSegment("poke", {"type": "qq", "id": str(target_id)})
    if group_id is not None and hasattr(bot, "send_group_msg"):
        await bot.send_group_msg(group_id=group_id, message=Message([poke]))
        return
    if group_id is None and hasattr(bot, "send_private_msg"):
        await bot.send_private_msg(user_id=target_id, message=Message([poke]))
        return
    raise RuntimeError("当前适配器不支持发送 poke 消息段。")


async def _send_poke(ctx: OptionalToolContext, target: ResolvedTarget) -> str:
    bot = _get_context_bot(ctx)
    if bot is None:
        return "无法获取 bot 实例，戳一戳失败。"

    target_id = _extract_numeric_id(target.user_id)
    if target_id is None:
        return f"目标用户 ID 无法转换为 QQ 号：{target.user_id}"

    self_id = _safe_text(getattr(bot, "self_id", None) or ctx.bot_id)
    if self_id and str(target_id) == self_id:
        return "不能戳 bot 自己。"

    group_id = _context_group_id(ctx)
    if group_id is not None:
        api_used = await _try_api_variants(
            bot,
            ("send_group_poke", "send_poke", "group_poke"),
            [
                {"group_id": group_id, "user_id": target_id},
                {"group_id": group_id, "target_id": target_id},
                {"group_id": group_id, "qq": target_id},
            ],
        )
        if api_used is not None:
            return "ok"

        try:
            await _send_poke_segment(bot, target_id=target_id, group_id=group_id)
            return "ok"
        except Exception as e:
            logger.warning(f"群 poke 消息段 fallback 失败: {type(e).__name__}: {e}")
            return f"戳一戳发送失败：{type(e).__name__}: {e}"

    api_used = await _try_api_variants(
        bot,
        ("send_friend_poke", "send_private_poke", "send_poke", "friend_poke"),
        [
            {"user_id": target_id},
            {"target_id": target_id},
            {"qq": target_id},
        ],
    )
    if api_used is not None:
        return "ok"

    try:
        await _send_poke_segment(bot, target_id=target_id, group_id=group_id)
        return "ok"
    except Exception as e:
        logger.warning(f"poke 消息段 fallback 失败: {type(e).__name__}: {e}")
        return f"戳一戳发送失败：{type(e).__name__}: {e}"


async def _can_continue(ctx: OptionalToolContext) -> bool:
    checker = getattr(ctx, "can_continue", None)
    if checker is not None:
        return bool(await checker())
    if ctx.request_id is None:
        return True
    return await _can_request_continue(ctx.session_id, ctx.request_id)


async def _record_poke_action(ctx: OptionalToolContext, target: ResolvedTarget) -> None:
    try:
        from nonebot_plugin_orm import get_session
        from nonebot_plugin_groupmate_agent.model import ChatHistory

        bot_name = getattr(ctx.config, "bot_name", None) or "bot"
        async with get_session() as db_session:
            db_session.add(
                ChatHistory(
                    session_id=ctx.session_id,
                    user_id=str(ctx.bot_id or bot_name),
                    content_type="bot",
                    content=(
                        "id: system\n"
                        f"系统记录：bot 戳了一下“{target.display_name}”（user_id: {target.user_id}）。"
                    ),
                    user_name=bot_name,
                )
            )
            await db_session.commit()
    except Exception as e:
        logger.warning(f"记录 poke 操作到聊天历史失败: {type(e).__name__}: {e}")


def _mark_sent(ctx: OptionalToolContext) -> None:
    marker = getattr(ctx, "mark_sent", None)
    if marker is not None:
        marker()


def create_poke_tool(ctx: OptionalToolContext):
    @tool("poke_user", args_schema=PokeArgs)
    async def poke_user(target_user_name: str) -> str:
        """
        发送 QQ 戳一戳给指定用户。

        只有用户明确要求“戳一下 / poke / 拍一拍”某人时使用。
        target_user_name 可以是昵称、QQ 号，或“我/自己”。
        """
        if not await _can_continue(ctx):
            return "请求已过期，已取消戳一戳。"

        try:
            target, error = await _resolve_target(ctx, target_user_name)
            if error:
                return error
            if target is None:
                return "无法确定要戳的目标。"

            if not await _can_continue(ctx):
                return "请求已过期，已取消戳一戳。"

            result = await _send_poke(ctx, target)
            if result != "ok":
                return result

            _mark_sent(ctx)
            await _record_poke_action(ctx, target)
            logger.info(f"poke 工具已发送: session_id={ctx.session_id} user_id={target.user_id}")
            return f"已戳一下“{target.display_name}”。"
        except Exception as e:
            logger.error(f"poke 工具执行失败: {e}")
            traceback.print_exc()
            return f"戳一戳失败: {type(e).__name__}: {e}"

    return poke_user


async def healthcheck(ctx: OptionalToolContext) -> tuple[bool, str]:
    return True, "ok"


async def build(ctx: OptionalToolContext) -> OptionalToolBundle:
    return OptionalToolBundle(
        name="poke",
        tools=[create_poke_tool(ctx)],
        prompt=PROMPT,
        tool_limits=[ToolLimitSpec(tool_name="poke_user", run_limit=3)],
    )
