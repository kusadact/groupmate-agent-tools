from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

import httpx
from langchain.tools import tool
from nonebot.log import logger
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, Field

try:
    from nonebot_plugin_groupmate_agent.agent.optional_tools import (
        OptionalToolBundle,
        OptionalToolContext,
        ToolLimitSpec,
    )
except Exception:
    try:
        from nonebot_plugin_groupmate_agent.agent.optional_tools.types import (
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
            config: Any = None
            model: Any = None
            stop_words: list[str] | None = None
            detach_request: Any = None
            can_continue: Any = None
            mark_sent: Any = None
            clear_detached: Any = None
            create_detached_task: Any = None

from nonebot_plugin_groupmate_agent.model import ChatHistory
try:
    from nonebot_plugin_groupmate_agent.reply_guard import is_request_active
except Exception:
    async def is_request_active(session_id: str, request_id: str | None) -> bool:
        return True

try:
    from nonebot_plugin_groupmate_agent.reply_guard import mark_request_sent
except Exception:
    def mark_request_sent(session_id: str, request_id: str | None) -> None:
        return None


@dataclass(frozen=True)
class ImageAgentConfig:
    enabled: bool
    base_url: str
    api_key: str
    model: str
    size: str
    quality: str
    generation_endpoint: str
    edit_endpoint: str
    edit_image_field_name: str
    timeout_seconds: float
    download_timeout_seconds: float
    retry_attempts: int
    retry_delay_seconds: float
    max_prompt_length: int
    max_reference_avatars: int


class ImageAgentScopedConfig(BaseModel):
    enabled: bool = True
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-image-2"
    size: str = "1024x1024"
    quality: str = "auto"
    generation_endpoint: str = "/images/generations"
    edit_endpoint: str = "/images/edits"
    edit_image_field_name: str = "auto"
    timeout_seconds: float = Field(default=180.0, ge=5.0)
    download_timeout_seconds: float = Field(default=30.0, ge=3.0)
    retry_attempts: int = Field(default=2, ge=0, le=5)
    retry_delay_seconds: float = Field(default=5.0, ge=0.0, le=120.0)
    max_prompt_length: int = Field(default=1200, ge=1, le=4000)
    max_reference_avatars: int = Field(default=2, ge=1, le=4)


class ImageAgentRootConfig(BaseModel):
    groupmate_agent_image_agent: ImageAgentScopedConfig = Field(default_factory=ImageAgentScopedConfig)


@dataclass(frozen=True)
class ReferenceImage:
    filename: str
    content: bytes
    content_type: str


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    mime_type: str
    revised_prompt: str | None = None


class ImageAgentError(RuntimeError):
    pass


class GenerateImageArgs(BaseModel):
    prompt: str = Field(description="最终用于生成图片的完整提示词，必须包含主体、风格、构图、文字约束。")
    reference_image_paths: list[str] | str | None = Field(
        default=None,
        description="由主插件内置 fetch_qq_avatar_references 返回的本地头像参考图路径。需要头像参考图时必须填写。",
    )


_image_lock = asyncio.Lock()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y", "enabled", "enable"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _load_config() -> ImageAgentConfig:
    try:
        from nonebot import get_plugin_config

        scoped = get_plugin_config(ImageAgentRootConfig).groupmate_agent_image_agent
        return _config_from_scoped(scoped)
    except Exception as e:
        logger.warning(f"读取 image agent 配置失败，回退到环境变量: {type(e).__name__}: {e}")

    prefix = "groupmate_agent_image_agent__"
    return ImageAgentConfig(
        enabled=_env_bool(prefix + "enabled", True),
        base_url=os.getenv(prefix + "base_url", "").strip(),
        api_key=os.getenv(prefix + "api_key", "").strip(),
        model=os.getenv(prefix + "model", "gpt-image-2").strip(),
        size=os.getenv(prefix + "size", "1024x1024").strip(),
        quality=os.getenv(prefix + "quality", "auto").strip(),
        generation_endpoint=os.getenv(prefix + "generation_endpoint", "/images/generations").strip(),
        edit_endpoint=os.getenv(prefix + "edit_endpoint", "/images/edits").strip(),
        edit_image_field_name=os.getenv(prefix + "edit_image_field_name", "auto").strip(),
        timeout_seconds=_env_float(prefix + "timeout_seconds", 180.0, minimum=5.0, maximum=600.0),
        download_timeout_seconds=_env_float(prefix + "download_timeout_seconds", 30.0, minimum=3.0, maximum=120.0),
        retry_attempts=_env_int(prefix + "retry_attempts", 2, minimum=0, maximum=5),
        retry_delay_seconds=_env_float(prefix + "retry_delay_seconds", 5.0, minimum=0.0, maximum=120.0),
        max_prompt_length=_env_int(prefix + "max_prompt_length", 1200, minimum=1, maximum=4000),
        max_reference_avatars=_env_int(prefix + "max_reference_avatars", 2, minimum=1, maximum=4),
    )


def _config_from_scoped(scoped: ImageAgentScopedConfig) -> ImageAgentConfig:
    return ImageAgentConfig(
        enabled=scoped.enabled,
        base_url=scoped.base_url.strip(),
        api_key=scoped.api_key.strip(),
        model=scoped.model.strip(),
        size=scoped.size.strip(),
        quality=scoped.quality.strip(),
        generation_endpoint=scoped.generation_endpoint.strip(),
        edit_endpoint=scoped.edit_endpoint.strip(),
        edit_image_field_name=scoped.edit_image_field_name.strip(),
        timeout_seconds=scoped.timeout_seconds,
        download_timeout_seconds=scoped.download_timeout_seconds,
        retry_attempts=scoped.retry_attempts,
        retry_delay_seconds=scoped.retry_delay_seconds,
        max_prompt_length=scoped.max_prompt_length,
        max_reference_avatars=scoped.max_reference_avatars,
    )


def _validate_config(config: ImageAgentConfig) -> tuple[bool, str]:
    if not config.enabled:
        return False, "image agent disabled"
    if not config.base_url:
        return False, "missing groupmate_agent_image_agent__base_url"
    if not config.api_key:
        return False, "missing groupmate_agent_image_agent__api_key"
    if not config.model:
        return False, "missing groupmate_agent_image_agent__model"
    return True, "ok"


def _join_url(base_url: str, endpoint: str) -> str:
    base = base_url.rstrip("/")
    path = "/" + endpoint.lstrip("/")
    return base + path


def _detect_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _mime_extension(mime_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(mime_type, ".png")


def _normalize_image_mime(content_type: str | None, data: bytes) -> str:
    detected = _detect_image_mime(data)
    if detected.startswith("image/"):
        return detected
    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized_content_type.startswith("image/"):
        return normalized_content_type
    return "image/png"


def _guess_image_mime(filename: str, data: bytes) -> str:
    detected = _detect_image_mime(data)
    if detected.startswith("image/"):
        return detected
    guessed = mimetypes.guess_type(filename)[0]
    if guessed and guessed.startswith("image/"):
        return guessed
    return "image/png"


def _build_reference_image(
    filename_hint: str,
    image_bytes: bytes,
    content_type: str | None = None,
) -> ReferenceImage:
    mime_type = _normalize_image_mime(content_type, image_bytes) if content_type else None
    path = urlparse(filename_hint).path or filename_hint
    suffix = _mime_extension(mime_type) if mime_type else Path(path).suffix or ".png"
    filename = f"reference{suffix}"
    return ReferenceImage(
        filename=filename,
        content=image_bytes,
        content_type=mime_type or _guess_image_mime(filename, image_bytes),
    )


class ImageApiClient:
    def __init__(self, config: ImageAgentConfig):
        self.config = config

    async def generate(self, prompt: str, reference_images: Sequence[ReferenceImage]) -> GeneratedImage:
        ok, reason = _validate_config(self.config)
        if not ok:
            raise ImageAgentError(reason)

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
        }
        timeout = httpx.Timeout(self.config.timeout_seconds)
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True, trust_env=False) as client:
            if reference_images:
                response = await self._post_edit(client, prompt, reference_images)
            else:
                response = await self._post_generation(client, prompt)
            payload = self._parse_json(response)
            return await self._extract_generated_image(payload)

    async def _post_generation(self, client: httpx.AsyncClient, prompt: str) -> httpx.Response:
        url = _join_url(self.config.base_url, self.config.generation_endpoint)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "prompt": prompt,
            "size": self.config.size,
            "n": 1,
        }
        if self.config.quality:
            payload["quality"] = self.config.quality
        return await self._request_with_retry("generation", lambda: client.post(url, json=payload))

    async def _post_edit(
        self,
        client: httpx.AsyncClient,
        prompt: str,
        reference_images: Sequence[ReferenceImage],
    ) -> httpx.Response:
        url = _join_url(self.config.base_url, self.config.edit_endpoint)
        data: dict[str, Any] = {
            "model": self.config.model,
            "prompt": prompt,
            "size": self.config.size,
            "n": 1,
        }
        if self.config.quality:
            data["quality"] = self.config.quality

        last_error: ImageAgentError | None = None
        for field_name in self._image_field_names():
            files = [
                (field_name, (image.filename, image.content, image.content_type))
                for image in reference_images
            ]
            try:
                return await self._request_with_retry(
                    f"edit field={field_name}",
                    lambda: client.post(url, data=data, files=files),
                )
            except ImageAgentError as e:
                last_error = e
                if not self._is_field_fallback_candidate(str(e)):
                    raise
                logger.warning(f"图片编辑接口字段 {field_name} 失败，尝试下一个字段: {e}")

        if last_error:
            raise last_error
        raise ImageAgentError("图片编辑请求失败")

    def _image_field_names(self) -> list[str]:
        field = self.config.edit_image_field_name
        if not field or field == "auto":
            return ["image[]", "image"]
        return [field]

    async def _request_with_retry(self, operation: str, request_factory) -> httpx.Response:
        max_attempts = self.config.retry_attempts + 1
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = await request_factory()
                self._raise_for_status(response)
                return response
            except ImageAgentError as e:
                last_error = e
                status_code = _extract_error_status(str(e))
                retryable = status_code in {408, 409, 425, 429, 500, 502, 503, 504}
                if attempt >= max_attempts or not retryable:
                    raise
            except httpx.HTTPError as e:
                last_error = e
                if attempt >= max_attempts:
                    raise ImageAgentError(f"网络请求失败: {type(e).__name__}: {e}") from e

            delay = min(self.config.retry_delay_seconds * (2 ** (attempt - 1)), 60.0)
            logger.warning(f"图片接口 {operation} 第 {attempt}/{max_attempts} 次失败，{delay:.1f}s 后重试: {last_error}")
            await asyncio.sleep(max(delay, 0.0))

        raise ImageAgentError(f"{operation} 请求失败: {last_error}")

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = response.text.replace("\n", " ").strip()
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("code") or detail)
            elif isinstance(error, str):
                detail = error
            elif payload.get("message"):
                detail = str(payload["message"])
        raise ImageAgentError(f"{response.status_code}: {detail or response.reason_phrase}")

    def _parse_json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as e:
            raise ImageAgentError(f"图片接口返回了非 JSON 内容: {e}") from e
        if not isinstance(payload, dict):
            raise ImageAgentError("图片接口返回格式不正确")
        return payload

    async def _extract_generated_image(self, payload: dict[str, Any]) -> GeneratedImage:
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise ImageAgentError("图片接口返回里没有图片数据")

        item = data[0]
        if not isinstance(item, dict):
            raise ImageAgentError("图片接口返回的图片项格式不正确")

        revised_prompt = item.get("revised_prompt")
        if isinstance(revised_prompt, str):
            revised_prompt = revised_prompt.strip() or None
        else:
            revised_prompt = None

        b64_json = item.get("b64_json")
        if isinstance(b64_json, str) and b64_json.strip():
            try:
                content = base64.b64decode(b64_json)
            except ValueError as e:
                raise ImageAgentError(f"返回的图片 Base64 无法解码: {e}") from e
            mime_type = _detect_image_mime(content)
            if not mime_type.startswith("image/"):
                mime_type = "image/png"
            return GeneratedImage(content=content, mime_type=mime_type, revised_prompt=revised_prompt)

        image_url = item.get("url")
        if isinstance(image_url, str) and image_url.strip():
            timeout = httpx.Timeout(self.config.download_timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
                response = await client.get(image_url.strip())
            self._raise_for_status(response)
            mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
            if not mime_type.startswith("image/"):
                mime_type = _detect_image_mime(response.content)
            return GeneratedImage(content=response.content, mime_type=mime_type, revised_prompt=revised_prompt)

        raise ImageAgentError("图片接口没有返回可用图片内容")

    def _is_field_fallback_candidate(self, message: str) -> bool:
        return _extract_error_status(message) in {400, 404, 415, 422}


def _extract_error_status(message: str) -> int:
    prefix, _, _ = message.partition(":")
    try:
        return int(prefix.strip())
    except ValueError:
        return 0


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _strip_at(value: str | None) -> str:
    return _normalize_text(value).lstrip("@＠").strip()


def _normalize_reference_image_paths(paths: list[str] | str | None) -> list[str]:
    if paths is None:
        return []
    if isinstance(paths, str):
        text = paths.strip()
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            candidates = [str(item) for item in parsed]
        elif isinstance(parsed, str):
            candidates = [parsed]
        else:
            candidates = re.split(r"[,，、\n]+", text)
    else:
        candidates = [str(path) for path in paths]

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        reference_path = _normalize_text(candidate)
        path_match = re.search(r"(?:^|\s)path=([^\s]+)", reference_path)
        if path_match:
            reference_path = path_match.group(1).strip()
        if not reference_path or reference_path in seen:
            continue
        seen.add(reference_path)
        normalized.append(reference_path)
    return normalized


def _reference_display_name(path: Path) -> str:
    stem = re.sub(r"[_-]+", " ", path.stem).strip()
    return stem or path.name


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


def _load_reference_images(paths: list[str]) -> tuple[list[ReferenceImage], list[str]]:
    references: list[ReferenceImage] = []
    reference_names: list[str] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        if not path.is_file():
            raise ImageAgentError(f"参考图不存在或不是文件: {raw_path}")

        content = path.read_bytes()
        if not content:
            raise ImageAgentError(f"参考图为空: {raw_path}")

        mime_type = _guess_image_mime(path.name, content)
        if not mime_type.startswith("image/"):
            raise ImageAgentError(f"参考图不是图片: {raw_path}")

        references.append(ReferenceImage(filename=path.name, content=content, content_type=mime_type))
        reference_names.append(_reference_display_name(path))
    return references, reference_names


async def _record_generated_image(
    ctx: OptionalToolContext,
    message_id: str,
    prompt: str,
    reference_names: list[str],
) -> None:
    try:
        async with get_session() as db_session:
            reference_desc = f"; reference avatars: {', '.join(reference_names)}" if reference_names else ""
            chat_history = ChatHistory(
                session_id=ctx.session_id,
                user_id=str(ctx.bot_id or ctx.config.bot_name),
                content_type="bot",
                content=f"id: {message_id}\n发送了一张 AI 生成图片，提示词: {prompt}{reference_desc}",
                user_name=ctx.config.bot_name,
            )
            db_session.add(chat_history)
            await db_session.commit()
    except Exception as e:
        logger.warning(f"记录 AI 生成图片到聊天历史失败: {type(e).__name__}: {e}")


def create_image_tool(ctx: OptionalToolContext, config: ImageAgentConfig):
    async def _send_failure(reason: str) -> None:
        if not await _is_current_request_active(ctx):
            logger.info(f"图片生成失败但请求已过期，跳过失败提示: {reason}")
            return
        try:
            await UniMessage.text(f"生成图片失败: {reason}").send()
            _mark_request_sent(ctx)
        except Exception as e:
            logger.warning(f"发送图片生成失败提示失败: {type(e).__name__}: {e}")

    async def _run_image_job(prompt_text: str, reference_paths: list[str]) -> None:
        try:
            references, reference_names = _load_reference_images(reference_paths)

            if references:
                names_text = "、".join(reference_names)
                prompt_text = f"{prompt_text}\n参考图包含：{names_text}。请按用户要求使用参考图，并保留主体身份特征。"

            logger.info(
                f"image_agent request: group={ctx.session_id} user={ctx.user_id} "
                f"prompt_len={len(prompt_text)} references={len(references)}"
            )

            async with _image_lock:
                result = await ImageApiClient(config).generate(prompt_text, references)

            if not await _is_current_request_active(ctx):
                logger.info("图片已生成但请求不再允许发送，跳过发送图片")
                return

            send_result = await UniMessage.image(raw=result.content).send()
            _mark_request_sent(ctx)
            msg_id = send_result.msg_ids[-1]["message_id"] if send_result.msg_ids else "unknown"
            await _record_generated_image(ctx, str(msg_id), prompt_text, reference_names)
            logger.info(f"AI 生成图片已发送: message_id={msg_id} references={reference_names}")
        except ImageAgentError as e:
            logger.warning(f"图片生成工具执行失败: {e}")
            await _send_failure(str(e))
        except httpx.HTTPError as e:
            logger.warning(f"图片生成网络请求失败: {type(e).__name__}: {e}")
            await _send_failure(f"网络请求失败 {type(e).__name__}: {e}")
        except Exception as e:
            logger.exception(f"图片生成工具异常: {e}")
            await _send_failure(f"{type(e).__name__}: {e}")

    @tool("generate_and_send_image", args_schema=GenerateImageArgs)
    async def generate_and_send_image(
        prompt: str,
        reference_image_paths: list[str] | str | None = None,
    ) -> str:
        """
        启动后台图片生成任务，完成后发送到当前群聊。

        只有用户明确要求生成、画、做、P、改造或风格化一张图片时才调用。
        如果用户只是要求发送或查看原始 QQ 头像，不要调用本工具。
        如果用户要求“给某个用户头像做图 / 用某人头像生成”，必须先调用主插件内置的
        fetch_qq_avatar_references，再把返回的本地文件路径放进 reference_image_paths。

        Args:
            prompt: 最终生图提示词。
            reference_image_paths: 主插件内置头像工具返回的本地参考图路径。
        """
        if not await _is_current_request_active(ctx):
            return "请求已过期，已取消生成图片"

        prompt_text = _normalize_text(prompt)
        if not prompt_text:
            return "提示词为空，未生成图片"
        if len(prompt_text) > config.max_prompt_length:
            return f"提示词过长，请控制在 {config.max_prompt_length} 字以内"

        reference_paths = _normalize_reference_image_paths(reference_image_paths)
        if len(reference_paths) > config.max_reference_avatars:
            return f"最多只能引用 {config.max_reference_avatars} 张参考图"

        try:
            _load_reference_images(reference_paths)
        except ImageAgentError as e:
            return str(e)

        create_detached_task = getattr(ctx, "create_detached_task", None)
        if create_detached_task is None:
            return "当前请求不支持后台长任务，未生成图片"

        create_detached_task(
            _run_image_job(prompt_text, reference_paths),
            "gpt_image_agent.generate_and_send_image",
        )
        return "图片生成任务已开始，完成后会发送结果。"

    return generate_and_send_image


async def healthcheck(ctx: OptionalToolContext) -> tuple[bool, str]:
    return _validate_config(_load_config())


async def build(ctx: OptionalToolContext) -> OptionalToolBundle:
    config = _load_config()
    if ctx.is_cross_user_direct_reply or not config.enabled:
        return OptionalToolBundle(name="gpt_image_agent")

    image_tool = create_image_tool(ctx, config)

    prompt = f"""- AI 生图：可使用 `generate_and_send_image` 生成并发送新图片
  - 只有用户明确要求“生成图片 / 画图 / 做图 / P图 / 改图 / 风格化头像 / 头像二创”时才调用
  - 用户只是要求“发送头像 / 发原头像 / 查看 QQ 头像 / 看看头像”时，不要调用本工具；应调用 `send_qq_avatar_image`
  - 用户只是聊天、评价图片、问你怎么看时，不要调用
  - `prompt` 必须是完整生图需求，包含主体、风格、构图、文字约束；需要头像参考时也写清头像处理要求
  - 用户说“给某人头像...”或“用某人的头像...”时，必须先调用主插件内置的 `fetch_qq_avatar_references`
  - 头像工具会返回本地头像文件路径；带头像生图时，必须把这些路径填入 `generate_and_send_image.reference_image_paths`
  - 当前最多引用 {config.max_reference_avatars} 张参考图
  - 用户要求“根据历史发言 / 互动记录 / 活动记录 / 以前聊天 / 群内记录”生成图片时，必须先调用 `search_history_context` 检索真实聊天历史，再调用 `generate_and_send_image`
  - 这类请求的 `prompt` 必须包含检索到的真实历史摘要、人物关系和具体互动元素；禁止凭空编造“历史”
  - 如果 `search_history_context` 没找到相关历史，不能假装查到了；可以说明未找到，或只按用户当前提供的信息生成
  - 工具返回“图片生成任务已开始”时只表示后台任务已启动，不代表图片已经发出；不要提前说“发了”
  - 后台任务完成后会自行发送图片；当前 Agent 只需调用 `finish`，或简短回复“在生成”
  - 如果工具返回失败，不要假装已经发图；可以用 `reply_user` 简短说明失败原因
"""
    return OptionalToolBundle(
        name="gpt_image_agent",
        tools=[image_tool],
        prompt=prompt,
        tool_limits=[
            ToolLimitSpec(tool_name="generate_and_send_image", run_limit=1),
        ],
    )
