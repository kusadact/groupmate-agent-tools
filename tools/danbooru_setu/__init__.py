from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlparse

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain.tools import tool
from nonebot.log import logger
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, Field

try:
    from nonebot_plugin_groupmate_agent.agent.optional_tools import (
        AgentSkill,
        OptionalToolBundle,
        OptionalToolContext,
        ToolLimitSpec,
    )
except Exception:
    try:
        from nonebot_plugin_groupmate_agent.agent.optional_tools.types import (
            AgentSkill,
            OptionalToolBundle,
            OptionalToolContext,
            ToolLimitSpec,
        )
    except Exception:

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
            detach_request: Any = None
            can_continue: Any = None
            mark_sent: Any = None
            clear_detached: Any = None
            create_detached_task: Any = None

from nonebot_plugin_groupmate_agent.model import ChatHistory
from nonebot_plugin_groupmate_agent.utils import check_and_compress_image_bytes

try:
    from nonebot_plugin_groupmate_agent.reply_guard import is_request_active, mark_request_sent
except Exception:

    async def is_request_active(session_id: str, request_id: str | None) -> bool:
        return True

    def mark_request_sent(session_id: str, request_id: str | None) -> None:
        return None


DANBOORU_BASE_URL = "https://danbooru.donmai.us"
DANBOORU_TIMEOUT_SECONDS = 15.0
DANBOORU_RESOLVER_TIMEOUT_SECONDS = 30.0
DANBOORU_MAX_TAGS = 2
DANBOORU_MAX_BYTES = 6 * 1024 * 1024
DANBOORU_RANDOM_ATTEMPTS = 4
DANBOORU_USER_AGENT = "nonebot-plugin-groupmate-agent danbooru_setu/1.0"
DANBOORU_MAX_IMAGE_MB = 6
DANBOORU_SAFE_IMAGE_MIMES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/gif",
    "image/webp",
}
DANBOORU_UNSAFE_QUERY_TOKENS = {
    "rating:e",
    "rating:q",
    "rating:s",
    "rating:explicit",
    "rating:questionable",
    "rating:sensitive",
    "status:any",
    "order:rank",
    "order:score",
    "order:favcount",
    "random:1",
}
DANBOORU_BLOCKED_TAGS = {
    "loli",
    "shota",
    "child",
    "children",
    "kid",
    "kids",
    "toddler",
    "baby",
    "infant",
    "young",
    "underage",
    "elementary_schooler",
    "middle_schooler",
    "kindergarten_uniform",
}
DANBOORU_BLOCKED_TAG_ALIASES = {
    "萝莉": "loli",
    "蘿莉": "loli",
    "洛丽塔": "loli",
    "洛麗塔": "loli",
    "lolita": "loli",
    "正太": "shota",
    "ショタ": "shota",
    "小孩": "child",
    "小孩子": "child",
    "儿童": "child",
    "兒童": "child",
    "孩子": "child",
    "幼女": "child",
    "幼童": "child",
    "未成年": "underage",
    "未成年人": "underage",
}
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_()+.!'-]{0,80}$")


@dataclass(frozen=True)
class DanbooruSetuConfig:
    name: str
    api: str
    rating: str
    proxy: str


class DanbooruSetuScopedConfig(BaseModel):
    name: str = ""
    api: str = ""
    username: str = ""
    api_key: str = ""
    rating: str = "g"
    proxy: str = ""


class DanbooruSetuRootConfig(BaseModel):
    groupmate_agent_danbooru_setu: DanbooruSetuScopedConfig = Field(default_factory=DanbooruSetuScopedConfig)


class SendDanbooruSetuArgs(BaseModel):
    raw_query: str = Field(description="用户原始搜索词，例如“丰川祥子 白丝”。工具内部会解析成 Danbooru canonical tags。")


class DanbooruSetuError(RuntimeError):
    pass


@dataclass(frozen=True)
class TagResolution:
    tags: tuple[str, ...]
    confidence: str
    reason: str
    raw_response: str


@dataclass(frozen=True)
class DanbooruPost:
    post_id: int
    page_url: str
    image_url: str
    preview_url: str
    rating: str
    tags: str


def _env_value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _load_config() -> DanbooruSetuConfig:
    try:
        from nonebot import get_plugin_config

        scoped = get_plugin_config(DanbooruSetuRootConfig).groupmate_agent_danbooru_setu
        return DanbooruSetuConfig(
            name=(scoped.name or scoped.username).strip(),
            api=(scoped.api or scoped.api_key).strip(),
            rating=_normalize_rating(scoped.rating),
            proxy=scoped.proxy.strip(),
        )
    except Exception as e:
        logger.warning(f"读取 Danbooru setu 配置失败，回退到环境变量: {type(e).__name__}: {e}")

    prefix = "groupmate_agent_danbooru_setu__"
    return DanbooruSetuConfig(
        name=_env_value(prefix + "name") or _env_value(prefix + "username"),
        api=_env_value(prefix + "api") or _env_value(prefix + "api_key"),
        rating=_normalize_rating(_env_value(prefix + "rating", "g")),
        proxy=_env_value(prefix + "proxy"),
    )


def _normalize_rating(value: str) -> str:
    rating = (value or "g").strip().lower()
    aliases = {
        "general": "g",
        "safe": "g",
        "sensitive": "s",
        "questionable": "q",
        "explicit": "e",
    }
    rating = aliases.get(rating, rating)
    return rating if rating in {"g", "s", "q", "e"} else "g"


def _rating_query(rating: str) -> str:
    return f"rating:{_normalize_rating(rating)}"


def _client_kwargs(config: DanbooruSetuConfig) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(DANBOORU_TIMEOUT_SECONDS),
        "follow_redirects": True,
        "trust_env": False,
        "headers": {"User-Agent": DANBOORU_USER_AGENT},
    }
    if config.proxy:
        kwargs["proxy"] = config.proxy
    return kwargs


def _auth_params(config: DanbooruSetuConfig) -> dict[str, str]:
    if config.name and config.api:
        return {"login": config.name, "api_key": config.api}
    return {}


def _build_api_url(path: str) -> str:
    return f"{DANBOORU_BASE_URL}/{path.lstrip('/')}"


def _normalize_tag_item(value: Any) -> str:
    tag = str(value or "").strip().lower()
    tag = tag.replace(" ", "_")
    tag = re.sub(r"[\u3000\t\r\n]+", "_", tag)
    tag = tag.strip("_")
    return tag


def _split_input_tags(tags: list[str] | str) -> list[str]:
    if isinstance(tags, str):
        return [item for item in re.split(r"[,，、\s]+", tags) if item]
    elif isinstance(tags, Sequence):
        return [str(item) for item in tags]
    return []


def _blocked_alias_for(raw_item: Any) -> str:
    text = str(raw_item or "").strip().lower()
    if not text:
        return ""
    normalized = _normalize_tag_item(text)
    if normalized in DANBOORU_BLOCKED_TAGS:
        return normalized
    compact = re.sub(r"[\s_、，,]+", "", text)
    return DANBOORU_BLOCKED_TAG_ALIASES.get(text) or DANBOORU_BLOCKED_TAG_ALIASES.get(compact, "")


def _normalize_input_tags(tags: list[str] | str) -> tuple[list[str], list[str]]:
    candidates = _split_input_tags(tags)

    normalized: list[str] = []
    blocked: list[str] = []
    seen: set[str] = set()
    seen_blocked: set[str] = set()
    for item in candidates:
        blocked_tag = _blocked_alias_for(item)
        if blocked_tag and blocked_tag not in seen_blocked:
            blocked.append(blocked_tag)
            seen_blocked.add(blocked_tag)
            continue

        tag = _normalize_tag_item(item)
        if not tag or tag in seen:
            continue
        if tag in DANBOORU_UNSAFE_QUERY_TOKENS:
            continue
        if tag.startswith("-") or ":" in tag or "*" in tag:
            continue
        if not _TAG_RE.fullmatch(tag):
            continue
        normalized.append(tag)
        seen.add(tag)
        if len(normalized) >= DANBOORU_MAX_TAGS:
            break
    return normalized, blocked


def _extract_message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    try:
        payload = json.loads(value)
    except Exception:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise DanbooruSetuError("tag resolver 没有返回 JSON")
        payload = json.loads(value[start : end + 1])
    if not isinstance(payload, dict):
        raise DanbooruSetuError("tag resolver 返回的 JSON 不是对象")
    return payload


def _parse_resolver_response(text: str) -> TagResolution:
    payload = _extract_json_object(text)
    raw_tags = payload.get("tags")
    if isinstance(raw_tags, str):
        candidate_tags: list[str] | str = raw_tags
    elif isinstance(raw_tags, Sequence):
        candidate_tags = [str(item) for item in raw_tags]
    else:
        candidate_tags = []

    tags, blocked = _normalize_input_tags(candidate_tags)
    if blocked:
        return TagResolution(tuple(blocked), "blocked", str(payload.get("reason") or ""), text)

    confidence = str(payload.get("confidence") or "").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    reason = str(payload.get("reason") or "").strip()
    return TagResolution(tuple(tags), confidence, reason, text)


async def _resolve_tags_with_model(ctx: OptionalToolContext, raw_query: str) -> TagResolution:
    if not getattr(ctx, "model", None):
        raise DanbooruSetuError("当前上下文没有可用模型，无法解析 Danbooru tag")

    system_prompt = """你只负责把用户的搜图词转换成 Danbooru canonical tags。
输出必须是单个 JSON 对象，不要 Markdown，不要额外解释。
JSON 格式：
{"tags":["tag1","tag2"],"confidence":"high|medium|low","reason":"简短中文原因"}

规则：
- 最多输出 2 个 tags。
- tags 必须是 Danbooru/booru 实际常用 canonical tag，小写，下划线分隔。
- 不要输出中文、日文原文、空格词、自然语言。
- 不要机械按汉字读音或中文读音罗马化角色名。
- 日文姓名要使用作品官方读法或 Danbooru/booru 社区 canonical tag。
- 如果只知道中文/日文原文但不确定 canonical tag，返回 tags=[] 且 confidence="low"。
- 如果输入包含被禁止的未成年性化相关请求，返回对应 blocked tag，例如 loli/shota/underage，并说明原因。
- 常见属性可转换为 Danbooru tag，例如 白丝 -> white_legwear。
"""
    user_prompt = f"用户原始搜图词：{raw_query}"
    response = await asyncio.wait_for(
        ctx.model.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]),
        timeout=DANBOORU_RESOLVER_TIMEOUT_SECONDS,
    )
    return _parse_resolver_response(_extract_message_text(response))


def _blocked_tags(tags: Sequence[str]) -> list[str]:
    return [tag for tag in tags if tag in DANBOORU_BLOCKED_TAGS]


def _read_tag_name(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("value", "label", "name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return _normalize_tag_item(value)
    return ""


def _read_tag_count(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    for key in ("post_count", "post_count_value"):
        try:
            return int(item.get(key) or 0)
        except Exception:
            continue
    return 0


def _choose_tag_candidate(raw_tag: str, payload: Any) -> str:
    if not isinstance(payload, list):
        return raw_tag

    candidates: list[tuple[int, int, str]] = []
    for item in payload:
        name = _read_tag_name(item)
        if not name or not _TAG_RE.fullmatch(name):
            continue
        if name in DANBOORU_BLOCKED_TAGS or name in DANBOORU_UNSAFE_QUERY_TOKENS:
            continue
        score = 0
        if name == raw_tag:
            score += 1000
        elif name.startswith(raw_tag):
            score += 500
        elif raw_tag in name:
            score += 100
        candidates.append((score, _read_tag_count(item), name))

    if not candidates:
        return raw_tag
    candidates.sort(reverse=True)
    return candidates[0][2]


async def _resolve_tag(client: httpx.AsyncClient, config: DanbooruSetuConfig, raw_tag: str) -> str:
    params: dict[str, str | int] = {
        "search[query]": raw_tag,
        "search[type]": "tag_query",
        "limit": 8,
        **_auth_params(config),
    }
    try:
        response = await client.get(_build_api_url("/autocomplete.json"), params=params)
        response.raise_for_status()
        return _choose_tag_candidate(raw_tag, response.json())
    except httpx.HTTPStatusError as e:
        logger.warning(f"Danbooru tag 校验失败: tag={raw_tag} status={e.response.status_code}")
    except Exception as e:
        logger.warning(f"Danbooru tag 校验异常，继续使用原 tag: tag={raw_tag} error={type(e).__name__}: {e}")
    return raw_tag


async def _resolve_tags(client: httpx.AsyncClient, config: DanbooruSetuConfig, tags: list[str]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        candidate = await _resolve_tag(client, config, tag)
        candidate = _normalize_tag_item(candidate)
        if not candidate or candidate in seen:
            continue
        if not _TAG_RE.fullmatch(candidate):
            continue
        resolved.append(candidate)
        seen.add(candidate)
    return resolved[:DANBOORU_MAX_TAGS]


def _pick_post_image_url(post: dict[str, Any]) -> str:
    for key in ("large_file_url", "file_url"):
        value = post.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _post_from_payload(payload: Any) -> DanbooruPost:
    if not isinstance(payload, dict):
        raise DanbooruSetuError("Danbooru 返回了无法识别的帖子数据")

    post_id = payload.get("id")
    try:
        post_id_int = int(post_id)
    except Exception as e:
        raise DanbooruSetuError("Danbooru 返回的帖子缺少 id") from e

    image_url = _pick_post_image_url(payload)
    if not image_url:
        raise DanbooruSetuError("随机到的帖子没有可发送图片")

    ext = str(payload.get("file_ext") or "").lower()
    if ext in {"webm", "mp4", "zip", "swf"}:
        raise DanbooruSetuError(f"随机到的帖子不是静态图片: {ext}")

    preview_url = str(payload.get("preview_file_url") or "")
    rating = str(payload.get("rating") or "")
    tag_string = str(payload.get("tag_string") or "")
    return DanbooruPost(
        post_id=post_id_int,
        page_url=f"{DANBOORU_BASE_URL}/posts/{post_id_int}",
        image_url=image_url,
        preview_url=preview_url,
        rating=rating,
        tags=tag_string,
    )


def _image_extension_from_content_type(content_type: str) -> str:
    return {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get(content_type.lower(), "jpg")


def _image_send_name(post: DanbooruPost, content_type: str) -> str:
    return f"danbooru_{post.post_id}.{_image_extension_from_content_type(content_type)}"


def _is_onebot_send_timeout_error(error: Exception) -> bool:
    retcode = getattr(error, "retcode", None)
    message = str(getattr(error, "message", "") or getattr(error, "wording", "") or error)
    return str(retcode) == "1200" and (
        "sendMsg" in message
        or "onMsgInfoListUpdate" in message
        or "NTEvent" in message
    )


async def _fetch_random_post(
    client: httpx.AsyncClient,
    config: DanbooruSetuConfig,
    tags: list[str],
) -> DanbooruPost:
    query = " ".join([*tags, _rating_query(config.rating)])
    params = {"tags": query, **_auth_params(config)}
    last_error: Exception | None = None

    for _ in range(DANBOORU_RANDOM_ATTEMPTS):
        try:
            response = await client.get(_build_api_url("/posts/random.json"), params=params)
            if response.status_code == 404:
                raise DanbooruSetuError(f"没有找到匹配图片: {query}")
            response.raise_for_status()
            return _post_from_payload(response.json())
        except DanbooruSetuError as e:
            last_error = e
        except httpx.HTTPStatusError as e:
            last_error = e
            logger.warning(f"Danbooru 随机取图失败: status={e.response.status_code} query={query}")
        except Exception as e:
            last_error = e
            logger.warning(f"Danbooru 随机取图异常: query={query} error={type(e).__name__}: {e}")

    if isinstance(last_error, DanbooruSetuError):
        raise last_error
    raise DanbooruSetuError(f"Danbooru 随机取图失败: {type(last_error).__name__ if last_error else 'unknown'}")


def _is_danbooru_image_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower()
    return host == "donmai.us" or host.endswith(".donmai.us")


async def _download_image(client: httpx.AsyncClient, url: str) -> tuple[bytes, str]:
    if not _is_danbooru_image_url(url):
        raise DanbooruSetuError("Danbooru 返回了非本站图片地址，已拒绝发送")

    response = await client.get(url)
    response.raise_for_status()
    content = bytes(response.content)
    if not content:
        raise DanbooruSetuError("图片下载为空")

    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in DANBOORU_SAFE_IMAGE_MIMES:
        raise DanbooruSetuError(f"下载结果不是可发送图片: {content_type or 'unknown'}")
    if len(content) > DANBOORU_MAX_BYTES:
        content = await _compress_image_for_send(content, content_type)
        content_type = "image/jpeg"
    if len(content) > DANBOORU_MAX_BYTES:
        raise DanbooruSetuError("图片压缩后仍过大，已取消发送")
    return content, content_type


async def _compress_image_for_send(content: bytes, content_type: str) -> bytes:
    image_format = {
        "image/jpeg": "JPEG",
        "image/jpg": "JPEG",
        "image/png": "JPEG",
        "image/webp": "JPEG",
        "image/gif": "JPEG",
    }.get(content_type, "JPEG")
    compressed = await asyncio.to_thread(
        check_and_compress_image_bytes,
        content,
        max_size_mb=DANBOORU_MAX_IMAGE_MB,
        image_format=image_format,
    )
    return bytes(compressed)


async def _is_current_request_active(ctx: OptionalToolContext) -> bool:
    can_continue = getattr(ctx, "can_continue", None)
    if can_continue is not None:
        return await can_continue()
    if ctx.request_id is None:
        return True
    return await is_request_active(ctx.session_id, ctx.request_id)


def _mark_request_sent(ctx: OptionalToolContext) -> None:
    mark_sent = getattr(ctx, "mark_sent", None)
    if mark_sent is not None:
        mark_sent()
        return
    if ctx.request_id is not None:
        mark_request_sent(ctx.session_id, ctx.request_id)


async def _record_sent_post(ctx: OptionalToolContext, message_id: str, post: DanbooruPost, tags: list[str]) -> None:
    try:
        bot_name = str(getattr(ctx.config, "bot_name", None) or "bot")
        async with get_session() as db_session:
            chat_history = ChatHistory(
                session_id=ctx.session_id,
                user_id=str(ctx.bot_id or bot_name),
                content_type="bot",
                content=(
                    f"id: {message_id}\n"
                    f"发送了一张 Danbooru 图片，post: {post.page_url}，"
                    f"tags: {' '.join(tags)}，rating: {post.rating}"
                ),
                user_name=bot_name,
            )
            db_session.add(chat_history)
            await db_session.commit()
    except Exception as e:
        logger.warning(f"记录 Danbooru 图片发送到聊天历史失败: {type(e).__name__}: {e}")


def create_danbooru_setu_tool(ctx: OptionalToolContext, config: DanbooruSetuConfig):
    @tool("send_danbooru_setu", args_schema=SendDanbooruSetuArgs)
    async def send_danbooru_setu(raw_query: str) -> str:
        """
        从 Danbooru 按 tag 随机找图并直接发送到当前群聊。

        只有用户明确要求 setu/色图/涩图/搜张图/找张图/来张图，并且给出了角色、作品或属性 tag 时才调用。
        只需要传用户原始搜索词；工具内部会用当前模型解析 Danbooru canonical tag。

        Args:
            raw_query: 用户原始搜索词。
        """
        if not await _is_current_request_active(ctx):
            return "请求已过期，已取消发送 Danbooru 图片。"

        raw_query_text = re.sub(r"\s+", " ", str(raw_query or "")).strip()
        if not raw_query_text:
            return "没有收到可用的 Danbooru 搜索词，未发送图片。"

        _, raw_blocked = _normalize_input_tags(raw_query_text)
        if raw_blocked:
            return f"tag 不适合发送：{' '.join(raw_blocked)}"

        logger.info(
            "Danbooru setu 请求准备: "
            f"session={ctx.session_id} user={ctx.user_id} raw_query={raw_query_text!r} rating={config.rating}"
        )

        try:
            resolution = await _resolve_tags_with_model(ctx, raw_query_text)
            if resolution.confidence == "blocked":
                return f"tag 不适合发送：{' '.join(resolution.tags)}"
            if not resolution.tags:
                logger.info(
                    "Danbooru setu tag resolver 未给出 tag: "
                    f"session={ctx.session_id} user={ctx.user_id} raw_query={raw_query_text!r} "
                    f"confidence={resolution.confidence} reason={resolution.reason!r} "
                    f"raw_response={resolution.raw_response!r}"
                )
                return "无法确定 Danbooru tag，未发送图片。"
            if resolution.confidence == "low":
                logger.info(
                    "Danbooru setu tag resolver 置信度过低: "
                    f"session={ctx.session_id} user={ctx.user_id} raw_query={raw_query_text!r} "
                    f"candidate_tags={list(resolution.tags)} reason={resolution.reason!r} "
                    f"raw_response={resolution.raw_response!r}"
                )
                return "无法确定 Danbooru tag，未发送图片。"

            candidate_tags = list(resolution.tags)
            blocked = _blocked_tags(candidate_tags)
            if blocked:
                return f"tag 不适合发送：{' '.join(blocked)}"
            logger.info(
                "Danbooru setu tag resolver 输出: "
                f"session={ctx.session_id} user={ctx.user_id} raw_query={raw_query_text!r} "
                f"candidate_tags={candidate_tags} confidence={resolution.confidence} reason={resolution.reason!r}"
            )

            async with httpx.AsyncClient(**_client_kwargs(config)) as client:
                resolved_tags = await _resolve_tags(client, config, candidate_tags)
                if not resolved_tags:
                    return "没有解析到可用的 Danbooru tag，未发送图片。"
                blocked = _blocked_tags(resolved_tags)
                if blocked:
                    return f"tag 不适合发送：{' '.join(blocked)}"
                query = " ".join([*resolved_tags, _rating_query(config.rating)])
                logger.info(
                    "Danbooru setu tag 映射与实际查询: "
                    f"session={ctx.session_id} user={ctx.user_id} raw_query={raw_query_text!r} "
                    f"input_tags={candidate_tags} actual_requested_tags={resolved_tags} query={query!r}"
                )

                post = await _fetch_random_post(client, config, resolved_tags)
                if not await _is_current_request_active(ctx):
                    return "请求已过期，已取消发送 Danbooru 图片。"

                image_content, image_content_type = await _download_image(client, post.image_url)
                if not await _is_current_request_active(ctx):
                    return "请求已过期，已取消发送 Danbooru 图片。"

            logger.info(
                "Danbooru 图片准备发送: "
                f"post_id={post.post_id} raw_query={raw_query_text!r} tags={resolved_tags} "
                f"bytes={len(image_content)} mimetype={image_content_type} name={_image_send_name(post, image_content_type)}"
            )
            send_result = await UniMessage.image(
                raw=image_content,
                mimetype=image_content_type,
                name=_image_send_name(post, image_content_type),
            ).send()
            _mark_request_sent(ctx)
            message_id = send_result.msg_ids[-1]["message_id"] if send_result.msg_ids else "unknown"
            await _record_sent_post(ctx, str(message_id), post, resolved_tags)
            logger.info(
                "Danbooru 图片已发送: "
                f"message_id={message_id} post_id={post.post_id} raw_query={raw_query_text!r} tags={resolved_tags}"
            )
            return f"已发送 Danbooru 图片：post={post.page_url} tags={' '.join(resolved_tags)}"
        except DanbooruSetuError as e:
            logger.warning(f"Danbooru setu 工具执行失败: {e}")
            return f"发送 Danbooru 图片失败: {e}"
        except httpx.HTTPError as e:
            logger.warning(f"Danbooru 网络请求失败: {type(e).__name__}: {e}")
            return f"发送 Danbooru 图片失败: 网络请求失败 {type(e).__name__}: {e}"
        except Exception as e:
            if _is_onebot_send_timeout_error(e):
                logger.warning(
                    "Danbooru 图片发送接口超时: "
                    f"raw_query={raw_query_text!r} "
                    f"post_id={getattr(locals().get('post', None), 'post_id', 'unknown')} "
                    f"tags={locals().get('resolved_tags', [])} "
                    f"bytes={len(locals().get('image_content', b''))} "
                    f"error={type(e).__name__}: {e}"
                )
                return "发送 Danbooru 图片失败: QQ/NapCat 图片发送回执超时，图片可能已发出但没有收到确认。"
            logger.exception(f"Danbooru setu 工具异常: {e}")
            return f"发送 Danbooru 图片失败: {type(e).__name__}: {e}"

    return send_danbooru_setu


async def healthcheck(ctx: OptionalToolContext) -> tuple[bool, str]:
    return True, "ok"


async def build(ctx: OptionalToolContext) -> OptionalToolBundle:
    config = _load_config()
    if ctx.is_cross_user_direct_reply:
        return OptionalToolBundle(name="danbooru_setu")

    skill_prompt = """- Danbooru 搜图：
  - 只有用户明确要求“setu / 色图 / 涩图 / 搜张图 / 找张图 / 来张图”等搜图意图，并且同时给出了角色、作品或属性 tag 时，才调用 `send_danbooru_setu`
  - 如果用户只是闲聊、评价图片、没有给出可搜索对象，不能调用
  - 调用时只填写 `raw_query`，内容是用户原始搜索词；不要自己翻译 Danbooru tag
  - 工具内部会单独调用模型把 `raw_query` 解析成 Danbooru canonical tag，并记录 raw_query、候选 tag、实际请求 tag
  - 工具会按当前配置 rating 过滤图片，默认 rating:g
  - 工具发送图片后，不要重复回复“已发送图片”；调用成功后直接 `finish`
"""
    return OptionalToolBundle(
        name="danbooru_setu",
        tools=[create_danbooru_setu_tool(ctx, config)],
        skills=[
            AgentSkill(
                name="danbooru_setu",
                description="用户明确要求按角色、作品或属性搜索图片时使用。",
                prompt=skill_prompt,
                tool_names=("send_danbooru_setu",),
            )
        ],
        tool_limits=[ToolLimitSpec(tool_name="send_danbooru_setu", run_limit=1)],
    )
