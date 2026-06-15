"""Abstract base class for AI model providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Set

from app.api.schemas import (
    NormalizedTaskRequest,
    UnifiedResponse,
    ContentBlock,
    TextContent,
    ImageContent,
    UsageInfo,
    TaskType,
    OutputType,
    PlanTier,
    CostClass,
)


@dataclass
class ModelConfig:
    """Configuration for a single model entry — used by both registry and router.

    Capabilities describe what the model can *actually* do. They are the
    ground truth used by the router to validate task compatibility.
    task_types and output_types are inferred from capabilities if not
    explicitly set.
    """

    # ── Identity ──────────────────────────────────────────────────────
    name: str                                    # Alias clients reference
    provider: str                                 # openai | anthropic | gemini | ollama
    model_id: str                                 # Provider-specific model identifier
    description: str = ""

    # ── Status ────────────────────────────────────────────────────────
    enabled: bool = True
    available: bool = True                        # Runtime availability (health checks)

    # ── Input capabilities ────────────────────────────────────────────
    supports_text_input: bool = True              # Can accept text prompts?
    supports_image_input: bool = False            # Can accept images as input?
    supports_multi_image_input: bool = False      # Can accept 2+ images per request?
    max_images: int = 0                           # Max images per request (0 = none)
    max_image_size_mb: float = 0.0                # Max image size in megabytes

    # ── Output capabilities ───────────────────────────────────────────
    supports_text_output: bool = True             # Can produce text?
    supports_image_output: bool = False           # Can generate/edit images?
    supports_image_edit: bool = False             # Can transform input images?

    # ── Streaming ─────────────────────────────────────────────────────
    supports_streaming: bool = False              # Can stream tokens in real-time?

    # ── Task routing (inferred from capabilities if not set) ──────────
    task_types: Set[TaskType] = field(default_factory=set)

    # ── Output types (inferred from capabilities if not set) ──────────
    output_types: Set[OutputType] = field(
        default_factory=lambda: {OutputType.TEXT}
    )

    # ── Tiering & cost ────────────────────────────────────────────────
    plan_tier: PlanTier = PlanTier.STANDARD
    cost_class: CostClass = CostClass.BALANCED
    cost_weight: float = 1.0

    # ── Connection ────────────────────────────────────────────────────
    api_key: str = ""
    base_url: Optional[str] = None

    # ── Derived properties ────────────────────────────────────────────

    @property
    def supports_vision(self) -> bool:
        """Convenience: can this model understand images?"""
        return self.supports_image_input and self.supports_text_input

    @property
    def supports_image_generation(self) -> bool:
        """Convenience: can this model generate images?"""
        return self.supports_image_output and self.supports_text_input

    # ── Task → capability requirements ────────────────────────────────

    # Mapping: what capabilities each task type requires
    TASK_REQUIREMENTS = {
        "chat_with_context": {
            "text_input": True, "image_input": False, "multi_image": False,
            "text_output": True, "image_output": False, "image_edit": False,
        },
        "vision_describe": {
            "text_input": True, "image_input": True, "multi_image": False,
            "text_output": True, "image_output": False, "image_edit": False,
        },
        "vision_qa": {
            "text_input": True, "image_input": True, "multi_image": False,
            "text_output": True, "image_output": False, "image_edit": False,
        },
        "image_compare": {
            "text_input": True, "image_input": True, "multi_image": True,
            "text_output": True, "image_output": False, "image_edit": False,
        },
        "image_generate": {
            "text_input": True, "image_input": False, "multi_image": False,
            "text_output": False, "image_output": True, "image_edit": False,
        },
        "image_edit": {
            "text_input": True, "image_input": True, "multi_image": False,
            "text_output": False, "image_output": True, "image_edit": True,
        },
    }

    # ── Capability inference ──────────────────────────────────────────

    def infer_task_types_from_capabilities(self) -> Set[TaskType]:
        """Derive which task types this model supports from its capabilities."""
        derived: Set[TaskType] = set()
        reqs = self.TASK_REQUIREMENTS

        for task_name, req in reqs.items():
            task = TaskType(task_name)
            if self._capabilities_satisfy(req):
                derived.add(task)

        return derived

    def infer_output_types_from_capabilities(self) -> Set[OutputType]:
        """Derive output types from capabilities."""
        derived: Set[OutputType] = set()
        if self.supports_text_output:
            derived.add(OutputType.TEXT)
        if self.supports_image_output:
            derived.add(OutputType.IMAGE)
        if self.supports_text_output and self.supports_image_output:
            derived.add(OutputType.TEXT_AND_IMAGE)
        return derived

    def _capabilities_satisfy(self, required: dict) -> bool:
        """Check if this model's capabilities satisfy a set of requirements."""
        if required.get("text_input") and not self.supports_text_input:
            return False
        if required.get("image_input") and not self.supports_image_input:
            return False
        if required.get("multi_image") and not self.supports_multi_image_input:
            return False
        if required.get("text_output") and not self.supports_text_output:
            return False
        if required.get("image_output") and not self.supports_image_output:
            return False
        if required.get("image_edit") and not self.supports_image_edit:
            return False
        return True

    # ── Capability checks ──────────────────────────────────────────────

    def supports_task(self, task_type: TaskType) -> bool:
        """Check if this model supports a given task type.

        Uses capabilities first (more precise), falls back to task_types set.
        """
        # If capabilities are explicitly configured, use them
        if self._has_explicit_capabilities():
            req = self.TASK_REQUIREMENTS.get(task_type.value)
            if req:
                return self._capabilities_satisfy(req)

        # Fall back to task_types set
        if self.task_types:
            return task_type in self.task_types

        # If neither is configured, assume all supported
        return True

    def supports_output(self, output_type: OutputType) -> bool:
        """Check if this model can produce a given output modality."""
        if self.output_types:
            return output_type in self.output_types
        return output_type == OutputType.TEXT

    def within_tier(self, max_tier: PlanTier) -> bool:
        """Check if this model is within the allowed tier."""
        tier_order = {PlanTier.FREE: 0, PlanTier.STANDARD: 1, PlanTier.PREMIUM: 2}
        return tier_order.get(self.plan_tier, 1) <= tier_order.get(max_tier, 2)

    def validate_capabilities(self) -> List[str]:
        """Validate capability consistency. Returns list of warnings (empty = valid)."""
        warnings = []
        if self.supports_multi_image_input and not self.supports_image_input:
            warnings.append(
                f"Model '{self.name}': multi_image_input=true but image_input=false — "
                f"multi_image_input requires image_input. Set image_input=true."
            )
        if self.supports_image_edit and not self.supports_image_output:
            warnings.append(
                f"Model '{self.name}': image_edit=true but image_output=false — "
                f"image_edit requires image_output. Set image_output=true."
            )
        if self.supports_image_edit and not self.supports_image_input:
            warnings.append(
                f"Model '{self.name}': image_edit=true but image_input=false — "
                f"image_edit requires image_input. Set image_input=true."
            )
        if self.supports_image_output and not self.supports_text_input:
            warnings.append(
                f"Model '{self.name}': image_output=true but text_input=false — "
                f"image_output requires text_input for prompts."
            )
        if self.max_images > 0 and not self.supports_image_input:
            warnings.append(
                f"Model '{self.name}': max_images={self.max_images} but image_input=false — "
                f"set image_input=true to accept images."
            )
        return warnings

    def _has_explicit_capabilities(self) -> bool:
        """Return True if capabilities have been explicitly configured.

        A model is considered to have explicit capabilities if at least one
        non-default capability boolean differs from the defaults (text input
        and text output default to True, everything else defaults to False).
        """
        # If image input is enabled, capabilities are definitely explicit
        if self.supports_image_input:
            return True
        # If image output is enabled, capabilities are explicit
        if self.supports_image_output:
            return True
        # If max_images is set to a non-zero value, it's explicit
        if self.max_images > 0:
            return True
        # If streaming is enabled
        if self.supports_streaming:
            return True
        return False

    # ── Serialization ─────────────────────────────────────────────────

    def capability_summary(self) -> dict:
        """Return a dict summarising all capabilities for display/logging."""
        return {
            "inputs": {
                "text": self.supports_text_input,
                "image": self.supports_image_input,
                "multi_image": self.supports_multi_image_input,
                "max_images": self.max_images,
                "max_image_size_mb": self.max_image_size_mb,
            },
            "outputs": {
                "text": self.supports_text_output,
                "image": self.supports_image_output,
                "image_edit": self.supports_image_edit,
            },
            "streaming": self.supports_streaming,
        }


class BaseModelProvider(ABC):
    """Abstract provider that translates normalized task requests to provider-specific API calls.

    Subclasses implement `_generate` which receives the normalized request
    and must return a UnifiedResponse.
    """

    def __init__(self, config: ModelConfig):
        self.config = config

    @abstractmethod
    async def _generate(self, request: NormalizedTaskRequest) -> UnifiedResponse:
        """Provider-specific implementation. Must be overridden."""
        ...

    async def generate(self, request: NormalizedTaskRequest) -> UnifiedResponse:
        """Public entry point. Handles pre/post processing around _generate."""
        if not self.config.enabled:
            return UnifiedResponse(
                task_type=request.task_type,
                model=request.model,
                content=[],
                error=f"Model '{self.config.name}' is disabled",
            )

        if not self.config.supports_task(request.task_type):
            return UnifiedResponse(
                task_type=request.task_type,
                model=request.model,
                content=[],
                error=(
                    f"Model '{self.config.name}' does not support task "
                    f"'{request.task_type.value}'. "
                    f"Supported: {[t.value for t in self.config.task_types]}"
                ),
            )

        return await self._generate(request)

    @staticmethod
    def extract_texts(contents: List[ContentBlock]) -> List[str]:
        """Helper: extract all text blocks from a content list."""
        return [c.text for c in contents if isinstance(c, TextContent)]

    @staticmethod
    def extract_images(contents: List[ContentBlock]) -> List[ImageContent]:
        """Helper: extract all image blocks from a content list."""
        return [c for c in contents if isinstance(c, ImageContent)]

    @staticmethod
    def make_response(
        model: str,
        task_type: Optional[TaskType] = None,
        text: str = "",
        images: Optional[List[ImageContent]] = None,
        usage: Optional[UsageInfo] = None,
        error: Optional[str] = None,
    ) -> UnifiedResponse:
        """Build a UnifiedResponse from text and optional images."""
        content: List[ContentBlock] = []
        if text:
            content.append(TextContent(text=text))
        if images:
            content.extend(images)
        return UnifiedResponse(
            task_type=task_type,
            model=model,
            content=content,
            usage=usage,
            error=error,
        )
