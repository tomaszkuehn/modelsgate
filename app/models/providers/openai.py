"""OpenAI provider — supports GPT-4o, GPT-4 Turbo, etc."""

import base64
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


class OpenAIProvider(BaseModelProvider):
    """Provider for OpenAI GPT models with vision support."""

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        api_key = config.api_key or settings.openai_api_key
        self.client = AsyncOpenAI(api_key=api_key)

    async def _generate(self, request: NormalizedTaskRequest) -> UnifiedResponse:
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
            logger.error(f"OpenAI provider error: {e}")
            return self.make_response(
                task_type=request.task_type,
                model=request.model,
                error=f"OpenAI error: {str(e)}",
            )

    def _convert_messages(self, messages: List[Message]) -> List[dict]:
        """Convert unified messages to OpenAI's chat completion format."""
        openai_messages = []

        for msg in messages:
            # Check if all content is text — use simple string format
            texts = self.extract_texts(msg.content)
            images = self.extract_images(msg.content)

            if images:
                # Vision format with content array
                content_parts = []
                for block in msg.content:
                    if isinstance(block, TextContent):
                        content_parts.append({"type": "text", "text": block.text})
                    elif isinstance(block, ImageContent):
                        image_url = block.image
                        if not image_url.startswith("http"):
                            # Assume base64, add data URI prefix if missing
                            image_url = f"data:image/png;base64,{image_url}"
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": image_url, "detail": "auto"},
                        })
                openai_messages.append({"role": msg.role, "content": content_parts})
            else:
                # Text-only — concatenate all text blocks
                combined = "\n".join(texts) if texts else ""
                openai_messages.append({"role": msg.role, "content": combined})

        return openai_messages
