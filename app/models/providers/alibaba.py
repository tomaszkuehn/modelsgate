"""Alibaba DashScope provider — Qwen models via OpenAI-compatible endpoint.

API docs: https://help.aliyun.com/zh/model-studio/
Base URL: https://dashscope.aliyuncs.com/compatible-mode/v1
Auth: API key in Authorization header
"""

import logging
from typing import List

from openai import AsyncOpenAI

from app.config import settings
from app.models.base import BaseModelProvider, ModelConfig
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

ALIBABA_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class AlibabaProvider(BaseModelProvider):
    """Provider for Alibaba DashScope models (Qwen series).

    Uses the OpenAI-compatible /chat/completions endpoint.
    Supports Qwen text models and vision-capable Qwen-VL variants.
    """

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        api_key = config.api_key or settings.alibaba_api_key
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=ALIBABA_BASE_URL,
        )

    async def _generate(self, request: NormalizedTaskRequest) -> UnifiedResponse:
        if not (self.config.api_key or settings.alibaba_api_key):
            return self.make_response(
                task_type=request.task_type,
                model=request.model,
                error=(
                    "Alibaba API key not configured. Set ALIBABA_API_KEY in .env "
                    "or add an api_key on the model in Admin → Models."
                ),
            )
        try:
            messages = self._convert_messages(request.messages)

            kwargs = {"model": self.config.model_id, "messages": messages}

            params = request.parameters
            if params:
                if params.max_tokens is not None:
                    kwargs["max_tokens"] = params.max_tokens
                if params.temperature is not None:
                    kwargs["temperature"] = params.temperature
                if params.top_p is not None:
                    kwargs["top_p"] = params.top_p
                if params.stop:
                    kwargs["stop"] = params.stop

            response = await self.client.chat.completions.create(**kwargs)

            choice = response.choices[0]
            content: List[ContentBlock] = []
            if choice.message.content:
                content.append(TextContent(text=choice.message.content))

            usage = None
            if response.usage:
                usage = UsageInfo(
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    total_tokens=response.usage.total_tokens,
                )

            return UnifiedResponse(
                task_type=request.task_type,
                model=request.model,
                content=content,
                usage=usage,
            )

        except Exception as e:
            logger.error(f"Alibaba provider error: {e}")
            return self.make_response(
                task_type=request.task_type,
                model=request.model,
                error=f"Alibaba error: {str(e)}",
            )

    def _convert_messages(self, messages: List[Message]) -> List[dict]:
        """Convert unified messages to OpenAI-compatible format."""
        openai_messages = []
        for msg in messages:
            texts = self.extract_texts(msg.content)
            images = self.extract_images(msg.content)

            if images:
                content_parts = []
                for block in msg.content:
                    if isinstance(block, TextContent):
                        content_parts.append({"type": "text", "text": block.text})
                    elif isinstance(block, ImageContent):
                        image_url = block.image
                        if not image_url.startswith("http"):
                            image_url = f"data:image/png;base64,{image_url}"
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": image_url, "detail": "auto"},
                        })
                openai_messages.append({"role": msg.role, "content": content_parts})
            else:
                combined = "\n".join(texts) if texts else ""
                openai_messages.append({"role": msg.role, "content": combined})

        return openai_messages
