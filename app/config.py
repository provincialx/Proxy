"""Application configuration via pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from .env or environment variables."""

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "Proxy"
    db_user: str = "ProxyUser"
    db_password: str = "1"

    app_host: str = "0.0.0.0"
    app_port: int = 8100
    debug: bool = False

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
