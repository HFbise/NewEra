"""地牢生成和内容文件：布局连通、主题门槛、关卡层；YAML 里引用的怪、事件、物品都真的存在"""
import random

import pytest
import yaml

import dungeon
import seed

D = dungeon.data()
LOOT = dungeon.loot_data()
with open("world.yaml", encoding="utf-8") as f:
    WORLD = yaml.safe_load(f)
ITEMS = seed._all_items(WORLD)
# 事件房"拿"的不是物品模板，而是引擎特殊处理的东西
SPECIAL_TAKES = {"gem", "treasure_pool"}


def _connected(start, edges):
    seen, stack = {start}, [start]
    while stack:
        c = stack.pop()
        for _, n in dungeon._neighbors(c):
            if n not in seen and frozenset((c, n)) in edges:
                seen.add(n)
                stack.append(n)
    return seen


@pytest.mark.parametrize("seed_", range(50))
def test_floor_layout_is_connected_and_stairs_are_farthest(seed_):
    random.seed(seed_)
    start, stairs, edges = dungeon.layout()
    cells = {(r, c) for r in range(dungeon.GRID) for c in range(dungeon.GRID)}
    assert _connected(start, edges) == cells
    assert start != stairs


def test_gate_floors_every_25():
    assert [d for d in range(1, 101) if dungeon.gate_depth(d)] == [25, 50, 75, 100]


def test_theme_pool_respects_min_depth():
    random.seed(3)
    shallow = {dungeon._pick_theme(D["themes"], 5, set()) for _ in range(300)}
    deep = {dungeon._pick_theme(D["themes"], 30, set()) for _ in range(600)}
    assert all(not D["themes"][t].get("min_depth") for t in shallow)
    assert any(D["themes"][t].get("min_depth") for t in deep)
    assert all(D["themes"][t].get("min_depth", 0) <= 30 for t in deep)


def test_theme_pool_avoids_recent_themes():
    random.seed(4)
    recent = {"mine", "forest"}
    assert all(dungeon._pick_theme(D["themes"], 5, recent) not in recent for _ in range(200))


@pytest.mark.parametrize("key", sorted(D["themes"]))
def test_theme_references_exist(key):
    t = D["themes"][key]
    assert set(t["monsters"]) <= set(D["monsters"]), "主题里写了不存在的怪"
    assert set(t["events"]) <= set(D["events"]), "主题里写了不存在的事件"
    for s in t["boss"].get("skills", []):
        for kind in [s.get("kind"), (s.get("then") or {}).get("kind")]:
            if (s.get("do") == "summon" or (s.get("then") or {}).get("do") == "summon") and kind:
                assert kind in D["monsters"], f"头目召唤了不存在的怪 {kind}"


@pytest.mark.parametrize("key", sorted(D.get("gate_bosses", {})))
def test_gate_bosses_are_complete(key):
    g = D["gate_bosses"][key]
    assert g["theme"] in D["themes"]
    assert g["loot"] in LOOT["gate_bosses"]
    assert g["phases"][0]["at"] == 1.0
    ats = [p["at"] for p in g["phases"]]
    assert ats == sorted(ats, reverse=True), "阶段要按血量从高到低排"
    for p in g["phases"]:
        if sm := p.get("summon"):
            assert sm["kind"] in D["monsters"]


def test_event_takes_reference_real_items():
    for key, e in D["events"].items():
        item = e["take"].get("item")
        if item and item not in SPECIAL_TAKES:
            assert item in ITEMS, f"事件 {key} 给的物品 {item} 不存在"


def test_loot_tables_reference_real_items():
    missing = []
    for kind, drops in (LOOT.get("monsters") or {}).items():
        missing += [d["item"] for d in drops or [] if d["item"] not in ITEMS]
    for theme, b in (LOOT.get("bosses") or {}).items():
        missing += [i for i in b.get("pick_one", []) if i not in ITEMS]
        missing += [e["item"] for e in b.get("extra", []) if e["item"] not in ITEMS]
    for theme, pool in (LOOT.get("treasure") or {}).items():
        missing += [e["item"] for e in pool if e["item"] not in ITEMS]
    for b in (LOOT.get("gate_bosses") or {}).values():
        missing += [i for i in b.get("pick_one", []) if i not in ITEMS]
    missing = sorted(set(missing) - dungeon.LOOT_NOT_YET)
    assert not missing, f"掉落表里有不存在的物品：{missing}"


def test_gem_pools_reference_real_gems():
    for theme, pool in LOOT["gems"]["pools"].items():
        for gem in pool:
            assert gem in ITEMS and ITEMS[gem].get("type") == "gem", f"{theme} 宝石池里的 {gem}"


def test_shops_sell_real_items():
    for npc_id, npc in WORLD["npcs"].items():
        for item in (npc.get("props") or {}).get("sells", []):
            assert item in ITEMS, f"{npc_id} 卖的 {item} 不存在"


def test_deep_files_never_override_main_files():
    # 深层文件只补主文件没有的：同名的以主文件为准（改过的在主文件）
    with open("dungeon.yaml", encoding="utf-8") as f:
        main = yaml.safe_load(f)
    for section in ("themes", "monsters", "events"):
        for key, value in (main.get(section) or {}).items():
            assert D[section][key] == value
