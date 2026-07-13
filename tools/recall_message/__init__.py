from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any

from langchain.tools import tool
from nonebot import get_bot
from nonebot.log import logger
from pydantic import BaseModel, Field

_optional_tools_module = sys.modules.get("nonebot_plugin_groupmate_agent.agent.optional_tools")
_optional_types_module = sys.modules.get("nonebot_plugin_groupmate_agent.agent.optional_tools.types")
if _optional_tools_module is not None:
    AgentSkill = _optional_tools_module.AgentSkill
    OptionalToolBundle = _optional_tools_module.OptionalToolBundle
    OptionalToolContext = _optional_tools_module.OptionalToolContext
    ToolLimitSpec = _optional_tools_module.ToolLimitSpec
elif _optional_types_module is not None:
    AgentSkill = _optional_types_module.AgentSkill
    OptionalToolBundle = _optional_types_module.OptionalToolBundle
    OptionalToolContext = _optional_types_module.OptionalToolContext
    ToolLimitSpec = _optional_types_module.ToolLimitSpec
else:

    @dataclass(frozen=True)
    class ToolLimitSpec:
        tool_name: str | None
        run_limit: int

    @dataclass(frozen=True)
    class AgentSkill:
        name: str
        description: str
        prompt: Any
        tool_names: tuple[str, ...] = ()

    @dataclass
    class OptionalToolBundle:
        name: str
        tools: list[Any] | None = None
        prompt: str = ""
        skills: list[AgentSkill] | None = None
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
        recent_forward_messages: list[dict[str, Any]] | None = None
        db_session: Any | None = None
        detach_request: Any = None
        can_continue: Any = None
        mark_sent: Any = None
        clear_detached: Any = None
        create_detached_task: Any = None
        send_target: Any = None
        is_private: bool = False
        bot: Any = None
        event: Any = None


GET_MSG_TIMEOUT_SECONDS = 10.0
DELETE_MSG_TIMEOUT_SECONDS = 10.0
MAX_SNIPPET_LENGTH = 120


class RecallReasonCategory(str, Enum):
    BOT_MISTAKE = "bot_mistake"
    SPAM_OR_AD = "spam_or_ad"
    SCAM_OR_MALICIOUS_LINK = "scam_or_malicious_link"
    PRIVACY_LEAK = "privacy_leak"
    SEXUAL_VIOLENT_OR_ILLEGAL = "sexual_violent_or_illegal"
    HARASSMENT_OR_HATE = "harassment_or_hate"
    MALICIOUS_DISRUPTION = "malicious_disruption"
    USER_SELF_SENSITIVE = "user_self_sensitive"


class RecallMessageArgs(BaseModel):
    target_msg_id: str | None = Field(
        default=None,
        description=(
            "要撤回的 OneBot message_id。用户回复某条消息并要求处理时可以不填；"
            "用户说“撤回你刚才那条”且目标是 bot 最近一条消息时也可以不填。"
        ),
    )
    reason_category: RecallReasonCategory = Field(
        description=(
            "撤回理由类别，只能选择确实匹配的一项：bot_mistake、spam_or_ad、"
            "scam_or_malicious_link、privacy_leak、sexual_violent_or_illegal、"
            "harassment_or_hate、malicious_disruption、user_self_sensitive。"
        )
    )
    reason: str = Field(description="bot 自己判断为什么应该撤回。不能只写“用户要求”。")
    evidence: str = Field(description="支持撤回的具体依据，例如消息内容、上下文行为或风险点。")
    requested_by_user: bool = Field(
        default=False,
        description="本轮是否有人提出撤回请求。注意：用户请求本身不是合理理由。",
    )


class RecallMessageError(RuntimeError):
    pass


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _safe_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _category_value(value: Any) -> str:
    if isinstance(value, RecallReasonCategory):
        return value.value
    return _safe_text(value)


def _get_item_attr(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _normalize_message_id(value: Any) -> str:
    text = _safe_text(value)
    match = re.search(r"(?:msg_id|message_id|id)?\s*[:=：]?\s*(\d{1,32})", text, re.I)
    return match.group(1) if match else ""


def _extract_numeric_id(raw: Any) -> int | None:
    match = re.search(r"(\d+)$", str(raw or "").strip())
    return int(match.group(1)) if match else None


def _parse_history_message_id(content: Any) -> str:
    text = str(content or "")
    for line in text.splitlines()[:3]:
        match = re.match(r"\s*id\s*[:：]\s*(\d{1,32})\s*$", line)
        if match:
            return match.group(1)
    return ""


def _parse_history_reply_id(content: Any) -> str:
    text = str(content or "")
    for line in text.splitlines()[:4]:
        match = re.match(r"\s*回复id\s*[:：]\s*(\d{1,32})\s*$", line)
        if match:
            return match.group(1)
    return ""


def _reply_target_id_from_context(ctx: OptionalToolContext) -> str:
    for target in getattr(ctx, "direct_targets", None) or []:
        reply_id = _normalize_message_id(target.get("reply_to_message_id"))
        if reply_id:
            return reply_id

    history = getattr(ctx, "history", None) or []
    if history:
        latest = history[-1]
        reply_id = _parse_history_reply_id(_get_item_attr(latest, "content", ""))
        if reply_id:
            return reply_id

    event = getattr(ctx, "event", None)
    if event is None:
        return ""

    for attr in ("reply", "source"):
        obj = getattr(event, attr, None)
        reply_id = _normalize_message_id(
            getattr(obj, "message_id", None)
            or getattr(obj, "id", None)
            or (obj.get("message_id") if isinstance(obj, dict) else None)
            or (obj.get("id") if isinstance(obj, dict) else None)
        )
        if reply_id:
            return reply_id

    return ""


def _recent_bot_message_id(ctx: OptionalToolContext) -> str:
    bot_id = _safe_text(getattr(ctx, "bot_id", None))
    bot_name = _safe_text(getattr(getattr(ctx, "config", None), "bot_name", None))

    for item in reversed(getattr(ctx, "history", None) or []):
        content_type = _safe_text(_get_item_attr(item, "content_type", ""))
        if content_type != "bot":
            continue

        user_id = _safe_text(_get_item_attr(item, "user_id", ""))
        user_name = _safe_text(_get_item_attr(item, "user_name", ""))
        if bot_id and user_id and user_id != bot_id:
            continue
        if not bot_id and bot_name and user_name and user_name != bot_name:
            continue

        msg_id = _parse_history_message_id(_get_item_attr(item, "content", ""))
        if msg_id:
            return msg_id

    return ""


def _resolve_target_msg_id(ctx: OptionalToolContext, target_msg_id: str | None) -> tuple[str, str]:
    normalized = _normalize_message_id(target_msg_id)
    if normalized:
        return normalized, "explicit_message_id"

    reply_id = _reply_target_id_from_context(ctx)
    if reply_id:
        return reply_id, "reply_target"

    recent_bot_id = _recent_bot_message_id(ctx)
    if recent_bot_id:
        return recent_bot_id, "recent_bot_message"

    raise RecallMessageError("没有找到要撤回的消息 id。请只撤回当前回复目标、明确给出的 message_id，或 bot 最近一条消息。")


def _get_context_bot(ctx: OptionalToolContext) -> Any | None:
    bot = getattr(ctx, "bot", None)
    if bot is not None:
        return bot

    bot_id = _safe_text(getattr(ctx, "bot_id", None))
    if not bot_id:
        return None

    try:
        return get_bot(bot_id)
    except Exception as e:
        logger.debug(f"撤回工具获取 bot 失败: {type(e).__name__}: {e}")
        return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _can_continue(ctx: OptionalToolContext) -> bool:
    checker = getattr(ctx, "can_continue", None)
    if checker is None:
        return True
    try:
        return bool(await _maybe_await(checker()))
    except Exception as e:
        logger.debug(f"撤回工具检查请求状态失败: {type(e).__name__}: {e}")
        return True


async def _get_msg_payload(bot: Any, message_id: str) -> dict[str, Any]:
    msg_id = int(message_id)
    if hasattr(bot, "get_msg"):
        payload = await asyncio.wait_for(bot.get_msg(message_id=msg_id), timeout=GET_MSG_TIMEOUT_SECONDS)
    elif hasattr(bot, "call_api"):
        payload = await asyncio.wait_for(
            bot.call_api("get_msg", message_id=msg_id),
            timeout=GET_MSG_TIMEOUT_SECONDS,
        )
    else:
        raise RecallMessageError("当前 bot 不支持 get_msg，无法确认撤回对象。")

    if not isinstance(payload, dict):
        raise RecallMessageError("get_msg 返回格式不支持，无法确认撤回对象。")
    return payload


async def _delete_msg(bot: Any, message_id: str) -> None:
    msg_id = int(message_id)
    if hasattr(bot, "delete_msg"):
        await asyncio.wait_for(bot.delete_msg(message_id=msg_id), timeout=DELETE_MSG_TIMEOUT_SECONDS)
        return
    if hasattr(bot, "call_api"):
        await asyncio.wait_for(
            bot.call_api("delete_msg", message_id=msg_id),
            timeout=DELETE_MSG_TIMEOUT_SECONDS,
        )
        return
    raise RecallMessageError("当前 bot 不支持 delete_msg。")


def _message_segment_text(segment: Any) -> str:
    if isinstance(segment, str):
        return segment
    if not isinstance(segment, dict):
        return _safe_text(segment)

    seg_type = _safe_text(segment.get("type"))
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}

    if seg_type == "text":
        return _safe_text(data.get("text"))
    if seg_type == "at":
        return f"@{_safe_text(data.get('qq')) or '某人'}"
    if seg_type == "image":
        return "[图片]"
    if seg_type == "face":
        return "[表情]"
    if seg_type == "reply":
        return "[回复]"
    if seg_type == "forward":
        return "[合并转发]"
    if seg_type:
        return f"[{seg_type}]"
    return _safe_text(segment)


def _message_snippet(payload: dict[str, Any]) -> str:
    raw = payload.get("raw_message")
    if raw:
        text = _safe_text(raw)
    else:
        message = payload.get("message")
        if isinstance(message, list):
            text = _safe_text(" ".join(_message_segment_text(seg) for seg in message))
        else:
            text = _safe_text(message)

    if not text:
        text = "[无法读取文本内容]"
    if len(text) > MAX_SNIPPET_LENGTH:
        return text[: MAX_SNIPPET_LENGTH - 3].rstrip() + "..."
    return text


def _sender_user_id(payload: dict[str, Any]) -> str:
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    return _safe_text(payload.get("user_id") or sender.get("user_id"))


def _sender_name(payload: dict[str, Any]) -> str:
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    return (
        _safe_text(sender.get("card"))
        or _safe_text(sender.get("nickname"))
        or _safe_text(sender.get("user_id"))
        or "未知用户"
    )


def _sender_role(payload: dict[str, Any]) -> str:
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    return _safe_text(sender.get("role")).lower()


def _is_same_group(ctx: OptionalToolContext, payload: dict[str, Any]) -> bool:
    payload_group_id = _extract_numeric_id(payload.get("group_id"))
    if payload_group_id is None:
        return True

    session_group_id = _extract_numeric_id(getattr(ctx, "session_id", None))
    if session_group_id is None:
        return True
    return payload_group_id == session_group_id


def _has_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    lower_text = text.lower()
    return any(keyword.lower() in lower_text for keyword in keywords)


CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    RecallReasonCategory.BOT_MISTAKE.value: (
        "发错",
        "误发",
        "重复",
        "答错",
        "不合适",
        "泄露",
        "跑题",
        "刷屏",
        "多发",
    ),
    RecallReasonCategory.SPAM_OR_AD.value: (
        "广告",
        "刷屏",
        "推广",
        "引流",
        "群发",
        "spam",
        "ad",
        "二维码",
        "重复",
    ),
    RecallReasonCategory.SCAM_OR_MALICIOUS_LINK.value: (
        "诈骗",
        "钓鱼",
        "盗号",
        "木马",
        "恶意链接",
        "返利",
        "中奖",
        "http",
        "链接",
        "收款",
    ),
    RecallReasonCategory.PRIVACY_LEAK.value: (
        "隐私",
        "泄露",
        "手机号",
        "电话",
        "地址",
        "住址",
        "身份证",
        "实名",
        "银行卡",
        "定位",
    ),
    RecallReasonCategory.SEXUAL_VIOLENT_OR_ILLEGAL.value: (
        "色情",
        "涉黄",
        "裸露",
        "血腥",
        "暴力",
        "违法",
        "毒品",
        "赌博",
        "枪",
        "犯罪",
    ),
    RecallReasonCategory.HARASSMENT_OR_HATE.value: (
        "骚扰",
        "辱骂",
        "仇恨",
        "歧视",
        "人身攻击",
        "威胁",
        "网暴",
        "恶意攻击",
    ),
    RecallReasonCategory.MALICIOUS_DISRUPTION.value: (
        "恶意",
        "捣乱",
        "带节奏",
        "引战",
        "轰炸",
        "破坏",
        "刷屏",
        "冒充",
    ),
    RecallReasonCategory.USER_SELF_SENSITIVE.value: (
        "自己",
        "本人",
        "误发",
        "发错",
        "敏感",
        "隐私",
        "照片",
        "手机号",
        "地址",
        "身份证",
    ),
}

GENERIC_REQUEST_PATTERNS = (
    "用户要求",
    "有人要求",
    "他说撤回",
    "她说撤回",
    "让撤回",
    "要求撤回",
    "想撤回",
)

TOO_GENERIC_REASONS = {
    "撤回",
    "删掉",
    "删除",
    "不合适",
    "违规",
    "不好",
    "用户要求",
    "有人要求",
    "按要求",
}


def _is_specific_reason(reason: str, evidence: str) -> bool:
    reason_text = _safe_text(reason)
    evidence_text = _safe_text(evidence)
    combined = f"{reason_text} {evidence_text}".strip()
    if reason_text in TOO_GENERIC_REASONS or evidence_text in TOO_GENERIC_REASONS:
        return False
    if len(combined) < 8:
        return False
    return True


def _is_request_only(reason: str, evidence: str) -> bool:
    combined = f"{_safe_text(reason)} {_safe_text(evidence)}"
    return _has_any_keyword(combined, GENERIC_REQUEST_PATTERNS) and not _has_any_keyword(
        combined,
        (
            "隐私",
            "敏感",
            "广告",
            "刷屏",
            "诈骗",
            "涉黄",
            "暴力",
            "骚扰",
            "威胁",
            "发错",
            "误发",
            "重复",
        ),
    )


def _validate_reasonableness(
    *,
    ctx: OptionalToolContext,
    payload: dict[str, Any],
    target_msg_id: str,
    target_source: str,
    reason_category: str,
    reason: str,
    evidence: str,
    requested_by_user: bool,
) -> dict[str, Any]:
    sender_id = _sender_user_id(payload)
    sender_name = _sender_name(payload)
    sender_role = _sender_role(payload)
    bot_id = _safe_text(getattr(ctx, "bot_id", None))
    current_user_id = _safe_text(getattr(ctx, "user_id", None))
    snippet = _message_snippet(payload)
    target_is_bot = bool(bot_id and sender_id and sender_id == bot_id)
    category = _category_value(reason_category)
    combined_for_keywords = f"{reason} {evidence} {snippet}"

    if category not in CATEGORY_KEYWORDS:
        raise RecallMessageError("撤回理由类别不在允许范围内。")
    if not _is_specific_reason(reason, evidence):
        raise RecallMessageError("撤回理由太泛，必须给出具体依据。")
    if _is_request_only(reason, evidence):
        raise RecallMessageError("用户要求本身不足以构成撤回理由。")
    if not _is_same_group(ctx, payload):
        raise RecallMessageError("目标消息不属于当前会话，拒绝撤回。")

    if target_source == "recent_bot_message" and not target_is_bot:
        raise RecallMessageError("只能把最近 bot 消息作为默认撤回目标。")

    if not target_is_bot:
        if not getattr(ctx, "has_admin_permission", False):
            raise RecallMessageError("bot 没有管理权限，不能撤回他人消息。")
        if getattr(ctx, "is_cross_user_direct_reply", False):
            raise RecallMessageError("多用户交叉直接回复场景不执行撤回他人消息。")
        if sender_role in {"owner", "admin"}:
            raise RecallMessageError("目标消息来自群主或管理员，拒绝撤回。")
        if category == RecallReasonCategory.BOT_MISTAKE.value:
            raise RecallMessageError("bot_mistake 只能用于撤回 bot 自己的消息。")

    if category == RecallReasonCategory.USER_SELF_SENSITIVE.value:
        if target_is_bot:
            raise RecallMessageError("user_self_sensitive 只能用于撤回用户自己的敏感消息。")
        if not requested_by_user:
            raise RecallMessageError("撤回用户本人敏感消息时，必须是用户本人提出或明确同意。")
        if not current_user_id or sender_id != current_user_id:
            raise RecallMessageError("只能按用户本人意愿撤回他自己发出的敏感消息。")

    if category != RecallReasonCategory.BOT_MISTAKE.value:
        keywords = CATEGORY_KEYWORDS[category]
        if not _has_any_keyword(combined_for_keywords, keywords):
            raise RecallMessageError("撤回类别与理由、证据或消息内容不匹配。")

    return {
        "target_msg_id": target_msg_id,
        "target_source": target_source,
        "target_sender_id": sender_id,
        "target_sender_name": sender_name,
        "target_sender_role": sender_role,
        "target_is_bot": target_is_bot,
        "message_snippet": snippet,
        "reason_category": category,
        "reason": _safe_text(reason),
        "evidence": _safe_text(evidence),
        "requested_by_user": requested_by_user,
    }


def create_recall_tool(ctx: OptionalToolContext):
    @tool("recall_message", args_schema=RecallMessageArgs)
    async def recall_message(
        target_msg_id: str | None = None,
        reason_category: str = "",
        reason: str = "",
        evidence: str = "",
        requested_by_user: bool = False,
    ) -> str:
        """
        撤回一条消息。调用前必须先由 bot 自己判断撤回是否合理。

        用户要求撤回不等于应该撤回；工具会拒绝缺少具体依据或类别不匹配的撤回。

        Args:
            target_msg_id: 要撤回的 OneBot message_id。可为空，工具会优先使用当前回复目标，其次是 bot 最近一条消息。
            reason_category: 撤回理由类别。
            reason: bot 自己给出的撤回理由，不能只写“用户要求”。
            evidence: 具体依据。
            requested_by_user: 是否有人提出撤回请求。
        """
        if not await _can_continue(ctx):
            return _json_result({"ok": False, "status": "expired", "error": "请求已过期，已取消撤回。"})

        bot = _get_context_bot(ctx)
        if bot is None:
            return _json_result({"ok": False, "status": "failed", "error": "无法获取 bot 实例。"})

        try:
            resolved_msg_id, target_source = _resolve_target_msg_id(ctx, target_msg_id)
            payload = await _get_msg_payload(bot, resolved_msg_id)
            decision = _validate_reasonableness(
                ctx=ctx,
                payload=payload,
                target_msg_id=resolved_msg_id,
                target_source=target_source,
                reason_category=reason_category,
                reason=reason,
                evidence=evidence,
                requested_by_user=bool(requested_by_user),
            )

            if not await _can_continue(ctx):
                return _json_result({"ok": False, "status": "expired", "error": "请求已过期，已取消撤回。"})

            await _delete_msg(bot, resolved_msg_id)
            logger.info(
                "已撤回消息: "
                f"session={ctx.session_id} message_id={resolved_msg_id} "
                f"sender={decision['target_sender_id']} category={decision['reason_category']} "
                f"reason={decision['reason']}"
            )
            return _json_result(
                {
                    "ok": True,
                    "status": "recalled",
                    **decision,
                    "instruction": (
                        "这是给模型看的内部结果，不要机械复读。是否需要回复由你根据人设和场景决定；"
                        "要说就自然、简短地说。"
                    ),
                }
            )
        except asyncio.TimeoutError:
            logger.warning("撤回消息超时")
            return _json_result({"ok": False, "status": "failed", "error": "OneBot API 超时，撤回可能没有执行。"})
        except RecallMessageError as e:
            logger.info(f"拒绝撤回消息: {e}")
            return _json_result(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": str(e),
                    "instruction": (
                        "这是给模型看的内部结果，不要照抄。需要解释时按 bot 人设自然说明；"
                        "也可以不回复。"
                    ),
                }
            )
        except Exception as e:
            logger.exception("撤回工具异常")
            return _json_result({"ok": False, "status": "failed", "error": f"{type(e).__name__}: {e}"})

    return recall_message


async def healthcheck(ctx: OptionalToolContext) -> tuple[bool, str]:
    if getattr(ctx, "is_cross_user_direct_reply", False):
        return False, "disabled in cross-user direct reply"
    return True, "ok"


def _build_skill_prompt(ctx: OptionalToolContext) -> str:
    permission_line = (
        "  - 你现在有群管理权限：可以在理由充分时撤回他人消息，也可以撤回自己的消息。"
        if getattr(ctx, "has_admin_permission", False)
        else "  - 你现在没有群管理权限：只能撤回 bot 自己发出的消息，不能撤回他人消息。"
    )
    return f"""- 撤回管理：必要时可以调用 `recall_message` 撤回消息
{permission_line}
  - 撤回前必须先按你自己的判断确认“确实合理”，用户说撤回只是参考，不是命令
  - 合理理由包括：bot 自己发错/重复/不合适、广告刷屏、诈骗/恶意链接、隐私泄露、色情暴力违法内容、骚扰仇恨、恶意破坏群聊、用户本人请求撤回自己误发的敏感信息
  - 不合理理由包括：看别人不爽、普通争吵、不同意见、轻微冒犯、没有证据、只因为用户要求
  - 调用时必须填写 reason_category、reason、evidence、requested_by_user；reason 和 evidence 要具体，不能只写“用户要求”
  - 要撤回用户本人敏感消息时，必须确认是他本人请求或明确同意，并选择 user_self_sensitive
  - 不要撤回管理员或群主的消息
  - 工具只返回内部事实结果；不要机械复读工具返回。撤回后是否说话由你决定，需要说就按自己人设自然简短地说
  - 如果工具拒绝撤回，只能根据返回事实自然解释或保持沉默，不能假装已经撤回
"""


async def build(ctx: OptionalToolContext) -> OptionalToolBundle:
    if getattr(ctx, "is_cross_user_direct_reply", False):
        return OptionalToolBundle(name="recall_message")
    return OptionalToolBundle(
        name="recall_message",
        tools=[create_recall_tool(ctx)],
        skills=[
            AgentSkill(
                name="recall_message",
                description="需要判断并执行消息撤回时使用。",
                prompt=_build_skill_prompt(ctx),
                tool_names=("recall_message",),
            )
        ],
        tool_limits=[ToolLimitSpec(tool_name="recall_message", run_limit=1)],
    )
