from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
TICK_SCRIPT = PROJECT / "bots" / "ai_agents" / "tick.sh"
RUNNER_SCRIPT = PROJECT / "scripts" / "run_ai_agents.sh"
WATCHDOG_SCRIPT = PROJECT / "scripts" / "watchdog.sh"


def _prompt_assignment(script: str) -> str:
    match = re.search(r'^PROMPT="(.*?)"\n\nrun_codex\(\)', script, re.MULTILINE | re.DOTALL)
    assert match is not None, "tick.sh PROMPT assignment not found"
    return match.group(1)


def test_tick_prompt_does_not_execute_markdown_backticks() -> None:
    """Literal backticks in PROMPT="..." become shell command substitutions."""
    prompt = _prompt_assignment(TICK_SCRIPT.read_text(encoding="utf-8"))
    unsafe = re.findall(r"(?<!\\)`", prompt)
    assert unsafe == [], "PROMPT contains unescaped shell command-substitution backticks"


def test_policy_model_uses_medium_reasoning_when_a_call_is_required() -> None:
    """안정 상태는 gate가 거르고, 실제 호출은 충분한 추론 강도를 사용한다."""
    script = TICK_SCRIPT.read_text(encoding="utf-8")

    assert 'MODEL_REASONING_EFFORT="${MODEL_REASONING_EFFORT:-medium}"' in script
    assert '-c model_reasoning_effort="$MODEL_REASONING_EFFORT"' in script


def test_parallel_runner_returns_failure_when_any_agent_tick_fails(tmp_path: Path) -> None:
    """Parallel mode must not claim success when a child tick exits non-zero."""
    scripts_dir = tmp_path / "scripts"
    tick_dir = tmp_path / "bots" / "ai_agents"
    scripts_dir.mkdir(parents=True)
    tick_dir.mkdir(parents=True)

    copied_runner = scripts_dir / "run_ai_agents.sh"
    shutil.copy2(RUNNER_SCRIPT, copied_runner)

    fake_tick = tick_dir / "tick.sh"
    fake_tick.write_text(
        "#!/bin/bash\n"
        "case \"$1\" in\n"
        "  bad@*) echo 'intentional tick failure' >&2; exit 7 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_tick.chmod(0o755)

    env = os.environ.copy()
    result = subprocess.run(
        [
            "bash",
            str(copied_runner),
            "--agents",
            "ok bad",
            "--parallel",
            "2",
            "--delay",
            "0",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "성공 1 / 실패 1 / 전체 2" in result.stdout


def test_watchdog_pins_the_bot_runtime_python() -> None:
    """수동 실행 셸의 conda PATH가 cron과 다른 인터프리터를 고르면 안 된다."""
    script = WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert 'PYTHON="/usr/bin/python3"' in script
    assert 'python3 scripts/run_universe.py' not in script
    assert 'python3 scripts/metrics_collector.py' not in script
    assert '"$PYTHON" scripts/run_universe.py' in script
    assert '"$PYTHON" scripts/metrics_collector.py' in script


def test_watchdog_runs_universe_on_ten_minute_even_spread() -> None:
    """102계정은 10분 전체에 균등 분산해 이전 틱 중첩을 막는다."""
    script = WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert "--interval-min 10" in script
    assert "--spread-sec 600" in script
    assert "--interval-min 4" not in script
    assert "--spread-sec 240" not in script
