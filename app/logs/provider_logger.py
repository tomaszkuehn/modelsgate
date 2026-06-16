"""Log outgoing provider requests with full details to the server log."""

import logging
from typing import Optional

logger = logging.getLogger("app.provider")
logger.setLevel(logging.INFO)


def log_outgoing_request(
    provider: str,
    model: str,
    destination: str,
    messages: list,
    api_key: str,
    extra_params: Optional[dict] = None,
):
    """Log a complete outgoing AI model request to the server info log.

    Args:
        provider: Provider name (openai, anthropic, alibaba, …).
        destination: Full HTTP endpoint URL.
        model: Model ID sent to the provider.
        messages: The complete message payload (provider-native format).
        api_key: The API key used (will be shown masked).
        extra_params: Any extra request params (temperature, max_tokens, …).
    """
    masked = api_key[:8] + "…" + api_key[-4:] if len(api_key) > 12 else (api_key[:8] + "…" if api_key else "<none>")
    logger.warning(
        f"→ {provider.upper()} | model={model} | key={masked} | dest={destination}"
    )
    logger.warning(f"  messages={messages}")
    if extra_params:
        logger.warning(f"  params={extra_params}")
