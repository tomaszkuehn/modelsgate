"""Ollama provider — supports local models via Ollama API."""

import base64
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


class OllamaProvider(BaseModelProvider):
    """Provider for Ollama-hosted local models with vision support.

    Uses Ollama's HTTP API directly since the Python client is synchronous.
    """

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self.base_url = settings.ollama_base_url.rstrip("/")

    async def _generate(self, request: NormalizedTaskRequest) -> UnifiedResponse:
        try:
            prompt, images = self._build_prompt_and_images(request.messages)

            payload = {
                "model": self.config.model_id,
                "prompt": prompt,
                "stream": False,
                "options": {},
            }

            if images:
                payload["images"] = images

            params = request.parameters
            if params:
                if params.temperature is not None:
                    payload["options"]["temperature"] = params.temperature
                if params.max_tokens is not None:
                    payload["options"]["num_predict"] = params.max_tokens
                if params.top_p is not None:
                    payload["options"]["top_p"] = params.top_p

            log_outgoing_request("ollama", self.config.model_id,
                f"{self.base_url}/api/generate",
                payload, "")
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()

            content: List[ContentBlock] = []
            if data.get("response"):
                content.append(TextContent(text=data["response"]))

            # Ollama provides some token counts
            usage = UsageInfo(
                prompt_tokens=data.get("prompt_eval_count", 0),
                completion_tokens=data.get("eval_count", 0),
                total_tokens=data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
            )

            return UnifiedResponse(
                task_type=request.task_type,
                model=request.model,
                content=content,
                usage=usage,
            )

        except Exception as e:
            logger.error(f"Ollama provider error: {e}")
            return self.make_response(
                task_type=request.task_type,
                model=request.model,
                error=f"Ollama error: {str(e)}",
            )

    def _build_prompt_and_images(self, messages: List[Message]) -> tuple:
        """Convert unified messages to Ollama's prompt + images format.

        Ollama expects a single prompt string and a list of base64-encoded images.
        """
        prompt_parts = []
        images = []

        for msg in messages:
            for block in msg.content:
                if isinstance(block, TextContent):
                    prompt_parts.append(block.text)
                elif isinstance(block, ImageContent):
                    prompt_parts.append("[image]")
                    image_data = block.image
                    # Strip data URI prefix if present
                    if image_data.startswith("data:") and "base64," in image_data:
                        image_data = image_data.split("base64,", 1)[1]
                    images.append(image_data)

        return "\n".join(prompt_parts), images
