from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.tools import tool
from langchain_core.messages import HumanMessage as LCHumanMessage
from langchain_core.messages import SystemMessage as LCSystemMessage
from langchain_openai import ChatOpenAI
from nonebot.log import logger
from pydantic import BaseModel, Field, SecretStr

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
            config: Any = None
            model: Any = None
            stop_words: list[str] | None = None
            detach_request: Any = None
            can_continue: Any = None
            mark_sent: Any = None
            clear_detached: Any = None
            create_detached_task: Any = None


DEFAULT_QUESTION = (
    "请用中文描述这个 QQ 头像长什么样。重点说明画面主体、人物/角色特征、表情、颜色、文字、"
    "风格和可能传达的氛围。不要识别真实身份，不要编造看不见的细节。"
)
MAX_AVATAR_IMAGES = 2
MULTIMODAL_TIMEOUT_SECONDS = 90.0


class DescribeQQAvatarArgs(BaseModel):
    reference_image_paths: list[str] | str = Field(
        description=(
            "主插件内置 fetch_qq_avatar_references 返回的本地头像图片路径；"
            "可以直接传完整返回文本，工具会自动提取 path=..."
        ),
    )
    question: str | None = Field(
        default=None,
        description="用户对头像的具体问题；为空时默认描述头像长什么样。",
    )


class QQAvatarDescribeError(RuntimeError):
    pass


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


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


def _extract_path_values(text: str) -> list[str]:
    values: list[str] = []
    for line in text.splitlines():
        matches = re.findall(r"path=(.+?)(?=\s+(?:name|qq|url)=|$)", line)
        values.extend(match.strip() for match in matches if match.strip())
    return values


def _split_path_candidates(text: str) -> list[str]:
    path_matches = _extract_path_values(text)
    if path_matches:
        return path_matches

    lines: list[str] = []
    for line in text.splitlines():
        lines.extend(part.strip() for part in re.split(r"[,，、]+", line) if part.strip())
    return lines


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
            candidates = _split_path_candidates(text)
    else:
        candidates = [str(path) for path in paths]

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        extracted_paths = _extract_path_values(str(candidate))
        reference_path = extracted_paths[0] if extracted_paths else _normalize_text(candidate)
        if not reference_path or reference_path in seen:
            continue
        seen.add(reference_path)
        normalized.append(reference_path)
    return normalized


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


def _image_to_data_uri(file_path: Path) -> str:
    content = file_path.read_bytes()
    payload = base64.b64encode(content).decode("utf-8")
    return f"data:{_detect_image_mime(content)};base64,{payload}"


def _resolve_image_paths(paths: list[str]) -> list[Path]:
    resolved: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        if not path.is_file():
            raise QQAvatarDescribeError(f"头像参考图不存在或不是文件: {raw_path}")
        content = path.read_bytes()
        if not content:
            raise QQAvatarDescribeError(f"头像参考图为空: {raw_path}")
        if not _detect_image_mime(content).startswith("image/"):
            raise QQAvatarDescribeError(f"头像参考图不是图片: {raw_path}")
        resolved.append(path)
    return resolved


def _create_multimodal_model(ctx: OptionalToolContext) -> ChatOpenAI | None:
    config = getattr(ctx, "config", None)
    model = _normalize_text(getattr(config, "multimodal_model_resolved", ""))
    api_key = _normalize_text(getattr(config, "multimodal_api_key_resolved", ""))
    base_url = _normalize_text(getattr(config, "multimodal_base_url_resolved", ""))
    if not model or not api_key:
        return None
    return ChatOpenAI(
        model=model,
        api_key=SecretStr(api_key),
        base_url=base_url or None,
        temperature=0.01,
    )


def _build_multimodal_content(question: str, image_paths: list[Path]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": question}]
    for image_path in image_paths:
        content.append({"type": "image_url", "image_url": {"url": _image_to_data_uri(image_path)}})
    return content


async def _is_current_request_active(ctx: OptionalToolContext) -> bool:
    can_continue = getattr(ctx, "can_continue", None)
    if can_continue is None:
        return True
    return await can_continue()


def create_avatar_describer_tool(ctx: OptionalToolContext):
    @tool("describe_qq_avatar_image", args_schema=DescribeQQAvatarArgs)
    async def describe_qq_avatar_image(
        reference_image_paths: list[str] | str,
        question: str | None = None,
    ) -> str:
        """
        调用多模态模型查看 QQ 头像参考图，并返回头像内容描述。

        当用户要求“看看头像长什么样 / 描述头像 / 分析头像内容 / 这个头像是什么”时调用。
        必须先调用主插件内置 fetch_qq_avatar_references 获取头像本地 path，再把返回的 path
        或完整返回文本传给 reference_image_paths。本工具不会匹配 QQ 号，也不会下载头像。

        Args:
            reference_image_paths: fetch_qq_avatar_references 返回的本地头像图片路径。
            question: 用户对头像的具体问题；为空时默认描述头像长什么样。
        """
        if not await _is_current_request_active(ctx):
            return "请求已过期，已取消查看头像。"

        raw_paths = _normalize_reference_image_paths(reference_image_paths)
        if not raw_paths:
            return "未提供头像参考图路径。请先调用 fetch_qq_avatar_references 获取 path。"
        if len(raw_paths) > MAX_AVATAR_IMAGES:
            return f"一次最多只能查看 {MAX_AVATAR_IMAGES} 张头像。"

        model = _create_multimodal_model(ctx)
        if model is None:
            return "未配置多模态模型，无法查看头像。请配置 multimodal_model 和 multimodal_api_key/qwen_key。"

        try:
            image_paths = _resolve_image_paths(raw_paths)
            prompt = _normalize_text(question) or DEFAULT_QUESTION
            response = await asyncio.wait_for(
                model.ainvoke(
                    [
                        LCSystemMessage(
                            content=(
                                "你是一个谨慎的图片分析助手。只描述图片里能看到的内容；"
                                "不要识别真实人物身份，不要推断隐私属性。"
                            )
                        ),
                        LCHumanMessage(content=_build_multimodal_content(prompt, image_paths)),
                    ]
                ),
                timeout=MULTIMODAL_TIMEOUT_SECONDS,
            )
            text = _strip_code_fence(_extract_model_text(response.content))
            return text or "多模态模型没有返回可用描述。"
        except asyncio.TimeoutError:
            logger.warning(f"QQ 头像多模态描述超时: {raw_paths}")
            return "查看头像超时了。"
        except QQAvatarDescribeError as e:
            logger.warning(f"QQ 头像描述失败: {e}")
            return f"查看头像失败: {e}"
        except Exception as e:
            logger.exception(f"QQ 头像描述工具异常: {e}")
            return f"查看头像失败: {type(e).__name__}: {e}"

    return describe_qq_avatar_image


async def healthcheck(ctx: OptionalToolContext) -> tuple[bool, str]:
    model = _normalize_text(getattr(getattr(ctx, "config", None), "multimodal_model_resolved", ""))
    api_key = _normalize_text(getattr(getattr(ctx, "config", None), "multimodal_api_key_resolved", ""))
    if not model:
        return False, "missing multimodal_model"
    if not api_key:
        return False, "missing multimodal_api_key or qwen_key"
    return True, "ok"


async def build(ctx: OptionalToolContext) -> OptionalToolBundle:
    if ctx.is_cross_user_direct_reply:
        return OptionalToolBundle(name="qq_avatar_describer")

    prompt = f"""- QQ 头像看图描述：可使用 `describe_qq_avatar_image` 调用多模态模型查看 QQ 头像内容
  - 用户要求“头像长什么样 / 描述头像 / 分析头像 / 看看头像内容 / 这个头像是什么”时：
    先调用主插件内置 `fetch_qq_avatar_references` 获取头像 path，再调用 `describe_qq_avatar_image`
  - 用户只是要求“发头像 / 发原头像 / 把头像发群里”时，不要调用本工具；应调用 `send_qq_avatar_image`
  - 用户要求“用头像生成图片 / 头像二创 / P图 / 改头像”时，不要调用本工具；应调用 `fetch_qq_avatar_references` 后交给生图工具
  - `describe_qq_avatar_image.reference_image_paths` 可以直接填写 `fetch_qq_avatar_references` 的完整返回文本或其中的 path
  - 当前最多查看 {MAX_AVATAR_IMAGES} 张头像
"""
    return OptionalToolBundle(
        name="qq_avatar_describer",
        tools=[create_avatar_describer_tool(ctx)],
        prompt=prompt,
        tool_limits=[ToolLimitSpec(tool_name="describe_qq_avatar_image", run_limit=1)],
    )
