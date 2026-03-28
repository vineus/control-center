import logging
import subprocess
import tomllib
from pathlib import Path

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".control-center"
CONFIG_FILE = CONFIG_DIR / "config.toml"

DEFAULT_CONFIG = """\
# Control Center configuration

[github]
# Your GitHub username (auto-detected from `gh` CLI if empty)
username = "{username}"

[server]
host = "0.0.0.0"
port = 8000
poll_interval_seconds = 180

[autofix]
enabled = false
max_budget_usd = 2.0
max_turns = 30
cooldown_minutes = 60
model = "sonnet"
repos_base_dir = "~/.control-center/repos"
"""


def _detect_github_username() -> str:
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _ensure_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_FILE.exists():
        username = _detect_github_username()
        logger.info("Creating default config at %s (detected username: %s)", CONFIG_FILE, username or "<none>")
        CONFIG_FILE.write_text(DEFAULT_CONFIG.format(username=username))

    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


class Settings(BaseSettings):
    github_username: str = ""
    poll_interval_seconds: int = 180
    host: str = "0.0.0.0"
    port: int = 8000

    # Auto-fix agent
    autofix_enabled: bool = False
    autofix_max_budget_usd: float = 2.0
    autofix_max_turns: int = 30
    autofix_cooldown_minutes: int = 60
    autofix_model: str = "sonnet"
    repos_base_dir: str = "~/.control-center/repos"

    model_config = {"env_prefix": "CC_"}

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from config file, with env var overrides."""
        toml = _ensure_config()

        gh = toml.get("github", {})
        srv = toml.get("server", {})
        af = toml.get("autofix", {})

        file_values = {
            "github_username": gh.get("username", ""),
            "poll_interval_seconds": srv.get("poll_interval_seconds", 180),
            "host": srv.get("host", "0.0.0.0"),
            "port": srv.get("port", 8000),
            "autofix_enabled": af.get("enabled", False),
            "autofix_max_budget_usd": af.get("max_budget_usd", 2.0),
            "autofix_max_turns": af.get("max_turns", 30),
            "autofix_cooldown_minutes": af.get("cooldown_minutes", 60),
            "autofix_model": af.get("model", "sonnet"),
            "repos_base_dir": af.get("repos_base_dir", "~/.control-center/repos"),
        }

        # Env vars (CC_ prefix) override file values via pydantic-settings
        return cls(**{k: v for k, v in file_values.items() if v is not None and v != ""})
