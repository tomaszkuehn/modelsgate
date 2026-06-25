"""Alibaba DashScope provider — Qwen models via native multimodal generation API.

API docs: https://help.aliyun.com/zh/model-studio/
Base URL: https://dashscope.aliyuncs.com/api/v1
Auth: API key in Authorization: Bearer header
"""

import logging
from typing import List

import httpx

from app.config import settings
from app.models.base import BaseModelProvider, ModelConfig
from app.logs.provider_logger import log_outgoing_request
from app.api.schemas import (
    NormalizedTaskRequest,
    UnifiedResponse,
    ContentBlock,
    Message,
    TextContent,
    ImageContent,
    UsageInfo,
)

logger = logging.getLogger(__name__)

# Native DashScope multimodal generation endpoint (China region)
DASHSCOPE_API_BASE = "https://dashscope.aliyuncs.com/api/v1"
DASHSCOPE_GENERATION_PATH = "/services/aigc/multimodal-generation/generation"


class AlibabaProvider(BaseModelProvider):
    """Provider for Alibaba DashScope models (Qwen series).

    Uses the native DashScope multimodal generation API.
    Supports Qwen text models and vision-capable Qwen-VL variants.
    """

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self.api_key = config.api_key or settings.alibaba_api_key or settings.dashscope_api_key
        # Derive native API base from configured base_url, or use default
        if config.base_url:
            # configured URL may point to compatible-mode or native; derive native base
            raw = config.base_url.rstrip("/")
            if "/compatible-mode" in raw:
                self.api_base = raw.split("/compatible-mode")[0] + "/api/v1"
            elif raw.endswith("/api/v1"):
                self.api_base = raw
            else:
                self.api_base = raw + "/api/v1"
        else:
            self.api_base = DASHSCOPE_API_BASE

    def _convert_messages(self, messages: List[Message]) -> List[dict]:
        """Convert unified messages to native DashScope multimodal format.

        Public interface used by trace logging to capture the converted request.
        """
        return self._build_dashscope_messages(messages)

    async def _generate(self, request: NormalizedTaskRequest) -> UnifiedResponse:
        if not self.api_key:
            return self.make_response(
                task_type=request.task_type,
                model=request.model,
                error=(
                    "Alibaba API key not configured. Set DASHSCOPE_API_KEY or ALIBABA_API_KEY in .env "
                    "or add an api_key on the model in Admin → Models."
                ),
            )
        try:
            # Build native DashScope multimodal request body
            msgs = self._build_dashscope_messages(request.messages)
            body = {
                "model": self.config.model_id,
                "input": {"messages": msgs},
            }
            params = {}
            if request.parameters:
                if request.parameters.temperature is not None:
                    params["temperature"] = request.parameters.temperature
                if request.parameters.max_tokens is not None:
                    params["max_tokens"] = request.parameters.max_tokens
                if request.parameters.top_p is not None:
                    params["top_p"] = request.parameters.top_p
            if params:
                body["parameters"] = params

            destination = f"{self.api_base}{DASHSCOPE_GENERATION_PATH}"
            log_outgoing_request("alibaba", self.config.model_id,
                destination, msgs, self.api_key, params if params else None)

            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    destination,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            # Parse DashScope response
            content: List[ContentBlock] = []
            output = data.get("output", {})
            choices = output.get("choices", [])
            if choices:
                msg = choices[0].get("message", {})
                msg_content = msg.get("content", [])
                for part in msg_content if isinstance(msg_content, list) else [msg_content]:
                    if isinstance(part, dict):
                        if "text" in part:
                            content.append(TextContent(text=part["text"]))
                        elif "image" in part:
                            # image URLs or base64 from response
                            content.append(ImageContent(image=part["image"]))
                    elif isinstance(part, str):
                        content.append(TextContent(text=part))

            usage = None
            if "usage" in data:
                u = data["usage"]
                usage = UsageInfo(
                    prompt_tokens=u.get("input_tokens", 0),
                    completion_tokens=u.get("output_tokens", 0),
                    total_tokens=u.get("total_tokens", u.get("input_tokens", 0) + u.get("output_tokens", 0)),
                )

            return UnifiedResponse(
                task_type=request.task_type,
                model=request.model,
                content=content,
                usage=usage,
            )

        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text

            # Normalise detail to a string for inspection
            detail_str = str(detail)

            logger.error(f"Alibaba provider error: {e.response.status_code} — {detail_str}")

            # Map Alibaba content-safety rejections to a clear client message.
            # "Data inspection failed" = the model's safety filter flagged the
            # input image(s) or prompt. Don't pass the raw API error back.
            if "data inspection failed" in detail_str.lower():
                return UnifiedResponse(
                    task_type=request.task_type,
                    model=request.model,
                    content=[],
                    error=(
                        "Request rejected by model content policy. "
                        "The input image(s) or prompt were flagged by the "
                        "provider's safety inspection. Try with different "
                        "images or rephrase the instruction."
                    ),
                    error_code="CONTENT_POLICY_REJECTION",
                )

            return self.make_response(
                task_type=request.task_type,
                model=request.model,
                error=f"Alibaba error: {e.response.status_code} — {detail_str}",
            )
        except Exception as e:
            logger.error(f"Alibaba provider error: {e}")
            return self.make_response(
                task_type=request.task_type,
                model=request.model,
                error=f"Alibaba error: {str(e)}",
            )

    def _build_dashscope_messages(self, messages: List[Message]) -> List[dict]:
        """Convert unified messages to native DashScope multimodal format.

        DashScope content is a flat array of {"image": "..."} and {"text": "..."}
        objects (not the OpenAI nested type/image_url format).

        The DashScope multimodal API requires messages[0].role == "user", so any
        leading system messages injected by workflows (e.g. image_edit) are
        extracted and their text is prepended to the first user message.
        """
        msgs = list(messages)

        # Extract leading system messages — the DashScope multimodal API
        # rejects {role:"system"} at position 0. Merge their text into the
        # first user message instead.
        system_texts = []
        while msgs and msgs[0].role == "system":
            for block in msgs[0].content:
                if isinstance(block, TextContent) and block.text.strip():
                    system_texts.append(block.text.strip())
            msgs.pop(0)

        result = []
        for i, msg in enumerate(msgs):
            content_parts = []
            for block in msg.content:
                if isinstance(block, TextContent) and block.text.strip():
                    content_parts.append({"text": block.text})
                elif isinstance(block, ImageContent):
                    image_url = block.image
                    if not image_url.startswith(("http://", "https://", "data:")):
                        image_url = f"data:image/png;base64,{image_url}"
                    content_parts.append({"image": image_url})

            if not content_parts:
                continue

            # Prepend extracted system text to the first user message
            if i == 0 and system_texts and msg.role == "user":
                prefix = "\n\n".join(system_texts)
                if content_parts and "text" in content_parts[0]:
                    content_parts[0]["text"] = prefix + "\n\n" + content_parts[0]["text"]
                else:
                    content_parts.insert(0, {"text": prefix})

            # If only text and no images, use simple string content
            text_only = all("text" in p for p in content_parts)
            if text_only and len(content_parts) == 1:
                result.append({"role": msg.role, "content": content_parts[0]["text"]})
            else:
                result.append({"role": msg.role, "content": content_parts})

        return result
