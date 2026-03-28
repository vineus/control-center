from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    github_username: str = "vineus"
    poll_interval_seconds: int = 180
    autofix_enabled: bool = False
    repos_base_dir: str = "~/.control-center/repos"
    host: str = "0.0.0.0"
    port: int = 8000

    model_config = {"env_prefix": "CC_"}
