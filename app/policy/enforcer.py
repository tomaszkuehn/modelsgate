"""Policy enforcement — validates requests against client/group policies.

Applied BEFORE routing. Rejects requests that violate policy rules.
Injects policy-derived constraints into the routing context.
Tracks cumulative usage against daily/monthly token ceilings.

Resolution order:
  1. Client-specific policy (if client has routing_policy_id set)
  2. Client's group policy (if client belongs to a group with a policy)
  3. Default: allow all (no policy)
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.stats.models import Client, ClientGroup, RoutingPolicy
from app.api.schemas import TaskType, PlanTier, CostClass

logger = logging.getLogger(__name__)


# ── Policy violation ──────────────────────────────────────────────────────

class PolicyViolationError(ValueError):
    """Raised when a request violates an active policy."""

    def __init__(self, reason: str, policy_name: str = ""):
        self.policy_name = policy_name
        prefix = f"Policy '{policy_name}' violation: " if policy_name else "Policy violation: "
        super().__init__(prefix + reason)


# ── Resolved policy (internal) ────────────────────────────────────────────

@dataclass
class ResolvedPolicy:
    """The effective policy after resolving client → group → default."""
    policy_name: str = "default"

    # Restrictions
    allowed_task_types: Optional[List[str]] = None
    allowed_providers: Optional[List[str]] = None
    allowed_models: Optional[List[str]] = None

    # Ceilings
    max_tokens_per_request: int = 0
    max_tokens_per_day: int = 0
    max_tokens_per_month: int = 0

    # Capabilities
    allow_image_input: bool = True
    allow_image_output: bool = False
    allow_image_edit: bool = False
    allow_streaming: bool = False

    # Tier/cost limits
    max_plan_tier: str = "premium"
    max_cost_class: str = "best"

    # Cumulative usage (loaded from client DB row)
    tokens_used_today: int = 0
    tokens_used_this_month: int = 0

    @classmethod
    def default(cls) -> "ResolvedPolicy":
        """Permissive default — allows everything (including image generation/editing)."""
        return cls(
            policy_name="default (allow all)",
            allow_image_output=True,
            allow_image_edit=True,
            allow_streaming=True,
        )

    @classmethod
    def from_db_policy(cls, policy: RoutingPolicy) -> "ResolvedPolicy":
        """Build from a RoutingPolicy ORM row."""
        return cls(
            policy_name=policy.name,
            allowed_task_types=policy.allowed_task_types,
            allowed_providers=policy.allowed_providers,
            allowed_models=policy.allowed_models,
            max_tokens_per_request=policy.max_tokens_per_request or 0,
            max_tokens_per_day=policy.max_tokens_per_day or 0,
            max_tokens_per_month=policy.max_tokens_per_month or 0,
            allow_image_input=policy.allow_image_input,
            allow_image_output=policy.allow_image_output,
            allow_image_edit=policy.allow_image_edit,
            allow_streaming=policy.allow_streaming,
            max_plan_tier=policy.max_plan_tier,
            max_cost_class=policy.max_cost_class,
        )


# ── Enforcer ──────────────────────────────────────────────────────────────

class PolicyEnforcer:
    """Enforces routing policies before requests reach the router."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_policy(self, client_id: Optional[str]) -> ResolvedPolicy:
        """Resolve the effective policy for a client.

        Args:
            client_id: The client_id from the request (may be None).

        Returns:
            A ResolvedPolicy — never None (defaults to allow-all).
        """
        if not client_id:
            return ResolvedPolicy.default()

        # Look up client
        result = await self.session.execute(
            select(Client)
            .where(Client.client_key == client_id, Client.is_active == True)
        )
        client = result.scalar_one_or_none()

        if client is None:
            # Unknown client — could log, but default to allow-all for now
            logger.debug(f"Unknown client_id '{client_id}' — using default policy")
            return ResolvedPolicy.default()

        # Client-level policy takes precedence
        if client.policy and client.policy.is_active:
            resolved = ResolvedPolicy.from_db_policy(client.policy)
            resolved.tokens_used_today = client.tokens_used_today
            resolved.tokens_used_this_month = client.tokens_used_this_month
            await self._reset_counters_if_needed(client)
            return resolved

        # Group-level policy
        if client.group and client.group.policy and client.group.policy.is_active:
            resolved = ResolvedPolicy.from_db_policy(client.group.policy)
            resolved.tokens_used_today = client.tokens_used_today
            resolved.tokens_used_this_month = client.tokens_used_this_month
            await self._reset_counters_if_needed(client)
            return resolved

        # No policy — allow all
        return ResolvedPolicy.default()

    async def _reset_counters_if_needed(self, client: Client):
        """Reset daily/monthly counters if the period has rolled over."""
        today_str = date.today().isoformat()
        month_str = date.today().strftime("%Y-%m")
        changed = False

        if client.last_usage_reset_day != today_str:
            client.tokens_used_today = 0
            client.last_usage_reset_day = today_str
            changed = True

        if client.last_usage_reset_month != month_str:
            client.tokens_used_this_month = 0
            client.last_usage_reset_month = month_str
            changed = True

        if changed:
            await self.session.commit()

    async def validate_request(
        self,
        policy: ResolvedPolicy,
        task_type: TaskType,
        image_count: int = 0,
        requested_tokens: int = 0,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        request_output_type: Optional[str] = None,
    ):
        """Validate a request against a resolved policy.

        Raises PolicyViolationError if any rule is violated.

        Args:
            policy: The resolved policy to check against.
            task_type: The requested task type.
            image_count: Number of images in the request.
            requested_tokens: max_tokens from request parameters (0 = use default).
            provider: Provider being routed to (if known).
            model: Model alias being used (if known).
        """
        task_val = task_type.value if hasattr(task_type, 'value') else str(task_type)

        # 1. Task type restriction
        if (
            policy.allowed_task_types is not None
            and task_val not in policy.allowed_task_types
        ):
            raise PolicyViolationError(
                f"Task type '{task_val}' is not allowed. "
                f"Allowed: {policy.allowed_task_types}",
                policy.policy_name,
            )

        # 2. Image input restriction
        if image_count > 0 and not policy.allow_image_input:
            raise PolicyViolationError(
                "Image input is not allowed by this policy.",
                policy.policy_name,
            )

        # 3. Image output / generation restriction
        if task_val in ("image_generate", "image_edit"):
            if not policy.allow_image_output:
                raise PolicyViolationError(
                    f"Task '{task_val}' requires image output (generation), "
                    f"which is disabled by this policy.",
                    policy.policy_name,
                )

        # 4. Image edit restriction
        if task_val == "image_edit" and not policy.allow_image_edit:
            raise PolicyViolationError(
                "Image editing is disabled by this policy.",
                policy.policy_name,
            )

        # 5. Provider restriction
        if policy.allowed_providers is not None and provider:
            if provider not in policy.allowed_providers:
                raise PolicyViolationError(
                    f"Provider '{provider}' is not allowed. "
                    f"Allowed: {policy.allowed_providers}",
                    policy.policy_name,
                )

        # 6. Model restriction
        if policy.allowed_models is not None and model:
            if model not in policy.allowed_models:
                raise PolicyViolationError(
                    f"Model '{model}' is not allowed. "
                    f"Allowed: {policy.allowed_models}",
                    policy.policy_name,
                )

        # 7. Per-request token ceiling
        if policy.max_tokens_per_request > 0 and requested_tokens > 0:
            if requested_tokens > policy.max_tokens_per_request:
                raise PolicyViolationError(
                    f"Requested max_tokens ({requested_tokens}) exceeds "
                    f"per-request limit ({policy.max_tokens_per_request}).",
                    policy.policy_name,
                )

        # 8. Daily token ceiling
        if policy.max_tokens_per_day > 0:
            if policy.tokens_used_today + requested_tokens > policy.max_tokens_per_day:
                raise PolicyViolationError(
                    f"Daily token ceiling ({policy.max_tokens_per_day}) would be "
                    f"exceeded (used: {policy.tokens_used_today}, "
                    f"requesting: ~{requested_tokens}).",
                    policy.policy_name,
                )

        # 9. Monthly token ceiling
        if policy.max_tokens_per_month > 0:
            if policy.tokens_used_this_month + requested_tokens > policy.max_tokens_per_month:
                raise PolicyViolationError(
                    f"Monthly token ceiling ({policy.max_tokens_per_month}) would be "
                    f"exceeded (used: {policy.tokens_used_this_month}, "
                    f"requesting: ~{requested_tokens}).",
                    policy.policy_name,
                )

    def apply_policy_constraints(
        self,
        policy: ResolvedPolicy,
        task_req,  # TaskRequest
    ):
        """Apply policy-derived constraints to the request's routing fields.

        Modifies the task_req in-place to enforce tier/cost/provider limits.
        Only tightens constraints — never loosens them.

        Args:
            policy: The resolved policy.
            task_req: The TaskRequest to modify.
        """
        # Apply tier limit
        if policy.max_plan_tier != "premium":
            try:
                policy_tier = PlanTier(policy.max_plan_tier)
                if task_req.plan_tier is None:
                    task_req.plan_tier = policy_tier
                else:
                    # Use the stricter (lower) tier
                    tier_order = {"free": 0, "standard": 1, "premium": 2}
                    if tier_order.get(task_req.plan_tier.value, 2) > tier_order.get(policy.max_plan_tier, 2):
                        task_req.plan_tier = policy_tier
            except ValueError:
                pass

        # Apply cost class limit
        if policy.max_cost_class != "best":
            try:
                policy_cost = CostClass(policy.max_cost_class)
                if task_req.cost_class is None:
                    task_req.cost_class = policy_cost
            except ValueError:
                pass

        # Apply provider restriction
        if policy.allowed_providers is not None and len(policy.allowed_providers) == 1:
            # If only one provider allowed, force it
            if task_req.preferred_provider is None:
                task_req.preferred_provider = policy.allowed_providers[0]

    async def record_usage(
        self,
        client_id: Optional[str],
        tokens_used: int,
    ):
        """Record token usage against a client's daily/monthly counters."""
        if not client_id or tokens_used <= 0:
            return

        result = await self.session.execute(
            select(Client).where(Client.client_key == client_id)
        )
        client = result.scalar_one_or_none()
        if client is None:
            return

        client.tokens_used_today += tokens_used
        client.tokens_used_this_month += tokens_used
        await self.session.commit()
