from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APISWITCH_", env_file=".env", extra="ignore")

    app_name: str = "APISwitch for DeepSeek Harness"
    plugin_mode: bool = True
    listen_host: str = "127.0.0.1"
    port: int = 8080
    database_url: str = "sqlite:///./apiswitch.db"
    auth_enabled: bool = False
    admin_auth_enabled: bool = False
    admin_token: str | None = None
    master_key: str | None = None
    file_storage_dir: str = "./apiswitch-files"
    harness_token_file: str = "~/.apiswitch-deepseek-harness/harness.token"
    file_max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    frontend_dist_dir: str | None = None
    reload: bool = False
    stream_failure_mode: str = Field(default="strict_compat_mode")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
