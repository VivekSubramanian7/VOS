"""Configuration, resolved from environment / .env.

Secrets live here and nowhere else. Nothing in this module is ever written to the
journal, the cassettes, or a log line.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram -------------------------------------------------------
    telegram_bot_token: SecretStr = Field(alias="TELEGRAM_BOT_TOKEN")

    # A bot token is effectively public: anyone who learns the bot's username can
    # message it. Without this allowlist, a stranger could write into your graph
    # and spend your model budget. Every update from another user is dropped.
    vos_allowed_user_id: int = Field(alias="VOS_ALLOWED_USER_ID")

    # --- Model ----------------------------------------------------------
    # "provider:model", resolved by langchain init_chat_model().
    vos_model: str = Field(default="anthropic:claude-opus-5", alias="VOS_MODEL")
    vos_daily_budget_usd: float = Field(default=2.00, alias="VOS_DAILY_BUDGET_USD")

    # --- Neo4j ----------------------------------------------------------
    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: SecretStr = Field(alias="NEO4J_PASSWORD")

    # --- Storage --------------------------------------------------------
    vos_journal_dir: Path = Field(default=Path("./journal"), alias="VOS_JOURNAL_DIR")
    vos_cassette_dir: Path = Field(default=Path("./cassettes"), alias="VOS_CASSETTE_DIR")

    # Fetched transcripts. Like the journal and unlike everything else derived, these
    # cannot be recomputed — the upstream video can be deleted or lose its captions.
    # Back this up.
    vos_artifact_dir: Path = Field(default=Path("./artifacts"), alias="VOS_ARTIFACT_DIR")

    # --- X pulse (optional) ---------------------------------------------
    # Absent key means /pulse is off; nothing else changes. Live Search bills
    # $0.025 per source on top of tokens, so `max_sources` is the cost lever:
    # 25 sources is roughly $0.63 per digest against the daily budget above.
    xai_api_key: SecretStr | None = Field(default=None, alias="XAI_API_KEY")
    vos_pulse_model: str = Field(default="grok-4.1-fast", alias="VOS_PULSE_MODEL")
    vos_pulse_max_sources: int = Field(default=25, alias="VOS_PULSE_MAX_SOURCES")
    vos_pulse_topic: str = Field(default="AI", alias="VOS_PULSE_TOPIC")
    vos_xai_base_url: str = Field(default="https://api.x.ai/v1", alias="VOS_XAI_BASE_URL")


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton.

    Not module-level so that importing `vos.settings` never requires a populated
    environment — tests construct `Settings(...)` directly.
    """
    global _settings
    if _settings is None:
        # Provider SDKs read their key from the real environment (ANTHROPIC_API_KEY,
        # GOOGLE_API_KEY, OPENAI_API_KEY, ...), and BaseSettings reads .env only into
        # its own declared fields. Without this, a key sitting in .env never reaches
        # init_chat_model() and every classification fails as an auth error while
        # capture keeps working — the confusing shape, because §4.2 correctly degrades
        # it to /pending rather than surfacing it loudly.
        #
        # Deliberately provider-agnostic: VOS never names a key, so a new provider
        # needs no code change. `override=False` keeps a real shell variable winning
        # over .env, which is what a VPS or CI run expects.
        load_dotenv(".env", override=False)
        _settings = Settings()  # type: ignore[call-arg]  # values come from env
    return _settings
