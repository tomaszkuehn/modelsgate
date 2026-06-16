"""Anthropic provider — supports Claude Sonnet, Haiku, etc."""

import logging
from typing import List

from anthropic import AsyncAnthropic

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


class AnthropicProvider(BaseModelProvider):
    """Provider for Anthropic Claude models with vision support."""

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        api_key = config.api_key or settings.anthropic_api_key
        self.client = AsyncAnthropic(api_key=api_key)

    async def _generate(self, request: NormalizedTaskRequest) -> UnifiedResponse:
        try:
            system_content = None
            user_messages = []

            for msg in request.messages:
                if msg.role == "system":
                    texts = self.extract_texts(msg.content)
                    system_content = "\n".join(texts) if texts else None
                else:
                    user_messages.append(msg)

            # Build Anthropic content blocks
            content_blocks = []
            for msg in user_messages:
                for block in msg.content:
                    if isinstance(block, TextContent):
                        content_blocks.append({"type": "text", "text": block.text})
                    elif isinstance(block, ImageContent):
                        media_type = "image/png"
                        image_data = block.image
                        if image_data.startswith("data:"):
                            # Extract media type from data URI
                            header, image_data = image_data.split("base64,", 1)
                            if "image/" in header:
                                media_type = header.split(":")[1].split(";")[0]
                        content_blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_data,
                            },
                        })

            kwargs = {
                "model": self.config.model_id,
                "messages": [{"role": "user", "content": content_blocks}],
                "max_tokens": 1024,
            }

            if system_content:
                kwargs["system"] = system_content

            params = request.parameters
            if params:
                if params.max_tokens is not None:
                    kwargs["max_tokens"] = params.max_tokens
                if params.temperature is not None:
                    kwargs["temperature"] = params.temperature
                if params.top_p is not None:
                    kwargs["top_p"] = params.top_p
                if params.stop:
                    kwargs["stop_sequences"] = params.stop

            log_outgoing_request("anthropic", self.config.model_id,
                "https://api.anthropic.com/v1/messages",
                kwargs["messages"], self.config.api_key or settings.anthropic_api_key, kwargs)
            response = await self.client.messages.create(**kwargs)

            content: List[ContentBlock] = []
            for block in response.content:
                if block.type == "text":
                    content.append(TextContent(text=block.text))
                elif block.type == "image":
                    # Anthropic can return images
                    content.append(ImageContent(image=block.source.data))

            usage = UsageInfo(
                prompt_tokens=response.usage.input_tokens or 0,
                completion_tokens=response.usage.output_tokens or 0,
                total_tokens=(response.usage.input_tokens or 0) + (response.usage.output_tokens or 0),
            )

            return UnifiedResponse(
                task_type=request.task_type,
                model=request.model,
                content=content,
                usage=usage,
            )

        except Exception as e:
            logger.error(f"Anthropic provider error: {e}")
            return self.make_response(
                task_type=request.task_type,
                model=request.model,
                error=f"Anthropic error: {str(e)}",
            )
