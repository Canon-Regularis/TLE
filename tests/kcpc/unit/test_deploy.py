"""Checks on settings in the deployment and CI files that no test would miss.

Removing either setting breaks nothing that a test sees, only the running bot
or a broken CI run, so these checks pin them.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def setting_values(path: Path, key: str) -> list[str]:
    """The values of every ``key: value`` line in the YAML file at ``path``."""
    text = path.read_text(encoding='utf-8')
    pattern = rf'^\s*{re.escape(key)}:\s*["\']?([^"\'#\s]+)["\']?\s*(?:#.*)?$'
    return re.findall(pattern, text, re.MULTILINE)


def test_docker_stops_the_bot_with_sigint() -> None:
    # Python runs as PID 1 in the container, where the SIGTERM Docker sends by
    # default is ignored until the SIGKILL. SIGINT makes discord.py close the
    # bot, which shuts KCPC down cleanly.
    compose = REPO_ROOT / 'docker-compose.yaml'

    assert setting_values(compose, 'stop_signal') == ['SIGINT']


def test_the_test_job_has_a_time_limit() -> None:
    # Broken async tests tend to hang rather than fail, and GitHub would let a
    # hung job run for 6 hours.
    workflow = REPO_ROOT / '.github' / 'workflows' / 'test.yaml'

    (minutes,) = setting_values(workflow, 'timeout-minutes')
    assert 0 < int(minutes) <= 60
