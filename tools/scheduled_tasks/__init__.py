from __future__ import annotations

import math
import uuid
import inspect
import datetime
from dataclasses import dataclass
from typing import Any

from langchain.tools import tool
from langchain_core.messages import HumanMessage
from nonebot.log import logger
from nonebot_plugin_alconna import Target, UniMessage
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, Field
from sqlalchemy import Select

try:
    from nonebot import get_plugin_config
except Exception:
    get_plugin_config = None

try:
    from nonebot_plugin_apscheduler import scheduler
except Exception:
    scheduler = None

try:
    from nonebot_plugin_ai_groupmate.agent.optional_tools import (
        OptionalToolBundle,
        OptionalToolContext,
        ToolLimitSpec,
    )
except Exception:
    try:
        from nonebot_plugin_ai_groupmate.agent.optional_tools.types import (
            OptionalToolBundle,
            OptionalToolContext,
            ToolLimitSpec,
        )
    except Exception:

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
            is_private: bool = False
            config: Any = None
            model: Any = None
            stop_words: list[str] | None = None
            detach_request: Any = None
            can_continue: Any = None
            mark_sent: Any = None
            clear_detached: Any = None
            create_detached_task: Any = None

from nonebot_plugin_ai_groupmate.model import ChatHistory, ChatHistorySchema

try:
    from nonebot_plugin_ai_groupmate.reply_guard import is_request_active
except Exception:

    async def is_request_active(session_id: str, request_id: str | None) -> bool:
        return True


SCHEDULED_AGENT_HISTORY_LIMIT = 20
DEFAULT_MIN_DELAY_SECONDS = 10.0
DEFAULT_MAX_DELAY_SECONDS = 7 * 24 * 3600.0
DEFAULT_MISFIRE_GRACE_TIME_SECONDS = 300


class ScheduledTasksScopedConfig(BaseModel):
    enabled: bool = True
    min_delay_seconds: float = Field(default=DEFAULT_MIN_DELAY_SECONDS, ge=0)
    max_delay_seconds: float = Field(default=DEFAULT_MAX_DELAY_SECONDS, ge=1)
    misfire_grace_time_seconds: int = Field(default=DEFAULT_MISFIRE_GRACE_TIME_SECONDS, ge=1)
    agent_history_limit: int = Field(default=SCHEDULED_AGENT_HISTORY_LIMIT, ge=1, le=100)
    record_text_history: bool = True
    default_private: bool = False


class ScheduledTasksRootConfig(BaseModel):
    ai_groupmate_scheduled_tasks: ScheduledTasksScopedConfig = Field(default_factory=ScheduledTasksScopedConfig)


class ScheduleMessageArgs(BaseModel):
    content: str = Field(description="到点后要发送的固定文本内容。")
    delay_minutes: float = Field(default=0, description="延迟多少分钟，可以是小数。")
    delay_hours: float = Field(default=0, description="延迟多少小时，可以和 delay_minutes 同时使用。")
    run_at: str | None = Field(
        default=None,
        description="可选的本地执行时间，格式 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS。填写后优先于 delay 参数。",
    )


class ScheduleAgentTaskArgs(BaseModel):
    task: str = Field(description="到点后要让 bot 重新进入 agent 完成的任务描述。")
    delay_minutes: float = Field(default=0, description="延迟多少分钟，可以是小数。")
    delay_hours: float = Field(default=0, description="延迟多少小时，可以和 delay_minutes 同时使用。")
    run_at: str | None = Field(
        default=None,
        description="可选的本地执行时间，格式 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS。填写后优先于 delay 参数。",
    )


class ScheduledTaskError(RuntimeError):
    pass


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _env_bool(name: str, default: bool) -> bool:
    import os

    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y", "enabled", "enable"}


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    import os

    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    import os

    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _load_config() -> ScheduledTasksScopedConfig:
    if get_plugin_config is not None:
        try:
            return get_plugin_config(ScheduledTasksRootConfig).ai_groupmate_scheduled_tasks
        except Exception as e:
            logger.warning(f"读取 scheduled_tasks 配置失败，回退到环境变量: {type(e).__name__}: {e}")

    prefix = "ai_groupmate_scheduled_tasks__"
    return ScheduledTasksScopedConfig(
        enabled=_env_bool(prefix + "enabled", True),
        min_delay_seconds=_env_float(prefix + "min_delay_seconds", DEFAULT_MIN_DELAY_SECONDS, minimum=0, maximum=3600),
        max_delay_seconds=_env_float(prefix + "max_delay_seconds", DEFAULT_MAX_DELAY_SECONDS, minimum=1, maximum=30 * 24 * 3600),
        misfire_grace_time_seconds=_env_int(
            prefix + "misfire_grace_time_seconds",
            DEFAULT_MISFIRE_GRACE_TIME_SECONDS,
            minimum=1,
            maximum=24 * 3600,
        ),
        agent_history_limit=_env_int(prefix + "agent_history_limit", SCHEDULED_AGENT_HISTORY_LIMIT, minimum=1, maximum=100),
        record_text_history=_env_bool(prefix + "record_text_history", True),
        default_private=_env_bool(prefix + "default_private", False),
    )


def _validate_config(config: ScheduledTasksScopedConfig) -> tuple[bool, str]:
    if not config.enabled:
        return False, "scheduled_tasks disabled"
    if scheduler is None:
        return False, "missing nonebot_plugin_apscheduler"
    if _host_has_builtin_schedule_tools():
        return False, "host already provides schedule tools"
    if config.max_delay_seconds <= 0:
        return False, "max_delay_seconds must be positive"
    if config.min_delay_seconds > config.max_delay_seconds:
        return False, "min_delay_seconds must be <= max_delay_seconds"
    return True, "ok"


def _host_has_builtin_schedule_tools() -> bool:
    try:
        import nonebot_plugin_ai_groupmate.agent as agent_module
    except Exception:
        return False
    return callable(getattr(agent_module, "create_schedule_message_tool", None)) and callable(
        getattr(agent_module, "create_schedule_agent_task_tool", None)
    )


def _normalize_text(value: str | None) -> str:
    return " ".join(str(value or "").split())


def _parse_run_at(run_at: str | None) -> datetime.datetime | None:
    text = (run_at or "").strip()
    if not text:
        return None

    normalized = text.replace("T", " ").replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
        ):
            try:
                parsed = datetime.datetime.strptime(normalized, fmt)
                break
            except ValueError:
                continue
        else:
            raise ScheduledTaskError("run_at 格式不正确，请使用 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS。")

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _resolve_run_at(
    *,
    run_at: str | None,
    delay_minutes: float,
    delay_hours: float,
    config: ScheduledTasksScopedConfig,
) -> datetime.datetime:
    now = datetime.datetime.now()
    absolute = _parse_run_at(run_at)
    if absolute is not None:
        delay_seconds = (absolute - now).total_seconds()
        if delay_seconds <= 0:
            raise ScheduledTaskError("执行时间必须晚于当前时间。")
    else:
        for name, value in (("delay_minutes", delay_minutes), ("delay_hours", delay_hours)):
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                raise ScheduledTaskError(f"{name} 必须是数字。") from None
            if not math.isfinite(numeric_value):
                raise ScheduledTaskError(f"{name} 必须是有限数字。")
        delay_seconds = float(delay_hours) * 3600 + float(delay_minutes) * 60
        if delay_seconds <= 0:
            raise ScheduledTaskError("延迟时间必须大于 0。")
        absolute = now + datetime.timedelta(seconds=delay_seconds)

    if delay_seconds < config.min_delay_seconds:
        return_msg = f"延迟时间太短，至少需要 {config.min_delay_seconds:g} 秒。"
        raise ScheduledTaskError(return_msg)
    if delay_seconds > config.max_delay_seconds:
        max_days = config.max_delay_seconds / 86400
        raise ScheduledTaskError(f"延迟时间太长，当前最多支持 {max_days:g} 天内的定时任务。")
    return absolute


def _is_private_context(ctx: OptionalToolContext, config: ScheduledTasksScopedConfig) -> bool:
    for attr in ("is_private", "private", "is_private_chat"):
        value = getattr(ctx, attr, None)
        if isinstance(value, bool):
            return value
    return config.default_private


def _bot_name(ctx: OptionalToolContext) -> str:
    config = getattr(ctx, "config", None)
    name = getattr(config, "bot_name", None)
    if name:
        return str(name)
    bot_id = getattr(ctx, "bot_id", None)
    return str(bot_id or "bot")


def _message_id_from_result(result: Any) -> str:
    msg_ids = getattr(result, "msg_ids", None) or []
    if not msg_ids:
        return "unknown"
    last_msg = msg_ids[-1]
    if isinstance(last_msg, dict):
        return str(last_msg.get("message_id") or last_msg.get("msg_id") or "unknown")
    return str(last_msg)


async def _send_scheduled_text(
    session_id: str,
    content: str,
    *,
    is_private: bool,
    bot_id: str | None,
    bot_name: str,
    record_history: bool,
) -> None:
    try:
        target = Target(id=session_id, private=is_private, self_id=bot_id)
        result = await UniMessage.text(content).send(target=target)
        msg_id = _message_id_from_result(result)

        if record_history:
            async with get_session() as db_session:
                chat_history = ChatHistory(
                    session_id=session_id,
                    user_id=bot_name,
                    content_type="bot",
                    content=f"id: {msg_id}\n" + content,
                    user_name=bot_name,
                )
                db_session.add(chat_history)
                await db_session.commit()

        logger.info(f"[自定义定时消息] 已发送到 {session_id}: {content}")
    except Exception as e:
        logger.exception(f"[自定义定时消息] 发送失败 {session_id}: {e}")


async def _run_scheduled_agent_task(
    session_id: str,
    task: str,
    *,
    is_private: bool,
    bot_id: str | None,
    bot_name: str,
    history_limit: int,
) -> None:
    try:
        try:
            import nonebot_plugin_ai_groupmate.agent as agent_module
        except ImportError as e:
            raise RuntimeError("当前 nonebot-plugin-ai-groupmate 版本不支持定时 agent 任务。") from e

        async with get_session() as db_session:
            rows = (
                (
                    await db_session.execute(
                        Select(ChatHistory)
                        .where(ChatHistory.session_id == session_id)
                        .order_by(ChatHistory.msg_id.desc())
                        .limit(history_limit)
                    )
                )
                .scalars()
                .all()
            )
            history = [ChatHistorySchema.model_validate(row) for row in rows[::-1]]

            graph, context_messages = await _create_scheduled_agent_graph(
                agent_module,
                db_session,
                session_id=session_id,
                history=history,
                bot_id=bot_id,
                bot_name=bot_name,
                is_private=is_private,
            )

            prompt = f"""
【定时任务触发】
这是之前安排的定时 agent 任务，现在已经到执行时间。

【任务内容】
{task}

【执行要求】
- 你必须通过工具完成任务，不要直接输出正文。
- 如果任务只是提醒/转告，调用 `reply_user`。
- 如果任务要求查最新信息，先调用 `search_web`，再调用 `reply_user`。
- 如果任务要求发送表情包图片，先调用 `search_meme_image` 或 `search_similar_meme_by_id`，再调用 `send_meme_image`。
- 定时任务没有可用的原始消息事件，不要调用 `add_message_reaction`。
- 任务完成后调用 `finish`。
"""
            history_messages = await _format_scheduled_history(agent_module, db_session, history)
            final_messages = list(context_messages) + list(history_messages) + [HumanMessage(content=prompt)]
            await graph.ainvoke(_make_scheduled_agent_state(agent_module, final_messages, session_id))
            await db_session.commit()

        logger.info(f"[自定义定时Agent任务] 已执行 {session_id}: {task}")
    except Exception as e:
        logger.exception(f"[自定义定时Agent任务] 执行失败 {session_id}: {e}")


async def _call_with_supported_kwargs(func: Any, **kwargs: Any) -> Any:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return await _maybe_await(func(**kwargs))

    parameters = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        call_kwargs = kwargs
    else:
        call_kwargs = {key: value for key, value in kwargs.items() if key in parameters}
    return await _maybe_await(func(**call_kwargs))


async def _create_scheduled_agent_graph(
    agent_module: Any,
    db_session: Any,
    *,
    session_id: str,
    history: list[ChatHistorySchema],
    bot_id: str | None,
    bot_name: str,
    is_private: bool,
) -> tuple[Any, list[Any]]:
    factory = getattr(agent_module, "create_chat_graph", None)
    if factory is None:
        factory = getattr(agent_module, "create_chat_agent", None)
    if factory is None:
        raise RuntimeError("当前 nonebot-plugin-ai-groupmate 版本缺少 create_chat_graph/create_chat_agent。")

    result = await _call_with_supported_kwargs(
        factory,
        db_session=db_session,
        session_id=session_id,
        request_id=None,
        user_id=bot_name,
        user_name=bot_name,
        history=history,
        interface=None,
        role_map=None,
        bot_id=bot_id,
        emoji_like_candidate_ids=set(),
        direct_targets=[],
        bot=None,
        event=None,
        is_private=is_private,
    )

    if isinstance(result, tuple):
        graph = result[0]
        context_messages = list(result[1] or []) if len(result) > 1 else []
        return graph, context_messages
    return result, []


async def _format_scheduled_history(
    agent_module: Any,
    db_session: Any,
    history: list[ChatHistorySchema],
) -> list[Any]:
    formatter = getattr(agent_module, "format_chat_history", None)
    if formatter is None:
        return []

    result = await _call_with_supported_kwargs(
        formatter,
        db_session=db_session,
        history=history,
        max_inline_images=0,
        omit_images=True,
    )
    return list(result or [])


def _make_scheduled_agent_state(agent_module: Any, final_messages: list[Any], session_id: str) -> dict[str, Any]:
    make_agent_state = getattr(agent_module, "make_agent_state", None)
    if make_agent_state is None:
        try:
            from nonebot_plugin_ai_groupmate.agent.graph import make_agent_state
        except Exception:
            make_agent_state = None
    if make_agent_state is not None:
        return dict(make_agent_state(final_messages, session_id, None))

    return {
        "messages": final_messages,
        "session_id": session_id,
        "request_id": None,
        "reply_count": 0,
        "tool_count": 0,
        "reply_this_round": 0,
        "reaction_this_round": 0,
        "called_finish": 0,
    }


def create_schedule_message_tool(ctx: OptionalToolContext, config: ScheduledTasksScopedConfig):
    @tool("schedule_message", args_schema=ScheduleMessageArgs)
    async def schedule_message(
        content: str,
        delay_minutes: float = 0,
        delay_hours: float = 0,
        run_at: str | None = None,
    ) -> str:
        """
        安排 bot 在指定时间向当前群聊/私聊发送一条固定文本消息。

        Args:
            content: 到点后要发送的文本内容。
            delay_minutes: 延迟多少分钟，可以是小数。
            delay_hours: 延迟多少小时，可以和 delay_minutes 同时使用。
            run_at: 可选的本地执行时间，格式 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS。填写后优先于 delay 参数。
        """
        if ctx.request_id is not None and not await is_request_active(ctx.session_id, ctx.request_id):
            return "请求已过期，已取消定时任务。"

        normalized_content = _normalize_text(content)
        if not normalized_content:
            return "定时消息内容为空，未创建任务。"

        try:
            run_datetime = _resolve_run_at(
                run_at=run_at,
                delay_minutes=delay_minutes,
                delay_hours=delay_hours,
                config=config,
            )
        except ScheduledTaskError as e:
            return str(e)

        job_id = f"ai_groupmate_custom_schedule_{ctx.session_id}_{uuid.uuid4().hex}"
        scheduler.add_job(
            _send_scheduled_text,
            "date",
            id=job_id,
            run_date=run_datetime,
            kwargs={
                "session_id": ctx.session_id,
                "content": normalized_content,
                "is_private": _is_private_context(ctx, config),
                "bot_id": getattr(ctx, "bot_id", None),
                "bot_name": _bot_name(ctx),
                "record_history": config.record_text_history,
            },
            misfire_grace_time=config.misfire_grace_time_seconds,
        )

        return f"定时任务已创建，将在 {run_datetime.strftime('%Y-%m-%d %H:%M:%S')} 发送：{normalized_content}"

    return schedule_message


def create_schedule_agent_task_tool(ctx: OptionalToolContext, config: ScheduledTasksScopedConfig):
    @tool("schedule_agent_task", args_schema=ScheduleAgentTaskArgs)
    async def schedule_agent_task(
        task: str,
        delay_minutes: float = 0,
        delay_hours: float = 0,
        run_at: str | None = None,
    ) -> str:
        """
        安排 bot 在指定时间重新进入 agent，并允许到点后调用可用工具完成任务。

        Args:
            task: 到点后要完成的任务描述，例如“查一下明天上海天气并提醒我带伞”。
            delay_minutes: 延迟多少分钟，可以是小数。
            delay_hours: 延迟多少小时，可以和 delay_minutes 同时使用。
            run_at: 可选的本地执行时间，格式 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS。填写后优先于 delay 参数。
        """
        if ctx.request_id is not None and not await is_request_active(ctx.session_id, ctx.request_id):
            return "请求已过期，已取消定时任务。"

        normalized_task = _normalize_text(task)
        if not normalized_task:
            return "定时 agent 任务内容为空，未创建任务。"

        try:
            run_datetime = _resolve_run_at(
                run_at=run_at,
                delay_minutes=delay_minutes,
                delay_hours=delay_hours,
                config=config,
            )
        except ScheduledTaskError as e:
            return str(e)

        job_id = f"ai_groupmate_custom_agent_schedule_{ctx.session_id}_{uuid.uuid4().hex}"
        scheduler.add_job(
            _run_scheduled_agent_task,
            "date",
            id=job_id,
            run_date=run_datetime,
            kwargs={
                "session_id": ctx.session_id,
                "task": normalized_task,
                "is_private": _is_private_context(ctx, config),
                "bot_id": getattr(ctx, "bot_id", None),
                "bot_name": _bot_name(ctx),
                "history_limit": config.agent_history_limit,
            },
            misfire_grace_time=config.misfire_grace_time_seconds,
        )

        return f"定时 agent 任务已创建，将在 {run_datetime.strftime('%Y-%m-%d %H:%M:%S')} 执行：{normalized_task}"

    return schedule_agent_task


async def healthcheck(ctx: OptionalToolContext) -> tuple[bool, str]:
    config = _load_config()
    return _validate_config(config)


async def build(ctx: OptionalToolContext) -> OptionalToolBundle:
    config = _load_config()
    ok, detail = _validate_config(config)
    if getattr(ctx, "is_cross_user_direct_reply", False) or not ok:
        return OptionalToolBundle(name="scheduled_tasks")

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    max_days = config.max_delay_seconds / 86400
    prompt = f"""- 定时任务：可使用 `schedule_message` 或 `schedule_agent_task`
  - 当前本地时间：{now}
  - 用户要求几分钟/几小时后提醒、转告或发送固定文本时，调用 `schedule_message`
  - 用户要求到点后再查询最新信息、搜索网页、挑选表情包、根据当时情况处理，或任务内容不是固定文本时，调用 `schedule_agent_task`
  - `schedule_message.content` 只写到点后要发送的最终固定文本
  - `schedule_agent_task.task` 写清到点后要完成的任务，不要写成已经完成
  - 相对时间可用 `delay_minutes` / `delay_hours`；明确日期时间可用 `run_at`
  - `run_at` 必须写成本地时间 `YYYY-MM-DD HH:MM` 或 `YYYY-MM-DD HH:MM:SS`
  - 当前最短延迟 {config.min_delay_seconds:g} 秒，最长延迟 {max_days:g} 天
  - 工具返回创建成功后，只表示任务已登记；不要说任务已经执行
  - 如果工具返回失败，直接用 `reply_user` 简短说明失败原因
"""
    return OptionalToolBundle(
        name="scheduled_tasks",
        tools=[
            create_schedule_message_tool(ctx, config),
            create_schedule_agent_task_tool(ctx, config),
        ],
        prompt=prompt,
        tool_limits=[
            ToolLimitSpec(tool_name="schedule_message", run_limit=1),
            ToolLimitSpec(tool_name="schedule_agent_task", run_limit=1),
        ],
    )
