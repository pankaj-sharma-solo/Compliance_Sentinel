from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True,)

    # Database
    mysql_url: str = Field(..., alias="MYSQL_URL")  # e.g. mysql+pymysql://user:pass@host/dbname

    # Qdrant
    qdrant_url: str = Field("http://localhost:6333", alias="QDRANT_URL")
    qdrant_collection: str = Field("compliance_rules", alias="QDRANT_COLLECTION")

    # LLM
    openai_api_key: str | None = Field(None, alias="OPENAI_API_KEY")
    google_api_key: str | None = Field(None, alias="GOOGLE_API_KEY")

    # Models — two-pass cost control
    cheap_model: str = Field("gemini-2.5-flash", alias="CHEAP_MODEL")       # Pass-1: candidate extraction
    strong_model: str = Field("gemini-2.5-pro", alias="STRONG_MODEL")          # Pass-2: structured decomposition

    # Similarity thresholds for version reconciliation
    similarity_high: float = Field(0.92, alias="SIMILARITY_HIGH")       # Same rule, reworded → human review
    similarity_mid: float = Field(0.75, alias="SIMILARITY_MID")         # Evolved rule → supersede old

    # Scan settings
    default_scan_cron: str = Field("0 2 * * *", alias="DEFAULT_SCAN_CRON")  # 2 AM daily
    max_relevant_rules_per_table: int = Field(12, alias="MAX_RELEVANT_RULES")

    # Enforcement fallback
    llm_fallback_confidence_threshold: float = Field(0.6, alias="LLM_FALLBACK_THRESHOLD")

    # App
    app_host: str = Field("0.0.0.0", alias="APP_HOST")
    app_port: int = Field(8000, alias="APP_PORT")
    debug: bool = Field(False, alias="DEBUG")


settings = Settings()
