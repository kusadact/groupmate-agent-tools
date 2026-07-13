from __future__ import annotations

import asyncio
import base64
import html
import json
import mimetypes
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage
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
        recent_forward_messages: list[dict[str, Any]] | None = None
        emoji_like_candidate_ids: set[str] | None = None
        has_direct_targets: bool = False
        is_multi_direct_reply: bool = False
        is_cross_user_direct_reply: bool = False
        has_admin_permission: bool = False
        config: Any = None
        model: Any = None
        stop_words: list[str] | None = None
        send_target: Any = None
        is_private: bool = False
        bot: Any = None
        event: Any = None
        can_continue: Any = None
        mark_sent: Any = None


SKILL_PROMPT = """- 合并转发阅读：只有用户主动要求“查看 / 读一下 / 总结 / 分析 / 看看”合并转发消息时，才调用 `read_forward_message`
  - 除非用户主动请求查看，不然不要调用本工具；不要因为当前消息里带有合并转发段就自动读取
  - 用户回复某条合并转发并要求查看时，可以不填参数；用户说“上面那个/刚才那个”时，也可以不填参数，工具会尝试找近期合并转发
  - 用户明确指定消息 id 时，把它填入 `target_msg_id`
  - 用户只是普通聊天、查历史、统计、看头像、看网页、看普通图片时，不要调用本工具
  - 工具一开始会直接发送“正在查看”的等待提示，然后读取合并转发、结合其中图片生成 100 字以内摘要和一句简短评价
  - 工具成功后会返回 `summary_to_send`；必须只调用一次 `reply_user` 原样发送 `summary_to_send`，然后调用 `finish`
  - 不要在 `reply_user` 里追加、改写或重复摘要；不要再发送第二条相同含义的总结
  - 如果工具返回失败，只能用 `reply_user` 简短说明失败原因，然后调用 `finish`
"""

WAIT_NOTICE = "我看看这条合并转发..."
FORWARD_API_TIMEOUT_SECONDS = 20.0
GET_MSG_TIMEOUT_SECONDS = 10.0
GET_IMAGE_TIMEOUT_SECONDS = 10.0
SUMMARY_TIMEOUT_SECONDS = 90.0
MAX_FORWARD_NODES = 100
MAX_TRANSCRIPT_CHARS = 12000
MAX_FORWARD_IMAGES = 6
MAX_SUMMARY_CHARS = 100
MAX_RECENT_MESSAGE_LOOKUP = 8


class ReadForwardMessageArgs(BaseModel):
    forward_id: str | None = Field(default=None, description="合并转发 ID。已知 forward.data.id 时填写。")
    target_msg_id: str | None = Field(
        default=None,
        description="包含合并转发的消息 id。用户明确指定“看这条消息/看我回复的那条”时填写。",
    )
    question: str | None = Field(default=None, description="用户对合并转发的具体问题；为空时默认简要总结。")


@dataclass(frozen=True)
class ForwardImageRef:
    label: str
    node_index: int
    sender: str
    image_url: str


@dataclass(frozen=True)
class ForwardReadResult:
    forward_id: str
    source: str
    transcript: str
    node_count: int
    read_count: int
    truncated_nodes: bool
    truncated_text: bool
    image_count: int
    images: tuple[ForwardImageRef, ...]


class ForwardMessageError(RuntimeError):
    pass


def _safe_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_id(value: Any) -> str:
    return _safe_text(value)


def _extract_model_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item.strip())
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


def _strip_code_fence(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:text|markdown)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _limit_summary(text: str) -> str:
    summary = re.sub(r"\s+", " ", _strip_code_fence(text)).strip()
    if len(summary) <= MAX_SUMMARY_CHARS:
        return summary
    return summary[: MAX_SUMMARY_CHARS - 3].rstrip() + "..."


def _message_id_from_result(result: Any) -> str:
    msg_ids = getattr(result, "msg_ids", None) or []
    if msg_ids:
        last = msg_ids[-1]
        if isinstance(last, dict):
            return str(last.get("message_id") or last.get("id") or "unknown")
        return str(getattr(last, "message_id", None) or getattr(last, "id", None) or last)
    return "unknown"


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
        logger.debug(f"合并转发工具获取 bot 失败: {type(e).__name__}: {e}")
        return None


def _send_target(ctx: OptionalToolContext) -> Any:
    target = getattr(ctx, "send_target", None)
    if target is not None:
        return target
    from nonebot_plugin_alconna import Target

    return Target(id=ctx.session_id, private=bool(getattr(ctx, "is_private", False)), self_id=ctx.bot_id)


async def _can_continue(ctx: OptionalToolContext) -> bool:
    can_continue = getattr(ctx, "can_continue", None)
    if can_continue is None:
        return True
    try:
        return bool(await can_continue())
    except Exception as e:
        logger.debug(f"合并转发工具检查请求状态失败: {type(e).__name__}: {e}")
        return True


def _mark_sent(ctx: OptionalToolContext) -> None:
    marker = getattr(ctx, "mark_sent", None)
    if marker is None:
        return
    try:
        marker()
    except Exception as e:
        logger.debug(f"合并转发工具标记已发送失败: {type(e).__name__}: {e}")


async def _send_text(ctx: OptionalToolContext, content: str) -> str:
    from nonebot_plugin_alconna import UniMessage

    result = await UniMessage.text(content).send(target=_send_target(ctx))
    return _message_id_from_result(result)


async def _send_wait_notice(ctx: OptionalToolContext) -> None:
    try:
        if await _can_continue(ctx):
            await _send_text(ctx, WAIT_NOTICE)
            _mark_sent(ctx)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug(f"发送合并转发等待提示失败: {type(e).__name__}: {e}")


def _parse_cq_data(raw_data: str | None) -> dict[str, str]:
    data: dict[str, str] = {}
    if not raw_data:
        return data
    for item in raw_data.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            continue
        data[key] = html.unescape(value)
    return data


def _iter_cq_string_segments(message: str):
    cursor = 0
    pattern = re.compile(r"\[CQ:([^,\]]+)(?:,([^\]]*))?\]")
    for match in pattern.finditer(message):
        if match.start() > cursor:
            text = html.unescape(message[cursor : match.start()])
            if text:
                yield {"type": "text", "data": {"text": text}}
        yield {"type": match.group(1), "data": _parse_cq_data(match.group(2))}
        cursor = match.end()
    if cursor < len(message):
        text = html.unescape(message[cursor:])
        if text:
            yield {"type": "text", "data": {"text": text}}


def _iter_message_segments(message_obj: Any):
    if message_obj is None:
        return

    if isinstance(message_obj, dict):
        if message_obj.get("type"):
            yield message_obj
            return
        for key in ("message", "content"):
            if key in message_obj:
                yield from _iter_message_segments(message_obj.get(key))
                return
        return

    if isinstance(message_obj, (bytes, bytearray)):
        message_obj = message_obj.decode("utf-8", errors="ignore")

    if isinstance(message_obj, str):
        yield from _iter_cq_string_segments(message_obj)
        return

    try:
        iterator = iter(message_obj)
    except TypeError:
        return

    for seg in iterator:
        yield seg


def _segment_type_and_data(seg: Any) -> tuple[str | None, dict[str, Any]]:
    if isinstance(seg, dict):
        seg_type = seg.get("type")
        seg_data = seg.get("data") or {}
        return str(seg_type) if seg_type else None, seg_data if isinstance(seg_data, dict) else {}

    seg_type = getattr(seg, "type", None)
    seg_data = getattr(seg, "data", None) or {}
    return str(seg_type) if seg_type else None, seg_data if isinstance(seg_data, dict) else {}


def _extract_forward_ids(message_obj: Any) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for seg in _iter_message_segments(message_obj):
        seg_type, seg_data = _segment_type_and_data(seg)
        if seg_type != "forward":
            continue
        forward_id = _normalize_id(seg_data.get("id"))
        if not forward_id or forward_id in seen:
            continue
        seen.add(forward_id)
        ids.append(forward_id)
    return ids


def _event_message_candidates(event: Any) -> list[Any]:
    candidates: list[Any] = []
    if event is None:
        return candidates
    for attr in ("message", "original_message"):
        value = getattr(event, attr, None)
        if value is not None:
            candidates.append(value)
    get_message = getattr(event, "get_message", None)
    if callable(get_message):
        try:
            candidates.append(get_message())
        except Exception:
            pass
    return candidates


def _payload_message_candidates(payload: Any) -> list[Any]:
    if payload is None:
        return []
    candidates: list[Any] = []
    if isinstance(payload, dict):
        for key in ("message", "content"):
            if key in payload:
                candidates.append(payload.get(key))
        return candidates
    for attr in ("message", "content"):
        value = getattr(payload, attr, None)
        if value is not None:
            candidates.append(value)
    return candidates


def _event_reply_payload(event: Any) -> Any | None:
    if event is None:
        return None
    return getattr(event, "reply", None)


def _extract_reply_ids_from_event(event: Any) -> list[str]:
    ids: list[str] = []
    reply = _event_reply_payload(event)
    if reply is not None:
        if isinstance(reply, dict):
            reply_id = _normalize_id(reply.get("message_id") or reply.get("id"))
            if reply_id:
                ids.append(reply_id)
        else:
            for attr in ("message_id", "id"):
                reply_id = _normalize_id(getattr(reply, attr, None))
                if reply_id:
                    ids.append(reply_id)
                    break

    for candidate in _event_message_candidates(event):
        for seg in _iter_message_segments(candidate):
            seg_type, seg_data = _segment_type_and_data(seg)
            if seg_type != "reply":
                continue
            reply_id = _normalize_id(seg_data.get("id") or seg_data.get("message_id"))
            if reply_id:
                ids.append(reply_id)

    seen: set[str] = set()
    deduped: list[str] = []
    for item in ids:
        if item and item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _message_id_to_int(message_id: str) -> int:
    text = _normalize_id(message_id)
    if not re.fullmatch(r"-?\d+", text):
        raise ForwardMessageError(f"消息 ID 不是数字，无法通过 get_msg 获取: {message_id}")
    return int(text)


async def _get_msg_payload(bot: Any, message_id: str) -> Any:
    return await asyncio.wait_for(
        bot.call_api("get_msg", message_id=_message_id_to_int(message_id)),
        timeout=GET_MSG_TIMEOUT_SECONDS,
    )


def _extract_message_ids_from_text(text: str) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?m)^\s*(?:id|消息id)\s*[:：]\s*(-?\d+)\s*$", text or ""):
        message_id = _normalize_id(match.group(1))
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        ids.append(message_id)
    return ids


def _recent_history_message_ids(ctx: OptionalToolContext, *, limit: int) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for msg in reversed(ctx.history or []):
        content = str(getattr(msg, "content", "") or "")
        for message_id in _extract_message_ids_from_text(content):
            if message_id in seen:
                continue
            seen.add(message_id)
            ids.append(message_id)
            if len(ids) >= limit:
                return ids
    return ids


def _find_recent_forward_ref(ctx: OptionalToolContext) -> tuple[str, str] | None:
    recent_refs = getattr(ctx, "recent_forward_messages", None) or []
    for item in recent_refs:
        if not isinstance(item, dict):
            continue
        forward_id = _normalize_id(item.get("forward_id") or item.get("id"))
        if not forward_id:
            continue
        message_id = _normalize_id(item.get("message_id") or item.get("msg_id"))
        source = f"近期合并转发消息 {message_id}" if message_id else "近期合并转发消息"
        return forward_id, source
    return None


async def _find_forward_id_in_message(bot: Any, message_id: str, source: str) -> tuple[str, str] | None:
    try:
        payload = await _get_msg_payload(bot, message_id)
    except Exception as e:
        logger.debug(f"通过 get_msg 获取合并转发失败 msg_id={message_id}: {type(e).__name__}: {e}")
        return None
    for candidate in _payload_message_candidates(payload):
        ids = _extract_forward_ids(candidate)
        if ids:
            return ids[0], source
    return None


async def _find_forward_id(
    ctx: OptionalToolContext,
    bot: Any,
    *,
    forward_id: str | None,
    target_msg_id: str | None,
) -> tuple[str, str]:
    explicit_forward_id = _normalize_id(forward_id)
    if explicit_forward_id:
        return explicit_forward_id, "参数 forward_id"

    target_message_id = _normalize_id(target_msg_id)
    if target_message_id:
        found = await _find_forward_id_in_message(bot, target_message_id, f"消息 {target_message_id}")
        if found:
            return found

    event = getattr(ctx, "event", None)
    for candidate in _event_message_candidates(event):
        ids = _extract_forward_ids(candidate)
        if ids:
            return ids[0], "当前消息"

    reply_payload = _event_reply_payload(event)
    for candidate in _payload_message_candidates(reply_payload):
        ids = _extract_forward_ids(candidate)
        if ids:
            return ids[0], "被回复消息"

    for reply_id in _extract_reply_ids_from_event(event):
        found = await _find_forward_id_in_message(bot, reply_id, f"被回复消息 {reply_id}")
        if found:
            return found

    for target in ctx.direct_targets or []:
        reply_id = _normalize_id(target.get("reply_to_message_id"))
        if not reply_id:
            continue
        found = await _find_forward_id_in_message(bot, reply_id, f"直接回复引用消息 {reply_id}")
        if found:
            return found

    for msg in reversed(ctx.history or []):
        content = str(getattr(msg, "content", "") or "")
        ids = _extract_forward_ids(content)
        if ids:
            return ids[0], "最近聊天记录"

    recent_forward = _find_recent_forward_ref(ctx)
    if recent_forward:
        return recent_forward

    for message_id in _recent_history_message_ids(ctx, limit=MAX_RECENT_MESSAGE_LOOKUP):
        found = await _find_forward_id_in_message(bot, message_id, f"最近消息 {message_id}")
        if found:
            return found

    raise ForwardMessageError("没有找到合并转发 ID。请回复那条合并转发，或明确指定包含合并转发的消息 id。")


async def _fetch_forward_payload(bot: Any, forward_id: str) -> Any:
    return await asyncio.wait_for(
        bot.call_api("get_forward_msg", id=forward_id),
        timeout=FORWARD_API_TIMEOUT_SECONDS,
    )


def _extract_forward_nodes(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        message = payload.get("message")
        raw_messages = payload.get("messages")
    else:
        message = getattr(payload, "message", None)
        raw_messages = getattr(payload, "messages", None)

    nodes: list[Any] = []
    for seg in _iter_message_segments(message):
        seg_type, _ = _segment_type_and_data(seg)
        if seg_type == "node":
            nodes.append(seg)

    if nodes:
        return nodes

    if isinstance(raw_messages, list):
        for item in raw_messages:
            if isinstance(item, dict) and item.get("type") == "node":
                nodes.append(item)
                continue
            if isinstance(item, dict):
                sender = item.get("sender") or {}
                if not isinstance(sender, dict):
                    sender = {}
                nickname = item.get("nickname") or sender.get("nickname") or sender.get("card")
                user_id = item.get("user_id") or sender.get("user_id")
                content = item.get("content") or item.get("message")
                nodes.append(
                    {
                        "type": "node",
                        "data": {
                            "nickname": nickname,
                            "user_id": user_id,
                            "content": content,
                        },
                    }
                )

    return nodes


def _detect_image_mime(data: bytes, fallback: str | None = None) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if fallback:
        return mimetypes.guess_type(f"image.{fallback.lstrip('.')}")[0] or "image/jpeg"
    return "image/jpeg"


def _path_to_data_uri(path: Path) -> str:
    content = path.read_bytes()
    payload = base64.b64encode(content).decode("utf-8")
    return f"data:{_detect_image_mime(content, path.suffix)};base64,{payload}"


async def _resolve_image_url(bot: Any, seg_data: dict[str, Any]) -> str | None:
    direct_url = _safe_text(seg_data.get("url"))
    if direct_url:
        return direct_url

    file_id = _safe_text(seg_data.get("file"))
    if not file_id:
        return None

    try:
        image_info = await asyncio.wait_for(
            bot.call_api("get_image", file=file_id),
            timeout=GET_IMAGE_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.debug(f"合并转发图片 get_image 失败 file={file_id}: {type(e).__name__}: {e}")
        return None

    if not isinstance(image_info, dict):
        return None

    local_path = _safe_text(image_info.get("file"))
    if local_path:
        path = Path(local_path)
        if path.is_file():
            try:
                return _path_to_data_uri(path)
            except Exception as e:
                logger.debug(f"合并转发图片读取本地文件失败 path={local_path}: {type(e).__name__}: {e}")

    image_url = _safe_text(image_info.get("url"))
    return image_url or None


def _format_known_segment(seg_type: str, seg_data: dict[str, Any]) -> str:
    if seg_type == "at":
        target = _safe_text(seg_data.get("qq") or seg_data.get("target"))
        return f"@{target}" if target else "[@]"
    if seg_type == "face":
        face_id = _safe_text(seg_data.get("id"))
        return f"[表情{face_id}]" if face_id else "[表情]"
    if seg_type == "reply":
        reply_id = _safe_text(seg_data.get("id") or seg_data.get("message_id"))
        return f"[回复 {reply_id}]" if reply_id else "[回复]"
    if seg_type == "record":
        return "[语音]"
    if seg_type == "video":
        return "[视频]"
    if seg_type == "file":
        name = _safe_text(seg_data.get("name") or seg_data.get("file"))
        return f"[文件: {name}]" if name else "[文件]"
    if seg_type == "forward":
        return "[合并转发]"
    if seg_type == "json":
        return "[JSON消息]"
    if seg_type == "xml":
        return "[XML消息]"
    if seg_type == "share":
        title = _safe_text(seg_data.get("title"))
        url = _safe_text(seg_data.get("url"))
        if title and url:
            return f"[链接: {title} {url}]"
        return "[链接分享]"
    if seg_type == "contact":
        contact_type = _safe_text(seg_data.get("type"))
        contact_id = _safe_text(seg_data.get("id"))
        label = "群" if contact_type == "group" else "联系人"
        return f"[推荐{label}: {contact_id}]" if contact_id else f"[推荐{label}]"
    if seg_type == "location":
        title = _safe_text(seg_data.get("title") or seg_data.get("content"))
        return f"[位置: {title}]" if title else "[位置]"
    return f"[{seg_type}消息]"


async def _format_node_content(
    bot: Any,
    content: Any,
    *,
    node_index: int,
    sender: str,
    images: list[ForwardImageRef],
    total_image_count: int,
) -> tuple[str, int]:
    parts: list[str] = []
    image_count = total_image_count

    for seg in _iter_message_segments(content):
        seg_type, seg_data = _segment_type_and_data(seg)
        if not seg_type:
            continue

        if seg_type == "text":
            text = str(seg_data.get("text") or "")
            if text:
                parts.append(text)
            continue

        if seg_type == "image":
            image_count += 1
            label = f"图片{image_count}"
            parts.append(f"[{label}]")
            if len(images) < MAX_FORWARD_IMAGES:
                image_url = await _resolve_image_url(bot, seg_data)
                if image_url:
                    images.append(
                        ForwardImageRef(
                            label=label,
                            node_index=node_index,
                            sender=sender,
                            image_url=image_url,
                        )
                    )
            continue

        parts.append(_format_known_segment(seg_type, seg_data))

    rendered = re.sub(r"\s+", " ", "".join(parts)).strip()
    return rendered or "[空消息]", image_count


def _node_sender(seg_data: dict[str, Any]) -> str:
    nickname = _safe_text(seg_data.get("nickname") or seg_data.get("name"))
    user_id = _safe_text(seg_data.get("user_id") or seg_data.get("uin"))
    if nickname and user_id:
        return f"{nickname}({user_id})"
    return nickname or user_id or "未知用户"


async def _read_forward(bot: Any, forward_id: str, source: str) -> ForwardReadResult:
    payload = await _fetch_forward_payload(bot, forward_id)
    nodes = _extract_forward_nodes(payload)
    if not nodes:
        raise ForwardMessageError("get_forward_msg 没有返回可读的 node 消息。")

    transcript_lines: list[str] = []
    images: list[ForwardImageRef] = []
    image_count = 0
    read_nodes = nodes[:MAX_FORWARD_NODES]

    for index, node in enumerate(read_nodes, 1):
        _, seg_data = _segment_type_and_data(node)
        sender = _node_sender(seg_data)
        content = seg_data.get("content")
        rendered, image_count = await _format_node_content(
            bot,
            content,
            node_index=index,
            sender=sender,
            images=images,
            total_image_count=image_count,
        )
        transcript_lines.append(f"{index}. {sender}: {rendered}")

    truncated_nodes = len(nodes) > len(read_nodes)
    if truncated_nodes:
        transcript_lines.append(f"...后续 {len(nodes) - len(read_nodes)} 条已省略")

    transcript = "\n".join(transcript_lines)
    truncated_text = len(transcript) > MAX_TRANSCRIPT_CHARS
    if truncated_text:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS].rstrip() + "\n...后续内容已截断"

    return ForwardReadResult(
        forward_id=forward_id,
        source=source,
        transcript=transcript,
        node_count=len(nodes),
        read_count=len(read_nodes),
        truncated_nodes=truncated_nodes,
        truncated_text=truncated_text,
        image_count=image_count,
        images=tuple(images),
    )


def _create_multimodal_model(ctx: OptionalToolContext) -> ChatOpenAI | None:
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    config = getattr(ctx, "config", None)
    model = _safe_text(getattr(config, "multimodal_model_resolved", ""))
    api_key = _safe_text(getattr(config, "multimodal_api_key_resolved", ""))
    base_url = _safe_text(getattr(config, "multimodal_base_url_resolved", ""))
    if not model or not api_key:
        return None
    return ChatOpenAI(
        model=model,
        api_key=SecretStr(api_key),
        base_url=base_url or None,
        temperature=0.01,
    )


def _summary_prompt(result: ForwardReadResult, question: str | None, *, include_images: bool) -> str:
    user_question = _safe_text(question) or "请简要总结这条合并转发的重点，并给出一句你的看法。"
    notices: list[str] = []
    if result.truncated_nodes or result.truncated_text:
        notices.append("内容较长，只能总结已读取部分。")
    if result.image_count and not include_images:
        notices.append("图片内容本轮无法直接查看，只能依据文字和图片占位总结。")
    if result.image_count > len(result.images):
        notices.append(f"共有 {result.image_count} 张图片，已尝试查看前 {len(result.images)} 张。")

    notice_text = "\n".join(f"- {item}" for item in notices) if notices else "- 无"
    return f"""请阅读下面的 QQ 合并转发消息，结合文字和可见图片回答用户问题。

用户问题：{user_question}

要求：
- 直接给出给群友看的中文摘要或回答
- 先概述合并转发内容，再补一句你自己的简短评价或看法
- 评价要基于已读内容，可以轻微吐槽或判断，但不要把主观看法说成事实
- 总字数必须控制在 100 字以内
- 不要编造看不见的信息；图片只描述能看到的内容
- 不要提工具名、API、JSON、prompt

读取情况：
- 合并转发 ID: {result.forward_id}
- 来源: {result.source}
- 节点数: {result.node_count}，已读取: {result.read_count}
- 图片数: {result.image_count}，已查看: {len(result.images) if include_images else 0}
{notice_text}

合并转发文本记录：
{result.transcript}
"""


def _build_multimodal_content(prompt: str, images: tuple[ForwardImageRef, ...]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in images:
        content.append(
            {
                "type": "text",
                "text": f"\n{image.label}，来自第 {image.node_index} 条 {image.sender}：",
            }
        )
        content.append({"type": "image_url", "image_url": {"url": image.image_url}})
    return content


async def _invoke_summary_model(
    model: Any,
    result: ForwardReadResult,
    question: str | None,
    *,
    include_images: bool,
) -> str:
    prompt = _summary_prompt(result, question, include_images=include_images)
    content: Any = _build_multimodal_content(prompt, result.images) if include_images else prompt
    response = await asyncio.wait_for(
        model.ainvoke(
            [
                SystemMessage(
                    content=(
                        "你是谨慎但有自己语气的QQ群合并转发摘要助手。"
                        "先概述内容，再给一句基于内容的简短评价；"
                        "不得扩写隐私信息，不得臆测图片之外的细节。"
                    )
                ),
                HumanMessage(content=content),
            ]
        ),
        timeout=SUMMARY_TIMEOUT_SECONDS,
    )
    return _limit_summary(_extract_model_text(getattr(response, "content", response)))


async def _summarize_forward(ctx: OptionalToolContext, result: ForwardReadResult, question: str | None) -> str:
    multimodal_model = _create_multimodal_model(ctx) if result.images else None
    text_model = getattr(ctx, "model", None) or multimodal_model
    if text_model is None:
        raise ForwardMessageError("当前没有可用模型，无法总结合并转发。")

    if multimodal_model is not None:
        try:
            summary = await _invoke_summary_model(multimodal_model, result, question, include_images=True)
            if summary:
                return summary
        except Exception as e:
            logger.warning(f"合并转发含图总结失败，准备降级为纯文本总结: {type(e).__name__}: {e}")

    summary = await _invoke_summary_model(text_model, result, question, include_images=False)
    if not summary:
        raise ForwardMessageError("模型没有返回可用摘要。")
    return summary


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def create_read_forward_tool(ctx: OptionalToolContext):
    @tool("read_forward_message", args_schema=ReadForwardMessageArgs)
    async def read_forward_message(
        forward_id: str | None = None,
        target_msg_id: str | None = None,
        question: str | None = None,
    ) -> str:
        """
        读取 OneBot 合并转发消息，先发送等待提示，再结合文字和图片生成 100 字以内摘要。

        只有用户明确要求查看、总结、阅读或分析合并转发消息时才调用。

        Args:
            forward_id: 合并转发 ID。
            target_msg_id: 包含合并转发的消息 ID。
            question: 用户对合并转发的具体问题。
        """
        if not await _can_continue(ctx):
            return "请求已过期，已取消查看合并转发。"

        bot = _get_context_bot(ctx)
        if bot is None or not hasattr(bot, "call_api"):
            return "无法获取支持 OneBot API 的 bot 实例，不能查看合并转发。"

        await _send_wait_notice(ctx)
        try:
            resolved_forward_id, source = await _find_forward_id(
                ctx,
                bot,
                forward_id=forward_id,
                target_msg_id=target_msg_id,
            )
            result = await _read_forward(bot, resolved_forward_id, source)
            summary = await _summarize_forward(ctx, result, question)

            if not await _can_continue(ctx):
                return "请求已过期，已取消发送合并转发摘要。"

            logger.info(
                f"已读取合并转发摘要 forward_id={resolved_forward_id} "
                f"nodes={result.read_count}/{result.node_count} images={result.image_count}"
            )
            return _json_result(
                {
                    "ok": True,
                    "forward_id": resolved_forward_id,
                    "source": source,
                    "summary_to_send": summary,
                    "node_count": result.node_count,
                    "read_count": result.read_count,
                    "image_count": result.image_count,
                    "wait_notice_sent": True,
                    "instruction": "等待提示已发送。现在必须只调用一次 reply_user，content 原样使用 summary_to_send，然后调用 finish；不要追加第二条摘要。",
                }
            )
        except ForwardMessageError as e:
            logger.warning(f"读取合并转发失败: {e}")
            return _json_result({"ok": False, "error": str(e)})
        except asyncio.TimeoutError:
            logger.warning("读取合并转发超时")
            return _json_result({"ok": False, "error": "读取合并转发超时。"})
        except Exception as e:
            logger.exception("合并转发工具异常")
            return _json_result({"ok": False, "error": f"{type(e).__name__}: {e}"})

    return read_forward_message


async def healthcheck(ctx: OptionalToolContext) -> tuple[bool, str]:
    if ctx.is_cross_user_direct_reply:
        return False, "cross-user direct reply disabled"
    if getattr(ctx, "model", None) is None and _create_multimodal_model(ctx) is None:
        return False, "missing model"
    return True, "ok"


async def build(ctx: OptionalToolContext) -> OptionalToolBundle:
    if ctx.is_cross_user_direct_reply:
        return OptionalToolBundle(name="read_forward_message")
    return OptionalToolBundle(
        name="read_forward_message",
        tools=[create_read_forward_tool(ctx)],
        skills=[
            AgentSkill(
                name="read_forward_message",
                description="用户明确要求查看、阅读、总结或分析合并转发消息时使用。",
                prompt=SKILL_PROMPT,
                tool_names=("read_forward_message",),
            )
        ],
        tool_limits=[ToolLimitSpec(tool_name="read_forward_message", run_limit=1)],
    )
