"""Normalized image_compare workflow.

Sits between the API route handler and the provider layer.
Validates inputs, builds system prompts for structured comparison,
transforms messages for provider consumption, and parses structured
JSON results from model responses.

Flow:
  TaskRequest (image_compare, 2+ images)
    → validate_inputs()
    → build_workflow_messages()
    → provider generates response
    → parse_compare_response()
    → enriched UnifiedResponse with compare_result
"""

import json
import logging
import re
from typing import List, Optional, Tuple

from app.api.schemas import (
    TaskType,
    Message,
    ContentBlock,
    TextContent,
    ImageContent,
    ImageCompareOptions,
    ImageCompareResult,
)

logger = logging.getLogger(__name__)

# ── Validation ────────────────────────────────────────────────────────────

class WorkflowValidationError(ValueError):
    """Raised when input validation fails for a workflow."""
    pass


def validate_inputs(
    messages: List[Message],
    options: Optional[ImageCompareOptions] = None,
) -> Tuple[int, str]:
    """Validate that the request is suitable for image comparison.

    Args:
        messages: The request messages.
        options: Optional comparison options.

    Returns:
        Tuple of (image_count, instruction_text).

    Raises:
        WorkflowValidationError: If validation fails.
    """
    image_count = 0
    instruction_parts: List[str] = []

    for msg in messages:
        for block in msg.content:
            if isinstance(block, TextContent) and block.text.strip():
                instruction_parts.append(block.text.strip())
            elif isinstance(block, ImageContent):
                image_count += 1

    if image_count < 2:
        raise WorkflowValidationError(
            f"image_compare requires at least 2 images, got {image_count}. "
            f"Use vision_describe or vision_qa for single-image tasks."
        )

    instruction = " ".join(instruction_parts) if instruction_parts else "Compare these images."

    return image_count, instruction


# ── Prompt building ───────────────────────────────────────────────────────

def build_system_prompt(options: ImageCompareOptions, image_count: int) -> str:
    """Build a system prompt that instructs the model to return structured comparison.

    Args:
        options: The comparison options from the request.
        image_count: Number of images being compared.

    Returns:
        A system prompt string to prepend to the messages.
    """
    focus = options.comparison_focus or "all relevant aspects"

    prompt = (
        f"You are comparing {image_count} images. "
        f"Focus your comparison on: {focus}.\n\n"
    )

    if options.structured_output:
        prompt += (
            "You MUST respond with a valid JSON object in the following format. "
            "Do not include any text outside the JSON.\n\n"
            "```json\n"
            "{\n"
        )
        if options.include_similarities:
            prompt += (
                '  "similarities": [\n'
                '    "Similarity point 1",\n'
                '    "Similarity point 2"\n'
                '  ],\n'
            )
        if options.include_differences:
            prompt += (
                '  "differences": [\n'
                '    "Difference point 1",\n'
                '    "Difference point 2"\n'
                '  ],\n'
            )
        prompt += (
            '  "image_specific": [\n'
        )
        for i in range(image_count):
            prompt += (
                f'    {{\n'
                f'      "image_index": {i + 1},\n'
                f'      "observations": ["Observation for image {i + 1}"]\n'
                f'    }}'
            )
            if i < image_count - 1:
                prompt += ","
            prompt += "\n"
        prompt += (
            '  ],\n'
            '  "overall_assessment": "A concise summary comparing the images."\n'
            '}\n'
            '```'
        )
    else:
        prompt += (
            "Describe what these images have in common and what differs between them. "
            "Be specific and thorough."
        )

    return prompt


def build_workflow_messages(
    messages: List[Message],
    options: Optional[ImageCompareOptions] = None,
) -> List[Message]:
    """Transform input messages for the image_compare workflow.

    If structured_output is requested, injects a system message with
    formatting instructions. The user messages with images are passed
    through unchanged (providers handle image formatting).

    Args:
        messages: Original request messages.
        options: Comparison options.

    Returns:
        Transformed messages ready for the provider.
    """
    image_count, _ = validate_inputs(messages, options)

    if options and options.structured_output:
        system_prompt = build_system_prompt(options, image_count)
        system_msg = Message(
            role="system",
            content=[TextContent(text=system_prompt)],
        )
        # Prepend system message before user messages
        return [system_msg] + list(messages)

    # Free-text mode: pass messages through unchanged
    return list(messages)


# ── Response parsing ──────────────────────────────────────────────────────

def parse_compare_response(
    text: str,
    options: Optional[ImageCompareOptions] = None,
) -> Optional[ImageCompareResult]:
    """Attempt to parse structured comparison JSON from a model text response.

    Tries multiple extraction strategies:
      1. The entire text is valid JSON
      2. JSON inside a ```json ... ``` code block
      3. JSON inside a ``` ... ``` code block (no language tag)
      4. The first { ... } block found in the text

    Args:
        text: The raw text response from the model.
        options: The original comparison options (used for defaults).

    Returns:
        An ImageCompareResult if parsing succeeded, None otherwise.
    """
    if not text or not text.strip():
        return None

    json_str: Optional[str] = None

    # Strategy 1: entire text is JSON
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            json.loads(stripped)
            json_str = stripped
        except json.JSONDecodeError:
            pass

    # Strategy 2: JSON inside ```json ... ``` block
    if json_str is None:
        match = re.search(r"```json\s*([\s\S]*?)```", stripped)
        if match:
            candidate = match.group(1).strip()
            if candidate.startswith("{"):
                try:
                    json.loads(candidate)
                    json_str = candidate
                except json.JSONDecodeError:
                    pass

    # Strategy 3: JSON inside ``` ... ``` block (any code block)
    if json_str is None:
        match = re.search(r"```\s*(\{[\s\S]*?\})\s*```", stripped)
        if match:
            candidate = match.group(1).strip()
            try:
                json.loads(candidate)
                json_str = candidate
            except json.JSONDecodeError:
                pass

    # Strategy 4: first { ... } block
    if json_str is None:
        match = re.search(r"\{[\s\S]*\}", stripped)
        if match:
            candidate = match.group(0).strip()
            try:
                json.loads(candidate)
                json_str = candidate
            except json.JSONDecodeError:
                pass

    if json_str is None:
        logger.debug("Could not extract valid JSON from compare response")
        return None

    # Parse into ImageCompareResult
    try:
        data = json.loads(json_str)
        return ImageCompareResult(
            similarities=data.get("similarities", []),
            differences=data.get("differences", []),
            image_specific=data.get("image_specific", []),
            overall_assessment=data.get("overall_assessment", ""),
            focus_area=options.comparison_focus if options else None,
        )
    except Exception as e:
        logger.warning(f"Failed to build ImageCompareResult from JSON: {e}")
        return None


# ── Full workflow ─────────────────────────────────────────────────────────

async def execute_image_compare(
    task_req,           # TaskRequest
    registry,           # ModelRegistry
    normalized,         # NormalizedTaskRequest
) -> Tuple[List[Message], Optional[ImageCompareResult]]:
    """Execute the full image_compare workflow.

    Called by the route handler when task_type == image_compare.

    Args:
        task_req: The original TaskRequest.
        registry: The ModelRegistry for provider access.
        normalized: The already-normalized request (model resolved, etc.)

    Returns:
        Tuple of (enhanced_messages, compare_result_or_none).
        The route handler uses enhanced_messages to call the provider,
        and attaches compare_result to the UnifiedResponse.
    """
    options = task_req.compare_options

    # 1. Validate
    image_count, instruction = validate_inputs(task_req.messages, options)
    logger.info(
        f"image_compare workflow: {image_count} images, "
        f"structured={options.structured_output if options else False}"
    )

    # 2. Build workflow messages (injects system prompt if structured)
    workflow_messages = build_workflow_messages(task_req.messages, options)

    # Update the normalized request with enhanced messages
    normalized.messages = workflow_messages

    return workflow_messages, None  # compare_result is populated after provider response


def finalize_image_compare(
    response_text: str,
    options: Optional[ImageCompareOptions] = None,
) -> Optional[ImageCompareResult]:
    """Parse the provider response and return structured comparison if requested.

    Called by the route handler after the provider returns.

    Args:
        response_text: The text content from the provider's response.
        options: The original comparison options.

    Returns:
        An ImageCompareResult if structured output was requested, else None.
    """
    if options and options.structured_output:
        return parse_compare_response(response_text, options)
    return None
