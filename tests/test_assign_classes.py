"""규칙봇과 AI의 무료 캐릭터 클래스 배정 계획 검증."""
from __future__ import annotations

import importlib.util
import pathlib


_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "assign_classes.py"
_SPEC = importlib.util.spec_from_file_location("assign_classes", _PATH)
assert _SPEC and _SPEC.loader
assign_classes = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(assign_classes)


def test_class_plan_covers_all_simulation_accounts_once():
    plan = assign_classes.class_plan()
    emails = [email for email, _personality, _class_id in plan]
    assert len(plan) == 102
    assert len(set(emails)) == 102
    assert ("pirate01@bots.local", "raider", 2) in plan
    assert ("explorer01@bots.local", "explorer", 3) in plan


def test_ai_personas_receive_matching_classes():
    plan = {email: (personality, class_id) for email, personality, class_id in assign_classes.class_plan()}
    assert plan["ai04@bots.local"] == ("explorer", 3)
    assert plan["ai06@bots.local"] == ("raider", 2)
    assert plan["ai03@bots.local"] == ("miner", 1)
    assert plan["ai02@bots.local"] == ("turtle", 1)
