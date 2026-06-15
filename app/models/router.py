"""Model routing service — resolves task-based requests to concrete models.

Sits between the API route handler and the provider registry.
Applies configurable routing rules: task_type, output_type, plan_tier,
cost_class, and provider availability.

Architecture:
  API route → Router.route() → RouteDecision
            → Registry.get_provider(decision.model) → Provider.generate()
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from app.api.schemas import (
    TaskType,
    OutputType,
    PlanTier,
    CostClass,
    MatchType,
    RouteDecision,
    NormalizedTaskRequest,
)
from app.models.base import ModelConfig

logger = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────

class NoModelAvailableError(ValueError):
    """Raised when no model can satisfy the routing constraints."""

    def __init__(self, task_type: TaskType, reason: str = ""):
        self.task_type = task_type
        self.reason = reason
        msg = f"No model available for task '{task_type.value}'"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)


# ── Routing context (internal) ────────────────────────────────────────────

@dataclass
class _RoutingContext:
    """Internal state built from the request and configs."""
    task_type: TaskType
    output_type: Optional[OutputType]
    plan_tier: Optional[PlanTier]
    cost_class: Optional[CostClass]
    preferred_provider: Optional[str]
    model_override: Optional[str]

    # Message-level metadata (for capability validation)
    image_count: int = 0
    has_text: bool = True
    estimated_image_size_mb: float = 0.0

    @classmethod
    def from_task_request(cls, task_req) -> "_RoutingContext":
        """Build context from a public TaskRequest, including message metadata."""
        from app.api.schemas import TextContent, ImageContent

        image_count = 0
        has_text = False

        for msg in task_req.messages:
            for block in msg.content:
                if isinstance(block, TextContent) and block.text.strip():
                    has_text = True
                elif isinstance(block, ImageContent):
                    image_count += 1

        return cls(
            task_type=task_req.task_type,
            output_type=task_req.output_type,
            plan_tier=task_req.plan_tier,
            cost_class=task_req.cost_class,
            preferred_provider=task_req.preferred_provider,
            model_override=task_req.model,
            image_count=image_count,
            has_text=has_text,
        )


# ── Router ────────────────────────────────────────────────────────────────

class ModelRouter:
    """Routes task-based requests to the best model using config-driven rules.

    Responsibilities:
      - Filter candidate models by task_type, output_type, plan_tier
      - Sort by cost preference and provider preference
      - Check runtime availability
      - Progressive constraint relaxation when no exact match exists
      - Return a RouteDecision explaining the choice

    Does NOT:
      - Instantiate providers (that's the registry's job)
      - Make API calls to providers
      - Track usage statistics
    """

    # Relaxation order when no candidates match
    RELAXATION_STEPS = [
        ("plan_tier",   "Upgraded plan tier to find a match"),
        ("output_type", "Relaxed output type constraint"),
        ("cost_class",  "Ignored cost preference"),
        ("preferred_provider", "Ignored provider preference"),
    ]

    def __init__(self, configs: List[ModelConfig]):
        """Initialise with model configurations (typically from the registry)."""
        self._configs: List[ModelConfig] = configs

    def update_configs(self, configs: List[ModelConfig]):
        """Refresh configs after config reload."""
        self._configs = configs

    # ── Public API ─────────────────────────────────────────────────────

    def route(self, ctx: _RoutingContext) -> RouteDecision:
        """Resolve routing constraints to a concrete model.

        Args:
            ctx: Routing context extracted from the client's request.

        Returns:
            RouteDecision with selected model and match metadata.

        Raises:
            NoModelAvailableError: If absolutely no model can handle the task.
        """
        # ── Override path (backward compat) ──────────────────────────
        if ctx.model_override:
            return self._handle_override(ctx)

        # ── Gather candidates ────────────────────────────────────────
        candidates = [c for c in self._configs if c.enabled]

        if not candidates:
            raise NoModelAvailableError(ctx.task_type, "No enabled models in config")

        # ── Strict filters ───────────────────────────────────────────
        matched: List[str] = []
        relaxed: List[str] = []

        candidates, filter_log = self._apply_filters(candidates, ctx)
        matched.extend(filter_log)

        # ── Progressive relaxation if filtered to zero ───────────────
        if not candidates:
            candidates, relaxed = self._relax(candidates, ctx)
            if not candidates:
                raise NoModelAvailableError(
                    ctx.task_type,
                    f"No model supports this task "
                    f"(tried all {len(self._configs)} configured models after relaxation)",
                )

        # ── Sort by preference ───────────────────────────────────────
        candidates = self._sort_by_preference(candidates, ctx)

        # ── Check runtime availability ───────────────────────────────
        candidates = self._prioritize_available(candidates)
        if not any(c.available for c in candidates):
            relaxed.append("availability (all models unavailable, using first match)")

        # ── Select winner ────────────────────────────────────────────
        winner = candidates[0]
        alternatives = [c.name for c in candidates[1:4]]

        match_type = MatchType.FALLBACK if relaxed else MatchType.EXACT
        if relaxed and len(relaxed) == 1 and "availability" in relaxed[0]:
            match_type = MatchType.RELAXED

        return RouteDecision(
            model=winner.name,
            provider=winner.provider,
            model_id=winner.model_id,
            match_type=match_type,
            matched_constraints=matched,
            relaxed_constraints=relaxed,
            alternatives=alternatives,
        )

    # ── Filter pipeline ───────────────────────────────────────────────

    def _apply_filters(
        self,
        candidates: List[ModelConfig],
        ctx: _RoutingContext,
    ) -> Tuple[List[ModelConfig], List[str]]:
        """Apply strict routing filters including capability checks.

        Returns (remaining, matched_log).
        """
        matched: List[str] = []
        initial_count = len(candidates)

        # 1. task_type (always strict — uses capabilities if configured)
        candidates = [c for c in candidates if c.supports_task(ctx.task_type)]
        if len(candidates) < initial_count:
            matched.append(f"task_type={ctx.task_type.value}")

        # 2. output_type (strict if specified)
        if ctx.output_type and len(candidates) > 0:
            before = len(candidates)
            candidates = [c for c in candidates if c.supports_output(ctx.output_type)]
            if len(candidates) < before:
                matched.append(f"output_type={ctx.output_type.value}")

        # 3. plan_tier (strict if specified)
        if ctx.plan_tier and len(candidates) > 0:
            before = len(candidates)
            candidates = [c for c in candidates if c.within_tier(ctx.plan_tier)]
            if len(candidates) < before:
                matched.append(f"plan_tier<={ctx.plan_tier.value}")

        # ── Capability-based filters ────────────────────────────────

        # 4. Image input capability
        if ctx.image_count > 0 and len(candidates) > 0:
            before = len(candidates)
            candidates = [
                c for c in candidates
                if c.supports_image_input
            ]
            if len(candidates) < before:
                matched.append("capability=image_input")

        # 5. Multi-image input capability
        if ctx.image_count > 1 and len(candidates) > 0:
            before = len(candidates)
            candidates = [
                c for c in candidates
                if c.supports_multi_image_input
            ]
            if len(candidates) < before:
                matched.append("capability=multi_image_input")

        # 6. Max image count
        if ctx.image_count > 0 and len(candidates) > 0:
            before = len(candidates)
            candidates = [
                c for c in candidates
                if c.max_images == 0 or ctx.image_count <= c.max_images
            ]
            if len(candidates) < before:
                matched.append(f"max_images>={ctx.image_count}")

        # 7. Image output capability (for generation/edit tasks)
        if ctx.task_type in (TaskType.IMAGE_GENERATE, TaskType.IMAGE_EDIT) and len(candidates) > 0:
            before = len(candidates)
            candidates = [c for c in candidates if c.supports_image_output]
            if len(candidates) < before:
                matched.append("capability=image_output")

        # 8. Image edit capability
        if ctx.task_type == TaskType.IMAGE_EDIT and len(candidates) > 0:
            before = len(candidates)
            candidates = [c for c in candidates if c.supports_image_edit]
            if len(candidates) < before:
                matched.append("capability=image_edit")

        return candidates, matched

    def _relax(
        self,
        candidates: List[ModelConfig],
        ctx: _RoutingContext,
    ) -> Tuple[List[ModelConfig], List[str]]:
        """Progressively relax constraints until we find at least one candidate."""
        relaxed: List[str] = []
        all_configs = [c for c in self._configs if c.enabled]

        # Start from scratch with just task_type
        candidates = [c for c in all_configs if c.supports_task(ctx.task_type)]

        # Relax plan_tier
        if ctx.plan_tier and not candidates:
            max_tier = ctx.plan_tier
            for upgrade in [PlanTier.STANDARD, PlanTier.PREMIUM]:
                tier_order = {
                    PlanTier.FREE: 0, PlanTier.STANDARD: 1, PlanTier.PREMIUM: 2
                }
                if tier_order.get(upgrade, 0) > tier_order.get(max_tier, 1):
                    candidates = [
                        c for c in all_configs
                        if c.supports_task(ctx.task_type) and c.within_tier(upgrade)
                    ]
                    if candidates:
                        relaxed.append(
                            f"plan_tier ({ctx.plan_tier.value} → {upgrade.value})"
                        )
                        break

        # Relax output_type
        if ctx.output_type and not candidates:
            candidates = [c for c in all_configs if c.supports_task(ctx.task_type)]
            if candidates:
                relaxed.append(
                    f"output_type ({ctx.output_type.value} → any)"
                )

        # Last resort: any model supporting the task
        if not candidates:
            candidates = [
                c for c in all_configs
                if c.supports_task(ctx.task_type)
                or (not c.task_types)  # models with no task_types = all
            ]

        return candidates, relaxed

    # ── Sorting ───────────────────────────────────────────────────────

    def _sort_by_preference(
        self,
        candidates: List[ModelConfig],
        ctx: _RoutingContext,
    ) -> List[ModelConfig]:
        """Sort candidates by cost preference and provider preference.

        Sorting key (lower = better):
          1. cost_class match (0 = preferred class, 1 = otherwise)
          2. cost_weight (lower = cheaper) — for CHEAPEST; reversed for BEST
          3. preferred_provider match (0 = preferred, 1 = not)
        """
        pref_provider = ctx.preferred_provider

        def sort_key(c: ModelConfig) -> Tuple[int, float, int]:
            # Cost class score
            if ctx.cost_class is None:
                cost_score = 0
            elif c.cost_class == ctx.cost_class:
                cost_score = 0
            else:
                cost_score = 1

            # Cost weight — direction depends on cost_class
            weight = c.cost_weight
            if ctx.cost_class == CostClass.CHEAPEST:
                # Lower weight = better
                pass
            elif ctx.cost_class == CostClass.BEST:
                # Higher weight = better (invert)
                weight = -weight
            # BALANCED: neutral — cost_weight doesn't matter much

            # Provider preference
            if pref_provider and c.provider.lower() == pref_provider.lower():
                provider_score = 0
            else:
                provider_score = 1 if pref_provider else 0

            return (cost_score, weight, provider_score)

        return sorted(candidates, key=sort_key)

    def _prioritize_available(
        self,
        candidates: List[ModelConfig],
    ) -> List[ModelConfig]:
        """Push available models to the front, but keep unavailable as fallback."""
        available = [c for c in candidates if c.available]
        unavailable = [c for c in candidates if not c.available]
        return available + unavailable

    # ── Override handling ─────────────────────────────────────────────

    def _handle_override(self, ctx: _RoutingContext) -> RouteDecision:
        """Handle an explicit model override (backward compat path)."""
        override_name = ctx.model_override
        config = next((c for c in self._configs if c.name == override_name), None)

        if config is None:
            raise NoModelAvailableError(
                ctx.task_type,
                f"Overridden model '{override_name}' not found in config. "
                f"Available: {[c.name for c in self._configs]}",
            )

        if not config.enabled:
            raise NoModelAvailableError(
                ctx.task_type,
                f"Overridden model '{override_name}' is disabled.",
            )

        return RouteDecision(
            model=config.name,
            provider=config.provider,
            model_id=config.model_id,
            match_type=MatchType.OVERRIDE,
            matched_constraints=[f"model={override_name} (explicit override)"],
            relaxed_constraints=[],
            alternatives=[],
        )

    # ── Introspection ─────────────────────────────────────────────────

    def get_routing_table(self) -> List[dict]:
        """Return a human-readable routing table for debugging/dashboard."""
        rows = []
        for c in self._configs:
            rows.append({
                "model": c.name,
                "provider": c.provider,
                "model_id": c.model_id,
                "enabled": c.enabled,
                "available": c.available,
                "task_types": [t.value for t in c.task_types],
                "output_types": [o.value for o in c.output_types],
                "plan_tier": c.plan_tier.value,
                "cost_class": c.cost_class.value,
                "cost_weight": c.cost_weight,
            })
        return rows

    def get_eligible_models_for_tasks(self) -> dict:
        """Return {task_type_value: [model_names...]} for all configured task types.

        Models are listed in priority order (sorted by cost_weight ascending).
        Only enabled models are included.
        """
        result: dict = {}
        enabled = [c for c in self._configs if c.enabled]

        for task in TaskType:
            eligible = [c for c in enabled if c.supports_task(task)]
            # Sort by cost_weight (cheapest first, matching default sort)
            eligible.sort(key=lambda c: c.cost_weight)
            result[task.value] = [c.name for c in eligible]

        return result

    def get_task_model_matrix(self) -> List[dict]:
        """Return a matrix of task × model with eligibility, priority, and fallback info.

        Each row represents a task type with its model candidates in priority order.
        """
        rows = []
        enabled = [c for c in self._configs if c.enabled]

        for task in TaskType:
            eligible = [c for c in enabled if c.supports_task(task)]
            # Sort by cost_weight ascending (primary sort key for default routing)
            eligible.sort(key=lambda c: c.cost_weight)

            # Build tier-based fallback chain
            tiers_present = sorted(
                set(c.plan_tier for c in eligible),
                key=lambda t: {"free": 0, "standard": 1, "premium": 2}.get(t.value, 1),
            )

            row = {
                "task_type": task.value,
                "task_label": task.value.replace("_", " ").title(),
                "total_eligible": len(eligible),
                "primary_model": eligible[0].name if eligible else None,
                "primary_provider": eligible[0].provider if eligible else None,
                "primary_tier": eligible[0].plan_tier.value if eligible else None,
                "candidates": [
                    {
                        "model": c.name,
                        "provider": c.provider,
                        "priority": i + 1,
                        "tier": c.plan_tier.value,
                        "cost_class": c.cost_class.value,
                        "cost_weight": c.cost_weight,
                        "available": c.available,
                    }
                    for i, c in enumerate(eligible)
                ],
                "fallback_chain": [
                    {
                        "tier": t.value,
                        "models": [
                            c.name for c in eligible if c.plan_tier == t
                        ],
                    }
                    for t in tiers_present
                ],
                "tiers_present": [t.value for t in tiers_present],
            }
            rows.append(row)

        return rows

    def get_relaxation_order(self) -> List[dict]:
        """Return the constraint relaxation order for documentation."""
        return [
            {"step": i + 1, "constraint": name, "description": desc}
            for i, (name, desc) in enumerate(self.RELAXATION_STEPS)
        ]

    async def group_has_any_assignments(self, group_id: int) -> bool:
        """Check if a group has any routing assignments at all."""
        from app.database import async_session
        from app.stats.models import GroupTaskRouting
        from sqlalchemy import select as _sel, func
        try:
            async with async_session() as session:
                count = (await session.execute(
                    _sel(func.count(GroupTaskRouting.id)).where(
                        GroupTaskRouting.group_id == group_id
                    )
                )).scalar() or 0
                return count > 0
        except Exception:
            return False

    async def get_group_assignment(
        self,
        group_id: Optional[int],
        task_type: TaskType,
    ) -> Optional[str]:
        """Return the raw assigned model name for a group+task, without capability checks.

        Returns the model_name if an assignment row exists, regardless of whether
        the model can actually handle the task. Used for error reporting.
        """
        if group_id is None:
            return None
        from app.database import async_session
        from app.stats.models import GroupTaskRouting
        from sqlalchemy import select as _sel
        try:
            async with async_session() as session:
                result = await session.execute(
                    _sel(GroupTaskRouting).where(
                        GroupTaskRouting.group_id == group_id,
                        GroupTaskRouting.task_type == task_type.value,
                    )
                )
                row = result.scalar_one_or_none()
                return row.model_name if (row and row.model_name) else None
        except Exception:
            return None

    async def get_group_override(
        self,
        group_id: Optional[int],
        task_type: TaskType,
    ) -> Optional[str]:
        """Check if there's a group-specific model assignment for this task."""
        if group_id is None:
            return None

        from app.database import async_session
        from app.stats.models import GroupTaskRouting
        from sqlalchemy import select as _sel

        try:
            async with async_session() as session:
                result = await session.execute(
                    _sel(GroupTaskRouting).where(
                        GroupTaskRouting.group_id == group_id,
                        GroupTaskRouting.task_type == task_type.value,
                    )
                )
                row = result.scalar_one_or_none()

            if not row or not row.model_name:
                return None

            cfg = next((c for c in self._configs if c.name == row.model_name), None)
            if cfg is None:
                return None
            if cfg.enabled and cfg.supports_task(task_type):
                return row.model_name
        except Exception:
            pass

        return None

    def get_routing_summary(self) -> dict:
        """Return a high-level summary of the routing configuration."""
        enabled = [c for c in self._configs if c.enabled]
        disabled = [c for c in self._configs if not c.enabled]

        providers = set(c.provider for c in self._configs)
        tiers = set(c.plan_tier.value for c in self._configs)
        cost_classes = set(c.cost_class.value for c in self._configs)

        return {
            "total_models": len(self._configs),
            "enabled_models": len(enabled),
            "disabled_models": len(disabled),
            "providers": sorted(providers),
            "tiers": sorted(tiers),
            "cost_classes": sorted(cost_classes),
            "task_types_configured": len([
                t for t in TaskType
                if any(c.supports_task(t) for c in enabled)
            ]),
            "relaxation_steps": len(self.RELAXATION_STEPS),
        }
