from pathlib import Path

from bots.rule_based import personality, post_actions, raider


def test_exact_personality_overrides_only():
    assert personality.assign_personality("pirate01@bots.local") == "raider"
    assert personality.assign_personality("explorer01@bots.local") == "explorer"
    assert personality.experimental_profile("pirate01@bots.local") == "raid_aggressive"
    assert personality.experimental_profile("explorer01@bots.local") == "exp_aggressive"
    assert personality.experimental_profile("pirate02@bots.local") is None


def test_aggressive_raid_options_are_account_isolated():
    opts = post_actions.raid_options_for_account("pirate01@bots.local")
    assert opts["min_loot"] < raider.MIN_EXPECTED_LOOT
    assert "max_active_missions" not in opts  # 서버 컴퓨터기술 슬롯 전체 사용
    assert post_actions.raid_options_for_account("bot001@bots.local") == {}


def test_watchdog_named_accounts_are_unique():
    text = (Path(__file__).parents[1] / "scripts/watchdog.sh").read_text()
    assert text.count("pirate01@bots.local") == 1
    assert text.count("explorer01@bots.local") == 1


def test_named_accounts_are_in_ai_policy_loop_and_spark_only():
    root = Path(__file__).parents[1]
    runner = (root / "scripts/run_ai_agents.sh").read_text()
    tick = (root / "bots/ai_agents/tick.sh").read_text()

    assert "pirate01 explorer01" in runner
    assert 'SPARK_ONLY_AGENTS="pirate01 explorer01"' in tick
    assert (root / "bots/ai_agents/personas/pirate01.md").is_file()
    assert (root / "bots/ai_agents/personas/explorer01.md").is_file()


def test_aggressive_explorer_profile_plumbing_and_idle_fallback_parity():
    assert personality.planning_personality("explorer01@bots.local", "explorer") == "explorer_aggressive"
    assert "explorer_aggressive" in personality.IDLE_FALLBACK_PERSONALITIES
