"""规则公式：伤害、减伤、持续伤害、升级几率、刷怪群数、头目出手次数、技能时机"""
import random

import pytest

import rules as R


def test_damage_drops_as_defense_grows():
    hits = [R.hurt_player_by(200, d, 20) for d in (0, 50, 100, 200)]
    assert hits == sorted(hits, reverse=True)
    assert hits[0] > hits[-1] > 0


def test_defense_counts_for_less_deeper_down():
    # 减伤常数从第 11 层起变大：同样的防御，越深挡得越少
    assert R.def_k(10) == R.def_k(1)
    assert R.def_k(20) > R.def_k(15) > R.def_k(10)
    assert R.hurt_player_by(200, 100, 25) > R.hurt_player_by(200, 100, 10)


def test_expected_hit_matches_formula():
    k = R.def_k(20)
    assert R.expected_hit(300, 50, 20) == pytest.approx(300 * k / (k + 50))


def test_dot_scales_with_the_hit_and_has_a_floor():
    assert R.dot_value("poison", 1000, 20) > R.dot_value("poison", 100, 20)
    assert R.dot_value("poison", 1, 20) >= R.DOT_MIN
    assert R.dot_value("bleed", 100, 20) < R.dot_value("poison", 100, 20)


def test_upgrade_chance_falls_with_level_and_never_hits_zero():
    chances = [R.upgrade_chance(level, 0, False, 0, False) for level in range(0, 20)]
    assert chances == sorted(chances, reverse=True)
    assert min(chances) > 0
    assert all(0 < c <= 1 for c in chances)


def test_upgrade_pity_ore_and_scrap_help():
    base = R.upgrade_chance(8, 0, False, 0, False)
    assert R.upgrade_chance(8, 3, False, 0, False) > base       # 连败保底
    assert R.upgrade_chance(8, 0, True, 0, False) > base        # 矿石
    assert R.upgrade_chance(8, 0, False, 2, False) > base       # 碎铁


@pytest.mark.parametrize("depth,second,third", [(1, 0, 0), (8, 0.5, 0), (13, 1, 0), (16, 1, 0.05), (25, 1, 0.5), (35, 1, 1)])
def test_group_chances(depth, second, third):
    s, t = R.group_chances(depth)
    assert s == pytest.approx(second)
    assert t == pytest.approx(third)


def test_rooms_never_spawn_more_than_three_groups():
    random.seed(1)
    assert all(1 <= R.roll_groups(d) <= 3 for d in range(1, 60) for _ in range(50))


def test_deep_bosses_act_more_often():
    assert R.boss_actions(10) == 1
    assert R.boss_actions(16) == 2
    assert R.boss_actions(31) == 3


def test_due_skill_timing():
    every3 = [{"when": "every", "value": 3, "do": "combo"}]
    assert R.due_skill(every3, 1.0, 0, set()) is None           # 第一下不放
    assert R.due_skill(every3, 1.0, 3, set()) == 0
    assert R.due_skill(every3, 1.0, 4, set()) is None


def test_due_skill_hp_threshold_fires_once():
    sk = [{"when": "hp_below", "value": 0.5, "do": "phase"}]
    assert R.due_skill(sk, 0.6, 5, set()) is None
    assert R.due_skill(sk, 0.4, 5, set()) == 0
    assert R.due_skill(sk, 0.4, 6, {0}) is None


def test_due_skill_dark_light_and_random_interval():
    dark = [{"when": "dark", "every": 2, "do": "summon"}]
    assert R.due_skill(dark, 1.0, 2, set(), dark=False) is None
    assert R.due_skill(dark, 1.0, 2, set(), dark=True) == 0
    bright = [{"when": "light_at_least", "value": 70, "every": 3, "do": "telegraph"}]
    assert R.due_skill(bright, 1.0, 3, set(), light=50) is None
    assert R.due_skill(bright, 1.0, 3, set(), light=80) == 0
    at = [{"when": "at_act", "value": 5, "do": "telegraph"}]
    assert R.due_skill(at, 1.0, 4, set()) is None
    assert R.due_skill(at, 1.0, 5, set()) == 0


def test_phase_only_skills_wait_for_phase():
    sk = [{"when": "every", "value": 2, "do": "strike", "phase": True}]
    assert R.due_skill(sk, 1.0, 2, set(), phased=False) is None
    assert R.due_skill(sk, 1.0, 2, set(), phased=True) == 0


def test_refine_never_goes_down_or_past_cap():
    random.seed(2)
    refine = {"double_up_chance": 0.1, "up_chance": [0.5, 0.4, 0.3], "catalyst_mult": 2}
    for tier in (1, 2, 3):
        for _ in range(200):
            new = R.refine_roll(tier, 3, refine, False)
            assert tier <= new <= max(tier, 3)
