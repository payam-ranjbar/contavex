from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from content_fleet.config import ConfigError, FleetConfig, Settings
from content_fleet.providers.registry import ProviderRegistry
from content_fleet.secrets import FileSecretResolver, Redactor, Secret, SecretError
from content_fleet.services import build_services
from tests.conftest import ScriptedProvider, fleet_config, write_secrets

pytestmark = pytest.mark.unit


class TestFileSecrets:
    @pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
    def test_trims_one_trailing_newline(self, tmp_path: Path, ending: str) -> None:
        (tmp_path / "token").write_bytes(b"s3cr3t-value" + ending.encode())
        assert FileSecretResolver(tmp_path).get("token").reveal() == "s3cr3t-value"

    def test_keeps_inner_whitespace(self, tmp_path: Path) -> None:
        (tmp_path / "token").write_bytes(b" a b \n\n")
        assert FileSecretResolver(tmp_path).get("token").reveal() == " a b \n"

    def test_missing_secret_names_it_without_a_value(self, tmp_path: Path) -> None:
        with pytest.raises(SecretError, match="'absent' not found"):
            FileSecretResolver(tmp_path).get("absent")

    def test_empty_secret_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "token").write_text("\n")
        with pytest.raises(SecretError, match="empty"):
            FileSecretResolver(tmp_path).get("token")

    @pytest.mark.parametrize("name", ["../etc/passwd", "a/b", "", ".hidden", "a\\b"])
    def test_rejects_names_that_could_leave_the_directory(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(SecretError, match="invalid secret name"):
            FileSecretResolver(tmp_path).get(name)


class TestSecretType:
    def test_does_not_print_itself(self) -> None:
        secret = Secret("hunter2-hunter2")
        assert "hunter2" not in f"{secret} {secret!r} {[secret]} { {'k': secret} }"
        assert secret.reveal() == "hunter2-hunter2"

    def test_cannot_be_pickled(self) -> None:
        with pytest.raises(TypeError):
            pickle.dumps(Secret("hunter2-hunter2"))


class TestRedactor:
    def test_scrubs_registered_values_anywhere(self) -> None:
        redactor = Redactor()
        redactor.register("abcdefgh12345678")
        scrubbed = redactor.scrub_obj({"a": ["x abcdefgh12345678 y"], "b": Secret("zzz")})
        assert scrubbed == {"a": ["x *** y"], "b": "***"}

    @pytest.mark.parametrize(
        "text",
        [
            "https://api.telegram.org/bot123456789:AAExampleExampleExampleExampleExample/sendMessage",
            "GET /me?access_token=IGQVJabc123&fields=id",
            "Authorization: Bearer abc.def-ghi_jkl",
            "client_secret=shhhhhh",
        ],
    )
    def test_scrubs_credential_shaped_text_never_registered(self, text: str) -> None:
        scrubbed = Redactor().scrub(text)
        assert "***" in scrubbed
        for leaked in ("AAExample", "IGQVJabc123", "abc.def", "shhhhhh"):
            assert leaked not in scrubbed


class TestSettings:
    def test_reads_cf_prefixed_environment(self) -> None:
        settings = Settings.from_env({"CF_PORT": "9000", "CF_DATA_DIR": "/x", "PORT": "1"})
        assert settings.port == 9000
        assert settings.db_path == Path("/x/content_fleet.db")

    def test_rejects_bad_values(self) -> None:
        with pytest.raises(ConfigError):
            Settings.from_env({"CF_PORT": "not-a-port"})


class TestFleetConfig:
    def test_loads_accounts_and_consumers(self, tmp_path: Path) -> None:
        path = tmp_path / "fleet.toml"
        path.write_text(
            """
            [consumers.app]
            api_key = "consumer_key"

            [accounts.news]
            platform = "telegram"
            implementation = "bot-api"
            credentials = { token = "telegram_news_bot_token" }
            settings = { chat_id = "-1001" }
            """
        )
        config = FleetConfig.load(path)
        assert config.accounts["news"].credentials == {"token": "telegram_news_bot_token"}
        assert config.accounts["news"].settings == {"chat_id": "-1001"}

    @pytest.mark.parametrize(
        "raw",
        [
            {"consumers": {}},
            {"consumers": {"a": {"api_key": "k"}}, "typo": {}},
            {"consumers": {"a": {"api_key": "k"}}, "accounts": {"x": {"platform": "t"}}},
            {"consumers": {"a": {"api_key": "k", "token": "inline-secret"}}},
            {"consumers": {"Bad Id": {"api_key": "k"}}},
        ],
    )
    def test_rejects_invalid_config(self, raw: dict[str, object]) -> None:
        with pytest.raises(ConfigError):
            FleetConfig.parse(raw)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            FleetConfig.load(tmp_path / "nope.toml")


class TestStartupFailsFast:
    def _settings(self, tmp_path: Path) -> Settings:
        return Settings(data_dir=tmp_path / "data", secrets_dir=tmp_path / "secrets")

    def test_missing_account_secret(self, tmp_path: Path) -> None:
        write_secrets(tmp_path / "secrets", consumer_key="k" * 16, other_key="o" * 16)
        with pytest.raises(SecretError, match="fake_token"):
            build_services(
                self._settings(tmp_path),
                config=fleet_config(),
                registry=ProviderRegistry([ScriptedProvider()]),
            )

    def test_account_without_a_provider(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="no provider for platform 'fake'"):
            build_services(
                self._settings(tmp_path), config=fleet_config(), registry=ProviderRegistry()
            )

    def test_account_settings_rejected_by_provider(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"settings.broken"):
            build_services(
                self._settings(tmp_path),
                config=fleet_config(broken=True),
                registry=ProviderRegistry([ScriptedProvider()]),
            )

    def test_duplicate_provider_registration(self) -> None:
        with pytest.raises(ValueError, match="already registered"):
            ProviderRegistry([ScriptedProvider(), ScriptedProvider()])
