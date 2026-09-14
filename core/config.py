"""Configuration management using Pydantic Settings."""

from enum import Enum
import json
import re

from pathlib import Path
from typing import Annotated, List, Optional
from pydantic import Field, field_validator, model_validator
from urllib.parse import urlsplit
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    admin_user_ids: Annotated[List[int], NoDecode] = Field(
        default_factory=list,
        description="List of Telegram User IDs with admin privileges"
    )

    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                try:
                    loaded = json.loads(v)
                    if isinstance(loaded, list):
                        return [int(uid) for uid in loaded if str(uid).strip()]
                except Exception:
                    pass
            return [int(uid.strip()) for uid in v.split(",") if uid.strip()]
        elif isinstance(v, (int, float)):
            return [int(v)]
        elif isinstance(v, (list, tuple, set)):
            return [int(uid) for uid in v if str(uid).strip()]
        return v

    # Resource Governor Configuration
    resource_mode: ResourceMode = Field(default=ResourceMode.AUTO_SHARED)
    memory_safety_headroom: float = Field(default=0.15, ge=0.0, le=0.5)
    disk_safety_headroom_gb: float = Field(default=0.0, ge=0.0)
    disk_safety_headroom_fraction: float = Field(default=0.08, ge=0.01, le=0.5)
    cpu_safety_headroom: float = Field(default=0.1, ge=0.0, le=0.5)
    # Operational bootstrap defaults. SQLite runtime overrides become
    # authoritative after an administrator changes them through Telegram.
    max_concurrent_extractions: int = Field(default=2, ge=0, le=4)
    max_concurrent_downloads: int = Field(default=2, ge=0, le=4)
    max_concurrent_merges: int = Field(default=1, ge=0, le=2)
    max_concurrent_uploads: int = Field(default=1, ge=0, le=3)
    bandwidth_ceiling: int = Field(default=0, ge=0)
    max_queued_jobs_per_user: int = Field(default=6, ge=1, le=50)
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
    ytdlp_socket_timeout: int = Field(default=15, ge=3, le=120)
    ytdlp_extractor_retries: int = Field(default=2, ge=0, le=10)
    ytdlp_impersonation_fallback: bool = Field(default=True)
    ytdlp_impersonate_target: str = Field(default="chrome", min_length=1, max_length=40)
    ytdlp_cookies_file: Optional[Path] = Field(default=None)
    ytdlp_cookie_domains: Annotated[List[str], NoDecode] = Field(default_factory=list)
    ytdlp_profiles_file: Optional[Path] = Field(default=None)
    max_collection_depth: int = Field(default=3, ge=1, le=8)
    maintenance_interval_seconds: int = Field(default=900, ge=60)
    orphan_grace_hours: int = Field(default=24, ge=1)
    job_history_days: int = Field(default=30, ge=1)
    cache_retention_days: int = Field(default=90, ge=1)
    ui_draft_ttl_hours: int = Field(default=24, ge=1)
    backup_retention_count: int = Field(default=5, ge=1, le=50)
    media_session_ttl: int = Field(default=1800, ge=60)
    max_collection_items: int = Field(default=50, ge=1, le=200)
    max_batch_urls: int = Field(default=4, ge=1, le=10)

    @field_validator("ytdlp_cookies_file", "ytdlp_profiles_file", mode="before")
    @classmethod
    def parse_optional_cookie_path(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("ytdlp_cookie_domains", mode="before")
    @classmethod
    def parse_cookie_domains(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            if value.startswith("[") and value.endswith("]"):
                try:
                    loaded = json.loads(value)
                except json.JSONDecodeError as error:
                    raise ValueError("Invalid JSON list in YTDLP_COOKIE_DOMAINS") from error
                if not isinstance(loaded, list):
                    raise ValueError("YTDLP_COOKIE_DOMAINS JSON value must be a list")
                value = loaded
            else:
                value = value.split(",")
        if isinstance(value, (list, tuple, set)):
            parts = [str(p).strip().lower().rstrip(".") for p in value if str(p).strip()]
        else:
            return []

        domain_re = re.compile(
            r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
        )
        seen = set()
        domains = []
        for d in parts:
            if not domain_re.match(d):
                raise ValueError(f"Invalid domain in YTDLP_COOKIE_DOMAINS: {d}")
            if d not in seen:
                seen.add(d)
                domains.append(d)
        return domains


    @field_validator("ytdlp_impersonate_target")
    @classmethod
    def validate_impersonate_target(cls, value: str) -> str:
        if not all(character.isalnum() or character in {"-", "_"} for character in value):
            raise ValueError("YTDLP_IMPERSONATE_TARGET contains unsupported characters")
        return value

    # Download Configuration
    download_timeout: int = Field(default=3600, ge=60)
    max_retries: int = Field(default=3, ge=0, le=10)
    resume_enabled: bool = Field(default=True)

    # Telegram Delivery Configuration
    use_local_api: bool = Field(default=False)
    local_api_base_url: str = Field(default="http://localhost:8081")
    local_api_max_file_size_mb: int = Field(default=2000, ge=1)
    default_send_mode: SendMode = Field(default=SendMode.DOCUMENT)

    # Logging Configuration
    log_level: str = Field(default="INFO")
    log_max_bytes: int = Field(default=10485760, ge=1024)
    log_backup_count: int = Field(default=5, ge=1, le=50)
    log_dir: Path = Field(default=Path("./logs"))

    # Security Configuration
    max_redirects: int = Field(default=10, ge=1, le=30)

    @field_validator("local_api_base_url")
    @classmethod
    def validate_local_api(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("LOCAL_API_BASE_URL must be an HTTP(S) endpoint without credentials, query or fragment")
        return value.rstrip("/")

    def ensure_directories(self) -> None:
        """Create necessary directories if they do not exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @model_validator(mode="after")
    def validate_runtime_layout(self):
        jobs = self.jobs_dir.resolve()
        protected = [self.database_path, self.log_dir, self.data_dir / "backups"]
        protected.extend(p for p in (self.ytdlp_cookies_file, self.ytdlp_profiles_file) if p)
        if any(path.resolve().is_relative_to(jobs) for path in protected):
            raise ValueError("Job storage must not contain the database, logs, backups or session secrets")
        if self.ytdlp_cookies_file and not self.ytdlp_cookie_domains:
            raise ValueError(
                "YTDLP_COOKIES_FILE is configured but YTDLP_COOKIE_DOMAINS is empty. "
                "Please set YTDLP_COOKIE_DOMAINS=youtube.com (or comma-separated domains)."
            )

        return self


# Lazy singleton pattern to avoid immediate instantiation during tests
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
        _settings.ensure_directories()
    return _settings
