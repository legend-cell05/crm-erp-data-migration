"""Configuration.

Every setting is an environment variable prefixed ``KEYSTONE_``, read from
``.env`` when present. Validation happens once, at start-up: a schema name
that could carry SQL, a batch size above what the target accepts, or a
rejection ceiling outside 0-100 fails here rather than halfway through writing
to the target.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _detect_project_root() -> Path:
    """Walk up from this file until a directory holding pyproject.toml.

    Falls back to the current working directory, which is what happens inside
    the container where the package is installed rather than checked out.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd()


class Settings(BaseSettings):
    """Runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="KEYSTONE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ----------------------------------------------------------
    db_host: str = "localhost"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "keystone"
    db_user: str = "keystone_app"
    db_password: SecretStr = SecretStr("change_me_local_only")

    legacy_schema: str = "legacy"
    migration_schema: str = "migration"

    # --- Target ------------------------------------------------------------
    target_base_url: str = "http://localhost:8080"
    target_api_key: SecretStr = SecretStr("local_dev_key_not_a_secret")
    target_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    batch_size: int = Field(default=200, ge=1, le=1000)

    retry_max_attempts: int = Field(default=5, ge=1, le=20)
    retry_base_delay_seconds: float = Field(default=0.5, gt=0, le=60)
    retry_max_delay_seconds: float = Field(default=30.0, gt=0, le=600)

    target_rate_limit_rate: float = Field(default=0.06, ge=0.0, le=1.0)
    target_fault_rate: float = Field(default=0.05, ge=0.0, le=1.0)

    # --- Migration policy --------------------------------------------------
    max_reject_rate_pct: float = Field(default=5.0, ge=0.0, le=100.0)
    max_retry_attempts: int = Field(default=3, ge=1, le=10)

    # --- Legacy generation -------------------------------------------------
    random_seed: int = 20260220
    n_accounts: int = Field(default=1200, ge=1, le=200_000)
    n_contacts: int = Field(default=4000, ge=1, le=500_000)
    n_opportunities: int = Field(default=2600, ge=0, le=500_000)
    n_activities: int = Field(default=9000, ge=0, le=1_000_000)
    duplicate_rate: float = Field(default=0.06, ge=0.0, le=0.5)
    defect_rate: float = Field(default=0.18, ge=0.0, le=1.0)

    # --- Paths -------------------------------------------------------------
    project_root: Path = Field(default_factory=_detect_project_root)
    data_dir: Path = Path("data")
    mapping_dir: Path = Path("mappings")

    # --- Runtime -----------------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "text"

    # --- Validation --------------------------------------------------------

    @field_validator("legacy_schema", "migration_schema")
    @classmethod
    def _valid_schema_name(cls, value: str) -> str:
        """Schema names end up in DDL, where they cannot be bound parameters.

        So they are checked against an allow-list pattern here, before a
        connection is ever opened. ``KEYSTONE_LEGACY_SCHEMA='x; DROP SCHEMA
        migration CASCADE'`` fails at start-up with a validation error.
        """
        if not _IDENTIFIER.match(value):
            raise ValueError(f"schema name {value!r} must match [a-z_][a-z0-9_]*")
        return value

    @field_validator("target_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log level must be one of {sorted(allowed)}")
        return upper

    @model_validator(mode="after")
    def _coherent(self) -> Settings:
        if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            raise ValueError("retry_max_delay_seconds must be >= retry_base_delay_seconds")
        if not self.data_dir.is_absolute():
            object.__setattr__(self, "data_dir", self.project_root / self.data_dir)
        if not self.mapping_dir.is_absolute():
            object.__setattr__(self, "mapping_dir", self.project_root / self.mapping_dir)
        return self

    # --- Derived -----------------------------------------------------------

    @property
    def dsn(self) -> str:
        """SQLAlchemy URL including the password -- never log this."""
        pwd = self.db_password.get_secret_value()
        return f"postgresql+psycopg://{self.db_user}:{pwd}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def safe_dsn(self) -> str:
        """Connection string with the password masked -- safe to log."""
        return (
            f"postgresql+psycopg://{self.db_user}:***@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "legacy_exports"

    @property
    def report_dir(self) -> Path:
        return self.data_dir / "reports"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once."""
    return Settings()
