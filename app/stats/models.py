"""SQLAlchemy ORM models for usage statistics and admin users."""

from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UsageLog(Base):
    """Tracks each request made through the backend with full observability."""

    __tablename__ = "usage_logs"

    # ── Primary key ────────────────────────────────────────────────────
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # ── Identity ───────────────────────────────────────────────────────
    request_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True,
        comment="Unique per-request identifier (req_XXXXXXXXXXXX)"
    )
    task_type: Mapped[str | None] = mapped_column(
        String(32), index=True, nullable=True,
        comment="Task type: chat_with_context, vision_describe, image_compare, etc."
    )
    workflow_id: Mapped[str | None] = mapped_column(
        String(36), index=True, nullable=True,
        comment="UUID for the workflow execution that processed this request"
    )

    # ── Model selection ────────────────────────────────────────────────
    model_name: Mapped[str] = mapped_column(
        String(128), index=True,
        comment="Model alias from config (e.g., 'gpt-4o', 'claude-haiku')"
    )
    model_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True,
        comment="Provider-specific model identifier (e.g., 'qwen-vl-plus', 'gpt-4o')"
    )
    provider: Mapped[str] = mapped_column(
        String(64),
        comment="Provider name: openai, anthropic, gemini, ollama"
    )
    routing_decision: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="JSON serialization of the RouteDecision (model, provider, match_type, alternatives)"
    )

    # ── Modality ───────────────────────────────────────────────────────
    input_modality: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment="Input modality summary: text, text+image, text+2_images, etc."
    )
    output_modality: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment="Output modality summary: text, image, text+image"
    )

    # ── Token usage ────────────────────────────────────────────────────
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)

    # ── Performance ────────────────────────────────────────────────────
    response_time_ms: Mapped[int] = mapped_column(Integer, default=0)

    # ── Status ─────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16), index=True,
        comment="'success' or 'error'"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Multi-tenancy & grouping ───────────────────────────────────────
    conversation_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True,
        comment="Client-supplied ID grouping multi-turn conversation requests"
    )
    client_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True,
        comment="Identifier for the calling application or service"
    )
    group_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True,
        comment="Organizational grouping: team, tenant, project"
    )

    # ── Assets ─────────────────────────────────────────────────────────
    asset_ids: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="JSON array of asset/image identifiers used in the request"
    )

    # ── Timestamp ──────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )

    def __repr__(self):
        return (f"<UsageLog {self.request_id} task={self.task_type} "
                f"model={self.model_name} status={self.status}>")


class AdminUser(Base):
    """Admin panel user accounts."""

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def __repr__(self):
        return f"<AdminUser {self.username}>"


class RoutingPolicy(Base):
    """Policy controlling what a client or group is allowed to do."""

    __tablename__ = "routing_policies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Identity
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Task restrictions (null = all allowed)
    allowed_task_types: Mapped[str | None] = mapped_column(
        JSON, nullable=True,
        comment="JSON array of allowed task_type values. null = all allowed."
    )

    # Model/provider restrictions (null = all allowed)
    allowed_providers: Mapped[str | None] = mapped_column(
        JSON, nullable=True,
        comment="JSON array of allowed provider names. null = all allowed."
    )
    allowed_models: Mapped[str | None] = mapped_column(
        JSON, nullable=True,
        comment="JSON array of allowed model aliases. null = all allowed."
    )

    # Token/cost ceilings (0 or null = no limit)
    max_tokens_per_request: Mapped[int] = mapped_column(
        Integer, default=0,
        comment="Maximum tokens allowed per single request. 0 = unlimited."
    )
    max_tokens_per_day: Mapped[int] = mapped_column(
        Integer, default=0,
        comment="Cumulative token ceiling per calendar day. 0 = unlimited."
    )
    max_tokens_per_month: Mapped[int] = mapped_column(
        Integer, default=0,
        comment="Cumulative token ceiling per calendar month. 0 = unlimited."
    )

    # Capability restrictions
    allow_image_input: Mapped[bool] = mapped_column(
        Boolean, default=True,
        comment="Allow requests containing images"
    )
    allow_image_output: Mapped[bool] = mapped_column(
        Boolean, default=False,
        comment="Allow image generation tasks (image_generate, image_edit)"
    )
    allow_image_edit: Mapped[bool] = mapped_column(
        Boolean, default=False,
        comment="Allow image editing tasks (image_edit)"
    )
    allow_streaming: Mapped[bool] = mapped_column(
        Boolean, default=False,
        comment="Allow streaming responses"
    )

    # Tier/cost limits
    max_plan_tier: Mapped[str] = mapped_column(
        String(16), default="premium",
        comment="Maximum allowed plan tier: free, standard, premium"
    )
    max_cost_class: Mapped[str] = mapped_column(
        String(16), default="best",
        comment="Maximum allowed cost class: cheapest, balanced, best"
    )

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(
        Integer, default=0,
        comment="Higher priority wins when multiple policies apply"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    clients: Mapped[list] = relationship("Client", back_populates="policy", lazy="selectin")
    groups: Mapped[list] = relationship("ClientGroup", back_populates="policy", lazy="selectin")

    def allowed_task_types_list(self) -> list | None:
        return self.allowed_task_types

    def allowed_providers_list(self) -> list | None:
        return self.allowed_providers

    def allowed_models_list(self) -> list | None:
        return self.allowed_models

    def __repr__(self):
        return f"<RoutingPolicy {self.name} active={self.is_active}>"


class ClientGroup(Base):
    """Organizational grouping of clients (team, tenant, project).

    The default group (id=1, group_key='default') is seeded on startup.
    All new clients are assigned to it automatically.
    """

    __tablename__ = "client_groups"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    group_key: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, index=True,
        comment="Unique identifier for this group (e.g., 'team-alpha', 'tenant-42')"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    routing_policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("routing_policies.id"), nullable=True
    )
    policy: Mapped[RoutingPolicy | None] = relationship(
        "RoutingPolicy", back_populates="groups", lazy="selectin"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    clients: Mapped[list] = relationship("Client", back_populates="group", lazy="selectin")

    def __repr__(self):
        return f"<ClientGroup {self.group_key}>"


class Client(Base):
    """Registered API client — identified by client_key in every request.

    Registration: POST /api/v1/register → returns client_key.
    Free plan = unlimited access. Blocked clients get 403 on all requests.
    """

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    client_key: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True,
        comment="Auto-generated unique ID sent in client_id field of every request"
    )
    plan: Mapped[str] = mapped_column(
        String(16), default="free", index=True,
        comment="free (unlimited) | premium"
    )
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Group membership (optional)
    client_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("client_groups.id"), nullable=True
    )
    group: Mapped[ClientGroup | None] = relationship(
        "ClientGroup", back_populates="clients", lazy="selectin"
    )

    # Direct policy (optional, overrides group policy)
    routing_policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("routing_policies.id"), nullable=True
    )
    policy: Mapped[RoutingPolicy | None] = relationship(
        "RoutingPolicy", back_populates="clients", lazy="selectin"
    )

    # Cumulative usage tracking
    tokens_used_today: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used_this_month: Mapped[int] = mapped_column(Integer, default=0)
    last_usage_reset_day: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_usage_reset_month: Mapped[str | None] = mapped_column(String(7), nullable=True)

    registered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Client {self.client_key} plan={self.plan}>"


class GroupTaskRouting(Base):
    """Per-group model assignment for each task type.

    When a client in a group makes a request, the router checks this table first.
    If a row exists for (group_id, task_type), that model is used.
    If model_name is NULL or empty, the default router is used (fallback).
    """

    __tablename__ = "group_task_routing"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("client_groups.id"), nullable=False, index=True
    )
    task_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    model_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment="Model alias to use, or NULL to use default router"
    )

    def __repr__(self):
        return f"<GroupTaskRouting group={self.group_id} {self.task_type}→{self.model_name or 'default'}>"


class Job(Base):
    """Async job for long-running tasks (image_generate, image_edit)."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True,
        comment="UUID v4 identifying this job"
    )
    task_type: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True,
        comment="pending | processing | completed | failed | cancelled"
    )

    # The original encrypted request (stored for reprocessing/debugging)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    # The completed response (null until done)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Error message if failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Progress
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)

    # Identity
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self):
        return f"<Job {self.job_id} {self.task_type} {self.status}>"


class ModelConfigRow(Base):
    """Persistent model configuration — editable via admin panel.

    Stored exclusively in the database (model_configs table).
    """

    __tablename__ = "model_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Identity
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Capabilities (stored as JSON for flexibility)
    capabilities_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}",
        comment="JSON: {text_input, image_input, multi_image_input, text_output, image_output, image_edit, streaming, max_images, max_image_size_mb}"
    )

    # Connection
    api_key: Mapped[str | None] = mapped_column(
        String(256), nullable=True,
        comment="Per-model API key override. Falls back to env var if empty."
    )
    base_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="Per-model base URL override. Uses provider default if empty."
    )

    # Tiering
    plan_tier: Mapped[str] = mapped_column(String(16), default="standard")
    cost_class: Mapped[str] = mapped_column(String(16), default="balanced")
    cost_weight: Mapped[float] = mapped_column(Float, default=1.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<ModelConfigRow {self.name} {self.provider}/{self.model_id}>"


class RequestLog(Base):
    """Full request/response trace log — FIFO-capped at 1000 entries."""

    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    task_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True,
        comment="Provider-specific model identifier (e.g., 'qwen-vl-plus', 'gpt-4o')"
    )
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    api_key_prefix: Mapped[str | None] = mapped_column(
        String(12), nullable=True,
        comment="First 12 characters of the API key used for this request"
    )

    # The decrypted unified request (TaskRequest)
    original_json: Mapped[str] = mapped_column(Text, nullable=False)
    # The provider-specific converted format (what was sent to the AI)
    converted_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The unified response
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="success")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<RequestLog {self.request_id} {self.task_type}→{self.model_name}>"
