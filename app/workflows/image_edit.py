"""Normalized image_edit workflow.

Accepts 1+ source images plus text editing instructions, transforms them
into provider-specific image-editing message formats, and returns edited
image content blocks with metadata.

Only models supporting image_output AND image_edit capabilities are eligible
(enforced by the router's capability filters).

Flow:
  TaskRequest (image_edit, 1+ images + instructions)
    → validate_edit_inputs()
    → build_edit_workflow_messages()   # System prompt with style/format guidance
    → provider generates edited image(s)
    → finalize_image_edit()            # Count images, build metadata
    → UnifiedResponse with image blocks + edit_result
"""

import logging
from typing import List, Optional, Tuple

from app.api.schemas import (
    Message,
    TextContent,
    ImageContent,
    ImageEditOptions,
    ImageEditResult,
)

logger = logging.getLogger(__name__)


# ── Validation ────────────────────────────────────────────────────────────

class WorkflowEditValidationError(ValueError):
    """Raised when input validation fails for the image_edit workflow."""
    pass


def validate_edit_inputs(
    messages: List[Message],
    options: Optional[ImageEditOptions] = None,
) -> Tuple[int, str]:
    """Validate that the request is suitable for image editing.

    Args:
        messages: The request messages.
        options: Optional edit options.

    Returns:
        Tuple of (source_image_count, instruction_text).

    Raises:
        WorkflowEditValidationError: If validation fails.
    """
    image_count = 0
    instruction_parts: List[str] = []

    for msg in messages:
        for block in msg.content:
            if isinstance(block, TextContent) and block.text.strip():
                instruction_parts.append(block.text.strip())
            elif isinstance(block, ImageContent):
                image_count += 1

    if image_count < 1:
        raise WorkflowEditValidationError(
            f"image_edit requires at least 1 source image, got {image_count}. "
            f"Use image_generate for text-to-image tasks without a source image."
        )

    if not instruction_parts:
        raise WorkflowEditValidationError(
            "image_edit requires editing instructions in the message text. "
            "Examples: 'remove the background', 'change the sky to sunset', "
            "'apply a watercolor filter'."
        )

    instruction = " ".join(instruction_parts)
    return image_count, instruction


# ── Prompt building ───────────────────────────────────────────────────────

def build_edit_system_prompt(
    options: Optional[ImageEditOptions],
    source_count: int,
    instruction: str,
) -> str:
    """Build a system prompt for the image editing task.

    Args:
        options: Edit options (style, format, quality, etc.).
        source_count: Number of source images.
        instruction: The user's editing instruction text.

    Returns:
        A system prompt string.
    """
    parts: List[str] = []

    parts.append(
        f"You are editing {source_count} source image(s). "
        f"Apply the following edit: {instruction}"
    )

    if options:
        if options.style_guidance:
            parts.append(f"Style guidance: {options.style_guidance}.")

        parts.append(f"Generate {options.num_outputs} edited image variant(s).")

        if options.target_resolution:
            parts.append(f"Target resolution: {options.target_resolution}.")

        if options.preserve_aspect_ratio:
            parts.append("Preserve the aspect ratio of the source image(s).")

        parts.append(
            f"Preferred output format: {options.output_format.upper()} "
            f"(quality {options.output_quality})."
        )
    else:
        parts.append("Generate 1 edited image.")

    parts.append(
        "\nAfter generating the image(s), briefly describe what you changed "
        "and why. Keep the description to 1–2 sentences."
    )

    return " ".join(parts)


def build_edit_workflow_messages(
    messages: List[Message],
    options: Optional[ImageEditOptions] = None,
) -> List[Message]:
    """Transform input messages for the image_edit workflow.

    Injects a system message with editing guidance and format instructions.
    The user messages with source images and instructions are passed through.

    Args:
        messages: Original request messages.
        options: Edit options.

    Returns:
        Transformed messages ready for the provider.
    """
    image_count, instruction = validate_edit_inputs(messages, options)

    system_prompt = build_edit_system_prompt(options, image_count, instruction)
    system_msg = Message(
        role="system",
        content=[TextContent(text=system_prompt)],
    )

    return [system_msg] + list(messages)


# ── Response finalization ─────────────────────────────────────────────────

def finalize_image_edit(
    content_blocks: List,
    options: Optional[ImageEditOptions] = None,
    source_image_count: int = 0,
    response_text: str = "",
) -> ImageEditResult:
    """Build edit metadata from the provider's response.

    Counts image blocks in the response, extracts the edit description
    from any accompanying text, and returns structured metadata.

    Args:
        content_blocks: The response content blocks (text and/or images).
        options: The original edit options.
        source_image_count: Number of source images in the request.
        response_text: Concatenated text from the response.

    Returns:
        An ImageEditResult with metadata about the edit.
    """
    edited_image_count = sum(
        1 for block in content_blocks
        if isinstance(block, ImageContent)
    )

    # Extract edit description from response text
    edit_description = ""
    for block in content_blocks:
        if isinstance(block, TextContent) and block.text.strip():
            edit_description = block.text.strip()
            break
    if not edit_description and response_text:
        edit_description = response_text.strip()

    # Truncate overly long descriptions
    if len(edit_description) > 500:
        edit_description = edit_description[:497] + "..."

    return ImageEditResult(
        source_images_used=source_image_count,
        edited_images=edited_image_count,
        style_applied=options.style_guidance if options else None,
        edit_description=edit_description,
        output_format=options.output_format if options else "png",
    )


# ── Full workflow ─────────────────────────────────────────────────────────

async def execute_image_edit(
    task_req,           # TaskRequest
    registry,           # ModelRegistry
    normalized,         # NormalizedTaskRequest
) -> Tuple[List[Message], int]:
    """Execute the image_edit workflow preprocessing.

    Called by the route handler when task_type == image_edit.

    Args:
        task_req: The original TaskRequest.
        registry: The ModelRegistry (unused here, for consistency).
        normalized: The already-normalized request.

    Returns:
        Tuple of (enhanced_messages, source_image_count).
    """
    options = task_req.edit_options

    # Validate
    source_count, instruction = validate_edit_inputs(task_req.messages, options)
    logger.info(
        f"image_edit workflow: {source_count} source image(s), "
        f"instruction='{instruction[:80]}...'"
        if len(instruction) > 80
        else f"image_edit workflow: {source_count} source image(s), "
             f"instruction='{instruction}'"
    )

    # Build enhanced messages with system prompt
    workflow_messages = build_edit_workflow_messages(task_req.messages, options)
    normalized.messages = workflow_messages

    return workflow_messages, source_count
