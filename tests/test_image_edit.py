"""Tests for the image_edit workflow module.

Covers:
  - Input validation (source image count, text instruction presence)
  - System prompt building (style guidance, format, quality, resolution)
  - Message transformation (system prompt injection)
  - Response finalization (image counting, metadata extraction)
  - Full workflow execution (validate → build → finalize)
  - Router capability filtering (image_output + image_edit required)
"""

import pytest

from app.api.schemas import (
    Message,
    TextContent,
    ImageContent,
    ImageEditOptions,
    ImageEditResult,
    TaskType,
    TaskRequest,
)
from app.workflows.image_edit import (
    validate_edit_inputs,
    build_edit_system_prompt,
    build_edit_workflow_messages,
    finalize_image_edit,
    execute_image_edit,
    WorkflowEditValidationError,
)


# ── Test fixtures ─────────────────────────────────────────────────────────

PLACEHOLDER_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

PLACEHOLDER_PNG2 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)


def make_msg(role="user", *blocks):
    return Message(role=role, content=list(blocks))


def text(text):
    return TextContent(text=text)


def image(b64=PLACEHOLDER_PNG):
    return ImageContent(image=b64)


# ── Validation tests ──────────────────────────────────────────────────────

class TestValidateEditInputs:
    """Test that validate_edit_inputs correctly checks inputs."""

    def test_one_image_with_instruction_passes(self):
        """1 source image + text instruction = valid."""
        messages = [
            make_msg("user",
                text("Remove the background from this image."),
                image(),
            ),
        ]
        count, instruction = validate_edit_inputs(messages)
        assert count == 1
        assert "Remove the background" in instruction

    def test_two_images_with_instruction_passes(self):
        """2 source images + text instruction = valid."""
        messages = [
            make_msg("user",
                text("Blend these two images into one."),
                image(PLACEHOLDER_PNG),
                image(PLACEHOLDER_PNG2),
            ),
        ]
        count, instruction = validate_edit_inputs(messages)
        assert count == 2
        assert "Blend" in instruction

    def test_zero_images_fails(self):
        """No source image = error."""
        messages = [
            make_msg("user", text("Make it red.")),
        ]
        with pytest.raises(WorkflowEditValidationError, match="at least 1 source image"):
            validate_edit_inputs(messages)

    def test_no_instruction_fails(self):
        """Image without text instruction = error."""
        messages = [
            make_msg("user", image()),
        ]
        with pytest.raises(WorkflowEditValidationError, match="requires editing instructions"):
            validate_edit_inputs(messages)

    def test_empty_instruction_fails(self):
        """Whitespace-only text = error."""
        messages = [
            make_msg("user", text("   "), image()),
        ]
        with pytest.raises(WorkflowEditValidationError):
            validate_edit_inputs(messages)

    def test_images_across_messages(self):
        """Images spread across multiple messages are counted."""
        messages = [
            make_msg("user", text("First edit"), image()),
            make_msg("user", text("Also use this"), image(PLACEHOLDER_PNG2)),
        ]
        count, instruction = validate_edit_inputs(messages)
        assert count == 2
        assert "First edit" in instruction
        assert "Also use this" in instruction

    def test_instruction_from_multiple_text_blocks(self):
        """Multiple text blocks are concatenated."""
        messages = [
            make_msg("user",
                text("Make the sky"),
                text("look like sunset."),
                image(),
            ),
        ]
        _, instruction = validate_edit_inputs(messages)
        assert "Make the sky" in instruction
        assert "look like sunset" in instruction


# ── Prompt building tests ─────────────────────────────────────────────────

class TestBuildEditSystemPrompt:
    """Test system prompt generation for image editing."""

    def test_basic_prompt_includes_instruction(self):
        """Prompt includes the user's edit instruction."""
        prompt = build_edit_system_prompt(
            options=None,
            source_count=1,
            instruction="Remove the background.",
        )
        assert "Remove the background" in prompt
        assert "1 source image" in prompt

    def test_style_guidance_in_prompt(self):
        """Style guidance is included in the prompt."""
        options = ImageEditOptions(style_guidance="watercolor painting style")
        prompt = build_edit_system_prompt(
            options=options,
            source_count=1,
            instruction="Restyle.",
        )
        assert "watercolor painting style" in prompt

    def test_output_format_in_prompt(self):
        """Output format and quality are mentioned."""
        options = ImageEditOptions(output_format="jpeg", output_quality=85)
        prompt = build_edit_system_prompt(
            options=options,
            source_count=1,
            instruction="Edit.",
        )
        assert "JPEG" in prompt
        assert "quality 85" in prompt

    def test_num_outputs_in_prompt(self):
        """Number of output variants is specified."""
        options = ImageEditOptions(num_outputs=3)
        prompt = build_edit_system_prompt(
            options=options,
            source_count=2,
            instruction="Edit.",
        )
        assert "3 edited image variant" in prompt

    def test_resolution_in_prompt(self):
        """Target resolution is included."""
        options = ImageEditOptions(target_resolution="1024x1024")
        prompt = build_edit_system_prompt(
            options=options,
            source_count=1,
            instruction="Edit.",
        )
        assert "1024x1024" in prompt

    def test_aspect_ratio_preservation(self):
        """Aspect ratio preservation is mentioned."""
        options = ImageEditOptions(preserve_aspect_ratio=True)
        prompt = build_edit_system_prompt(
            options=options,
            source_count=1,
            instruction="Edit.",
        )
        assert "Preserve the aspect ratio" in prompt

    def test_no_aspect_ratio_when_false(self):
        """No aspect ratio mention when preserve_aspect_ratio is False."""
        options = ImageEditOptions(preserve_aspect_ratio=False)
        prompt = build_edit_system_prompt(
            options=options,
            source_count=1,
            instruction="Edit.",
        )
        assert "Preserve the aspect ratio" not in prompt

    def test_multiple_source_images_in_prompt(self):
        """Prompt mentions correct source count."""
        prompt = build_edit_system_prompt(
            options=None,
            source_count=3,
            instruction="Combine these.",
        )
        assert "3 source image" in prompt

    def test_describe_changes_instruction(self):
        """Prompt asks model to describe what it changed."""
        prompt = build_edit_system_prompt(
            options=None,
            source_count=1,
            instruction="Edit.",
        )
        assert "describe what you changed" in prompt.lower()


# ── Message transformation tests ──────────────────────────────────────────

class TestBuildEditWorkflowMessages:
    """Test that build_edit_workflow_messages transforms messages correctly."""

    def test_system_message_injected(self):
        """A system message is prepended."""
        messages = [
            make_msg("user", text("Edit."), image()),
        ]
        result = build_edit_workflow_messages(messages, None)
        assert len(result) == 2
        assert result[0].role == "system"
        assert "Edit." in result[0].content[0].text

    def test_user_messages_preserved(self):
        """Original user messages are unchanged."""
        messages = [
            make_msg("user", text("Edit."), image(), image()),
        ]
        result = build_edit_workflow_messages(messages, None)
        assert result[1].role == "user"
        assert len(result[1].content) == 3  # 1 text + 2 images

    def test_options_flow_through_to_prompt(self):
        """Edit options appear in the injected system prompt."""
        options = ImageEditOptions(
            style_guidance="cyberpunk",
            output_format="webp",
            num_outputs=2,
        )
        messages = [
            make_msg("user", text("Restyle."), image()),
        ]
        result = build_edit_workflow_messages(messages, options)
        system_text = result[0].content[0].text
        assert "cyberpunk" in system_text
        assert "WEBP" in system_text
        assert "2 edited image" in system_text

    def test_existing_system_message_preserved(self):
        """If the user already has a system message, workflow prepends another."""
        messages = [
            make_msg("system", text("You are a photo editor.")),
            make_msg("user", text("Edit."), image()),
        ]
        result = build_edit_workflow_messages(messages, None)
        assert len(result) == 3
        assert result[0].role == "system"
        assert result[1].role == "system"


# ── Response finalization tests ───────────────────────────────────────────

class TestFinalizeImageEdit:
    """Test that finalize_image_edit builds correct metadata."""

    def test_counts_output_images(self):
        """Edited image count is correctly computed from response content."""
        content = [
            TextContent(text="I changed the background to blue."),
            ImageContent(image=PLACEHOLDER_PNG),
        ]
        result = finalize_image_edit(
            content_blocks=content,
            options=None,
            source_image_count=1,
        )
        assert result.edited_images == 1
        assert result.source_images_used == 1

    def test_multiple_output_images(self):
        """Multiple output images are counted."""
        content = [
            ImageContent(image=PLACEHOLDER_PNG),
            ImageContent(image=PLACEHOLDER_PNG2),
            TextContent(text="Two variants."),
        ]
        result = finalize_image_edit(
            content_blocks=content,
            options=None,
            source_image_count=2,
        )
        assert result.edited_images == 2
        assert result.source_images_used == 2

    def test_extracts_edit_description_from_text(self):
        """The first text block becomes the edit description."""
        content = [
            TextContent(text="Applied sepia filter and increased contrast."),
            ImageContent(image=PLACEHOLDER_PNG),
        ]
        result = finalize_image_edit(
            content_blocks=content,
            options=None,
            source_image_count=1,
        )
        assert "sepia filter" in result.edit_description

    def test_edit_description_truncated(self):
        """Long descriptions are truncated at 500 chars."""
        long_text = "x" * 600
        content = [
            TextContent(text=long_text),
            ImageContent(image=PLACEHOLDER_PNG),
        ]
        result = finalize_image_edit(
            content_blocks=content,
            options=None,
            source_image_count=1,
        )
        assert len(result.edit_description) <= 500
        assert result.edit_description.endswith("...")

    def test_style_applied_preserved(self):
        """Style guidance from options is preserved in result."""
        options = ImageEditOptions(style_guidance="oil painting")
        content = [
            TextContent(text="Done."),
            ImageContent(image=PLACEHOLDER_PNG),
        ]
        result = finalize_image_edit(
            content_blocks=content,
            options=options,
            source_image_count=1,
        )
        assert result.style_applied == "oil painting"

    def test_output_format_preserved(self):
        """Output format from options is preserved."""
        options = ImageEditOptions(output_format="jpeg")
        content = [
            ImageContent(image=PLACEHOLDER_PNG),
        ]
        result = finalize_image_edit(
            content_blocks=content,
            options=options,
            source_image_count=1,
        )
        assert result.output_format == "jpeg"

    def test_no_images_in_response(self):
        """Zero edited images when response has no image blocks."""
        content = [
            TextContent(text="I cannot edit this image."),
        ]
        result = finalize_image_edit(
            content_blocks=content,
            options=None,
            source_image_count=1,
        )
        assert result.edited_images == 0

    def test_fallback_to_response_text(self):
        """When no TextContent blocks, falls back to response_text parameter."""
        content = [
            ImageContent(image=PLACEHOLDER_PNG),
        ]
        result = finalize_image_edit(
            content_blocks=content,
            options=None,
            source_image_count=1,
            response_text="Fallback description here.",
        )
        assert "Fallback description" in result.edit_description


# ── Integration tests ─────────────────────────────────────────────────────

class TestTwoSourceImagePattern:
    """End-to-end tests for the two-source-image editing pattern."""

    def test_full_edit_workflow_with_options(self):
        """Full workflow: validate → build → finalize with all options set."""
        messages = [
            make_msg("user",
                text("Blend these two logos into a single cohesive design."),
                image(PLACEHOLDER_PNG),
                image(PLACEHOLDER_PNG2),
            ),
        ]
        options = ImageEditOptions(
            style_guidance="minimalist flat design",
            output_format="png",
            output_quality=95,
            num_outputs=1,
            preserve_aspect_ratio=True,
            target_resolution="512x512",
        )

        # Validate
        count, instruction = validate_edit_inputs(messages, options)
        assert count == 2

        # Build
        workflow_msgs = build_edit_workflow_messages(messages, options)
        assert len(workflow_msgs) == 2
        system_text = workflow_msgs[0].content[0].text
        assert "Blend these two logos" in system_text
        assert "minimalist flat design" in system_text
        assert "PNG" in system_text
        assert "512x512" in system_text

        # Simulate provider response
        response_content = [
            TextContent(text="Combined the logos using a minimalist approach with shared iconography."),
            ImageContent(image=PLACEHOLDER_PNG),  # the edited result
        ]
        result = finalize_image_edit(
            content_blocks=response_content,
            options=options,
            source_image_count=count,
        )
        assert result.edited_images == 1
        assert result.source_images_used == 2
        assert result.style_applied == "minimalist flat design"
        assert result.output_format == "png"
        assert "Combined the logos" in result.edit_description

    def test_full_edit_workflow_minimal(self):
        """Full workflow with minimal options (single image, no options)."""
        messages = [
            make_msg("user",
                text("Remove the background."),
                image(),
            ),
        ]

        count, _ = validate_edit_inputs(messages)
        assert count == 1

        workflow_msgs = build_edit_workflow_messages(messages, None)
        system_text = workflow_msgs[0].content[0].text
        assert "Remove the background" in system_text
        assert "1 edited image" in system_text

        response_content = [
            TextContent(text="Background removed with clean edges."),
            ImageContent(image=PLACEHOLDER_PNG),
        ]
        result = finalize_image_edit(
            content_blocks=response_content,
            options=None,
            source_image_count=count,
        )
        assert result.edited_images == 1
        assert result.style_applied is None
        assert result.output_format == "png"

    def test_multi_variant_output(self):
        """Request 3 variants and get 3 images back."""
        messages = [
            make_msg("user",
                text("Apply 3 different color schemes."),
                image(),
            ),
        ]
        options = ImageEditOptions(num_outputs=3)

        workflow_msgs = build_edit_workflow_messages(messages, options)
        assert "3 edited image variant" in workflow_msgs[0].content[0].text

        # Simulate provider returning 3 images
        response_content = [
            ImageContent(image=PLACEHOLDER_PNG),
            ImageContent(image=PLACEHOLDER_PNG2),
            ImageContent(image=PLACEHOLDER_PNG),
            TextContent(text="Generated warm, cool, and neutral variants."),
        ]
        result = finalize_image_edit(
            content_blocks=response_content,
            options=options,
            source_image_count=1,
        )
        assert result.edited_images == 3


# ── TaskRequest integration test ──────────────────────────────────────────

class TestTaskRequestIntegration:
    """Test that edit_options flows through TaskRequest correctly."""

    def test_task_request_with_edit_options(self):
        """TaskRequest accepts edit_options."""
        req = TaskRequest(
            task_type=TaskType.IMAGE_EDIT,
            messages=[
                make_msg("user",
                    text("Make it darker."),
                    image(),
                ),
            ],
            edit_options=ImageEditOptions(
                style_guidance="noir",
                num_outputs=2,
            ),
        )
        assert req.task_type == TaskType.IMAGE_EDIT
        assert req.edit_options is not None
        assert req.edit_options.style_guidance == "noir"
        assert req.edit_options.num_outputs == 2

    def test_task_request_without_edit_options(self):
        """TaskRequest works without edit_options."""
        req = TaskRequest(
            task_type=TaskType.IMAGE_EDIT,
            messages=[
                make_msg("user", text("Edit."), image()),
            ],
        )
        assert req.edit_options is None


# ── Capability routing test ───────────────────────────────────────────────

class TestCapabilityRouting:
    """Verify that only image_output + image_edit models are considered.

    These tests validate that the model config capability fields correctly
    gate the image_edit task type. The router already applies these filters.
    """

    def test_gemini_pro_supports_edit(self):
        """gemini-pro is the only enabled model with image_edit=true."""
        from app.models.base import ModelConfig
        from app.api.schemas import TaskType as TT, OutputType, PlanTier, CostClass

        config = ModelConfig(
            name="gemini-pro",
            provider="gemini",
            model_id="gemini-2.5-pro",
            supports_text_input=True,
            supports_image_input=True,
            supports_multi_image_input=True,
            supports_text_output=True,
            supports_image_output=True,
            supports_image_edit=True,
        )
        assert config.supports_task(TT.IMAGE_EDIT)
        assert config.supports_image_output
        assert config.supports_image_edit

    def test_model_without_edit_fails(self):
        """gpt-4o has image_output but NOT image_edit — should fail."""
        from app.models.base import ModelConfig
        from app.api.schemas import TaskType as TT

        config = ModelConfig(
            name="gpt-4o",
            provider="openai",
            model_id="gpt-4o",
            supports_text_input=True,
            supports_image_input=True,
            supports_multi_image_input=True,
            supports_text_output=True,
            supports_image_output=True,    # can generate images
            supports_image_edit=False,      # but cannot edit/transform
        )
        # The MODEL_CONFIG's TASK_REQUIREMENTS for image_edit includes image_edit=true
        assert not config.supports_task(TT.IMAGE_EDIT)

    def test_model_without_image_output_fails(self):
        """claude-haiku has no image output — should fail."""
        from app.models.base import ModelConfig
        from app.api.schemas import TaskType as TT

        config = ModelConfig(
            name="claude-haiku",
            provider="anthropic",
            model_id="claude-haiku-4-5-20251001",
            supports_text_input=True,
            supports_image_input=True,
            supports_multi_image_input=True,
            supports_text_output=True,
            supports_image_output=False,
            supports_image_edit=False,
        )
        assert not config.supports_task(TT.IMAGE_EDIT)
