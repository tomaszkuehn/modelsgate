"""Google Gemini provider — supports Gemini 2.5 Pro, Flash, etc."""

import logging
from typing import List

import google.generativeai as genai

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


class GeminiProvider(BaseModelProvider):
    """Provider for Google Gemini models with vision support."""

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        api_key = config.api_key or settings.gemini_api_key
        print(f"[DEBUG] GeminiProvider init: model={config.name}, api_key_from_config={'set' if config.api_key else 'empty'}, api_key_from_env={'set' if settings.gemini_api_key else 'empty'}, using={'config' if config.api_key else 'env' if settings.gemini_api_key else 'NONE'}")
        logger.warning(f"GeminiProvider init: model={config.name}, api_key_from_config={'set' if config.api_key else 'empty'}, api_key_from_env={'set' if settings.gemini_api_key else 'empty'}, using={'config' if config.api_key else 'env' if settings.gemini_api_key else 'NONE'}")
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(config.model_id)

    async def _generate(self, request: NormalizedTaskRequest) -> UnifiedResponse:
        logger.warning(f"GeminiProvider _generate: model={request.model}, config.api_key={'set' if self.config.api_key else 'empty'}, env={'set' if settings.gemini_api_key else 'empty'}")
        if not (self.config.api_key or settings.gemini_api_key):
            return self.make_response(
                task_type=request.task_type,
                model=request.model,
                error=(
                    "Gemini API key not configured. Set GEMINI_API_KEY in .env "
                    "or add an api_key on the model in Admin → Models."
                ),
            )
        try:
            # Convert unified messages to Gemini format
            contents = self._convert_messages(request.messages)

            generation_config = {}
            params = request.parameters
            if params:
                if params.temperature is not None:
                    generation_config["temperature"] = params.temperature
                if params.max_tokens is not None:
                    generation_config["max_output_tokens"] = params.max_tokens
                if params.top_p is not None:
                    generation_config["top_p"] = params.top_p

            response = await self.model.generate_content_async(
                contents,
                generation_config=generation_config if generation_config else None,
            )

            # Build content from response
            content: List[ContentBlock] = []
            try:
                if response.text:
                    content.append(TextContent(text=response.text))
            except ValueError:
                # Multi-part response
                for part in response.parts:
                    if hasattr(part, "text") and part.text:
                        content.append(TextContent(text=part.text))

            # Gemini doesn't provide exact token counts in the same way
            usage = UsageInfo(
                prompt_tokens=response.usage_metadata.prompt_token_count if hasattr(response, 'usage_metadata') and response.usage_metadata else 0,
                completion_tokens=response.usage_metadata.candidates_token_count if hasattr(response, 'usage_metadata') and response.usage_metadata else 0,
                total_tokens=response.usage_metadata.total_token_count if hasattr(response, 'usage_metadata') and response.usage_metadata else 0,
            )

            return UnifiedResponse(
                task_type=request.task_type,
                model=request.model,
                content=content,
                usage=usage,
            )

        except Exception as e:
            logger.error(f"Gemini provider error: {e}")
            return self.make_response(
                task_type=request.task_type,
                model=request.model,
                error=f"Gemini error: {str(e)}",
            )

    def _convert_messages(self, messages: List[Message]) -> List[dict]:
        """Convert unified messages to Gemini's content format."""
        gemini_contents = []

        for msg in messages:
            parts = []
            for block in msg.content:
                if isinstance(block, TextContent):
                    parts.append({"text": block.text})
                elif isinstance(block, ImageContent):
                    image_data = block.image
                    mime_type = "image/png"
                    if image_data.startswith("data:"):
                        header, image_data = image_data.split("base64,", 1)
                        if "image/" in header:
                            mime_type = header.split(":")[1].split(";")[0]
                    parts.append({
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": image_data,
                        }
                    })

            # Gemini uses "user" and "model" roles
            role = "user" if msg.role in ("user", "system") else "model"
            gemini_contents.append({"role": role, "parts": parts})

        return gemini_contents
