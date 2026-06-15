"""Model registry — loads configuration, manages provider instances.

Responsibilities:
  - Load model configs from YAML (with all routing fields)
  - Expose configs to the router
  - Lazily instantiate and cache provider adapters
  - Forward requests to providers

Model *selection* is handled by app.models.router.ModelRouter.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

from app.config import settings
from app.api.schemas import (
    TaskType,
    OutputType,
    PlanTier,
    CostClass,
    NormalizedTaskRequest,
    UnifiedResponse,
)
from app.models.base import ModelConfig, BaseModelProvider

logger = logging.getLogger(__name__)


class ModelRegistry:
    """Registry that loads model configs and manages provider instances.

    Singleton that reads models_config.yaml, parses all routing fields,
    exposes configs to the router, and lazily instantiates providers.

    Does NOT select models — that's the router's job.
    """

    _instance: Optional["ModelRegistry"] = None
    _providers: Dict[str, BaseModelProvider] = {}
    _configs: Dict[str, ModelConfig] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # Don't load config here - use async initialize() instead
            cls._instance._configs = {}
            cls._instance._providers = {}
        return cls._instance

    async def initialize(self):
        """Async initialization - load configs from DB."""
        await self._load_from_db_async()
        if not self._configs:
            # Fallback to YAML
            self._load_from_yaml()
        logger.info(f"ModelRegistry initialized with {len(self._configs)} models")

    async def _load_from_db_async(self) -> bool:
        """Load configs from the model_configs table asynchronously."""
        from app.database import async_session
        from app.stats.models import ModelConfigRow
        from sqlalchemy import select as _sel
        import json as _json

        async with async_session() as session:
            result = await session.execute(_sel(ModelConfigRow).order_by(ModelConfigRow.name))
            rows = result.scalars().all()

        if not rows:
            return False

        self._configs.clear()
        for row in rows:
            try:
                caps = _json.loads(row.capabilities_json) if row.capabilities_json else {}
            except _json.JSONDecodeError:
                caps = {}

            config = ModelConfig(
                name=row.name, provider=row.provider, model_id=row.model_id,
                description=row.description, enabled=row.enabled,
                api_key=row.api_key or "",
                supports_text_input=caps.get("text_input", True),
                supports_image_input=caps.get("image_input", False),
                supports_multi_image_input=caps.get("multi_image_input", False),
                supports_text_output=caps.get("text_output", True),
                supports_image_output=caps.get("image_output", False),
                supports_image_edit=caps.get("image_edit", False),
                supports_streaming=caps.get("streaming", False),
                max_images=int(caps.get("max_images", 0)),
                max_image_size_mb=float(caps.get("max_image_size_mb", 0.0)),
                plan_tier=PlanTier(row.plan_tier) if row.plan_tier in ("free","standard","premium") else PlanTier.STANDARD,
                cost_class=CostClass(row.cost_class) if row.cost_class in ("cheapest","balanced","best") else CostClass.BALANCED,
                cost_weight=row.cost_weight,
            )
            config.task_types = config.infer_task_types_from_capabilities()
            config.output_types = config.infer_output_types_from_capabilities()
            self._configs[config.name] = config

            # Log capability contradictions
            for w in config.validate_capabilities():
                logger.warning(w)

        return True

    def _load_from_yaml(self):
        """Load model configurations from YAML file."""
        config_path = Path(settings.models_config_path)
        if not config_path.exists():
            logger.warning(f"Models config not found at {config_path}")
            return

        with open(config_path, "r") as f:
            data = yaml.safe_load(f)

        self._configs.clear()
        rows_to_seed = []
        for entry in data.get("models", []):
            config = self._parse_yaml_entry(entry)
            self._configs[config.name] = config
            rows_to_seed.append(self._config_to_row(config))

        # Seed DB
        self._seed_db(rows_to_seed)

        logger.info(f"Loaded {len(self._configs)} model configurations from YAML, seeded DB")

    def _parse_yaml_entry(self, entry: dict) -> ModelConfig:
        """Parse a single YAML model entry into a ModelConfig."""
        name = entry["name"]
        caps = entry.get("capabilities", {})
        has_explicit_caps = any([
            caps.get("image_input", False), caps.get("image_output", False),
            caps.get("streaming", False), caps.get("multi_image_input", False),
            caps.get("image_edit", False), caps.get("max_images", 0) > 0,
        ])

        raw_task_types = entry.get("task_types", [])
        task_types: Set[TaskType] = set()
        for t in raw_task_types:
            try: task_types.add(TaskType(t))
            except ValueError: logger.warning(f"Unknown task_type '{t}' in model '{name}'")

        raw_output_types = entry.get("output_types", [])
        output_types: Set[OutputType] = set()
        for o in raw_output_types:
            try: output_types.add(OutputType(o))
            except ValueError: logger.warning(f"Unknown output_type '{o}' in model '{name}'")
        if not raw_output_types and not has_explicit_caps:
            output_types.add(OutputType.TEXT)

        raw_tier = entry.get("plan_tier", "standard")
        try: plan_tier = PlanTier(raw_tier)
        except ValueError: plan_tier = PlanTier.STANDARD

        raw_cost = entry.get("cost_class", "balanced")
        try: cost_class = CostClass(raw_cost)
        except ValueError: cost_class = CostClass.BALANCED

        config = ModelConfig(
            name=name, provider=entry["provider"], model_id=entry["model_id"],
            description=entry.get("description", ""),
            enabled=entry.get("enabled", True), available=entry.get("available", True),
            supports_text_input=caps.get("text_input", True),
            supports_image_input=caps.get("image_input", False),
            supports_multi_image_input=caps.get("multi_image_input", False),
            supports_text_output=caps.get("text_output", True),
            supports_image_output=caps.get("image_output", False),
            supports_image_edit=caps.get("image_edit", False),
            supports_streaming=caps.get("streaming", False),
            max_images=int(caps.get("max_images", 0)),
            max_image_size_mb=float(caps.get("max_image_size_mb", 0.0)),
            task_types=task_types, output_types=output_types,
            plan_tier=plan_tier, cost_class=cost_class,
            cost_weight=float(entry.get("cost_weight", 1.0)),
        )
        if has_explicit_caps and not raw_task_types:
            config.task_types = config.infer_task_types_from_capabilities()
        if has_explicit_caps and not raw_output_types:
            config.output_types = config.infer_output_types_from_capabilities()
        return config

    # ── DB persistence ─────────────────────────────────────────────────

    def _seed_db(self, rows: list):
        """Insert initial model config rows into the database."""
        import asyncio as _asyncio
        from app.database import async_session
        from app.stats.models import ModelConfigRow
        from sqlalchemy import select as _sel

        async def _seed():
            async with async_session() as session:
                existing = (await session.execute(_sel(ModelConfigRow))).scalars().all()
                if not existing:
                    session.add_all(rows)
                    await session.commit()

        try:
            _asyncio.run(_seed())
        except Exception as e:
            logger.warning(f"DB seed skipped: {e}")

    def _config_to_row(self, config: ModelConfig):
        """Convert a ModelConfig to a ModelConfigRow for DB storage."""
        import json as _json
        from app.stats.models import ModelConfigRow
        caps = {
            "text_input": config.supports_text_input,
            "image_input": config.supports_image_input,
            "multi_image_input": config.supports_multi_image_input,
            "text_output": config.supports_text_output,
            "image_output": config.supports_image_output,
            "image_edit": config.supports_image_edit,
            "streaming": config.supports_streaming,
            "max_images": config.max_images,
            "max_image_size_mb": config.max_image_size_mb,
        }
        return ModelConfigRow(
            name=config.name, provider=config.provider, model_id=config.model_id,
            description=config.description, enabled=config.enabled,
            api_key=config.api_key or None,
            capabilities_json=_json.dumps(caps),
            plan_tier=config.plan_tier.value, cost_class=config.cost_class.value,
            cost_weight=config.cost_weight,
        )

    async def reload_from_db(self):
        """Reload configs from DB asynchronously (called after admin CRUD changes)."""
        from app.database import async_session
        from app.stats.models import ModelConfigRow
        from sqlalchemy import select as _sel
        import json as _json

        async with async_session() as session:
            result = await session.execute(
                _sel(ModelConfigRow).order_by(ModelConfigRow.name)
            )
            rows = result.scalars().all()

        if not rows:
            return

        self._configs.clear()
        self._providers.clear()  # reset cached providers too
        for row in rows:
            try:
                caps = _json.loads(row.capabilities_json) if row.capabilities_json else {}
            except _json.JSONDecodeError:
                caps = {}

            config = ModelConfig(
                name=row.name, provider=row.provider, model_id=row.model_id,
                description=row.description, enabled=row.enabled,
                api_key=row.api_key or "",
                supports_text_input=caps.get("text_input", True),
                supports_image_input=caps.get("image_input", False),
                supports_multi_image_input=caps.get("multi_image_input", False),
                supports_text_output=caps.get("text_output", True),
                supports_image_output=caps.get("image_output", False),
                supports_image_edit=caps.get("image_edit", False),
                supports_streaming=caps.get("streaming", False),
                max_images=int(caps.get("max_images", 0)),
                max_image_size_mb=float(caps.get("max_image_size_mb", 0.0)),
                plan_tier=PlanTier(row.plan_tier) if row.plan_tier in ("free","standard","premium") else PlanTier.STANDARD,
                cost_class=CostClass(row.cost_class) if row.cost_class in ("cheapest","balanced","best") else CostClass.BALANCED,
                cost_weight=row.cost_weight,
            )
            config.task_types = config.infer_task_types_from_capabilities()
            config.output_types = config.infer_output_types_from_capabilities()
            self._configs[config.name] = config

            for w in config.validate_capabilities():
                logger.warning(w)

        logger.info(f"Reloaded {len(self._configs)} models from database")

    def sync_to_yaml(self):
        """Write current configs back to models_config.yaml."""
        import json as _json
        config_path = Path(settings.models_config_path)
        entries = []
        for config in self._configs.values():
            entries.append({
                "name": config.name,
                "provider": config.provider,
                "model_id": config.model_id,
                "description": config.description,
                "capabilities": {
                    "text_input": config.supports_text_input,
                    "image_input": config.supports_image_input,
                    "multi_image_input": config.supports_multi_image_input,
                    "text_output": config.supports_text_output,
                    "image_output": config.supports_image_output,
                    "image_edit": config.supports_image_edit,
                    "streaming": config.supports_streaming,
                    "max_images": config.max_images,
                    "max_image_size_mb": config.max_image_size_mb,
                },
                "plan_tier": config.plan_tier.value,
                "cost_class": config.cost_class.value,
                "cost_weight": config.cost_weight,
                "enabled": config.enabled,
            })
        try:
            with open(config_path, "w") as f:
                yaml.dump({"models": entries}, f, default_flow_style=False, sort_keys=False)
            logger.info(f"Synced {len(entries)} models to {config_path}")
        except OSError as e:
            logger.warning(f"Could not sync to YAML ({e}) — changes saved to DB only")

    # ── Config accessors ────────────────────────────────────────────────

    def get_all_configs(self) -> List[ModelConfig]:
        """Return all model configs (used by the router)."""
        return list(self._configs.values())

    def get_config(self, model_name: str) -> Optional[ModelConfig]:
        """Get the config for a model by alias name."""
        return self._configs.get(model_name)

    def get_provider_name(self, model_name: str) -> str:
        """Get the provider name for a model alias."""
        config = self._configs.get(model_name)
        return config.provider if config else "unknown"

    def list_models(self) -> Dict[str, ModelConfig]:
        """Return all registered model configs (keyed by alias)."""
        return dict(self._configs)

    # ── Provider instantiation ──────────────────────────────────────────

    def _get_provider(self, model_name: str) -> BaseModelProvider:
        """Get or create a provider instance for the given model alias.

        Raises ValueError if the model is not configured.
        """
        config = self._configs.get(model_name)
        if config is None:
            raise ValueError(
                f"Unknown model '{model_name}'. "
                f"Available: {list(self._configs.keys())}"
            )

        logger.warning(f"Registry _get_provider: model_name={model_name}, config.api_key={'set' if config.api_key else 'empty'}")

        if model_name not in self._providers:
            provider = self._instantiate_provider(config)
            self._providers[model_name] = provider

        return self._providers[model_name]

    def _instantiate_provider(self, config: ModelConfig) -> BaseModelProvider:
        """Create a provider instance based on configuration."""
        provider_type = config.provider.lower()

        if provider_type == "openai":
            from app.models.providers.openai import OpenAIProvider
            return OpenAIProvider(config)
        elif provider_type == "anthropic":
            from app.models.providers.anthropic import AnthropicProvider
            return AnthropicProvider(config)
        elif provider_type == "gemini":
            from app.models.providers.gemini import GeminiProvider
            return GeminiProvider(config)
        elif provider_type == "ollama":
            from app.models.providers.ollama import OllamaProvider
            return OllamaProvider(config)
        elif provider_type == "openrouter":
            from app.models.providers.openrouter import OpenRouterProvider
            return OpenRouterProvider(config)
        elif provider_type == "alibaba":
            from app.models.providers.alibaba import AlibabaProvider
            return AlibabaProvider(config)
        elif provider_type == "deepseek":
            from app.models.providers.deepseek import DeepseekProvider
            return DeepseekProvider(config)
        else:
            raise ValueError(f"Unknown provider type: {provider_type}")

    # ── Request forwarding ──────────────────────────────────────────────

    async def generate(self, request: NormalizedTaskRequest) -> UnifiedResponse:
        """Forward a normalized task request to the selected provider."""
        provider = self._get_provider(request.model)
        return await provider.generate(request)

    async def generate_legacy(self, request) -> UnifiedResponse:
        """Backward-compat: route an old UnifiedRequest (model-only, no task_type)."""
        normalized = NormalizedTaskRequest(
            task_type=TaskType.CHAT_WITH_CONTEXT,
            model=request.model,
            messages=request.messages,
            parameters=request.parameters,
        )
        return await self.generate(normalized)
