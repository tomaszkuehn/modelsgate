"""Application configuration loaded from environment variables."""

import os
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration for the AI backend."""

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Paths
    data_dir: str = "./data"
    models_config_path: str = "./models_config.yaml"

    # Admin
    admin_username: str = "admin"
    admin_password: str = "admin123"
    session_secret: str = "change-me-to-a-random-string"

    # Provider API keys
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    openrouter_api_key: str = ""
    alibaba_api_key: str = ""
    deepseek_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def keys_dir(self) -> Path:
        p = Path(self.data_dir) / "keys"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def database_url(self) -> str:
        db_path = Path(self.data_dir) / "app.db"
        return f"sqlite+aiosqlite:///{db_path}"


settings = Settings()
