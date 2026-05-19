from __future__ import annotations

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Worker-Proxy
    worker_base_url: str = "http://127.0.0.1:8080"
    worker_token: str = ""

    # Web-Server
    web_host: str = "127.0.0.1"
    web_port: int = 3000
    public: bool = Field(default=False, validation_alias=AliasChoices("SC_PUBLIC"))

    # Auth
    session_max_age_hours: int = 8
    login_rate_limit: int = 5
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

    # Paths
    database_path: str = "data/sentinelcam.db"
    recordings_path: str = "data/recordings"

    @field_validator(
        "worker_base_url",
        "worker_token",
        "webauthn_rp_id",
        "webauthn_rp_name",
        "initial_admin_user",
        "initial_admin_password",
        "database_path",
        "recordings_path",
        mode="before",
    )
    @classmethod
    def strip_string_values(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    model_config = {"env_file": ".env", "extra": "ignore", "validate_assignment": True}


settings = Settings()
