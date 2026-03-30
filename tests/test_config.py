import tomllib
from unittest.mock import patch

from control_center.config import Settings, _ensure_config


class TestSettingsSaveRoundTrip:
    def test_save_and_reload(self, tmp_path):
        config_file = tmp_path / "config.toml"

        with patch("control_center.config.CONFIG_DIR", tmp_path), patch(
            "control_center.config.CONFIG_FILE", config_file
        ):
            # Create settings with non-default values
            settings = Settings(
                github_username="testuser",
                default_org="myorg",
                theme="light",
                poll_interval_seconds=120,
                host="127.0.0.1",
                port=9000,
                autofix_enabled=True,
                autofix_max_budget_usd=5.0,
                autofix_max_turns=50,
                autofix_cooldown_minutes=30,
                autofix_model="opus",
                repos_base_dir="/tmp/repos",
            )
            settings.save()

            # Verify file is valid TOML
            with open(config_file, "rb") as f:
                data = tomllib.load(f)

            assert data["github"]["username"] == "testuser"
            assert data["github"]["default_org"] == "myorg"
            assert data["ui"]["theme"] == "light"
            assert data["server"]["poll_interval_seconds"] == 120
            assert data["server"]["host"] == "127.0.0.1"
            assert data["server"]["port"] == 9000
            assert data["autofix"]["enabled"] is True
            assert data["autofix"]["max_budget_usd"] == 5.0
            assert data["autofix"]["max_turns"] == 50
            assert data["autofix"]["cooldown_minutes"] == 30
            assert data["autofix"]["model"] == "opus"
            assert data["autofix"]["repos_base_dir"] == "/tmp/repos"

    def test_save_escapes_special_chars(self, tmp_path):
        config_file = tmp_path / "config.toml"

        with patch("control_center.config.CONFIG_DIR", tmp_path), patch(
            "control_center.config.CONFIG_FILE", config_file
        ):
            settings = Settings(github_username='user"with"quotes')
            settings.save()

            with open(config_file, "rb") as f:
                data = tomllib.load(f)

            assert data["github"]["username"] == 'user"with"quotes'


class TestEnsureConfig:
    def test_creates_default_config(self, tmp_path):
        config_dir = tmp_path / ".control-center"
        config_file = config_dir / "config.toml"

        with patch("control_center.config.CONFIG_DIR", config_dir), patch(
            "control_center.config.CONFIG_FILE", config_file
        ), patch("control_center.config._detect_github_username", return_value="detected_user"):
            data = _ensure_config()

        assert config_file.exists()
        assert data["github"]["username"] == "detected_user"

    def test_loads_existing_config(self, tmp_path):
        config_dir = tmp_path
        config_file = config_dir / "config.toml"
        config_file.write_text(
            '[github]\nusername = "existing"\n[ui]\ntheme = "dark"\n[server]\n[autofix]\n'
        )

        with patch("control_center.config.CONFIG_DIR", config_dir), patch(
            "control_center.config.CONFIG_FILE", config_file
        ):
            data = _ensure_config()

        assert data["github"]["username"] == "existing"
