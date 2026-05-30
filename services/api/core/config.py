from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    store_id: str = "store_001"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    db_path: str = "/data/store_intelligence.db"
    log_level: str = "INFO"
    stale_feed_threshold_minutes: int = 10

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
