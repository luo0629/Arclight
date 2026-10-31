from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

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
    service_encryption_key: SecretStr = SecretStr("")
    model_service_allowed_hosts: list[str] = Field(default_factory=lambda: ["api.openai.com"])
    text_duration_tolerance_percent: int = Field(default=10, ge=0, le=100)
    max_story_chars: int = 20000
    jwt_secret: SecretStr = SecretStr("arclight-local-jwt-secret-change-before-deploying")
    access_token_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_days: int = Field(default=30, ge=1, le=90)
    allow_registration: bool = True
    max_request_bytes: int = Field(default=2_000_000, ge=1024, le=100_000_000)
    asset_storage_path: Path | None = None
    object_storage_backend: Literal["local", "s3"] = "local"
    s3_endpoint_url: str | None = None
    s3_public_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = Field(default="arclight-media", pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    file_gateway_secret: SecretStr = SecretStr("local-file-gateway-change-before-deploying")
    file_transfer_base_url: str = "/file-transfers"
    auth_requests_per_minute: int = Field(default=30, ge=1, le=10000)
    auth_allowed_origins: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_storage(self) -> "Settings":
        transfer = urlsplit(self.file_transfer_base_url)
        if self.file_transfer_base_url != "/file-transfers" and (
            transfer.scheme not in {"http", "https"}
            or not transfer.hostname
            or transfer.username
            or transfer.password
            or transfer.query
            or transfer.fragment
            or transfer.path != "/file-transfers"
            or (self.environment == "production" and transfer.scheme != "https")
        ):
            raise ValueError("文件网关必须是同源路径或独立 HTTP(S) /file-transfers 地址")
        if self.object_storage_backend == "s3" and (
            not self.s3_endpoint_url
            or not self.s3_access_key.get_secret_value()
            or not self.s3_secret_key.get_secret_value()
        ):
            raise ValueError("S3 必须显式配置地址和独立凭证")
        for endpoint in (self.s3_endpoint_url, self.s3_public_endpoint_url):
            if endpoint is None:
                continue
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("S3 地址必须是无凭证、参数或路径的 HTTP(S) 地址")
            if self.environment == "production" and parsed.scheme != "https":
                raise ValueError("生产环境 S3 必须使用 HTTPS")
        return self

    @model_validator(mode="after")
    def validate_secrets(self) -> "Settings":
        if self.environment == "production":
            gateway_secret = self.file_gateway_secret.get_secret_value()
            if (
                len(gateway_secret) < 32
                or gateway_secret == "local-file-gateway-change-before-deploying"
                or gateway_secret == self.jwt_secret.get_secret_value()
            ):
                raise ValueError("生产环境必须配置独立的文件网关密钥")
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
