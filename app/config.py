from __future__ import annotations

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Web-Server
    web_host: str = "0.0.0.0"
    web_port: int = 3000
    public: bool = Field(default=True, validation_alias=AliasChoices("SC_PUBLIC"))

    # Auth
    session_max_age_hours: int = 8
    login_rate_limit: int = 5
    login_rate_limit_window_minutes: int = 15
    lockout_threshold: int = 10
    lockout_duration_minutes: int = 30
    min_password_length: int = 12

    # WebAuthn
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "sentinelCam"

    # Recordings
    max_upload_size_mb: int = 100
    max_recording_duration_minutes: int = 5
    storage_quota_per_user_mb: int = 500

    # Initial Admin
    initial_admin_user: str = Field(default="", validation_alias=AliasChoices("INITIAL_ADMIN_USER", "ADMIN_USER"))
    initial_admin_password: str = Field(default="", validation_alias=AliasChoices("INITIAL_ADMIN_PASSWORD", "ADMIN_PASSWORD"))

    # PostgreSQL
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "sentinelcam"
    postgres_user: str = "sentinelcam"
    postgres_password: str = ""
    postgres_min_pool: int = 2
    postgres_max_pool: int = 10

    # Local recording storage
    local_storage_path: str = Field(
        default="data/recordings",
        validation_alias=AliasChoices("LOCAL_STORAGE_PATH", "STORAGE_PATH"),
    )

    @field_validator(
        "webauthn_rp_id",
        "webauthn_rp_name",
        "initial_admin_user",
        "initial_admin_password",
        "postgres_host",
        "postgres_db",
        "postgres_user",
        "postgres_password",
        "local_storage_path",
        mode="before",
    )
    @classmethod
    def strip_string_values(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @property
    def postgres_dsn(self) -> str:
        from urllib.parse import quote_plus

        user = quote_plus(self.postgres_user)
        password = quote_plus(self.postgres_password)
        return (
            f"postgresql://{user}:{password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    model_config = {"env_file": ".env", "extra": "ignore", "validate_assignment": True}


settings = Settings()
