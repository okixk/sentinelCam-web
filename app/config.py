from __future__ import annotations
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Worker-Proxy
    worker_base_url: str = "http://127.0.0.1:8080"
    worker_token: str = ""

    # Web-Server
    web_host: str = "127.0.0.1"
    web_port: int = 3000
    public: bool = False

    # Auth
    secret_key: str = ""  # auto-generated if empty, but should be set in production
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
    initial_admin_user: str = ""
    initial_admin_password: str = ""

    # Paths
    database_path: str = "data/sentinelcam.db"
    recordings_path: str = "data/recordings"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
