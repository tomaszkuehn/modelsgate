"""Deepseek provider — deepseek-chat, deepseek-reasoner via OpenAI-compatible API.

API docs: https://platform.deepseek.com/api-docs
Base URL: https://api.deepseek.com/v1
Auth: Bearer token
"""

import logging
from typing import List

from openai import AsyncOpenAI

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

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class DeepseekProvider(BaseModelProvider):
    """Provider for Deepseek models (deepseek-chat, deepseek-reasoner).

    Uses the OpenAI-compatible /chat/completions endpoint.
    deepseek-chat: general-purpose chat model
    deepseek-reasoner: reasoning-focused model with chain-of-thought
    """

    def __init__(self, config: ModelConfig):
        super().__init__(config)

    def _get_client(self):
        """Lazily create the API client so missing keys are caught cleanly."""
        key = self.config.api_key or settings.deepseek_api_key
        if not key:
            return None
        return AsyncOpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)

    async def _generate(self, request: NormalizedTaskRequest) -> UnifiedResponse:
        client = self._get_client()
        if client is None:
            return self.make_response(
                task_type=request.task_type,
                model=request.model,
                error=(
                    "Deepseek API key not configured. Set DEEPSEEK_API_KEY in .env "
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

            log_outgoing_request("deepseek", self.config.model_id,
                DEEPSEEK_BASE_URL + "/chat/completions",
                messages, self.config.api_key or settings.deepseek_api_key, kwargs)
            response = await client.chat.completions.create(**kwargs)

            choice = response.choices[0]
            content: List[ContentBlock] = []
            if choice.message.content:
                content.append(TextContent(text=choice.message.content))
            # Deepseek reasoner returns reasoning in choice.message.reasoning_content
            if hasattr(choice.message, 'reasoning_content') and choice.message.reasoning_content:
                content.insert(0, TextContent(
                    text=f"[Reasoning]\n{choice.message.reasoning_content}\n[/Reasoning]"
                ))

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
            logger.error(f"Deepseek provider error: {e}")
            return self.make_response(
                task_type=request.task_type,
                model=request.model,
                error=f"Deepseek error: {str(e)}",
            )

    def _convert_messages(self, messages: List[Message]) -> List[dict]:
        """Convert unified messages to OpenAI-compatible format.

        Deepseek models are text-only — images are not supported.
        """
        openai_messages = []
        for msg in messages:
            texts = self.extract_texts(msg.content)
            combined = "\n".join(texts) if texts else ""
            openai_messages.append({"role": msg.role, "content": combined})
        return openai_messages
