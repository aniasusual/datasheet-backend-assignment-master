from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/datasheet_extraction"

    # LLM (via litellm — supports gemini/, openai/, anthropic/ prefixes)
    LLM_MODEL: str = "gemini/gemini-2.5-flash"
    LLM_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    # File storage
    UPLOAD_DIR: Path = Path("./uploads")

    # Accepted file types
    ACCEPTED_EXTENSIONS: set[str] = {".pdf"}

    @property
    def sync_database_url(self) -> str:
        """Sync URL for Alembic migrations."""
        return self.DATABASE_URL.replace("+asyncpg", "")


settings = Settings()
