"""Tests for the image_compare workflow module.

Covers:
  - Input validation (image count, text presence)
  - System prompt building (structured vs free-text)
  - Message transformation (system prompt injection)
  - JSON response parsing (all 4 extraction strategies)
  - Full workflow execution (validate → build → parse)
  - Integration pattern: the two-image comparison from API docs
"""

import pytest

from app.api.schemas import (
    Message,
    TextContent,
    ImageContent,
    ImageCompareOptions,
    ImageCompareResult,
    TaskType,
    TaskRequest,
)
from app.workflows.image_compare import (
    validate_inputs,
    build_system_prompt,
    build_workflow_messages,
    parse_compare_response,
    finalize_image_compare,
    execute_image_compare,
    WorkflowValidationError,
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

class TestValidateInputs:
    """Test that validate_inputs correctly checks image count."""

    def test_two_images_passes(self):
        """Two images + instruction = valid."""
        messages = [
            make_msg("user",
                text("Compare these two designs."),
                image(PLACEHOLDER_PNG),
                image(PLACEHOLDER_PNG2),
            ),
        ]
        count, instruction = validate_inputs(messages)
        assert count == 2
        assert "Compare" in instruction

    def test_three_images_passes(self):
        """Three images are fine."""
        messages = [
            make_msg("user",
                text("Which is best?"),
                image(), image(), image(),
            ),
        ]
        count, _ = validate_inputs(messages)
        assert count == 3

    def test_zero_images_fails(self):
        """No images = error."""
        messages = [make_msg("user", text("No images here."))]
        with pytest.raises(WorkflowValidationError, match="at least 2 images"):
            validate_inputs(messages)

    def test_one_image_fails(self):
        """Single image = error (use vision_describe instead)."""
        messages = [make_msg("user", text("One image."), image())]
        with pytest.raises(WorkflowValidationError, match="at least 2 images"):
            validate_inputs(messages)

    def test_images_across_messages(self):
        """Images spread across multiple messages are counted."""
        messages = [
            make_msg("user", text("First set"), image()),
            make_msg("user", text("Second set"), image(PLACEHOLDER_PNG2)),
        ]
        count, _ = validate_inputs(messages)
        assert count == 2

    def test_no_text_instruction_defaults(self):
        """No text instruction → default prompt used."""
        messages = [
            make_msg("user", image(), image()),
        ]
        _, instruction = validate_inputs(messages)
        assert "Compare these images" in instruction


# ── Prompt building tests ─────────────────────────────────────────────────

class TestBuildSystemPrompt:
    """Test system prompt generation for structured and free-text modes."""

    def test_structured_prompt_contains_json_format(self):
        """Structured prompt must include JSON template."""
        options = ImageCompareOptions(structured_output=True)
        prompt = build_system_prompt(options, image_count=2)
        assert '"similarities"' in prompt
        assert '"differences"' in prompt
        assert '"image_specific"' in prompt
        assert '"overall_assessment"' in prompt
        assert '```json' in prompt

    def test_structured_prompt_image_count_in_template(self):
        """JSON template has entries for each image."""
        options = ImageCompareOptions(structured_output=True)
        prompt = build_system_prompt(options, image_count=3)
        assert 'image_index": 1' in prompt
        assert 'image_index": 2' in prompt
        assert 'image_index": 3' in prompt

    def test_free_text_prompt_no_json(self):
        """Free-text mode has no JSON formatting instructions."""
        options = ImageCompareOptions(structured_output=False)
        prompt = build_system_prompt(options, image_count=2)
        assert "```json" not in prompt
        assert "similarities" not in prompt.lower()

    def test_focus_area_in_prompt(self):
        """Comparison focus is included in the prompt."""
        options = ImageCompareOptions(
            structured_output=True,
            comparison_focus="color palette",
        )
        prompt = build_system_prompt(options, image_count=2)
        assert "color palette" in prompt

    def test_omit_similarities(self):
        """If include_similarities is False, similarities not in prompt."""
        options = ImageCompareOptions(
            structured_output=True,
            include_similarities=False,
        )
        prompt = build_system_prompt(options, image_count=2)
        assert '"similarities"' not in prompt
        assert '"differences"' in prompt  # differences still included

    def test_omit_differences(self):
        """If include_differences is False, differences not in prompt."""
        options = ImageCompareOptions(
            structured_output=True,
            include_differences=False,
        )
        prompt = build_system_prompt(options, image_count=2)
        assert '"differences"' not in prompt
        assert '"similarities"' in prompt  # similarities still included


# ── Message transformation tests ──────────────────────────────────────────

class TestBuildWorkflowMessages:
    """Test that build_workflow_messages correctly transforms messages."""

    def test_structured_mode_adds_system_message(self):
        """Structured mode prepends a system message."""
        messages = [
            make_msg("user", text("Compare."), image(), image()),
        ]
        options = ImageCompareOptions(structured_output=True)
        result = build_workflow_messages(messages, options)
        assert len(result) == 2
        assert result[0].role == "system"
        assert "similarities" in result[0].content[0].text

    def test_free_text_mode_passes_through(self):
        """Free-text mode does not modify messages."""
        messages = [
            make_msg("user", text("Compare."), image(), image()),
        ]
        options = ImageCompareOptions(structured_output=False)
        result = build_workflow_messages(messages, options)
        assert len(result) == 1
        assert result[0].role == "user"

    def test_no_options_passes_through(self):
        """No options → pass through unchanged."""
        messages = [
            make_msg("user", text("Compare."), image(), image()),
        ]
        result = build_workflow_messages(messages, None)
        assert len(result) == 1

    def test_existing_system_message_preserved(self):
        """If messages already have a system message, workflow prepends another."""
        messages = [
            make_msg("system", text("You are a design critic.")),
            make_msg("user", text("Compare."), image(), image()),
        ]
        options = ImageCompareOptions(structured_output=True)
        result = build_workflow_messages(messages, options)
        assert len(result) == 3
        assert result[0].role == "system"
        assert result[1].role == "system"


# ── JSON parsing tests ────────────────────────────────────────────────────

class TestParseCompareResponse:
    """Test JSON extraction from model responses with various formats."""

    VALID_JSON = """{
  "similarities": ["Both use blue tones", "Both are landscapes"],
  "differences": ["Image 1 is brighter", "Image 2 has more detail"],
  "image_specific": [
    {"image_index": 1, "observations": ["Bright sky", "Mountains in background"]},
    {"image_index": 2, "observations": ["Darker tones", "Urban foreground"]}
  ],
  "overall_assessment": "Image 1 is a natural scene while Image 2 is urban."
}"""

    def test_direct_json_parses(self):
        """Strategy 1: entire text is valid JSON."""
        result = parse_compare_response(self.VALID_JSON, None)
        assert result is not None
        assert len(result.similarities) == 2
        assert len(result.differences) == 2
        assert len(result.image_specific) == 2
        assert "natural scene" in result.overall_assessment

    def test_json_in_code_block_parses(self):
        """Strategy 2: JSON inside ```json block."""
        text = f"Here is my comparison:\n\n```json\n{self.VALID_JSON}\n```\n\nI hope that helps."
        result = parse_compare_response(text, None)
        assert result is not None
        assert len(result.similarities) == 2

    def test_json_in_plain_code_block_parses(self):
        """Strategy 3: JSON inside ``` block without language tag."""
        text = f"Comparison result:\n```\n{self.VALID_JSON}\n```"
        result = parse_compare_response(text, None)
        assert result is not None
        assert len(result.differences) == 2

    def test_first_brace_block_extracted(self):
        """Strategy 4: find first { ... } block."""
        text = f"Here is the result: {self.VALID_JSON} And some more text."
        result = parse_compare_response(text, None)
        assert result is not None
        assert result.image_specific[0]["image_index"] == 1

    def test_invalid_json_returns_none(self):
        """Malformed JSON returns None."""
        text = "This is not JSON at all, just plain text."
        result = parse_compare_response(text, None)
        assert result is None

    def test_empty_text_returns_none(self):
        """Empty string returns None."""
        result = parse_compare_response("", None)
        assert result is None

    def test_partial_json_returns_none(self):
        """Incomplete JSON returns None."""
        text = '{"similarities": ["One"], "differences": ['
        result = parse_compare_response(text, None)
        assert result is None

    def test_focus_area_preserved(self):
        """Focus area from options is preserved in result."""
        options = ImageCompareOptions(comparison_focus="lighting")
        result = parse_compare_response(self.VALID_JSON, options)
        assert result is not None
        assert result.focus_area == "lighting"

    def test_inline_json_parses(self):
        """JSON with surrounding whitespace and newlines."""
        text = f"\n\n  {self.VALID_JSON}  \n\n"
        result = parse_compare_response(text, None)
        assert result is not None
        assert len(result.similarities) == 2


# ── finalize_image_compare tests ──────────────────────────────────────────

class TestFinalizeImageCompare:
    """Test the post-provider finalize step."""

    def test_structured_mode_parses(self):
        """When structured_output is True, result is returned."""
        options = ImageCompareOptions(structured_output=True)
        json_text = '{"similarities":["A"],"differences":["B"],"image_specific":[],"overall_assessment":"Ok"}'
        result = finalize_image_compare(json_text, options)
        assert result is not None
        assert result.similarities == ["A"]

    def test_free_text_mode_skips_parsing(self):
        """When structured_output is False, None is returned even if JSON present."""
        options = ImageCompareOptions(structured_output=False)
        json_text = '{"similarities":["A"],"differences":["B"],"image_specific":[],"overall_assessment":"Ok"}'
        result = finalize_image_compare(json_text, options)
        assert result is None

    def test_no_options_skips_parsing(self):
        """Without options, None is returned."""
        json_text = '{"similarities":["A"],"differences":["B"],"image_specific":[],"overall_assessment":"Ok"}'
        result = finalize_image_compare(json_text, None)
        assert result is None


# ── Integration tests (two-image pattern from docs) ───────────────────────

class TestTwoImageComparisonPattern:
    """End-to-end tests matching the two-image comparison pattern from API docs."""

    def test_full_free_text_workflow(self):
        """Free-text comparison: validate → build → parse (no change)."""
        messages = [
            make_msg("user",
                text("Which design is better and why?"),
                image(PLACEHOLDER_PNG),
                image(PLACEHOLDER_PNG2),
            ),
        ]
        # Validate
        count, instruction = validate_inputs(messages)
        assert count == 2

        # Build (free-text, no options)
        workflow_msgs = build_workflow_messages(messages, None)
        assert len(workflow_msgs) == 1  # no system message injected
        assert workflow_msgs[0].content[0].text == "Which design is better and why?"

        # After provider (simulate text response)
        result = finalize_image_compare(
            "Design A uses warmer colors while Design B is cooler.",
            None,
        )
        assert result is None  # no structured parsing

    def test_full_structured_workflow(self):
        """Structured comparison: validates, injects system prompt, parses JSON."""
        messages = [
            make_msg("user",
                text("Compare these two UI mockups."),
                image(PLACEHOLDER_PNG),
                image(PLACEHOLDER_PNG2),
            ),
        ]
        options = ImageCompareOptions(
            structured_output=True,
            comparison_focus="UI layout and typography",
        )

        # Validate
        count, _ = validate_inputs(messages, options)
        assert count == 2

        # Build (adds system prompt)
        workflow_msgs = build_workflow_messages(messages, options)
        assert len(workflow_msgs) == 2
        assert workflow_msgs[0].role == "system"
        assert "UI layout" in workflow_msgs[0].content[0].text

        # Simulate provider response
        model_response = """```json
{
  "similarities": ["Both use sans-serif fonts", "Both have top nav bars"],
  "differences": ["Mockup A uses a 2-column layout, B uses 3 columns", "Mockup A has larger headings"],
  "image_specific": [
    {"image_index": 1, "observations": ["Clean layout", "Good whitespace"]},
    {"image_index": 2, "observations": ["Denser layout", "More content visible"]}
  ],
  "overall_assessment": "Mockup A is cleaner and more readable while Mockup B packs more information."
}
```"""
        result = finalize_image_compare(model_response, options)
        assert result is not None
        assert len(result.similarities) == 2
        assert "sans-serif" in result.similarities[0]
        assert len(result.differences) == 2
        assert len(result.image_specific) == 2
        assert result.image_specific[0]["image_index"] == 1
        assert result.image_specific[1]["image_index"] == 2
        assert "cleaner" in result.overall_assessment
        assert result.focus_area == "UI layout and typography"

    def test_structured_without_similarities(self):
        """Request differences only, no similarities."""
        messages = [
            make_msg("user",
                text("What's different?"),
                image(), image(),
            ),
        ]
        options = ImageCompareOptions(
            structured_output=True,
            include_similarities=False,
            include_differences=True,
        )

        workflow_msgs = build_workflow_messages(messages, options)
        prompt = workflow_msgs[0].content[0].text
        assert '"similarities"' not in prompt
        assert '"differences"' in prompt

        # Simulate response without similarities
        response = """```json
{
  "differences": ["Color scheme differs", "Font size varies"],
  "image_specific": [
    {"image_index": 1, "observations": ["Blue theme"]},
    {"image_index": 2, "observations": ["Red theme"]}
  ],
  "overall_assessment": "The main difference is color."
}
```"""
        result = finalize_image_compare(response, options)
        assert result is not None
        assert len(result.similarities) == 0  # not requested
        assert len(result.differences) == 2


# ── TaskRequest integration test ──────────────────────────────────────────

class TestTaskRequestIntegration:
    """Test that compare_options flows through TaskRequest correctly."""

    def test_task_request_with_compare_options(self):
        """TaskRequest accepts compare_options."""
        req = TaskRequest(
            task_type=TaskType.IMAGE_COMPARE,
            messages=[
                make_msg("user",
                    text("Compare these."),
                    image(PLACEHOLDER_PNG),
                    image(PLACEHOLDER_PNG2),
                ),
            ],
            compare_options=ImageCompareOptions(
                structured_output=True,
                comparison_focus="composition",
            ),
        )
        assert req.task_type == TaskType.IMAGE_COMPARE
        assert req.compare_options is not None
        assert req.compare_options.structured_output is True
        assert req.compare_options.comparison_focus == "composition"

    def test_task_request_without_compare_options(self):
        """TaskRequest works without compare_options."""
        req = TaskRequest(
            task_type=TaskType.CHAT_WITH_CONTEXT,
            messages=[make_msg("user", text("Hello"))],
        )
        assert req.compare_options is None
