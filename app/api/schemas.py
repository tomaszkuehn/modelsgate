"""Unified request and response schemas for the AI backend API.

Public API (what clients send/receive):
  - TaskRequest  — task_type + messages + optional model override
  - Legacy field 'model' is accepted for backward compatibility
  - UnifiedResponse — id + task_type + model + content + usage + error

Internal (after normalization):
  - NormalizedTaskRequest — task_type + resolved model + messages + parameters

Supports content types: text, image, text+image, text+2_images.
All content is represented as a list of ContentBlock objects.
"""

import uuid
from enum import Enum
from typing import List, Optional, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Task types ──────────────────────────────────────────────────────────

class TaskType(str, Enum):
    """The kind of AI task the client wants to perform.

    The backend maps each task type to the best available model
    unless the client explicitly overrides with the 'model' field.
    """
    CHAT_WITH_CONTEXT = "chat_with_context"    # Multi-turn conversation with context
    IMAGE_COMPARE     = "image_compare"         # Compare 2+ images, describe differences
    IMAGE_EDIT        = "image_edit"            # Edit/transform an input image
    IMAGE_GENERATE    = "image_generate"        # Generate an image from a text prompt
    VISION_DESCRIBE   = "vision_describe"       # Describe what is in an image
    VISION_QA         = "vision_qa"             # Answer questions about an image


# ── Routing constraints ──────────────────────────────────────────────────

class OutputType(str, Enum):
    """Desired output modality from the model."""
    TEXT           = "text"            # Text-only response
    IMAGE          = "image"           # Image-only response
    TEXT_AND_IMAGE = "text_and_image"  # Both text and image (multi-modal output)


class PlanTier(str, Enum):
    """Service plan tier — controls which models are available."""
    FREE     = "free"      # Free-tier models only
    STANDARD = "standard"  # Standard paid models
    PREMIUM  = "premium"   # Top-tier / high-capability models


class CostClass(str, Enum):
    """Preferred cost profile for model selection."""
    CHEAPEST = "cheapest"  # Lowest cost per token
    BALANCED = "balanced"  # Middle ground
    BEST     = "best"      # Highest capability (ignores cost)


# ── Content blocks ──────────────────────────────────────────────────────

class TextContent(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageContent(BaseModel):
    type: Literal["image"] = "image"
    image: str = Field(description="Base64-encoded image (with or without data URI prefix)")

    @field_validator("image")
    @classmethod
    def strip_data_uri(cls, v: str) -> str:
        """Strip data URI prefix if present, keeping only base64."""
        if v.startswith("data:") and "base64," in v:
            return v.split("base64,", 1)[1]
        return v


ContentBlock = Union[TextContent, ImageContent]


# ── Messages ────────────────────────────────────────────────────────────

class Message(BaseModel):
    role: Literal["user", "assistant", "system"] = "user"
    content: List[ContentBlock]


# ── Parameters ──────────────────────────────────────────────────────────

class RequestParameters(BaseModel):
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=128000)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    stop: Optional[List[str]] = None


# ── Workflow-specific options ──────────────────────────────────────────

class ImageCompareOptions(BaseModel):
    """Options for the image_compare task_type workflow."""
    structured_output: bool = Field(
        default=False,
        description="If true, the workflow injects a system prompt requesting "
                    "structured JSON output with similarities, differences, and "
                    "per-image observations. The response content will contain "
                    "a parsed JSON object under the 'compare_result' key.",
    )
    comparison_focus: Optional[str] = Field(
        default=None,
        description="Optional focus area for the comparison "
                    "(e.g., 'colors', 'composition', 'lighting', 'style').",
    )
    include_similarities: bool = Field(
        default=True,
        description="Whether to include similarities in the structured output.",
    )
    include_differences: bool = Field(
        default=True,
        description="Whether to include differences in the structured output.",
    )


class ImageCompareResult(BaseModel):
    """Structured result of an image comparison, extracted from the model response."""
    similarities: List[str] = Field(
        default_factory=list,
        description="What the images have in common.",
    )
    differences: List[str] = Field(
        default_factory=list,
        description="What differs between the images.",
    )
    image_specific: List[dict] = Field(
        default_factory=list,
        description="Per-image observations. Each entry has 'image_index' (int) "
                    "and 'observations' (list of strings).",
    )
    overall_assessment: str = Field(
        default="",
        description="A summary paragraph comparing the images as a whole.",
    )
    focus_area: Optional[str] = Field(
        default=None,
        description="The comparison focus area, if one was specified.",
    )


# ── Image edit schemas ───────────────────────────────────────────────────

class ImageEditOptions(BaseModel):
    """Options for the image_edit task_type workflow.

    Controls how source images are transformed into output images.
    """
    style_guidance: Optional[str] = Field(
        default=None,
        description="Natural-language style guidance for the edit "
                    "(e.g., 'make it look like a watercolor painting', "
                    "'convert to night scene').",
    )
    output_format: str = Field(
        default="png",
        description="Preferred output image format: 'png', 'jpeg', or 'webp'.",
    )
    output_quality: int = Field(
        default=90,
        ge=1,
        le=100,
        description="Output image quality (1–100). Only meaningful for lossy formats.",
    )
    num_outputs: int = Field(
        default=1,
        ge=1,
        le=4,
        description="Number of edited image variants to generate.",
    )
    preserve_aspect_ratio: bool = Field(
        default=True,
        description="Whether to preserve the aspect ratio of the source images.",
    )
    target_resolution: Optional[str] = Field(
        default=None,
        description="Target resolution hint (e.g., '1024x1024', '512x512').",
    )


class ImageEditResult(BaseModel):
    """Metadata about an image editing operation, returned alongside image content blocks."""
    source_images_used: int = Field(
        default=0,
        description="Number of source images that were used in the edit.",
    )
    edited_images: int = Field(
        default=0,
        description="Number of edited output images produced.",
    )
    style_applied: Optional[str] = Field(
        default=None,
        description="The style guidance that was applied, if any.",
    )
    edit_description: str = Field(
        default="",
        description="A natural-language description of what was edited.",
    )
    output_format: str = Field(
        default="png",
        description="The output format used for the edited images.",
    )


# ── Public request (what clients send) ──────────────────────────────────

class TaskRequest(BaseModel):
    """Public request schema — clients specify a task_type instead of a model.

    The backend resolves task_type → best model via configuration.
    The legacy 'model' field is accepted for backward compatibility:
      - If only 'model' is given, task_type defaults to chat_with_context.
      - If both are given, 'model' overrides the configured default for task_type.
      - If only task_type is given, the best model is chosen automatically.
    """
    task_type: TaskType = Field(
        default=TaskType.CHAT_WITH_CONTEXT,
        description="Type of AI task to perform. Determines which model is used.",
    )
    messages: List[Message] = Field(
        description="Ordered conversation messages with text and/or image content blocks."
    )
    parameters: Optional[RequestParameters] = Field(
        default=None,
        description="Generation parameters forwarded to the model provider.",
    )
    model: Optional[str] = Field(
        default=None,
        description="[DEPRECATED] Model alias override. Prefer task_type + routing constraints. "
                    "If set, bypasses the router and uses this exact model. "
                    "If set without task_type, task_type defaults to chat_with_context.",
    )
    output_type: Optional[OutputType] = Field(
        default=None,
        description="Desired output modality. Filters out models that cannot produce this output.",
    )
    plan_tier: Optional[PlanTier] = Field(
        default=None,
        description="Service plan tier. Restricts model selection to this tier and below.",
    )
    cost_class: Optional[CostClass] = Field(
        default=None,
        description="Preferred cost profile. cheapest = lowest cost, balanced = middle, best = highest capability.",
    )
    preferred_provider: Optional[str] = Field(
        default=None,
        description="Prefer a specific provider (openai, anthropic, gemini, ollama). "
                    "Router will try this provider first but fall back if unavailable.",
    )
    compare_options: Optional[ImageCompareOptions] = Field(
        default=None,
        description="Options for the image_compare workflow. "
                    "Only meaningful when task_type is 'image_compare'.",
    )
    edit_options: Optional[ImageEditOptions] = Field(
        default=None,
        description="Options for the image_edit workflow. "
                    "Only meaningful when task_type is 'image_edit'.",
    )

    # ── Observability / multi-tenancy (optional, stored in usage log) ──
    client_id: Optional[str] = Field(
        default=None,
        description="Identifier for the calling application or service.",
    )
    group_id: Optional[str] = Field(
        default=None,
        description="Organizational grouping: team, tenant, project.",
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description="Client-supplied ID to group multi-turn conversation requests.",
    )
    async_mode: bool = Field(
        default=False,
        description="If true, process asynchronously and return a job_id for polling. "
                    "Auto-enabled for image_generate and image_edit tasks.",
    )

    @model_validator(mode="after")
    def infer_task_type_from_model(self):
        """Backward compatibility: if model is given without an explicit task_type,
        keep the default (chat_with_context). The route handler will use the model
        field as an override during normalization."""
        return self


class NormalizedTaskRequest(BaseModel):
    """Internal request after routing resolution.

    This is what the route handler passes to providers after the router
    has selected the best model. Clients never create this directly.
    """
    task_type: TaskType
    model: str = Field(description="Resolved model alias from config")
    messages: List[Message]
    parameters: Optional[RequestParameters] = None
    output_type: Optional[OutputType] = None
    plan_tier: Optional[PlanTier] = None
    cost_class: Optional[CostClass] = None
    preferred_provider: Optional[str] = None


# ── Router output ────────────────────────────────────────────────────────

class MatchType(str, Enum):
    """How closely the router matched the requested constraints."""
    EXACT       = "exact"        # All constraints satisfied perfectly
    RELAXED     = "relaxed"      # Some constraints relaxed (e.g., higher tier)
    FALLBACK    = "fallback"     # No good match — used any available model
    OVERRIDE    = "override"     # Model was explicitly overridden by client


class RouteDecision(BaseModel):
    """Output of the routing service — which model was chosen and why."""
    model: str = Field(description="Selected model alias")
    provider: str = Field(description="Provider name (openai, anthropic, gemini, ollama)")
    model_id: str = Field(description="Provider-specific model identifier")
    match_type: MatchType = Field(description="How the router matched constraints")
    matched_constraints: List[str] = Field(
        default_factory=list,
        description="Which routing constraints were satisfied (task_type, output_type, etc.)"
    )
    relaxed_constraints: List[str] = Field(
        default_factory=list,
        description="Which constraints were relaxed to find a match"
    )
    alternatives: List[str] = Field(
        default_factory=list,
        description="Other candidate models that were considered (up to 3)"
    )


# ── Legacy (kept for internal transition) ───────────────────────────────

class UnifiedRequest(BaseModel):
    """Legacy request format — accepted temporarily for backward compatibility.

    When a request arrives with 'model' but no 'task_type', it is parsed
    as a UnifiedRequest first, then normalized into a NormalizedTaskRequest.
    """
    model: str = Field(description="Model alias as configured in models_config.yaml")
    messages: List[Message]
    parameters: Optional[RequestParameters] = None


# ── Response ────────────────────────────────────────────────────────────

class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class UnifiedResponse(BaseModel):
    """Unified response — what the server returns (before encryption)."""
    id: str = Field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")
    task_type: Optional[TaskType] = Field(
        default=None,
        description="The task type that was performed.",
    )
    model: str = Field(description="Model alias that handled the request.")
    content: List[ContentBlock] = Field(
        description="Response content blocks — text, images, or both."
    )
    usage: Optional[UsageInfo] = Field(default=None, description="Token usage breakdown.")
    compare_result: Optional[ImageCompareResult] = Field(
        default=None,
        description="Structured comparison result. Only populated for image_compare "
                    "tasks when structured_output is requested.",
    )
    edit_result: Optional[ImageEditResult] = Field(
        default=None,
        description="Metadata about an image edit operation. Only populated for "
                    "image_edit tasks.",
    )
    error: Optional[str] = Field(default=None, description="Error message if the request failed.")
    error_code: Optional[str] = Field(
        default=None,
        description="Machine-readable error code. e.g. 'NO_MODEL_AVAILABLE', 'POLICY_VIOLATION', "
                    "'GROUP_ROUTING_MISCONFIGURED', 'MODEL_DISABLED', 'PROVIDER_ERROR', 'DECRYPT_FAILED'."
    )


# ── Encryption envelopes ────────────────────────────────────────────────

class EncryptedRequest(BaseModel):
    """The outer encrypted request envelope."""
    encrypted_key: str = Field(description="RSA-OAEP encrypted AES session key (base64)")
    encrypted_payload: str = Field(description="AES-256-GCM encrypted JSON payload (base64)")
    nonce: str = Field(description="AES-GCM nonce (base64)")


class EncryptedResponse(BaseModel):
    """The outer encrypted response envelope."""
    encrypted_payload: str = Field(description="AES-256-GCM encrypted JSON response (base64)")
    nonce: str = Field(description="AES-GCM nonce (base64)")


class PublicKeyResponse(BaseModel):
    public_key: str = Field(description="RSA public key in PEM format")
    key_size: int = 2048
    algorithm: str = "RSA-OAEP+SHA256/AES-256-GCM"
