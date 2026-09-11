"""Configuration management using Pydantic Settings."""

from enum import Enum
from pathlib import Path
from typing import List
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ResourceMode(str, Enum):
    AUTO_SHARED = "auto-shared"
    AUTO_DEDICATED = "auto-dedicated"
    MANUAL = "manual"


class SendMode(str, Enum):
    VIDEO = "video"
    DOCUMENT = "document"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # Telegram Bot Configuration
    bot_token: str = Field(..., description="Telegram Bot Token")
    admin_user_ids: List[int] = Field(
        default_factory=list,
        description="List of Telegram User IDs with admin privileges"
    )

    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if isinstance(v, str):
            if not v.strip():
                return []
            return [int(uid.strip()) for uid in v.split(",") if uid.strip()]
        elif isinstance(v, (int, float)):
            return [int(v)]
        return v

    # Resource Governor Configuration
    resource_mode: ResourceMode = Field(default=ResourceMode.AUTO_SHARED)
    memory_safety_headroom: float = Field(default=0.15, ge=0.0, le=0.5)
    disk_safety_headroom_gb: float = Field(default=0.0, ge=0.0)
    disk_safety_headroom_fraction: float = Field(default=0.08, ge=0.01, le=0.5)
    cpu_safety_headroom: float = Field(default=0.1, ge=0.0, le=0.5)
    max_concurrent_extractions: int = Field(default=0, ge=0)
    max_concurrent_downloads: int = Field(default=0, ge=0)
    max_concurrent_merges: int = Field(default=0, ge=0)
    max_concurrent_uploads: int = Field(default=0, ge=0)
    bandwidth_ceiling: int = Field(default=0, ge=0)
    max_queued_jobs_per_user: int = Field(default=3, ge=1, le=50)
    max_total_queued_jobs: int = Field(default=50, ge=1, le=1000)

    # Storage Configuration
    data_dir: Path = Field(default=Path("./data"))
    jobs_dir: Path = Field(default=Path("./data/jobs"))
    database_path: Path = Field(default=Path("./data/bot.db"))
    max_job_size_bytes: int = Field(default=0, ge=0)
    scheduler_max_workers: int = Field(default=0, ge=0, le=64)
    scheduler_retry_seconds: float = Field(default=10.0, ge=1.0)
    process_terminate_grace_seconds: float = Field(default=5.0, ge=0.1, le=60.0)

    # Extraction Configuration
    ytdlp_timeout: int = Field(default=60, ge=10)
    media_session_ttl: int = Field(default=1800, ge=60)
    max_collection_items: int = Field(default=50, ge=1, le=200)
    max_batch_urls: int = Field(default=10, ge=1, le=25)

    # Download Configuration
    download_timeout: int = Field(default=3600, ge=60)
    max_retries: int = Field(default=3, ge=0)
    resume_enabled: bool = Field(default=True)

    # Telegram Delivery Configuration
    use_local_api: bool = Field(default=False)
    local_api_base_url: str = Field(default="http://localhost:8081")
    local_api_max_file_size_mb: int = Field(default=2000)
    default_send_mode: SendMode = Field(default=SendMode.DOCUMENT)

    # Logging Configuration
    log_level: str = Field(default="INFO")
    log_max_bytes: int = Field(default=10485760)
    log_backup_count: int = Field(default=5)
    log_dir: Path = Field(default=Path("./logs"))

    # Security Configuration
    max_redirects: int = Field(default=10, ge=1, le=30)

    def ensure_directories(self) -> None:
        """Create necessary directories if they do not exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


# Lazy singleton pattern to avoid immediate instantiation during tests
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
        _settings.ensure_directories()
    return _settings
