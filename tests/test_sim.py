"""平衡模拟器跑得通、结果在合理范围（不测具体数值，那是 balance/result.md 的事）"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "balance"))
import sim  # noqa: E402


def test_single_fight_runs():
    random.seed(1)
    lost, wiped, rounds = sim.fight_test("sword_shield", "normal", 0, 5, [("boss", "boss")], "mine", n=20)
    assert lost >= 0 and 0 <= wiped <= 1 and rounds >= 1


def test_gate_boss_fight_runs():
    random.seed(2)
    lost, wiped, rounds = sim.gate_test("gate_astral", "normal", 25, n=10)
    assert 0 <= wiped <= 1 and rounds >= 1


def test_economy_buys_some_potions_and_upgrades():
    random.seed(3)
    kits = sim.economy("normal", "sword_shield")
    assert len(kits) == sim.TRIPS
    assert kits[-1].weapon_plus[0] > kits[0].weapon_plus[0]
    assert all(0 <= k.potions <= sim.POTION_MAX for k in kits)
