"""pydantic-settings: .env -> Settings (endpoint, one model, langs, paths)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration loaded from .env.

    Endpoint fields default to None so store/worklist phases run without any
    LLM credentials; agent phases assert them explicitly at use time. Paths
    anchor at the install root so the CLI behaves identically from any CWD
    (the project is expected to be installed editable, so the package root IS
    the repository root).
    """

    model_config = SettingsConfigDict(env_file=_PACKAGE_ROOT / ".env", extra="ignore")

    litellm_base_url: str | None = None
    litellm_api_key: str | None = None
    litellm_model: str | None = None

    known_lang: str = "cs"
    target_lang: str = "pl"
    tts_voice: str = "pl-PL-ZofiaNeural"
    tts_rate: str = "-25%"
    chat_self_name: str = ""

    data_dir: Path = Field(default_factory=lambda: _PACKAGE_ROOT / "var")
    worklist_scan_forms: int = 20000

    # words identical in both languages are auto-skipped before generation
    # (false-friends guard still applies); off by default
    skip_free_cognates: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "parlance.db"

    @property
    def wordlist_path(self) -> Path:
        return self.data_dir / "wordlist" / f"{self.target_lang}_50k.txt"


@lru_cache
def get_settings() -> Settings:
    return Settings()
