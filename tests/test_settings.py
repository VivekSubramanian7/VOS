"""Settings, and specifically the .env -> environment hand-off.

This is worth a test because the failure it guards against is silent and
misdirecting: a provider key sitting in .env that never reaches the provider SDK
produces an auth error inside the classifier, which §4.2 correctly degrades to
`status=unclassified` in /pending. The symptom therefore looks like "the model
provider is having a bad day" rather than "the key was never loaded at all".
"""

from __future__ import annotations

import os

import pytest

import vos.settings as settings_mod
from vos.settings import Settings, get_settings

ENV = """\
TELEGRAM_BOT_TOKEN=telegram-token
VOS_ALLOWED_USER_ID=4242
VOS_MODEL=google_genai:gemini-3.6-flash
NEO4J_PASSWORD=neo4j-password
GOOGLE_API_KEY=google-key
ANTHROPIC_API_KEY=anthropic-key
"""


@pytest.fixture
def env_dir(tmp_path, monkeypatch):
    """A cwd holding a .env, with the settings singleton and key vars cleared.

    load_dotenv mutates os.environ for the life of the process, and monkeypatch
    cannot undo a variable it did not set — so the environment is snapshotted and
    restored wholesale rather than key by key.
    """
    (tmp_path / ".env").write_text(ENV, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings_mod, "_settings", None)

    saved = os.environ.copy()
    for key in ("GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "VOS_MODEL"):
        os.environ.pop(key, None)
    try:
        yield tmp_path
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_provider_keys_reach_the_environment(env_dir):
    """The whole point: provider SDKs read os.environ, not our Settings object."""
    assert "GOOGLE_API_KEY" not in os.environ

    get_settings()

    # init_chat_model("google_genai:...") can only find its key here.
    assert os.environ["GOOGLE_API_KEY"] == "google-key"
    assert os.environ["ANTHROPIC_API_KEY"] == "anthropic-key"


def test_declared_fields_still_load(env_dir):
    s = get_settings()
    assert s.vos_model == "google_genai:gemini-3.6-flash"
    assert s.vos_allowed_user_id == 4242
    assert s.neo4j_password.get_secret_value() == "neo4j-password"


def test_real_environment_wins_over_dotenv(env_dir, monkeypatch):
    """A VPS or CI run sets real variables; .env must not clobber them."""
    monkeypatch.setenv("GOOGLE_API_KEY", "from-the-real-environment")

    get_settings()

    assert os.environ["GOOGLE_API_KEY"] == "from-the-real-environment"


def test_secrets_do_not_leak_through_repr(env_dir):
    """Nothing may print a secret — §9.3. SecretStr is what enforces it."""
    s = get_settings()
    blob = f"{s!r} {s.neo4j_password} {s.telegram_bot_token}"
    assert "neo4j-password" not in blob
    assert "telegram-token" not in blob


def test_settings_constructible_without_an_env_file(tmp_path, monkeypatch):
    """Importing vos.settings must never require a populated environment."""
    monkeypatch.chdir(tmp_path)
    s = Settings(
        TELEGRAM_BOT_TOKEN="t",
        VOS_ALLOWED_USER_ID=1,
        NEO4J_PASSWORD="p",
    )
    assert s.vos_model == "anthropic:claude-opus-5"
