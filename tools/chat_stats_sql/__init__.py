from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from typing import Literal

from langchain.tools import tool
from nonebot.log import logger
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, Field
from sqlalchemy import Select, desc, func

from nonebot_plugin_groupmate_agent.agent.optional_tools import AgentSkill, OptionalToolBundle, OptionalToolContext, ToolLimitSpec
from nonebot_plugin_groupmate_agent.model import ChatHistory


SKILL_PROMPT = """- 聊天统计 SQL：只有当用户明确询问“数量、次数、排行、谁最多、某时间段发了多少”等可聚合统计问题时，才调用 `query_chat_stats_sql`
  - 适合：`xx 说了多少次 xx`、`谁最爱说 xx`、`今天谁发言最多`、`我这个月发了多少张图`
  - 不适合：回忆某件事、找历史原话、理解某段上下文、按语义搜索聊天记录；这些继续用 `search_history_context`
  - 不要用 SQL 工具查“发生了什么 / 当时怎么聊的 / 帮我找那段话”
  - SQL 工具返回 JSON 事实包，不是最终回复；拿到结果后自己组织自然群聊语言
  - 用户问“说了多少次/出现多少次”时，默认只回答 `occurrence_count`，不要主动说 `message_count`
  - 只有用户明确问“多少条消息包含 xx”时，才使用 `message_count`
  - 排行类问题使用 `rows`，数量类问题使用 `count` 或 `occurrence_count`
  - 回复要简短，不要提数据库、SQL、RAG、JSON、字段名或工具名
"""


StatisticType = Literal[
    "count_user_keyword_messages",
    "count_keyword_messages",
    "rank_keyword_users",
    "rank_active_users",
    "count_user_messages",
]
ContentType = Literal["text", "image", "all"]
TimeRange = Literal[
    "all",
    "today",
    "yesterday",
    "this_week",
    "this_month",
    "this_year",
    "last_7_days",
    "last_30_days",
    "recent_days",
    "custom",
]

MAX_TOP_N = 20
MAX_SCAN_ROWS = 50000


class ChatStatsSqlArgs(BaseModel):
    statistic_type: StatisticType = Field(
        description=(
            "统计类型。count_user_keyword_messages=某人说某关键词多少次；"
            "count_keyword_messages=全群某关键词多少次；rank_keyword_users=谁最常说某关键词；"
            "rank_active_users=发言排行；count_user_messages=某人发了多少消息/图片。"
        )
    )
    keyword: str | None = Field(default=None, description="要统计的关键词。只有关键词相关统计才填写。")
    target_user: str | None = Field(
        default=None,
        description="要统计的用户昵称、QQ号、我/自己。只有用户相关统计才填写。",
    )
    content_type: ContentType = Field(
        default="text",
        description="消息类型。关键词统计固定用 text；发言排行可用 text/image/all。",
    )
    time_range: TimeRange = Field(
        default="all",
        description="时间范围。明确日期区间时用 custom 并填写 start_at/end_at。",
    )
    recent_days: int | None = Field(
        default=None,
        ge=1,
        le=3660,
        description="time_range=recent_days 时填写最近多少天。",
    )
    start_at: str | None = Field(
        default=None,
        description="自定义开始时间，本地时间，格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS。",
    )
    end_at: str | None = Field(
        default=None,
        description="自定义结束时间，本地时间，格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS；日期会按当天结束处理。",
    )
    top_n: int = Field(default=5, ge=1, le=MAX_TOP_N, description="排行返回前几名，最多 20。")


@dataclass(frozen=True)
class TimeWindow:
    start: datetime.datetime | None
    end: datetime.datetime | None
    label: str


@dataclass(frozen=True)
class UserMatch:
    user_id: str
    user_name: str
    ambiguous_names: tuple[str, ...] = ()


def _json_result(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _ok_payload(args: ChatStatsSqlArgs, window: TimeWindow) -> dict[str, object]:
    return {
        "ok": True,
        "statistic_type": args.statistic_type,
        "time_range": window.label,
        "content_type": args.content_type,
    }


def _error_result(message: str, *, code: str = "invalid_request") -> str:
    return _json_result({"ok": False, "error": code, "message": message})


def _safe_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


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


def _display_name(name: str | None, user_id: str | None = None) -> str:
    clean_name = _strip_role_prefix(_safe_text(name))
    return clean_name or _safe_text(user_id) or "未知用户"


def _parse_datetime(value: str | None, *, is_end: bool = False) -> datetime.datetime | None:
    text = _safe_text(value)
    if not text:
        return None

    normalized = text.replace("T", " ").replace("/", "-")
    has_time = bool(re.search(r"\d{1,2}:\d{1,2}", normalized))

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )
    parsed: datetime.datetime | None = None
    for fmt in formats:
        try:
            parsed = datetime.datetime.strptime(normalized, fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        try:
            parsed = datetime.datetime.fromisoformat(normalized)
        except ValueError as e:
            raise ValueError(f"无法识别时间：{value}") from e

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    if is_end and not has_time:
        parsed = parsed + datetime.timedelta(days=1)
    return parsed


def _format_datetime(value: datetime.datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _resolve_time_window(
    *,
    time_range: TimeRange,
    recent_days: int | None,
    start_at: str | None,
    end_at: str | None,
) -> TimeWindow:
    now = datetime.datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if time_range == "all":
        return TimeWindow(None, None, "全部历史")
    if time_range == "today":
        return TimeWindow(today, now, "今天")
    if time_range == "yesterday":
        start = today - datetime.timedelta(days=1)
        return TimeWindow(start, today, "昨天")
    if time_range == "this_week":
        start = today - datetime.timedelta(days=today.weekday())
        return TimeWindow(start, now, "本周")
    if time_range == "this_month":
        start = today.replace(day=1)
        return TimeWindow(start, now, "本月")
    if time_range == "this_year":
        start = today.replace(month=1, day=1)
        return TimeWindow(start, now, "今年")
    if time_range == "last_7_days":
        return TimeWindow(now - datetime.timedelta(days=7), now, "最近 7 天")
    if time_range == "last_30_days":
        return TimeWindow(now - datetime.timedelta(days=30), now, "最近 30 天")
    if time_range == "recent_days":
        days = recent_days or 7
        return TimeWindow(now - datetime.timedelta(days=days), now, f"最近 {days} 天")

    start = _parse_datetime(start_at)
    end = _parse_datetime(end_at, is_end=True)
    if start is None and end is None:
        raise ValueError("自定义时间范围需要填写 start_at 或 end_at。")
    if start is not None and end is not None and start >= end:
        raise ValueError("开始时间必须早于结束时间。")

    if start and end:
        label = f"{_format_datetime(start)} 至 {_format_datetime(end)}"
    elif start:
        label = f"{_format_datetime(start)} 之后"
    else:
        label = f"{_format_datetime(end)} 之前"
    return TimeWindow(start, end, label)


def _apply_time_window(stmt, window: TimeWindow):
    if window.start is not None:
        stmt = stmt.where(ChatHistory.created_at >= window.start)
    if window.end is not None:
        stmt = stmt.where(ChatHistory.created_at < window.end)
    return stmt


def _apply_content_type(stmt, content_type: ContentType):
    if content_type == "all":
        return stmt.where(ChatHistory.content_type != "bot")
    return stmt.where(ChatHistory.content_type == content_type)


def _keyword_filter(keyword: str):
    return ChatHistory.content.contains(keyword, autoescape=True)


async def _resolve_user(db_session, ctx: OptionalToolContext, target_user: str | None) -> UserMatch | None:
    raw_target = _safe_text(target_user)
    if raw_target in {"我", "我自己", "自己", "本人", "咱", "俺"}:
        if not getattr(ctx, "user_id", None):
            raise ValueError("当前没有明确的用户身份，无法按“我”统计。")
        return UserMatch(str(ctx.user_id), _display_name(ctx.user_name, ctx.user_id))

    if not raw_target:
        if not getattr(ctx, "user_id", None):
            raise ValueError("需要指定要统计的用户。")
        return UserMatch(str(ctx.user_id), _display_name(ctx.user_name, ctx.user_id))

    target_compact = _compact(_strip_role_prefix(raw_target))
    rows = (
        (
            await db_session.execute(
                Select(
                    ChatHistory.user_id,
                    ChatHistory.user_name,
                    func.max(ChatHistory.created_at).label("last_seen"),
                )
                .where(ChatHistory.session_id == ctx.session_id)
                .group_by(ChatHistory.user_id, ChatHistory.user_name)
                .order_by(desc("last_seen"))
                .limit(1000)
            )
        )
        .all()
    )

    latest_by_id: dict[str, str] = {}
    exact_ids: list[tuple[str, str]] = []
    exact_names: list[tuple[str, str]] = []
    fuzzy_names: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for user_id, user_name, _ in rows:
        uid = _safe_text(user_id)
        display = _display_name(user_name, uid)
        if not uid or uid not in latest_by_id:
            latest_by_id[uid] = display

        pair = (uid, display)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        name_compact = _compact(display)
        raw_name_compact = _compact(user_name)
        if raw_target == uid:
            exact_ids.append(pair)
        if target_compact and target_compact in {name_compact, raw_name_compact}:
            exact_names.append(pair)
        elif target_compact and target_compact in name_compact:
            fuzzy_names.append(pair)

    matches = exact_ids or exact_names or fuzzy_names
    unique_by_id: dict[str, str] = {}
    for uid, display in matches:
        unique_by_id.setdefault(uid, latest_by_id.get(uid, display))

    if not unique_by_id:
        return None
    if len(unique_by_id) > 1:
        names = tuple(f"{name}({uid})" for uid, name in list(unique_by_id.items())[:5])
        return UserMatch("", "", ambiguous_names=names)

    uid, display = next(iter(unique_by_id.items()))
    return UserMatch(uid, display)


def _count_keyword_occurrences(
    rows: list[tuple[str, str, str]],
    keyword: str,
) -> tuple[int, int, dict[str, tuple[str, int, int]]]:
    message_count = 0
    occurrence_count = 0
    by_user: dict[str, tuple[str, int, int]] = {}

    for user_id, user_name, content in rows:
        body = _parse_msg_body(content)
        count = body.count(keyword)
        if count <= 0:
            continue

        message_count += 1
        occurrence_count += count
        uid = _safe_text(user_id)
        display = _display_name(user_name, uid)
        old_name, old_messages, old_occurrences = by_user.get(uid, (display, 0, 0))
        by_user[uid] = (old_name or display, old_messages + 1, old_occurrences + count)

    return message_count, occurrence_count, by_user


async def _count_user_keyword_messages(db_session, ctx: OptionalToolContext, args: ChatStatsSqlArgs, window: TimeWindow) -> str:
    keyword = _safe_text(args.keyword)
    if not keyword:
        return _error_result("需要提供要统计的关键词。")

    user = await _resolve_user(db_session, ctx, args.target_user)
    if user is None:
        return _error_result(
            f"没有在当前会话里找到用户“{_safe_text(args.target_user)}”。",
            code="user_not_found",
        )
    if user.ambiguous_names:
        return _json_result(
            {
                "ok": False,
                "error": "ambiguous_user",
                "message": "匹配到多个用户，请说得更具体一点。",
                "candidates": list(user.ambiguous_names),
            }
        )

    base_filters = (
        ChatHistory.session_id == ctx.session_id,
        ChatHistory.user_id == user.user_id,
        ChatHistory.content_type == "text",
        _keyword_filter(keyword),
    )

    count_stmt = _apply_time_window(Select(func.count(ChatHistory.msg_id)).where(*base_filters), window)
    exact_message_count = int((await db_session.execute(count_stmt)).scalar() or 0)

    stmt = Select(ChatHistory.user_id, ChatHistory.user_name, ChatHistory.content).where(*base_filters)
    stmt = _apply_time_window(stmt, window).order_by(desc(ChatHistory.created_at)).limit(MAX_SCAN_ROWS)
    rows = (await db_session.execute(stmt)).all()
    scanned_message_count, occurrence_count, _ = _count_keyword_occurrences(rows, keyword)

    payload = _ok_payload(args, window)
    payload.update(
        {
            "keyword": keyword,
            "target_user": {"user_id": user.user_id, "user_name": user.user_name},
            "occurrence_count": occurrence_count,
            "message_count": exact_message_count,
            "scanned_message_count": scanned_message_count,
            "is_occurrence_count_exact": exact_message_count <= scanned_message_count,
            "answer_hint": (
                f"{user.user_name} 一共说了“{keyword}”{occurrence_count} 次。"
                if exact_message_count <= scanned_message_count
                else (
                    f"{user.user_name} 至少说了“{keyword}”{occurrence_count} 次，"
                    f"还有更早的 {exact_message_count - scanned_message_count} 条匹配消息没逐条数词频。"
                )
            ),
        }
    )
    return _json_result(payload)


async def _count_keyword_messages(db_session, ctx: OptionalToolContext, args: ChatStatsSqlArgs, window: TimeWindow) -> str:
    keyword = _safe_text(args.keyword)
    if not keyword:
        return _error_result("需要提供要统计的关键词。")

    base_filters = (
        ChatHistory.session_id == ctx.session_id,
        ChatHistory.content_type == "text",
        _keyword_filter(keyword),
    )

    count_stmt = _apply_time_window(Select(func.count(ChatHistory.msg_id)).where(*base_filters), window)
    exact_message_count = int((await db_session.execute(count_stmt)).scalar() or 0)

    stmt = Select(ChatHistory.user_id, ChatHistory.user_name, ChatHistory.content).where(*base_filters)
    stmt = _apply_time_window(stmt, window).order_by(desc(ChatHistory.created_at)).limit(MAX_SCAN_ROWS)
    rows = (await db_session.execute(stmt)).all()
    scanned_message_count, occurrence_count, by_user = _count_keyword_occurrences(rows, keyword)

    top_items = sorted(by_user.values(), key=lambda item: (-item[2], -item[1], item[0]))[:3]
    payload = _ok_payload(args, window)
    payload.update(
        {
            "keyword": keyword,
            "occurrence_count": occurrence_count,
            "message_count": exact_message_count,
            "scanned_message_count": scanned_message_count,
            "is_occurrence_count_exact": exact_message_count <= scanned_message_count,
            "top_users_by_occurrence": [
                {"user_name": name, "message_count": messages, "occurrence_count": occurrences}
                for name, messages, occurrences in top_items
            ],
            "answer_hint": (
                f"“{keyword}”一共出现了 {occurrence_count} 次。"
                if exact_message_count <= scanned_message_count
                else (
                    f"“{keyword}”至少出现了 {occurrence_count} 次，"
                    f"还有更早的 {exact_message_count - scanned_message_count} 条匹配消息没逐条数词频。"
                )
            ),
        }
    )
    return _json_result(payload)


async def _rank_keyword_users(db_session, ctx: OptionalToolContext, args: ChatStatsSqlArgs, window: TimeWindow) -> str:
    keyword = _safe_text(args.keyword)
    if not keyword:
        return _error_result("需要提供要统计的关键词。")

    stmt = Select(
        ChatHistory.user_id,
        func.max(ChatHistory.user_name).label("user_name"),
        func.count(ChatHistory.msg_id).label("message_count"),
    ).where(
        ChatHistory.session_id == ctx.session_id,
        ChatHistory.content_type == "text",
        _keyword_filter(keyword),
    )
    stmt = _apply_time_window(stmt, window)
    stmt = stmt.group_by(ChatHistory.user_id).order_by(desc("message_count")).limit(args.top_n)
    rows = (await db_session.execute(stmt)).all()

    payload = _ok_payload(args, window)
    payload.update(
        {
            "keyword": keyword,
            "rank_metric": "message_count",
            "rows": [
                {
                    "rank": index,
                    "user_id": _safe_text(user_id),
                    "user_name": _display_name(user_name, user_id),
                    "message_count": int(count or 0),
                }
                for index, (user_id, user_name, count) in enumerate(rows, 1)
            ],
            "answer_hint": "没有统计到结果。" if not rows else "",
        }
    )
    return _json_result(payload)


async def _rank_active_users(db_session, ctx: OptionalToolContext, args: ChatStatsSqlArgs, window: TimeWindow) -> str:
    content_type = args.content_type
    stmt = Select(
        ChatHistory.user_id,
        func.max(ChatHistory.user_name).label("user_name"),
        func.count(ChatHistory.msg_id).label("message_count"),
    ).where(ChatHistory.session_id == ctx.session_id)
    stmt = _apply_content_type(stmt, content_type)
    stmt = _apply_time_window(stmt, window)
    stmt = stmt.group_by(ChatHistory.user_id).order_by(desc("message_count")).limit(args.top_n)
    rows = (await db_session.execute(stmt)).all()

    label = "发言"
    if content_type == "text":
        label = "文本发言"
    elif content_type == "image":
        label = "图片"

    payload = _ok_payload(args, window)
    payload.update(
        {
            "rank_metric": "message_count",
            "label": label,
            "rows": [
                {
                    "rank": index,
                    "user_id": _safe_text(user_id),
                    "user_name": _display_name(user_name, user_id),
                    "message_count": int(count or 0),
                }
                for index, (user_id, user_name, count) in enumerate(rows, 1)
            ],
            "answer_hint": "没有统计到结果。" if not rows else "",
        }
    )
    return _json_result(payload)


async def _count_user_messages(db_session, ctx: OptionalToolContext, args: ChatStatsSqlArgs, window: TimeWindow) -> str:
    user = await _resolve_user(db_session, ctx, args.target_user)
    if user is None:
        return _error_result(
            f"没有在当前会话里找到用户“{_safe_text(args.target_user)}”。",
            code="user_not_found",
        )
    if user.ambiguous_names:
        return _json_result(
            {
                "ok": False,
                "error": "ambiguous_user",
                "message": "匹配到多个用户，请说得更具体一点。",
                "candidates": list(user.ambiguous_names),
            }
        )

    content_type = args.content_type
    stmt = Select(func.count(ChatHistory.msg_id)).where(
        ChatHistory.session_id == ctx.session_id,
        ChatHistory.user_id == user.user_id,
    )
    stmt = _apply_content_type(stmt, content_type)
    stmt = _apply_time_window(stmt, window)
    count = int((await db_session.execute(stmt)).scalar() or 0)

    label = "消息"
    if content_type == "text":
        label = "文本消息"
    elif content_type == "image":
        label = "图片"
    payload = _ok_payload(args, window)
    payload.update(
        {
            "target_user": {"user_id": user.user_id, "user_name": user.user_name},
            "label": label,
            "count": count,
            "answer_hint": f"{user.user_name} 发了 {count} 条{label}。",
        }
    )
    return _json_result(payload)


def create_chat_stats_sql_tool(ctx: OptionalToolContext):
    @tool("query_chat_stats_sql", args_schema=ChatStatsSqlArgs)
    async def query_chat_stats_sql(
        statistic_type: StatisticType,
        keyword: str | None = None,
        target_user: str | None = None,
        content_type: ContentType = "text",
        time_range: TimeRange = "all",
        recent_days: int | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        top_n: int = 5,
    ) -> str:
        """
        查询当前会话聊天历史的可聚合统计结果。

        仅用于明确的统计问题，例如某人说某词多少次、关键词排行、发言榜、图片数量。
        不用于回忆聊天内容、查找原话、语义搜索或了解上下文。
        """
        try:
            args = ChatStatsSqlArgs(
                statistic_type=statistic_type,
                keyword=keyword,
                target_user=target_user,
                content_type=content_type,
                time_range=time_range,
                recent_days=recent_days,
                start_at=start_at,
                end_at=end_at,
                top_n=min(max(int(top_n or 5), 1), MAX_TOP_N),
            )
            window = _resolve_time_window(
                time_range=args.time_range,
                recent_days=args.recent_days,
                start_at=args.start_at,
                end_at=args.end_at,
            )

            async with get_session() as db_session:
                if args.statistic_type == "count_user_keyword_messages":
                    return await _count_user_keyword_messages(db_session, ctx, args, window)
                if args.statistic_type == "count_keyword_messages":
                    return await _count_keyword_messages(db_session, ctx, args, window)
                if args.statistic_type == "rank_keyword_users":
                    return await _rank_keyword_users(db_session, ctx, args, window)
                if args.statistic_type == "rank_active_users":
                    return await _rank_active_users(db_session, ctx, args, window)
                if args.statistic_type == "count_user_messages":
                    return await _count_user_messages(db_session, ctx, args, window)

            return _error_result("未知统计类型。", code="unknown_statistic_type")
        except Exception as e:
            logger.warning(f"聊天统计 SQL 工具执行失败: {type(e).__name__}: {e}")
            return _error_result(f"统计失败：{e}", code="query_failed")

    return query_chat_stats_sql


async def healthcheck(ctx: OptionalToolContext) -> tuple[bool, str]:
    return True, "ok"


async def build(ctx: OptionalToolContext) -> OptionalToolBundle:
    return OptionalToolBundle(
        name="chat_stats_sql",
        tools=[create_chat_stats_sql_tool(ctx)],
        skills=[
            AgentSkill(
                name="chat_statistics",
                description="用户明确询问聊天数量、次数、排行或时间段统计时使用。",
                prompt=SKILL_PROMPT,
                tool_names=("query_chat_stats_sql",),
            )
        ],
        tool_limits=[ToolLimitSpec(tool_name="query_chat_stats_sql", run_limit=1)],
    )
