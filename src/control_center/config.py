from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    github_username: str = "vineus"
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
