import json
import logging
import subprocess

logger = logging.getLogger(__name__)


def gh_graphql(query: str, variables: dict[str, str] | None = None) -> dict:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    if variables:
        for k, v in variables.items():
            cmd.extend(["-f", f"{k}={v}"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        logger.error("gh graphql failed: %s", result.stderr)
        raise RuntimeError(f"gh api graphql failed: {result.stderr}")
    return json.loads(result.stdout)


def gh_rest(endpoint: str) -> dict:
    cmd = ["gh", "api", endpoint]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        logger.error("gh api failed: %s", result.stderr)
        raise RuntimeError(f"gh api failed: {result.stderr}")
    return json.loads(result.stdout)
