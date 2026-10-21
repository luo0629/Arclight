from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARCLIGHT_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://arclight:arclight@localhost:5432/arclight"
    checkpoint_url: str = "postgresql://arclight:arclight@localhost:5432/arclight"
    redis_url: str = "redis://localhost:6379/0"
    api_token: SecretStr = SecretStr("local-development-token-change-me")
    provider: Literal["mock", "openai_compatible"] = "mock"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4.1-mini"
    llm_timeout_seconds: float = 60
    max_story_chars: int = 20000
    jwt_secret: SecretStr = SecretStr("arclight-local-jwt-secret-change-before-deploying")
    access_token_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_days: int = Field(default=30, ge=1, le=90)
    allow_registration: bool = True
    max_request_bytes: int = Field(default=2_000_000, ge=1024, le=100_000_000)
    auth_requests_per_minute: int = Field(default=30, ge=1, le=10000)
    auth_allowed_origins: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_secrets(self) -> "Settings":
        if self.environment == "production":
            if (
                self.jwt_secret.get_secret_value()
                == "arclight-local-jwt-secret-change-before-deploying"
                or len(self.jwt_secret.get_secret_value()) < 32
            ):
                raise ValueError("生产环境必须配置独立的 JWT Secret")
            token = self.api_token.get_secret_value()
            if len(token) < 32 or token == "local-development-token-change-me":
                raise ValueError("生产环境必须设置至少 32 字符的独立 API Token")
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("生产环境必须使用 PostgreSQL")
            if not self.checkpoint_url.startswith("postgresql://"):
                raise ValueError("生产环境必须使用 PostgreSQL checkpoint")
            if self.provider == "mock":
                raise ValueError("生产环境不允许使用 Mock Provider")
        if self.provider == "openai_compatible" and not self.llm_api_key.get_secret_value():
            raise ValueError("真实模型模式必须配置 LLM API Key")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
