"""
数值模拟：经济（每层赚多少钱、回城时升到几级）+ 战斗（每层死亡率、掉血、每场打几轮、用几瓶药）。

规则数值全部来自 rules.py（跟引擎共用：命中、减伤、伤害、升级费用和失败率、怪物数值、掉钱）和
dungeon.yaml / loot.yaml / 物品表；这里只写"一层怎么走、一场仗怎么打"的流程，照着 dungeon._make_floor、
engine.round_enemies / _enemy_act / do_attack 的顺序。随机种子固定，结果可复现。

没模拟的（所以模拟会比真人难一点，见 balance/README）：环境物件、花样、逃跑、躲藏偷袭、事件房、走路时的随机事件、
卖掉捡到的东西、带条件的装备特效（队友倒下、血少时才加的）、状态类装备特效（打中附带定身、中毒）。

用法：python balance/sim.py [--trips 300] [--seed 1] [--out balance/result.md]
"""
import argparse
import math
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

import dungeon  # noqa: E402
import rules as R  # noqa: E402

MONSTERS = dungeon.data()["monsters"]
THEMES = dungeon.data()["themes"]
GATES = dungeon.data().get("gate_bosses", {})
ITEMS = {}
for _f in ("world.yaml", "items_dungeon.yaml"):
    ITEMS.update(yaml.safe_load(open(_f, encoding="utf-8"))["items"])

TRIP = 5                                # 一趟：两块传送石之间（1–5、6–10……），出发前回城买药、升级
TRIPS = 6                               # 一共几趟：走到第 30 层（深层装备、主题还没做，21 层以后先用第 16 层那套装备，只当预警）
FLOORS = TRIPS * TRIP
POTION = "blood_potion"                 # 血药
HERB_HEAL = ITEMS["herb"]["heal"]
TORCH_LIT = ITEMS["torch_lit"]["props"]["light"]
TORCH_DIM = ITEMS["torch_dim"]["props"]["light"]
TORCH_PRICE = ITEMS["torch"]["props"]["price"]
DRINK_BELOW = 0.35                      # 血掉到这个比例以下就喝药（战斗里算一个动作）
CAMP_BELOW = 0.6                        # 打完一场血在这以下、这层还没扎营，就去空房扎营
SURVIVAL_LEVEL = 1                      # 扎营判定用的生存等级
STASH_CHANCE = 0.5                      # 空房藏着的古币翻得到的几率（调查判定）
FORAGE_CHANCE = 0.5                     # 空房搜到药草的几率
ROUND_CAP = 60                          # 一场打这么多轮还没完就算僵住（记下来）
ORE_PER_FLOOR = 0.5                     # 每层大约能弄到的奥利哈刚（事件房、掉落），回城都砸在武器上
# 对照用的开关（命令行改）：怪打人的伤害倍数（真人比模拟难约 1.5 倍）、远程武器每级加多少
VARIANT = {"dmg": 1.0, "ranged_step": R.RANGED_UPGRADE_STEP, "reload_back": 1, "reload_melee_only": False,
           "gems": False, "focus_boss": False}
GEMS = dungeon.loot_data()["gems"]
# 带孔的装备只有地牢掉的：每一段"实际拿得到"的那几件按来源定稀有度（商店货、开局装备没有孔）
SOCKET_SOURCE = {"精钢短剑": "uncommon", "锁子甲": "common", "头目胸甲": "rare", "头盔": "rare", "塔盾": "uncommon",
                 "猎弓": "uncommon", "绞盘重弩": "uncommon"}
PREP_BELOW = 0.7                        # 进头目（楼梯间）之前血量低于这个比例：先扎营（这层还没扎过），再喝药
# 远程打法从拿得到那把弓的那一段开始算：猎弓地牢第 4 层起才可能掉（不常见），算第 2 趟（6 层）起；
# 绞盘重弩第 7–9 层起才掉，算第 3 趟（11 层）起。手弩莉娜店里就有，第 1 层起
START_TRIP = {"bow": 1, "xbow": 2, "sword_shield+bow": 1}


# ============ 装备：按"走到这一段实际拿得到的"拼，数字是模板的基础值，升级另算 ============
# 每段一行：第 1 趟（1–5 层）、第 2 趟（6–10）、第 3 趟（11–15）、第 4 趟（16–20）
# 护甲：差装备只有开局的皮甲和古旧护符（地窖桌上人人拿得到），一直不升级；
#       正常：第 1 趟是莉娜店里的全套（皮帽、皮甲、皮靴）+ 护符；第 2 趟锁子甲；第 3 趟起头目的胸甲（3）和典狱长的头盔（2）
ARMOR = {
    "poor": [[("皮甲", 10), ("古旧护符", 20)]] * TRIPS,
    "normal": [[("皮帽", 10), ("皮甲", 10), ("皮靴", 10), ("古旧护符", 20)],
               [("皮帽", 10), ("锁子甲", 20), ("皮靴", 10), ("古旧护符", 20)],
               [("头盔", 20), ("头目胸甲", 30), ("皮靴", 10), ("古旧护符", 20)]]
              + [[("头盔", 20), ("头目胸甲", 30), ("皮靴", 10), ("古旧护符", 20)]] * (TRIPS - 3),
}
ARMOR["fav"] = ARMOR["normal"]
ARMOR["real"] = ARMOR["normal"]        # 真人档：地牢掉的防具照穿，只是一件都不升
ARMOR["cap"] = ARMOR["normal"]         # 上限档：防具每件 +CAP_ARMOR_PLUS
# 上限档（照玩家A第 41 层的配置）：头目的鹤嘴锄 +10（破甲 20）、完美血石加伤、防具每件 +6、每趟带 13 瓶血药
CAP_WEAPON = ("工头的鹤嘴锄", ITEMS["foreman_mattock"]["damage"])
# 深层装备（deep_items.yaml 的 tier）：正常档比所在的层落后一档（要先从怪、宝箱、头目身上拿到）：
# 第 21–25 层用第一档（单手 95、胸甲 40、盾 40），第 26–30 层用第二档（单手 130、胸甲 55、头盔 45、靴子 30）；升级等级照旧
DEEP_GEAR = {4: {"weapon": 95, "armor": [20, 40, 20, 20], "shield": 40},
             5: {"weapon": 130, "armor": [45, 55, 30, 20], "shield": 40}}
CAP_WEAPON_PLUS, CAP_ARMOR_PLUS, CAP_POTIONS, CAP_GEM_BONUS, CAP_PIERCE = 10, 6, 13, 35, 20
# 盾：剑盾打法第 1 趟木盾，之后塔盾（挡远程 2 点）；最后一项是格挡几率（物品表的 props.block）
_TOWER = ("塔盾", ITEMS["tower_shield"]["defense"],
          next(e["value"] for e in ITEMS["tower_shield"]["props"]["effects"] if e["do"] == "guard"),
          ITEMS["tower_shield"]["props"].get("block", 0))
SHIELD = [("木盾", ITEMS["wooden_shield"]["defense"], 0, ITEMS["wooden_shield"]["props"].get("block", 0))] + [_TOWER] * (TRIPS - 1)
DMG = {k: ITEMS[k]["damage"] for k in ("shiny_sword", "steel_shortsword", "iron_axe", "hunting_bow", "heavy_crossbow", "sling")}


@dataclass
class Weapon:
    name: str
    damage: float
    ranged: bool = False
    steady: bool = False
    reload_steps: int = 1
    pierce: int = 0
    first_bonus: int = 0                # 这场第一次出手加（猎手之戒）
    free_reload: bool = False           # 箭袋腰带：每场白给一次装填


HUNTER_RING = next(e["value"] for e in ITEMS["hunter_ring"]["props"]["effects"] if e["do"] == "bonus")
XBOW_PIERCE = next(e["value"] for e in ITEMS["heavy_crossbow"]["props"]["effects"] if e["do"] == "pierce")


def melee_weapons(build: str, trip: int) -> list[tuple[str, int]]:
    """(名字, 伤害) 主手在前"""
    sword = ("闪亮的短剑", DMG["shiny_sword"]) if trip == 0 else ("精钢短剑", DMG["steel_shortsword"])
    if build in ("dual", "dual_sneak"):
        return [sword, ("铁斧", DMG["iron_axe"]) if trip == 0 else ("闪亮的短剑", DMG["shiny_sword"]) if trip == 1
                else ("精钢短剑", DMG["steel_shortsword"])]
    if build in ("sword_shield", "sword_torch", "sling_sword", "mcb_sword", "hxb_sword"):
        return [sword]
    return []


def ranged_weapon(build: str, trip: int) -> Optional[Weapon]:
    if build == "bow":
        return Weapon("猎弓", DMG["hunting_bow"], True, first_bonus=HUNTER_RING if trip >= 2 else 0, free_reload=trip >= 2)
    if build == "xbow":
        return (Weapon("猎弓", DMG["hunting_bow"], True) if trip == 0
                else Weapon("绞盘重弩", DMG["heavy_crossbow"], True, steady=True, reload_steps=2, pierce=XBOW_PIERCE,
                            free_reload=trip >= 2))
    if build == "sling_sword":
        return Weapon("投石索", DMG["sling"], True)
    if build == "mcb_sword":
        return Weapon("麦琪的轻弩", ITEMS["maggie_crossbow"]["damage"], True, steady=True)
    if build == "hxb_sword":
        return Weapon("手弩", ITEMS["hand_crossbow"]["damage"], True)
    return None


BUILDS = {"dual": "双持", "dual_sneak": "双持潜行", "sword_shield": "剑盾", "sword_torch": "剑+火把", "bow": "猎弓", "xbow": "绞盘重弩",
          "sling_sword": "投石索配剑", "mcb_sword": "麦琪的弩配剑", "hxb_sword": "手弩配剑",
          "sword_shield+bow": "剑盾 + 猎弓"}
QUALITY = {"poor": "装备差", "normal": "正常", "fav": "好感全满", "real": "真人档", "cap": "上限档"}
# 真人档（照玩家A这一趟）：只升武器、钱攒着不花完（每趟最多花 REAL_SPEND），不升防具、不镶宝石、不买血药，只靠路上捡的药草
REAL_SPEND = 0.1                        # 照玩家A调的：第 16 层主武器 +4、身上攒着约 4000 金


def endurance_hp(depth: int) -> int:
    """走到这一层时的血量上限：耐性熟练大约每层涨 2 次（玩家A到第 6 层是 13 次、上限 26）"""
    return 20 * R.SCALE + R.ENDURANCE_HP * R.skill_level(round(2.2 * (depth - 1)))


# ============ 经济：按期望走一遍每层（不打仗，假设都活着），算每层收入；回城时买药、火把，剩下的钱升级 ============

def floor_income(depth: int, size: int, torch: bool) -> int:
    """这一层捡到的钱（一个人分到的）：怪掉的、宝箱房钱袋、空房古币。越暗掉得越多"""
    theme = THEMES[dungeon._pick_theme(THEMES, depth, set())]
    gold = 0.0

    def light() -> int:
        return room_light(theme, torch_light=TORCH_LIT if torch else 0)

    def group_gold(rank: str) -> float:
        lo, hi = R.monster_gold(depth, rank)
        return random.randint(lo, hi) * (1 + 0.5 * R.dark_factor(light()))
    for _ in range(4):                                  # 四个战斗房
        count = R.roll_groups(depth)
        for i in range(count):
            gold += group_gold("elite" if i == 0 and random.random() < R.elite_chance(depth) else "normal")
    gold += group_gold("boss" if depth % R.BOSS_EVERY == 0 else "elite")        # 楼梯间
    if random.random() < R.TREASURE_GUARD:
        gold += group_gold("elite" if random.random() < R.elite_chance(depth) else "normal")
    dark = (50 - (theme.get("light", 35) + dungeon.LIGHT_OFFSET.get(theme["treasure"].get("light", "dim"), 0))) / 50
    gold += R.treasure_gold(depth, dark, size) / size
    if random.random() < STASH_CHANCE:
        gold += R.stash_gold(depth, size) / size
    return round(gold)


@dataclass
class Kit:
    """回城时的状态：升级到几级、带几瓶药、镶的宝石加多少"""
    weapon_plus: list[int]
    armor_plus: list[int]
    potions: int
    gold_left: int
    spent_upgrade: int
    gem_bonus: float = 0.0              # 武器上宝石加的伤害（小数按几率进位）
    gem_def: float = 0.0                # 护具上宝石加的防御（全身封顶）
    spent_refine: int = 0
    gem_pierce: float = 0.0             # 破甲石
    gem_crit: float = 0.0               # 锋墨石：会心几率（只取最高）


def floor_gems(depth: int) -> list[tuple[str, int]]:
    """这一层捡到的宝石（按掉率估：普通怪大约 5 只、精英大约 1.3 只、头目、宝箱房）"""
    theme = dungeon._pick_theme(THEMES, depth, set())
    pools, drops = GEMS["pools"], GEMS["drops"]
    got = []
    deep = drops.get("deep_mult", 1) if depth >= drops.get("deep_from_floor", 999) else 1
    rolls = [(drops["normal"] * deep, theme)] * 5 + [(drops["elite"] * deep, theme)] * 1 + [(drops["elite"] * 0.3 * deep, theme)] \
        + [(drops["treasure"] * deep, theme if random.random() < 0.5 else "common")]
    if depth % R.BOSS_EVERY == 0:
        rolls += [(drops["boss"], theme)] * (drops.get("boss_deep", 1) if deep > 1 else 1)
    for chance, pool in rolls:
        if random.random() < chance:
            gem = random.choice(pools[pool])
            got.append((gem, R.gem_tier(depth, GEMS, bool(ITEMS[gem]["props"].get("numeric")))))
    return got


def gem_value(gem: str, tier: int, cat: str, do: str) -> float:
    fx = R.gem_effects(ITEMS[gem]["props"], tier, cat) if R.gem_fits(ITEMS[gem]["props"]["gem_slot"], cat) else []
    return sum(e.get("value", 0) for e in fx if e.get("do") == do and not e.get("vs"))


def gem_chance(gem: str, tier: int, do: str) -> float:
    fx = R.gem_effects(ITEMS[gem]["props"], tier, "weapon") if R.gem_fits(ITEMS[gem]["props"]["gem_slot"], "weapon") else []
    return max([e.get("chance", 0) for e in fx if e.get("do") == do] or [0])


def weapon_score(g: list) -> float:
    """武器孔挑宝石的分数：加伤害、破甲按点算，会心按几率 × 一下大约 8 点"""
    return gem_value(g[0], g[1], "weapon", "bonus") + gem_value(g[0], g[1], "weapon", "pierce") + 8 * R.SCALE * gem_chance(g[0], g[1], "crit")


def fit_gems(bag: list[list], w_sockets: int, a_sockets: int) -> tuple[float, float, float, float]:
    """最简单的镶法：武器孔镶进攻最强的（加伤害、破甲、会心），护具孔镶加防御最多的（全身封顶），功能型的不算。
    返回 (加伤害, 防御, 破甲, 会心几率)"""
    wv = [g for g in sorted(bag, key=weapon_score, reverse=True)[:w_sockets] if weapon_score(g) > 0]
    av = sorted((gem_value(g, t, "armor", "defense") for g, t in bag), reverse=True)[:a_sockets]
    return (sum(gem_value(g, t, "weapon", "bonus") for g, t in wv),
            min(GEMS["body_caps"]["defense"], sum(v for v in av if v > 0)),
            sum(gem_value(g, t, "weapon", "pierce") for g, t in wv),
            max([gem_chance(g, t, "crit") for g, t in wv] or [0]))


REFINE_SHARE = 0.3                      # 回村先拿三成的钱去诺艾尔那里刷宝石，剩下的再升级（真人面对抽奖多半先刷）


def refine_run(gold: int, bag: list[list], deepest: int) -> int:
    """刷宝石：先刷用得上的（进攻、加防御的）里品质最低的，有同种的就当垫子。返回花掉的钱"""
    budget, spent = int(gold * REFINE_SHARE), 0
    rf = GEMS["refine"]
    while True:
        useful = [g for g in bag if (weapon_score(g) > 0 or gem_value(g[0], g[1], "armor", "defense") > 0)
                  and g[1] < R.refine_cap(bool(ITEMS[g[0]]["props"].get("numeric")), deepest, rf)]
        if not useful:
            break
        g = min(useful, key=lambda x: x[1])
        cost = rf["cost"][g[1] - 1]
        if cost > budget - spent:
            break
        spent += cost
        pads = [x for x in bag if x is not g and x[0] == g[0]]
        pad = min(pads, key=lambda x: x[1]) if pads else None
        if pad:
            bag.remove(pad)
        g[1] = R.refine_roll(g[1], R.refine_cap(bool(ITEMS[g[0]]["props"].get("numeric")), deepest, rf), rf, pad is not None)
    return spent


ORE_BELOW = 0.4


def upgrade_run(gold: int, weapons: list[int], armor: list[int], w_plus: list[int], a_plus: list[int],
                discount: float, oil: int, ore: list[int], w_share: float = 0.5) -> tuple[int, int]:
    """engine.do_upgrade 的规则：失败不掉级、钱照收，同一级连续失败每次 +UPGRADE_PITY（保底），矿石这一次成功率翻倍、用掉。
    钱一半花在武器、一半花在防具上，每样按"最便宜的一次"先升；有矿石就垫在主武器上。返回 (剩下的钱, 花掉的钱)"""
    spent = 0
    fails: dict = {}
    for pool, base, plus in (("w", weapons, w_plus), ("a", armor, a_plus)):
        budget = int(gold * w_share) if pool == "w" else gold - spent
        while True:
            opts = []
            for k, b in enumerate(base):
                lvl = plus[k] + 1
                if lvl > R.UPGRADE_MAX:
                    continue
                cost, _ = R.upgrade_cost(lvl)
                opts.append((round(cost * discount), k, lvl))
            if not opts:
                break
            cost, k, lvl = min(opts)
            if oil > 0:                  # 淬火油：必成、不收钱
                oil -= 1
                plus[k] += 1
                continue
            if cost > budget:
                break
            budget -= cost
            spent += cost
            f = fails.get((pool, k, lvl), 0)
            # 矿石留到这一次成功率低于 ORE_BELOW 才用（翻倍、用掉；低等级用太浪费）
            use_ore = pool == "w" and k == 0 and ore[0] > 0 and R.upgrade_chance(lvl, f) < ORE_BELOW
            if use_ore:
                ore[0] -= 1
            if random.random() < R.upgrade_chance(lvl, f, use_ore, armor=pool == "a"):
                plus[k] += 1
            else:
                fails[(pool, k, lvl)] = f + 1
    return gold - spent, spent

def economy(quality: str, build: str, trips: int = TRIPS, size: int = 1) -> list[Kit]:
    """每一趟出发时的装备等级和药（差装备不升级，只带能买得起的药）"""
    if quality == "cap":
        return [Kit([CAP_WEAPON_PLUS], [CAP_ARMOR_PLUS] * 5, CAP_POTIONS, 0, 0, CAP_GEM_BONUS, 0.0, 0, CAP_PIERCE)
                for _ in range(trips)]
    kits, gold = [], 0
    w_plus = [0] * len(upgradable_weapons(build, 0, quality))
    a_plus = [0] * 8
    oil = 1 if quality == "fav" else 0
    discount = R.UPGRADE_DISCOUNT if quality == "fav" else 1.0
    potion_price = ITEMS[POTION]["props"]["price"]
    ore = [0]
    ore_bank = 0.0
    bag: list[list] = []                # 捡到的宝石 [id, 品质]
    sockets: dict[str, int] = {}        # 每件带孔装备的孔数（拿到时掷一次）
    for trip in range(trips):
        want = {"poor": 2, "normal": 4, "fav": 4, "real": 0}[quality]
        potions = min(want, gold // potion_price)
        gold -= potions * potion_price
        torches = 3 if build == "sword_torch" else 0
        gold -= min(gold, torches * TORCH_PRICE)
        spent = 0
        refine_spent = 0
        if VARIANT["gems"] and quality not in ("poor", "real"):
            refine_spent = refine_run(gold, bag, trip * TRIP)
            gold -= refine_spent
        if quality != "poor":
            weapons = upgradable_weapons(build, trip, quality)
            if len(w_plus) != len(weapons):
                w_plus = [0] * len(weapons)
            armor = [d for _, d in ARMOR[quality][trip]] + ([SHIELD[trip][1]] if build == "sword_shield" else [])
            a_plus = (a_plus + [0] * len(armor))[:len(armor)]
            before = gold
            if quality == "real":
                kept = gold - int(gold * REAL_SPEND)
                left, _ = upgrade_run(int(gold * REAL_SPEND), weapons, [], w_plus, [], discount, oil, ore, w_share=1.0)
                gold = kept + left
            else:
                gold, spent = upgrade_run(gold, weapons, armor, w_plus, a_plus, discount, oil, ore)
            spent = before - gold
            oil = 0
        gem_bonus = gem_def = gem_pierce = gem_crit = 0.0
        if VARIANT["gems"] and quality not in ("poor", "real"):
            pieces = [n for n, _ in (melee_weapons(build, trip)[:1] if build not in ("bow", "xbow") else [])] \
                + ([ranged_weapon(build, trip).name] if build in ("bow", "xbow") else []) \
                + [n for n, _ in ARMOR[quality][trip]] + ([SHIELD[trip][0]] if build == "sword_shield" else [])
            for n in pieces:
                if n in SOCKET_SOURCE and n not in sockets:
                    sockets[n] = R.roll_sockets(SOCKET_SOURCE[n], GEMS)
            weapon = pieces[0] if pieces else ""
            gem_bonus, gem_def, gem_pierce, gem_crit = fit_gems(bag, sockets.get(weapon, 0),
                                                                sum(sockets.get(n, 0) for n in pieces[1:]))
        kits.append(Kit(list(w_plus), list(a_plus), potions, gold, spent, gem_bonus, gem_def, refine_spent, gem_pierce, gem_crit))
        for depth in range(trip * TRIP + 1, trip * TRIP + TRIP + 1):
            gold += floor_income(depth, size, build == "sword_torch")
            if VARIANT["gems"]:
                bag += [list(g) for g in floor_gems(depth)]
            ore_bank += ORE_PER_FLOOR
            while ore_bank >= 1:
                ore_bank -= 1
                ore[0] += random.random() < 1.0
    return kits


def upgradable_weapons(build: str, trip: int, quality: str) -> list[int]:
    """升级的钱花在哪几件武器上（伤害基础值）：远程打法升远程武器，双持两把都升，别的升主手"""
    r = ranged_weapon(build, trip)
    if build in ("bow", "xbow"):
        return [r.damage]
    ws = melee_weapons(build, trip)
    if build in ("hxb_sword", "mcb_sword"):
        if quality == "fav":
            ws = [("无铭", blade_damage(trip * TRIP))]
        return [r.damage, ws[0][1]]
    if quality == "fav" and ws:
        ws = [("无铭", blade_damage(trip * TRIP))] + ws[1:]
    return [d for _, d in ws][:2 if build in ("dual", "dual_sneak") else 1]


def blade_damage(deepest: int) -> int:
    """莉娜的专属剑无铭：伤害 5 + 最深层数/3（最多 12），dungeon.arrived"""
    return min(12, 5 + deepest // 3) * R.SCALE


# ============ 战斗 ============

def room_light(theme: dict, torch_light: int = 0) -> int:
    """房间的光亮：主题的底子 + 房间本身亮暗 + 点着的火盆（每个 +20）+ 火把"""
    text = random.choice(theme["rooms"])
    level = (random.randint(*theme["light_range"]) if theme.get("light_range")
             else theme.get("light", 35) + dungeon.LIGHT_OFFSET.get(text.get("light", "dim"), 0))
    feats = random.sample(theme["features"], random.randint(1, 2))
    level += dungeon.LAMP_LIGHT * sum(1 for f in feats if f.get("lamp"))
    return max(0, min(100, level + torch_light))


@dataclass
class Mon:
    name: str
    hp: int
    max_hp: int
    atk: int
    df: int
    depth: int
    props: dict
    rank: str
    attacks: int = 1
    status: Optional[dict] = None       # {"escape": n, "attempts": n}
    base_attacks: int = 99              # 迅捷的：超过这个数的出手按 extra_chance 掷
    held: int = 0                       # 这一场被控过几次（头目只吃一次）
    backs: int = 0
    heals: int = 0
    theme: str = ""
    skills: list = field(default_factory=list)      # 头目这一层解锁的招（rules.unlocked_skills）
    acts: int = 0
    used: set = field(default_factory=set)
    pending: Optional[dict] = None      # 预告了的大招
    mark: Optional[object] = None       # 判了罪的人
    mark_bonus: int = 0
    silence: int = 0
    minion: bool = False
    threat: dict = field(default_factory=dict)      # 每个人打了它多少（蓄力重击打最有威胁的人）
    phased: bool = False                # 转过阶段了
    extra_acts: int = 0                 # 转阶段多出来的副动作
    phase_atk: int = 0
    regen: float = 0.0                  # 扎根：每轮回血
    stance: str = ""                    # 冷却 / 熔化（矮人王的铸像）
    stance_at: int = 0
    gphase: int = 0                     # 关卡头目的阶段
    rnd: dict = field(default_factory=dict)
    vanished: bool = False
    sneak: float = 0.0
    flying: int = 0
    grounded: bool = False
    rung: int = 0                       # 敲钟人摇铃加的攻击
    memory: int = 0                     # 永不醒来的人换过几段记忆
    lured: bool = False                 # 雾中渔灯：第一下打在灯上
    pull_acts: int = 0
    ambushed: bool = False              # 雾鳗：每场第一口 ×1.5
    fare_acts: int = 0
    fare_due: bool = False
    bell_acts: int = 0
    shield: int = 0                     # 共鸣者套的光膜：挡下几次
    shield_acts: int = 0


@dataclass
class Hero:
    max_hp: int
    hp: int
    atk: int
    defense: float
    melee: list[tuple[str, int]]        # (名字, 伤害)，已含升级
    ranged: Optional[Weapon]
    guard_ranged: int = 0               # 塔盾：远程伤害 −2
    gem_bonus: float = 0.0              # 武器宝石加的伤害
    gem_pierce: float = 0.0
    gem_crit: float = 0.0
    block: float = 0.0                  # 盾的格挡几率（跟闪避合计最多 AVOID_CAP）
    torch_light: int = 0               # 手上点着的火把给的光（点燃 35、弱光 20）
    potions: int = 0
    herbs: int = 0
    sneak: bool = False                 # 潜行打法：进门没被警觉的怪盯上就先偷袭一下
    guard: Optional[str] = None         # 刚挣脱、爬起来的那种控制：这个敌人回合里不再中
    still: int = 0                      # 流沙里站着没挪几轮了
    sin: int = 0                        # 罪（在其山岳之上者）
    trip_potions: int = 0               # 这一趟出发时带的药（关卡层回城补满）
    stepped: bool = False
    pinch: bool = False                 # 这一轮掐了自己（永不醒来的人的催眠）
    eyes: bool = False                  # 这一轮闭着眼（晶母的晃眼大招冲着所有睁眼的人）
    dodging: bool = False               # 这一轮闪避了（被蓄力重击盯上时，普通档一半几率会这么做，上限档总会）
    smart: float = 0.5
    stealth: int = 0                    # 隐匿等级
    perks: set = field(default_factory=set)
    effects: dict = field(default_factory=dict)     # kind -> {"value", "left", "hp"}
    status: Optional[dict] = None
    loaded: bool = True
    wound: int = 0
    reloaded_free: bool = False
    backs: int = 0                      # 这场装填时退了几步
    struck: bool = False
    down: bool = False
    # 统计
    taken: int = 0
    drank: int = 0
    # 回礼每层 / 每趟一次的
    floor_used: set = field(default_factory=set)
    trip_used: set = field(default_factory=set)
    cheer: bool = False


def power(h: Hero, shooter: Optional[Weapon]) -> int:
    base = h.atk + (shooter.damage if shooter else sum(d if k == 0 else int(d * R.OFFHAND_SHARE)
                                                         for k, (_, d) in enumerate(h.melee)))
    return int(base * (1 + R.CHEER_ATTACK / 100 if h.cheer else 1) + 0.5)


def edge_of(theme: dict) -> bool:
    """这间房是不是在虚空边上：按主题房间里 edge 的比例抽"""
    rooms = theme.get("rooms", [])
    return random.random() < sum(1 for r in rooms if r.get("edge")) / max(1, len(rooms))


def sand_of(theme: dict) -> Optional[dict]:
    """这间房是不是流沙：按主题房间里 ground: sand 的比例抽"""
    rooms = theme.get("rooms", [])
    share = sum(1 for r in rooms if r.get("ground") == "sand") / max(1, len(rooms))
    return (theme.get("sand") or {"move_max": 1, "stuck_chance": 0.3, "sink_after": 1}) if random.random() < share else None


def stance_of(m) -> dict:
    """铸像现在这个状态加减的攻防"""
    st = m.props.get("stances")
    return st.get(m.stance or st.get("start", "cold"), {}) if st else {}


def heal_scale(h: Hero, amount: int) -> int:
    """重伤：受到的治疗 × value%（engine._heal_scale）"""
    w = h.effects.get("wound")
    return round(amount * w["value"] / 100) if w else amount


def hero_def(h: Hero) -> int:
    c = h.effects.get("corrode")
    return max(0, h.defense - (c["value"] if c else 0))


def spawn(depth: int, kind: str, rank: str, theme_key: str, size: int, groups: int, boss_room: bool,
          minion: bool = False, affix: Optional[str] = None) -> list[Mon]:
    """一群怪（dungeon._spawn_group / _spawn_boss / spawn_minions）：精英抽一个词缀，头目带上这一层解锁的招"""
    gate = GATES.get(kind) if rank == "boss" else None
    m = gate or (THEMES[theme_key]["boss"] if rank == "boss" else MONSTERS[kind])
    hp, atk, df = R.monster_stats(depth, m, rank)
    props = {k: m[k] for k in ("animal", "light_averse", "undead", "keen", "ranged", "healer", "on_hit", "guard_allies",
                               "immune", "weak", "stances", "resist_element", "reflect_ranged", "shield_allies", "nodes",
                               "dormant", "bell", "silence_field", "swarm", "mirror_attack", "revive_once", "memories",
                               "lure", "ambush", "fare", "dark_evasion", "only_dark", "pull")
             if m.get(k)}
    if m.get("hearing"):
        props["keen"] = True                # 聆听者：偷袭要动身子，它听得见
    if m.get("pack"):
        props["pack"] = kind
    if boss_room:
        props["keen"] = True                # 楼梯间守卫、头目都警觉：偷袭不了、绕不过去
    if rank == "elite" and affix is None:
        affix = random.choice(list(R.ELITE_AFFIXES))
    elif rank != "elite":
        affix = None
    fx = R.ELITE_AFFIXES.get(affix, {})
    hp *= fx.get("hp_mult", 1) * (R.SUMMON_HP if minion else 1)
    df += fx.get("def", 0)
    for key in ("dmg_mult", "frenzy", "lifesteal", "thorns", "thorns_chance", "extra_chance"):
        if fx.get(key):
            props[key] = fx[key]
    if plague := fx.get("plague"):
        props["on_hit"] = ({**props["on_hit"], "chance": props["on_hit"].get("chance", 0.25) + plague} if props.get("on_hit")
                           else {"kind": R.THEME_PLAGUE.get(theme_key, "poison"), "chance": plague})
    if minion:                              # 小怪：血一半、下手一半，附带的中毒流血也减半
        props["dmg_mult"] = props.get("dmg_mult", 1) * R.SUMMON_DMG
        if props.get("on_hit"):
            props["on_hit"] = {**props["on_hit"], "value_mult": props["on_hit"].get("value_mult", 1) * R.SUMMON_DMG}
    name = m["name"] if rank != "elite" else f"{fx.get('name', '凶悍的')}{m['name']}"
    skills = R.unlocked_skills(m.get("skills") or [], depth) if rank == "boss" else []
    if gate:
        skills = list(gate["phases"][0]["skills"])
        props |= {k: gate[k] for k in ("phases", "sin", "grounded_weak") if gate.get(k)}
    if boss_room:
        mult = 1 + R.BOSS_PARTY_HP * (size - 1)
        deep = rank == "boss" and depth >= R.BOSS_DEEP_FROM
        if deep:                                # 深层头目一轮两动，第二下 ×BOSS_EXTRA_MULT
            props["extra_chance"], props["extra_mult"] = 1.0, R.BOSS_EXTRA_MULT
        out = [Mon(name, max(2 * R.SCALE, round(hp * mult)), max(2 * R.SCALE, round(hp * mult)), atk, df, depth, props, rank,
                   attacks=size * fx.get("attacks", 1) * (R.boss_actions(depth) if deep else 1), theme=theme_key, skills=skills,
                   base_attacks=size if fx.get("extra_chance") or deep else 99)]
    else:
        copies = 1 if minion else R.party_copies(size, groups)
        mult = size / copies if not minion else 1
        base = max(1, round(size / copies))
        out = [Mon(name, max(2 * R.SCALE, round(hp * mult)), max(2 * R.SCALE, round(hp * mult)), atk, df, depth, props, rank,
                   attacks=base * fx.get("attacks", 1), theme=theme_key, minion=minion,
                   base_attacks=base if fx.get("extra_chance") else 99)
               for _ in range(copies)]
    for _ in range(fx.get("minions", 0)):
        out += spawn(depth, kind, "normal", theme_key, 1, 1, False, minion=True)        # 号令的：带一只同类小怪
    if rank == "boss" and props.get("nodes"):
        out += make_nodes(out[0], props["nodes"])
    return out


def make_nodes(boss: Mon, cfg: dict, n: Optional[int] = None) -> list[Mon]:
    """晶母身上的晶簇（dungeon.spawn_nodes）：不出手，活着的时候头目防御 +while_alive.def，头目一倒就散"""
    hp = max(R.SCALE, round(boss.max_hp * cfg.get("hp_pct", 0.08)))
    return [Mon("晶簇", hp, hp, 0, 0, boss.depth, {"inert": True, "node": True}, "normal", minion=True, theme=boss.theme)
            for _ in range(cfg.get("count", 3) if n is None else n)]


def pick_target(mon: Mon, heroes: list[Hero], hits: dict) -> Hero:
    """engine._pick_target：野兽扑血最少的；怕光的躲开拿火把的；远程的先射拿火把的；别的先打上一轮打它的人"""
    live = [h for h in heroes if not h.down]
    if mon.props.get("animal"):
        return min(live, key=lambda h: (h.hp / h.max_hp, random.random()))
    cands = live
    if mon.props.get("light_averse") and (dark := [h for h in live if not h.torch_light]):
        cands = dark
    elif mon.props.get("ranged") and (lit := [h for h in live if h.torch_light]):
        cands = lit
    if (h := hits.get(id(mon))) is not None and h in cands:
        return h
    return random.choice(cands)


class Fight:
    def __init__(self, heroes: list[Hero], mons: list[Mon], light: int, cover: bool, depth: int):
        # 沉睡的石像：只有吵起来才醒（说话、砸东西、被撞倒），安静地打大约一半的仗它一直睡着
        mons = [m for m in mons if not (m.props.get("dormant") and random.random() < DORMANT_SLEEPS)] or mons[:1]
        self.heroes, self.mons, self.base_light, self.cover, self.depth = heroes, mons, light, cover, depth
        self.dist = {(id(h), id(m)): 2 for h in heroes for m in mons}      # START_DISTANCE
        self.hits: dict = {}
        self.rounds = 0
        for h in heroes:
            h.struck, h.reloaded_free, h.backs = False, False, 0

    def light(self) -> int:
        torch = max([h.torch_light for h in self.heroes if not h.down] or [0])
        if self.fog:
            torch = round(torch * self.fog.get("torch_mult", 0.5))       # 浓雾里火把只照得见一半
        return max(0, min(100, self.base_light + self.shift + torch))

    def set_fog(self, fog: Optional[dict]) -> None:
        """浓雾：一开战怪就在一步之遥"""
        self.fog = fog
        if fog:
            for key in self.dist:
                self.dist[key] = min(self.dist[key], fog.get("start_distance", 1))

    def sneak_open(self) -> None:
        """潜行打法（engine 第一批改完的规则）：房间里没有警觉的怪才能偷袭，每人开场一下（隐匿对暗杀难度）；
        致命的普通怪直接死，精英、头目改成重伤 ×2；偷袭完这个房间就戒备了，之后正常打"""
        live = self.alive()
        if not live or any(m.props.get("keen") or m.rank == "boss" for m in live):
            return
        for h in self.heroes:
            live = self.alive()
            if not h.sneak or h.down or not live:
                continue
            diff = 4 + round(R.steps(self.depth, 6))
            if random.random() >= R.skill_chance(h.stealth, diff):
                continue                          # 没成：被发现，正常开打
            m = min(live, key=lambda x: x.hp)
            self.sneaks = getattr(self, "sneaks", 0) + 1
            heavy = R.hurt_npc_by(power(h, None) + random.randint(30, 60), m.df)
            if m.rank == "elite":
                m.hp -= 2 * heavy
            elif random.random() < SNEAK_LETHAL:
                m.hp = 0
            else:
                m.hp -= heavy
            for key in list(self.dist):
                if key[0] == id(h) and key[1] == id(m):
                    self.dist[key] = 0            # 偷袭得贴上去

    def alive(self) -> list[Mon]:
        return [m for m in self.mons if m.hp > 0]

    # ---- 玩家 ----
    def hurt_hero(self, h: Hero, dmg: int) -> None:
        if VARIANT["dmg"] != 1.0:
            x = dmg * VARIANT["dmg"]
            dmg = int(x) + (random.random() < x - int(x))
        if dmg >= h.hp and "bookmark" in h.perks and "bookmark" not in h.floor_used:
            h.floor_used.add("bookmark")        # 守护书签：每层挡一次致命
            return
        h.hp -= dmg
        h.taken += dmg
        if h.hp <= 0:
            h.hp, h.down = 0, True

    def drink(self, h: Hero) -> bool:
        if h.potions:
            h.potions -= 1
            h.drank += 1
            h.hp = min(h.max_hp, h.hp + heal_scale(h, R.heal_amount(ITEMS[POTION]["heal"], h.max_hp)))
            return True
        if "flask" in h.perks and "flask" not in h.trip_used:
            h.trip_used.add("flask")            # 麦琪的私酿：回满，这一层攻击 +25%
            h.hp, h.cheer = h.max_hp, True
            return True
        if h.herbs:
            h.herbs -= 1
            h.hp = min(h.max_hp, h.hp + heal_scale(h, R.heal_amount(HERB_HEAL, h.max_hp)))
            return True
        return False

    fog: Optional[dict] = None          # 浓雾（迷雾葬海）
    star: Optional[dict] = None         # 星光周期（星界秘境）
    period: int = 0
    edge: Optional[dict] = None         # 虚空边缘的房间（theme.void）
    shift: int = 0                      # 梦境的光亮浮动、星光明暗（engine env.shift）
    jitter: int = 0
    dislocate: int = 0
    turns: int = 0
    revived: bool = False
    sand: Optional[dict] = None         # 流沙（沙漠迷城 ground: sand 的房间、法老转阶段）
    glare: Optional[dict] = None        # 炫光（水晶洞窟）：光亮到 at 以上每个敌人回合 chance 几率看不清
    heat: float = 0.0                   # 灼热（地底熔炉）：每个动作掉血量上限的这个比例
    heat_mult: float = 1.0

    def tick_action(self, h: Hero) -> None:
        """流血：每个动作前掉血；灼热的楼层每个动作也烤掉一点（engine._heat，不看防御）"""
        if self.heat and not h.down:
            h.hp -= max(1, round(h.max_hp * self.heat * self.heat_mult))
            if h.hp <= 0:
                h.hp, h.down = 0, True
                return
        b = h.effects.get("bleed")
        if b:
            if b["left"] <= 0:
                del h.effects["bleed"]
            else:
                self.hurt_hero(h, b["value"])
                b["left"] -= 1

    def tick_turn(self, h: Hero) -> None:
        """中毒、看不清、腐蚀、重伤：每回合"""
        for kind in ("poison", "blind", "corrode", "wound", "silence"):
            e = h.effects.get(kind)
            if not e:
                continue
            if e["left"] <= 0:
                if e.get("hp"):
                    h.max_hp += e["hp"]
                del h.effects[kind]
                continue
            if kind == "poison" and not h.down:
                self.hurt_hero(h, e["value"])
            e["left"] -= 1

    def strike_on(self, h: Hero) -> bool:
        """有头目预告了冲着他来的蓄力重击"""
        for m in self.alive():
            if m.pending is not None:
                then = m.skills[m.pending["i"]].get("then") or {}
                if then.get("do") == "strike" and h in self.boss_targets(m, then.get("target", "highest_threat")):
                    return True
        return False

    def flash_on(self, unless: str = "eyes_closed") -> bool:
        """有头目预告了闭眼（掐自己）能躲的招"""
        return any(m.pending is not None and (m.skills[m.pending["i"]].get("then") or {}).get("unless") == unless
                   for m in self.alive())

    def boss_targets(self, m: Mon, mode: str) -> list:
        live = [h for h in self.heroes if not h.down]
        if not live or mode == "all":
            return live
        if mode == "marked" and m.mark in live:
            return [m.mark]
        if mode == "lowest_hp":
            return [min(live, key=lambda h: h.hp / h.max_hp)]
        return [max(live, key=lambda h: m.threat.get(id(h), 0))]

    def target(self, h: Hero) -> Mon:
        """先打治疗的，再打远程的，再打血少的"""
        live = [m for m in self.alive() if not (m.props.get("only_dark") and not self.dark())] or self.alive()
        if VARIANT["focus_boss"] and any(m.minion for m in live) and (lead := [m for m in live if m.rank in ("boss", "elite")]):
            return min(lead, key=lambda m: m.hp)     # 知道"主子一死帮手就跑"的人：先打头目
        return min(live, key=lambda m: (not m.props.get("healer"), not m.props.get("ranged"), m.hp))

    def hero_turn(self, h: Hero) -> None:
        self.tick_turn(h)
        if h.down:
            return
        if h.status:
            st = h.status
            if st["kind"] == "incapacitated":
                if random.random() < R.escape_chance(st["escape"], st["attempts"]):
                    h.status, h.guard = None, st["kind"]
                else:
                    st["attempts"] += 1
                return
        acts = R.ENEMY_EVERY
        while acts > 0 and not h.down and self.alive():
            self.tick_action(h)
            if h.down:
                return
            acts -= 1
            if h.status:                         # 绊倒：爬起来；缠住：挣脱
                st = h.status
                if st["kind"] == "prone" or random.random() < R.escape_chance(st["escape"], st["attempts"]):
                    h.status, h.guard = None, st["kind"]
                else:
                    st["attempts"] += 1
                continue
            flyer = next((x for x in self.alive() if x.flying), None)
            if flyer:
                # 飞鞋：做什么都被俯冲打断（挨一下 ×0.5）；普通档一半几率想到去抓他的脚踝（运动判定大约五五开）
                if random.random() < h.smart and random.random() < GROUND_CHANCE:
                    flyer.flying, flyer.grounded = 0, True
                else:
                    self.strike(flyer, h, 1.0, ranged=False, mult=0.5)
                continue
            if h.hp < h.max_hp * DRINK_BELOW and self.drink(h):
                h.sin = min(6, h.sin + 1) if any(x.props.get("sin") for x in self.alive()) else h.sin
                continue
            if not h.pinch and self.flash_on("pinch") and random.random() < h.smart:
                h.pinch = True                   # 掐自己一把，不睡过去
                continue
            if not h.eyes and self.flash_on() and random.random() < h.smart:
                h.eyes = True                    # 看见晶簇一齐亮起：闭眼背过身，这一轮自己也看不清
                h.effects.setdefault("blind", {"value": 1, "left": 1})
                continue
            if not h.dodging and self.strike_on(h) and random.random() < h.smart:
                h.dodging = True                 # 看见大招冲着自己来：这一下用来闪避
                h.sin = min(6, h.sin + 1) if any(x.props.get("sin") for x in self.alive()) else h.sin
                continue
            live = self.alive()
            # 回礼：诺艾尔的书（每层一次，全场定住）、一口倒（每趟一次，放倒一只）
            ok = [m for m in live if m.rank != "boss" or not m.held]
            if any(x.silence or x.props.get("silence_field") for x in live) or "silence" in h.effects:
                ok = []                          # 馆长禁了声、大祭司在场、自己中了禁声：书念不了
            if "tome" in h.perks and "tome" not in h.floor_used and ok and (len(ok) >= 2 or ok[0].rank == "boss"):
                h.floor_used.add("tome")
                for m in ok:
                    m.status = {"escape": 3, "attempts": 0}
                    m.held += 1
                continue
            if "drug" in h.perks and "drug" not in h.trip_used and live[0].rank in ("boss", "elite") \
                    and (big := max(live, key=lambda m: m.max_hp)).status is None and not (big.rank == "boss" and big.held):
                h.trip_used.add("drug")
                big.status = {"escape": 2, "attempts": 0}
                big.held += 1
                continue
            m = self.target(h)
            key = (id(h), id(m))
            d = self.dist[key]
            # 只带远程武器的：近战怪贴上来先退开两步再射（真人会这么打；远程怪贴不贴无所谓，它们自己会跳开）
            if not h.melee and h.ranged and not h.ranged.steady and d == 0 and not m.props.get("ranged"):
                self.dist[key] = 2
                continue
            shooter = h.ranged if h.ranged and (d > 0 or not h.melee or h.ranged.steady) else None
            if shooter and not h.loaded:
                if not h.melee or d > 0:
                    # 装填（绞盘重弩要摇两次）；被贴身就边退一步边装（一场最多 RELOAD_BACKS 步）
                    close = [x for x in self.alive() if self.dist[(id(h), id(x))] == 0
                             and not (VARIANT["reload_melee_only"] and x.props.get("ranged"))]
                    if close and h.backs < R.RELOAD_BACKS:
                        h.backs += 1
                        for x in close:
                            self.dist[(id(h), id(x))] = VARIANT["reload_back"]
                    h.wound += 1
                    if h.wound >= shooter.reload_steps:
                        h.loaded, h.wound = True, 0
                    continue
                shooter = None
            if self.sand and h.still >= self.sand.get("sink_after", 1) and not h.stepped and random.random() < h.smart:
                h.stepped = True                 # 流沙里挪一步，免得陷进去
                continue
            if not shooter and d > 0:
                h.stepped = True
                if self.sand and random.random() < self.sand.get("stuck_chance", 0.3):
                    continue                     # 陷在沙里没挪动
                self.dist[key] = max(0, d - (self.sand.get("move_max", 1) if self.sand else 2))     # 冲上去（MAX_STEP）
                continue
            self.attack(h, m, shooter, d)

    def attack(self, h: Hero, m: Mon, shooter: Optional[Weapon], d: int) -> None:
        if shooter:
            base = R.STEADY_HIT if shooter.steady else R.RANGED_HIT.get(d, R.RANGED_HIT[max(R.RANGED_HIT)])
            base -= R.RANGED_COVER if self.cover else 0
            if self.fog and d >= 2:
                base *= self.fog.get("ranged_far_mult", 0.5)
            if not shooter.free_reload or h.reloaded_free:
                h.loaded = False
            else:
                h.reloaded_free = True
        else:
            base = R.MELEE_HIT.get(d, 0)
        chance = R.light_hit(self.light(), base) * (R.BLIND_HIT if "blind" in h.effects else 1)
        if (ev := m.props.get("dark_evasion")) and self.dark():
            chance *= 1 - ev
        chance -= R.POISON_HIT if "poison" in h.effects else 0
        first = not h.struck
        h.struck = True
        if random.random() >= max(0.0, chance):
            return
        if m.vanished:
            m.vanished = False
            if random.random() > HEAR_CHANCE:
                return                           # 隐身：扑了个空（察觉没过）
        bonus = shooter.first_bonus if shooter and first else 0
        pierce = shooter.pierce if shooter else 0
        guard = (m.props["nodes"].get("while_alive", {}).get("def", 0)
                 if m.props.get("nodes") and any(x.props.get("node") for x in self.alive()) else 0)
        dmg = R.hurt_npc_by(power(h, shooter) + bonus + R.whole(h.gem_bonus),
                            m.df + guard + stance_of(m).get("def", 0) - pierce - R.whole(h.gem_pierce))
        if shooter and (ref := m.props.get("reflect_ranged")) and random.random() < ref:
            self.hurt_hero(h, max(R.SCALE, dmg // 2))      # 棱镜元素把这一箭折了回来
            return
        if m.shield > 0:
            m.shield -= 1                        # 光膜挡下这一下
            return
        if h.gem_crit and random.random() < h.gem_crit:
            dmg *= 2                             # 锋墨石会心一击
        if "bleed" in h.effects:
            dmg = max(R.SCALE, math.floor(dmg * R.BLEED_DAMAGE))
        if m.props.get("swarm"):
            dmg = max(R.SCALE, dmg // 2)         # 虫群：单体只拍死一小片
        if m.grounded:
            dmg = round(dmg * m.props.get("grounded_weak", 1.5))
        if m.props.get("lure") and not m.lured:
            m.lured = True
            if random.random() < 0.7:
                return                           # 打在灯上
        weak = m.props.get("weak")
        if (weak == "pierce" and pierce) or (weak in ("light", "bright") and self.light() >= R.LIGHT_BRIGHT):
            dmg = math.ceil(dmg * R.WEAK_MULT)
        m.hp -= dmg
        m.threat[id(h)] = m.threat.get(id(h), 0) + dmg
        if m.hp <= 0 and m.props.get("leader"):
            for x in self.alive():
                if x.props.get("pack") == m.props["pack"]:
                    x.hp = 0                     # 头领倒了，狗狼夹着尾巴跑
        if m.hp <= 0 and m.rank in ("boss", "elite") and not any(x.rank in ("boss", "elite") for x in self.alive()):
            for x in self.alive():
                if x.minion:
                    x.hp = 0                     # 主子倒了，帮手四散逃走
        if not shooter and m.hp > 0 and (thorns := m.props.get("thorns")) and random.random() < m.props.get("thorns_chance", 1.0):
            self.hurt_hero(h, thorns)
        self.hits[id(m)] = h
        # 被定住、迷倒的挨打时掷一次挣脱（engine._npc_counter）
        if m.hp > 0 and m.status and not m.status.get("freed"):
            if random.random() < R.escape_chance(m.status["escape"], m.status["attempts"]):
                m.status = {"freed": True}
            else:
                m.status["attempts"] += 1

    # ---- 怪 ----
    def boss_turn(self, m: Mon) -> bool:
        """engine._boss_turn：该放技能就放，返回这次出手是不是用掉了"""
        targets = [h for h in self.heroes if not h.down]
        if not m.skills or not targets:
            return False
        acts = m.acts
        m.acts += 1
        if m.flying:
            m.flying -= 1
        m.grounded = False
        phases = m.props.get("phases")
        if phases and m.gphase + 1 < len(phases) and m.hp / m.max_hp < phases[m.gphase + 1]["at"]:
            m.gphase += 1
            ph = phases[m.gphase]
            m.skills, m.pending, m.used, m.rnd = list(ph["skills"]), None, set(), {}
            m.extra_acts += ph.get("actions", 0)
            m.phase_atk += ph.get("atk", 0)
            if ph.get("on_hit"):
                m.props = {**m.props, "on_hit": ph["on_hit"]}
            if "light" in (ph.get("env") or {}):
                self.base_light = max(0, min(100, self.base_light + ph["env"]["light"]))
            if sm := ph.get("summon"):
                for _ in range(sm.get("count", 1)):
                    for x in spawn(m.depth, sm["kind"], "normal", m.theme, 1, 1, False, minion=True):
                        self.mons.append(x)
                        for h in self.heroes:
                            self.dist[(id(h), id(x))] = 2
        mems = m.props.get("memories") or []
        if m.memory < len(mems) and m.hp / m.max_hp < mems[m.memory]["at"]:
            r = mems[m.memory].get("room") or {}
            m.memory += 1
            self.base_light = 40 + dungeon.LIGHT_OFFSET.get(r.get("light", "dim"), 0)
            self.cover, self.heat, self.shift, self.jitter = bool(r.get("cover")), r.get("heat", 0), 0, 0
        if m.silence:
            m.silence -= 1
        if m.regen and m.hp < m.max_hp:
            m.hp = min(m.max_hp, m.hp + math.ceil(m.max_hp * m.regen))
        if st := m.props.get("stances"):
            m.stance = m.stance or st.get("start", "cold")
            if acts - m.stance_at >= st.get("every", 3):
                m.stance, m.stance_at = ("molten" if m.stance == "cold" else "cold"), acts
        if fare := m.props.get("fare"):
            if m.fare_due:
                m.fare_due = False
                if random.random() >= min(h.smart for h in targets):
                    self.skill_effect(m, fare["refuse"], targets)
                return True
            m.fare_acts += 1
            if m.fare_acts % fare.get("every", 4) == 0:
                m.fare_due = True
                return True
        if m.pending is not None:
            pend, m.pending = m.pending, None
            if pend["hp"] - m.hp >= math.ceil(m.max_hp * R.INTERRUPT_SHARE):
                return True                     # 被打断了
            self.skill_effect(m, m.skills[pend["i"]]["then"], targets) if pend["i"] < len(m.skills) else None
            return True
        skills = []
        for k, x in enumerate(m.skills):
            if x.get("when") == "every_random":
                m.rnd.setdefault(k, acts + random.randint(*x["value"]))
                x = {**x, "when": "at_act", "value": m.rnd[k]}
            skills.append(x)
        i = R.due_skill(skills, m.hp / m.max_hp, acts, m.used, m.phased, self.dark(), self.light())
        if i is None:
            return False
        sk = m.skills[i]
        if sk.get("when") == "every_random":
            m.rnd[i] = acts + random.randint(*sk["value"])
        if sk.get("when") in ("hp_below", "fight_start"):
            m.used.add(i)
        if sk["do"] == "telegraph":
            m.pending = {"i": i, "hp": m.hp}
            return True
        if sk["do"] == "mark":
            m.mark, m.mark_bonus = random.choice(targets), sk.get("bonus", 2)
            return False
        if sk["do"] == "silence":
            m.silence = sk.get("turns", 2)
            return True
        self.skill_effect(m, sk, targets)
        return True

    def skill_effect(self, m: Mon, sk: dict, targets: list) -> None:
        do = sk["do"]
        if do == "vanish":
            m.vanished, m.sneak = True, sk.get("sneak_mult", 1.8)
            return
        if do == "flight":
            m.flying = sk.get("rounds", 2) + 1
            return
        if do == "petrify":
            for h in targets:
                if not h.eyes:
                    self.afflict(h, "stun", m.depth, {"escape": 1})
            return
        if do == "summon":
            room = R.SUMMON_MAX - sum(1 for x in self.alive() if x.minion)
            elite = sk.get("elite") or m.depth >= R.ELITE_MINIONS_FROM
            for _ in range(max(0, min(R.summon_count(sk, m.depth), room))):
                for x in spawn(m.depth, sk["kind"], "elite" if elite else "normal", m.theme, 1, 1, False,
                               minion=True, affix="plain" if elite else None):
                    self.mons.append(x)
                    for h in self.heroes:
                        self.dist[(id(h), id(x))] = 2
        elif do in ("status_all", "effect_all"):
            for h in targets:
                self.afflict(h, sk["kind"], m.depth, sk, R.expected_hit(m.atk, hero_def(h), m.depth))
        elif do == "self_heal":
            m.hp = min(m.max_hp, m.hp + math.ceil(m.max_hp * sk.get("heal", 0.2)))
        elif do == "strike":
            atk = m.atk + m.phase_atk
            for h in self.boss_targets(m, sk.get("target", "highest_threat")):
                if sk.get("only_below") and h.hp / h.max_hp >= sk["only_below"]:
                    continue                     # 斩首：没伤得够重就落空
                dmg = round(R.hurt_player_by(atk, hero_def(h), m.depth) * sk.get("mult", 2.0))
                if sk.get("per_sin"):
                    dmg = round(dmg * (1 + sk["per_sin"] * h.sin))
                    if (cv := sk.get("convict")) and h.sin >= cv.get("sin_at_least", 4):
                        self.afflict(h, "wound", m.depth, cv["effect"])
                    h.sin //= 2
                if h.dodging or (sk.get("cover_halves") and self.cover):
                    dmg //= 2
                self.hurt_hero(h, max(R.SCALE, dmg))
                for e in sk.get("effects", []):
                    self.afflict(h, e["kind"], m.depth, e, dmg)
            if sk.get("target") == "marked":
                m.mark = None
        elif do == "combo":
            for h in self.boss_targets(m, sk.get("target", "all")):
                if (sk.get("unless") == "eyes_closed" and h.eyes) or (sk.get("unless") == "pinch" and h.pinch):
                    continue
                hard = False
                for e in sk.get("effects", []):
                    k = e["kind"]
                    if k in ("restrained", "prone", "stun"):
                        if hard:
                            continue
                        hard = True
                    if k == "silence":
                        m.silence = e.get("turns", 2)
                    elif k == "mark":
                        m.mark, m.mark_bonus = h, e.get("bonus", 2 * R.SCALE)
                    else:
                        self.afflict(h, k, m.depth, e, R.expected_hit(m.atk, hero_def(h), m.depth))
        elif do == "phase":
            m.phased = True
            m.extra_acts += sk.get("actions", 0) or sk.get("actions_add", 0)
            if (sk.get("env") or {}).get("ground") == "sand":
                self.sand = THEMES[m.theme].get("sand") or {"move_max": 1, "stuck_chance": 0.3, "sink_after": 1}
            m.phase_atk += sk.get("atk", 0)
            m.regen = sk.get("regen", m.regen)
            if "light" in (sk.get("env") or {}):
                self.base_light = max(0, min(100, self.base_light + sk["env"]["light"]))
            if "heat_mult" in (sk.get("env") or {}):
                self.heat_mult = sk["env"]["heat_mult"]
            if "star_cycle_period" in (sk.get("env") or {}):
                self.period = sk["env"]["star_cycle_period"]
            if "fog_start_distance" in (sk.get("env") or {}):
                for key in self.dist:
                    self.dist[key] = min(self.dist[key], sk["env"]["fog_start_distance"])
            if (cfg := m.props.get("nodes")) and cfg.get("regrow_on_phase"):
                have = sum(1 for x in self.alive() if x.props.get("node"))
                for x in make_nodes(m, cfg, max(0, cfg.get("count", 3) - have)):
                    self.mons.append(x)
                    for h in self.heroes:
                        self.dist[(id(h), id(x))] = self.dist[(id(h), id(m))]
            if then := sk.get("then"):
                self.skill_effect(m, then, targets)

    def dark(self) -> bool:
        return bool(self.star) and self.shift < 0

    def void_fall(self, h: Hero, add: int = 0) -> None:
        """虚空边缘：被撞倒、推到边上，运动没过就坠下去（掉血量上限的 15%，回到旁边的房间重新走过来：这一轮白费，距离回到 2）"""
        if not self.edge or h.down or random.random() < VOID_KEEP - 0.15 * add:
            return
        self.hurt_hero(h, max(R.SCALE, round(h.max_hp * self.edge.get("fall_damage_pct", 0.15))))
        h.status = None
        for key in self.dist:
            if key[0] == id(h):
                self.dist[key] = 2

    def room_turn(self) -> None:
        self.turns += 1
        if self.star:
            self.shift = self.star.get("swing", 35) * (1 if (self.turns // (self.period or self.star.get("period", 4))) % 2 == 0 else -1)
        if self.jitter:
            self.shift = random.randint(-self.jitter, self.jitter)
        if self.dislocate and self.turns % self.dislocate == 0:
            for key in self.dist:
                self.dist[key] = random.randint(0, 2)

    def enemies_turn(self) -> None:
        for m in self.alive():
            if m.props.get("inert") or (m.props.get("only_dark") and not self.dark()):
                continue
            if m.status:
                if m.status.get("freed") or m.rank == "boss":      # 刚挣开的这轮来不及还手；头目只困一轮
                    m.status = None
                continue
            acted = self.boss_turn(m)
            for k in range(m.attacks + m.extra_acts):
                if acted and k < m.base_attacks:
                    continue                     # 主动作放了技能，副动作照打
                if all(h.down for h in self.heroes):
                    return
                if k >= m.base_attacks and random.random() >= m.props.get("extra_chance", 1):
                    continue
                self.enemy_act(m, pick_target(m, self.heroes, self.hits),
                               m.props.get("extra_mult", 1.0) if k >= m.base_attacks else 1.0)
        for h in self.heroes:
            h.guard = None
            h.dodging = False
            if self.sand and not h.down:
                h.still = 0 if h.stepped else h.still + 1
                if h.still > self.sand.get("sink_after", 1):
                    h.still = 0
                    self.afflict(h, "restrained", 0, {"escape": 1})      # 陷进流沙
            h.stepped = False
            if self.glare and not h.down and not h.eyes and self.light() >= self.glare.get("at", 70) \
                    and random.random() < self.glare.get("chance", 0.25) and "blind" not in h.effects:
                h.effects["blind"] = {"value": 1, "left": 1}       # 晶面反光晃眼（engine._glare）
            h.eyes = h.pinch = False
        self.room_turn()

    def enemy_act(self, m: Mon, h: Hero, mult: float = 1.0) -> None:
        props = m.props
        if pull := props.get("pull"):
            m.pull_acts += 1
            if m.pull_acts % pull.get("every", 3) == 0:
                if self.edge:
                    self.void_fall(h)
                else:
                    for key in self.dist:
                        if key[0] == id(h):
                            self.dist[key] = min(2, self.dist[key] + 1)
                return
        if (share := props.get("revive_once")) and not self.revived:
            dead = [x for x in self.mons if x.hp <= 0 and not x.minion and x is not m]
            if dead:
                self.revived = True
                dead[-1].hp = max(1, round(dead[-1].max_hp * share))
                return
        if bell := props.get("bell"):
            m.bell_acts += 1
            if m.bell_acts % bell.get("every", 3) == 0:
                for x in self.alive():
                    x.rung += bell.get("atk", R.SCALE)      # 敲钟人摇铃：全场的怪这一场攻击加一截
                return
        if sh := props.get("shield_allies"):
            m.shield_acts += 1
            bare = [n for n in self.alive() if n is not m and not n.props.get("inert") and n.shield <= 0]
            if m.shield_acts % sh.get("every", 3) == 0 and bare:
                random.choice(bare).shield = sh.get("absorb", 1)
                return
        if (share := props.get("healer")) and random.random() < R.HEALER_CHANCE and m.heals < R.HEALER_MAX:
            hurt = [n for n in self.alive() if n is not m and n.hp < n.max_hp * R.HEALER_BELOW]
            if hurt:
                n = min(hurt, key=lambda n: n.hp / n.max_hp)
                n.hp = min(n.max_hp, n.hp + max(1, math.ceil(R.monster_stats(m.depth, {})[0] * share)))
                m.heals += 1
                return
        key = (id(h), id(m))
        d = self.dist[key]
        pinned = self.hits.get(id(m)) is h
        if props.get("ranged"):
            if d == 0 and not pinned and m.backs < R.RANGED_BACKS:
                m.backs += 1
                self.dist[key] = 1
                return
            chance = R.ENEMY_RANGED_HIT.get(d, R.ENEMY_RANGED_HIT[max(R.ENEMY_RANGED_HIT)])
            chance -= R.RANGED_COVER if self.cover else 0
            if self.fog and d >= 2:
                chance *= self.fog.get("ranged_far_mult", 0.5)
            if not h.torch_light:
                chance = R.light_hit(self.light(), chance)
            self.strike(m, h, chance, ranged=True, mult=mult)
            return
        if d > 0:
            d = self.dist[key] = d - 1
        if d in R.MELEE_HIT:
            self.strike(m, h, R.MELEE_HIT[d], ranged=False, mult=mult)

    def strike(self, m: Mon, h: Hero, chance: float, ranged: bool, mult: float = 1.0) -> None:
        if random.random() >= chance:
            return
        if random.random() < min(h.block, R.AVOID_CAP):
            return                              # 用盾挡下了这一击
        light = self.light()
        atk = m.atk + R.dark_attack(light) - (R.SCALE if m.props.get("light_averse") and light >= R.LIGHT_BRIGHT else 0)
        ratio = m.hp / m.max_hp
        if m.props.get("frenzy") and ratio < 0.5:
            atk += m.props["frenzy"]
        if m.rank == "boss" and R.enraged(m.depth, ratio):
            atk += R.ENRAGE_ATK
        atk += m.phase_atk + stance_of(m).get("atk", 0) + m.rung
        if mirror := m.props.get("mirror_attack"):
            atk = round(power(h, None) * mirror)
        marked = m.mark is h
        if marked:
            atk += m.mark_bonus
        dmg = R.hurt_player_by(atk, hero_def(h), m.depth, guard=h.guard_ranged if ranged else 0)
        if m.sneak:
            mult *= m.sneak                      # 隐身以后从看不见的地方出手
            m.sneak = 0.0
        if m.props.get("ambush") and not m.ambushed:
            m.ambushed = True
            mult *= m.props["ambush"]            # 雾鳗从雾里扑出来的第一口
        dmg = R.scale_damage(dmg, m.props.get("dmg_mult", 1) * mult)
        self.hurt_hero(h, dmg)
        if marked:
            m.mark = None
        if (steal := m.props.get("lifesteal")):
            m.hp = min(m.max_hp, m.hp + R.whole(dmg * steal))
        if h.down or not (hit := m.props.get("on_hit")) or random.random() >= hit.get("chance", 0.25):
            return
        self.afflict(h, hit["kind"], m.depth, hit, dmg)

    def afflict(self, h: Hero, kind: str, depth: int, hit: dict, base: Optional[float] = None) -> None:
        """怪打中附带的、头目全场放的效果（engine._inflict）：中毒流血按那一下的伤害（rules.dot_value），
        重复中招只延长一回合；刚挣脱、爬起来的这个敌人回合不再被同一种控制打中"""
        if h.down:
            return
        if kind == "charm":
            for key in self.dist:
                if key[0] == id(h):
                    self.dist[key] = min(2, self.dist[key] + 1)      # 迷迷糊糊往雾里走了一步
            return
        if kind == "dispel":
            h.cheer = False                      # 梦食貘把私酿的劲头吸走了
            return
        if kind == "wrapped":
            kind = "restrained"                  # 裹尸布：缠住的一种（药也喝不了，本来缠住就用不了东西）
        if kind in ("restrained", "prone", "stun"):
            k = "incapacitated" if kind == "stun" else kind
            if not h.status and h.guard != k:
                h.status = {"kind": k, "escape": hit.get("escape", 2), "attempts": 0}
                if k == "prone":
                    self.void_fall(h, hit.get("void_difficulty", 0))
            return
        full = hit.get("turns") or R.EFFECT_TURNS[kind]
        if kind in h.effects:
            h.effects[kind]["left"] = min(full, h.effects[kind]["left"] + 1)
            return
        e = {"value": R.dot_value(kind, base, depth, hit.get("value_mult", 1.0)) if kind != "wound"
             else round(100 * (hit.get("heal_mult") if hit.get("heal_mult") is not None else 0.5)), "left": full}
        if kind == "corrode":
            e["hp"] = min(h.max_hp - 1, max(1, round(h.max_hp * R.CORRODE_HP)))
            h.max_hp -= e["hp"]
            h.hp = min(h.hp, h.max_hp)
        h.effects[kind] = e

    def run(self) -> str:
        """打到一边倒下；返回 "win" / "wipe" / "stall" """
        while self.rounds < ROUND_CAP:
            self.rounds += 1
            for h in self.heroes:
                if self.alive():
                    self.hero_turn(h)
            if not self.alive():
                return "win"
            self.enemies_turn()
            if all(h.down for h in self.heroes):
                return "wipe"
        return "stall"


# ============ 一层、一趟 ============

@dataclass
class FloorStat:
    reached: int = 0
    died: int = 0                       # 人次（两人队算两个人）
    people: int = 0
    taken: float = 0.0                  # 掉的血 / 血量上限
    potions: float = 0.0
    fights: int = 0
    rounds: int = 0
    stalls: int = 0


def stealth_level(depth: int) -> int:
    """隐匿等级（照玩家A：第 12 层 3 级、第 13 层 4 级），每 4 层大约一级"""
    return min(8, 1 + depth // 4)


DORMANT_SLEEPS = 0.5                    # 静默神殿：一场仗里沉睡的石像一直没被吵醒的几率
GROUND_CHANCE = 0.55                    # 飞鞋：抓脚踝、甩绳子把他拽下来的运动判定
HEAR_CHANCE = 0.35                      # 隐身：察觉判定听声辨位的几率（深层角色察觉不高）
VOID_KEEP = 0.55                        # 虚空边缘被撞倒时稳住的几率（运动对难度 2 + 层数/10，深层角色大约五五开）
HEAT_WALK, HEAT_QUENCH = 10, 8          # 灼热的一层战斗外大约几个动作、两瓶泉水压住几个
SNEAK_LETHAL = 0.7                      # 偷袭时 AI 判成致命的比例（其余按重伤）
SNEAK_PAST = 0.85                       # 潜行打法溜过一个房间的几率 = SNEAK_PAST ^ (1 + 群数)：一群 72%、两群 61%、三群 52%
                                        # （照玩家A：第 35–41 层每层溜过 3–8 个房间，只打约 2.7 场）


def make_heroes(build: str, quality: str, trip: int, kit, size: int, depth: int) -> list[Hero]:
    if "+" in build:                        # 混编两人队：每人一种打法、一份自己的升级进度
        return [h for b, k in zip(build.split("+"), kit) for h in make_heroes(b, quality, trip, k, 1, depth)]
    heroes = []
    for _ in range(size):
        ws = melee_weapons(build, trip)
        if quality == "cap" and ws:
            ws = [CAP_WEAPON]                   # 单手一把头目武器（玩家A左手空着）
        if quality == "fav" and ws:
            ws = [("无铭", blade_damage(trip * TRIP))] + ws[1:]
        r = ranged_weapon(build, trip)
        wp = kit.weapon_plus
        if build in ("bow", "xbow"):
            r.damage += (wp[0] if wp else 0) * VARIANT["ranged_step"]
        elif build in ("hxb_sword", "mcb_sword"):
            r.damage += (wp[0] if wp else 0) * VARIANT["ranged_step"]
            ws = [(n, d + (wp[1] if len(wp) > 1 else 0) * R.UPGRADE_STEP["damage"]) for n, d in ws]
        else:
            ws = [(n, d + (wp[k] if k < len(wp) else 0) * R.UPGRADE_STEP["damage"]) for k, (n, d) in enumerate(ws)]
        armor = [d for _, d in ARMOR[quality][trip]]
        guard, block = 0, 0.0
        deep = DEEP_GEAR.get(trip) if quality in ("normal", "fav", "real") else None
        if deep:
            ws = [(n, max(d, deep["weapon"] + (d - DMG["steel_shortsword"]))) for n, d in ws]    # 换成深层武器，升级等级带过去
            armor = [max(a, b) for a, b in zip(armor, deep["armor"])] + armor[len(deep["armor"]):]
        if build == "sword_shield":
            armor.append(max(SHIELD[trip][1], deep["shield"]) if deep else SHIELD[trip][1])
            guard, block = SHIELD[trip][2], SHIELD[trip][3]
        defense = sum(d + (kit.armor_plus[k] if k < len(kit.armor_plus) else 0) * R.UPGRADE_STEP["defense"]
                      for k, d in enumerate(armor)) + kit.gem_def
        hp = endurance_hp(depth)
        perks = {"bookmark", "tome", "flask", "drug"} if quality == "fav" else set()
        heroes.append(Hero(hp, hp, 2 * R.SCALE, defense, ws, r, sneak=build == "dual_sneak", stealth=stealth_level(depth),
                           smart=1.0 if quality == "cap" else 0.5,
                           guard_ranged=guard, block=block, gem_bonus=kit.gem_bonus, gem_pierce=kit.gem_pierce, gem_crit=kit.gem_crit,
                           potions=kit.potions, trip_potions=kit.potions, perks=perks))
    return heroes


def pick_kind(kinds: list[str]) -> str:
    """按 dungeon.yaml 的 weight 抽（dungeon._pick_kind）"""
    return random.choices(kinds, [MONSTERS[k].get("weight", 1) for k in kinds])[0]


def mark_leader(mons: list) -> None:
    """房间里第一只狗狼是头领（dungeon._spawn_group lead）"""
    lead = next((x for x in mons if x.props.get("pack")), None)
    if lead:
        lead.props = {**lead.props, "leader": True}


def floor_rooms(depth: int, theme_key: str) -> list[tuple[str, list[tuple[str, str]], bool]]:
    """这一层要打的仗：[(房间种类, [(怪, 等级)], 有没有掩体)]，楼梯间最后"""
    theme = THEMES[theme_key]
    kinds = dungeon._kinds(theme, depth)
    rooms = []
    for _ in range(4):
        count = R.roll_groups(depth)
        groups, packs = [], 0
        for i in range(count):
            kind = pick_kind(kinds)
            for _ in range(5):
                if not MONSTERS[kind].get("pack") or packs < dungeon.PACK_MAX:
                    break
                kind = pick_kind(kinds)
            packs += bool(MONSTERS[kind].get("pack"))
            groups.append((kind, "elite" if i == 0 and random.random() < R.elite_chance(depth) else "normal"))
        rooms.append(("combat", groups, bool(random.choice(theme["rooms"]).get("cover"))))
    if random.random() < R.TREASURE_GUARD:
        rooms.append(("treasure", [(pick_kind(kinds), "elite" if random.random() < R.elite_chance(depth) else "normal")],
                      False))
    random.shuffle(rooms)
    boss = depth % R.BOSS_EVERY == 0
    gate = next((k for k, g in GATES.items() if g["theme"] == theme_key), None) if dungeon.gate_depth(depth) else None
    rooms.append(("stairs", [(gate or ("boss" if boss else pick_kind(kinds)), "boss" if boss else "elite")], False))
    return rooms


def run_trip(build: str, quality: str, trip: int, kit: Kit, size: int, stats: dict[int, FloorStat],
             room_log: Optional[dict] = None) -> None:
    depth0 = trip * TRIP + 1
    heroes = make_heroes(build, quality, trip, kit, size, depth0)
    recent: list[str] = []
    torch_age = 0
    for depth in range(depth0, depth0 + TRIP):
        live = [h for h in heroes if not h.down]
        if not live:
            return
        gate = random.choice(list(GATES)) if dungeon.gate_depth(depth) and GATES else None     # 第 25 层：关卡头目
        theme_key = GATES[gate]["theme"] if gate else dungeon._pick_theme(THEMES, depth, set(recent[-dungeon.THEME_GAP:]))
        recent.append(theme_key)
        st = stats.setdefault(depth, FloorStat())
        st.reached += 1
        st.people += len(live)
        for h in live:
            new_max = endurance_hp(depth)        # 耐性涨了，上限跟着涨
            h.hp += new_max - h.max_hp if not h.effects.get("corrode") else 0
            h.max_hp = new_max if not h.effects.get("corrode") else h.max_hp
            h.floor_used, h.cheer, h.taken, h.drank = set(), False, 0, 0
            if heat := THEMES[theme_key].get("heat", {}).get("pct", 0):
                # 战斗外大约 HEAT_WALK 个动作，两瓶泉水（冷却水池）各压住 4 个，剩下的每个掉 1%
                h.hp = max(1, h.hp - max(0, HEAT_WALK - HEAT_QUENCH) * max(1, round(h.max_hp * heat)))
            h.torch_light = (TORCH_LIT if torch_age == 0 else TORCH_DIM) if build == "sword_torch" else 0
        torch_age = 1 - torch_age               # 一支火把两层（点燃、弱光），第三层换新的
        camped = False
        for h in live:
            h.herbs += 1                         # 宝箱房的药草
            if random.random() < FORAGE_CHANCE:
                h.herbs += 1                     # 空房搜到的
        for kind, groups, cover in floor_rooms(depth, theme_key):
            if all(h.down for h in heroes):
                break
            if kind != "stairs" and all(h.sneak for h in heroes) \
                    and not any(MONSTERS[g].get("keen") for g, _ in groups if g in MONSTERS) \
                    and random.random() < SNEAK_PAST ** (1 + len(groups)):
                continue                        # 潜行溜过去了（钱和掉落也没了，经济表里没扣）
            if kind == "stairs" and gate:
                # 关卡层：前厅的传送石一亮就能回城补给，再传回前厅：满血、带满这一趟的药进场
                for h in heroes:
                    if not h.down:
                        h.hp, h.potions, h.effects, h.status = h.max_hp, max(h.potions, h.trip_potions), {}, None
            if kind == "stairs":
                # 楼梯间守着头目或精英：血量低于七成先整备，这层还没扎营就扎营，再不够就喝药
                # 头目层：楼梯间隔壁是休息点，能多扎一次营（不算这一层的次数，engine.do_camp）
                rests = ["floor"] * (not camped) + ["rest"] * (depth % R.BOSS_EVERY == 0)
                for which in rests:
                    if not any(not h.down and h.hp < h.max_hp * PREP_BELOW for h in heroes):
                        break
                    camped = camped or which == "floor"
                    ok = random.random() < R.skill_chance(SURVIVAL_LEVEL, 1 + depth // 3)
                    share = (R.CAMP_BASE + (R.CAMP_SURVIVAL if ok else 0)) * R.CAMP_EMPTY
                    for h in heroes:
                        if not h.down:
                            h.hp = min(h.max_hp, h.hp + round(h.max_hp * share))
                for h in heroes:
                    while not h.down and h.hp < h.max_hp * PREP_BELOW and (h.potions or h.herbs):
                        prep = Fight([h], [], 50, False, depth)
                        prep.drink(h)
            mons = []
            for monster, rank in groups:
                mons += spawn(depth, monster, rank, theme_key, size, len(groups), kind == "stairs")
            mark_leader(mons)
            light = room_light(THEMES[theme_key])
            fight = Fight([h for h in heroes if not h.down], mons, light, cover, depth)
            fight.heat = THEMES[theme_key].get("heat", {}).get("pct", 0)
            fight.glare = THEMES[theme_key].get("glare")
            fight.set_fog(THEMES[theme_key].get("fog"))
            fight.star = THEMES[theme_key].get("star_cycle")
            fight.shift = fight.star.get("swing", 35) if fight.star else 0
            fight.edge = THEMES[theme_key].get("void") if kind != "stairs" and edge_of(THEMES[theme_key]) else None
            fight.jitter = THEMES[theme_key].get("light_jitter", 0)
            fight.dislocate = (THEMES[theme_key].get("dislocate") or {}).get("every", 0)
            fight.sand = sand_of(THEMES[theme_key]) if kind != "stairs" else None
            fight.sneak_open()
            hp_before = {id(h): h.taken for h in fight.heroes}
            result = fight.run()
            st.fights += 1
            st.rounds += fight.rounds
            st.stalls += result == "stall"
            if room_log is not None:
                key = "boss" if groups[0][1] == "boss" else "ranged" if any(MONSTERS.get(g, {}).get("ranged") for g, _ in groups) \
                    else "healer" if any(MONSTERS.get(g, {}).get("healer") for g, _ in groups) else "melee"
                lost = sum(h.taken - hp_before[id(h)] for h in fight.heroes) / sum(h.max_hp for h in fight.heroes)
                room_log.setdefault((depth, key), []).append((lost, result == "wipe"))
            # 打完：倒下的队友急救（医药 0 级对难度 1），醒过来 1 点血
            standing = [h for h in fight.heroes if not h.down]
            if standing:
                for h in fight.heroes:
                    if h.down and random.random() < R.skill_chance(0, 1):
                        h.down, h.hp = False, R.SCALE
                        h.status, h.effects = None, {}
            for h in heroes:
                h.status = None
                h.loaded, h.wound = True, 0        # 打完顺手装好
                if not h.down and h.hp < h.max_hp * 0.3:
                    fight.drink(h)
            if not camped and any(not h.down and h.hp < h.max_hp * CAMP_BELOW for h in heroes):
                camped = True
                ok = random.random() < R.skill_chance(SURVIVAL_LEVEL, 1 + depth // 3)
                share = (R.CAMP_BASE + (R.CAMP_SURVIVAL if ok else 0)) * R.CAMP_EMPTY
                for h in heroes:
                    if not h.down:
                        for e in list(h.effects.values()):
                            h.max_hp += e.get("hp", 0)
                        h.effects = {}
                        h.hp = min(h.max_hp, h.hp + round(h.max_hp * share))
        for h in live:
            st.taken += h.taken / h.max_hp
            st.potions += h.drank
            if h.down:
                st.died += 1


# ============ 报表 ============

def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def gate_test(key: str, quality: str, depth: int = 25, n: int = 400, size: int = 1) -> tuple[float, float, float]:
    """关卡头目：带药进场（普通 4 瓶、上限 13 瓶）。返回 (挨的伤害占上限, 团灭率, 平均轮数)"""
    return fight_test("sword_shield", quality, min(TRIPS - 1, (depth - 1) // TRIP), depth, [(key, "boss")], GATES[key]["theme"],
                      size=size, n=n, potions=CAP_POTIONS if quality == "cap" else 4)


def fight_test(build: str, quality: str, trip: int, depth: int, groups: list[tuple[str, str]], theme: str,
               size: int = 1, n: int = 400, light: Optional[int] = None, affix: Optional[str] = None,
               potions: int = 0) -> tuple[float, float, float]:
    """单独一场：(平均掉血占上限, 团灭率, 平均轮数)。装备、升级按这一趟的经济结果，满血进场，默认不喝药（potions 给了就带着）"""
    lost = wiped = rounds = 0.0
    eco = [economy(quality, build) for _ in range(10)]
    boss = groups[0][1] == "boss"
    for i in range(n):
        heroes = make_heroes(build, quality, trip, eco[i % len(eco)][trip], size, depth)
        for h in heroes:
            h.potions = potions
        mons = []
        for monster, rank in groups:
            mons += spawn(depth, monster, rank, theme, size, len(groups), boss, affix=affix)
        mark_leader(mons)
        f = Fight(heroes, mons, room_light(THEMES[theme]) if light is None else light, False, depth)
        f.heat = THEMES[theme].get("heat", {}).get("pct", 0)
        f.glare = THEMES[theme].get("glare")
        f.set_fog(THEMES[theme].get("fog"))
        f.star = THEMES[theme].get("star_cycle")
        f.shift = f.star.get("swing", 35) if f.star else 0
        f.edge = THEMES[theme].get("void") if not boss and edge_of(THEMES[theme]) else None
        f.jitter = THEMES[theme].get("light_jitter", 0)
        f.dislocate = (THEMES[theme].get("dislocate") or {}).get("every", 0)
        f.sand = sand_of(THEMES[theme]) if not boss else None
        f.sneak_open()
        before = sum(h.taken for h in heroes)
        r = f.run()
        lost += (sum(h.taken for h in heroes) - before) / sum(h.max_hp for h in heroes)     # 挨的伤害合计（中途喝药、私酿回的不抵）
        wiped += r == "wipe"
        rounds += f.rounds
    return lost / n, wiped / n, rounds / n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trips", type=int, default=300, help="每种组合每一趟跑几次")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="balance/result.md")
    ap.add_argument("--dmg", type=float, default=1.0, help="怪打人的伤害倍数（对照版用 1.5）")
    ap.add_argument("--ranged-step", type=float, default=R.RANGED_UPGRADE_STEP, help="远程武器每级加多少伤害")
    ap.add_argument("--reload-back", type=int, default=1, help="装填时被贴身往后退几步")
    ap.add_argument("--reload-melee-only", action="store_true", help="只从近战怪身边退开（远程怪隔一格反而更准）")
    ap.add_argument("--gems", action="store_true", help="正常装备、好感全满按层数期望镶上宝石，回村拿一半余钱刷品质")
    ap.add_argument("--focus-boss", action="store_true", help="召唤战先打头目（知道帮手会跑的玩家）")
    args = ap.parse_args()
    VARIANT.update(dmg=args.dmg, ranged_step=args.ranged_step, reload_back=args.reload_back,
                   reload_melee_only=args.reload_melee_only, gems=args.gems, focus_boss=args.focus_boss)
    out = [f"# 模拟结果（怪伤害 ×{args.dmg:g}，远程每级 +{args.ranged_step:g}，{'镶宝石' if args.gems else '不镶宝石'}，"
           f"{'召唤战先打头目' if args.focus_boss else '先打血少的'}，每种组合每趟 {args.trips} 次，种子 {args.seed}）\n"]

    # ---- 经济 ----
    random.seed(args.seed)
    incomes = {d: sum(floor_income(d, 1, False) for _ in range(300)) / 300 for d in range(1, FLOORS + 1)}
    out.append("## 1. 经济（剑盾，单人，假设一路活着；钱一半升武器、一半升防具，失败不掉级、同一级连败有保底；"
               "矿石留到主武器成功率低于 40% 时垫上；真人档只拿一成的钱升武器）\n")
    out.append("每层收入（金币）：" + "、".join(f"{d} 层 {incomes[d]:.0f}" for d in range(1, FLOORS + 1)) + "\n")
    out.append("| 出发时 | 攒下的钱 | 带血药 | 花在升级 | 武器 | 防具（每件） |")
    out.append("|---|---|---|---|---|---|")
    for q in ("normal", "fav", "real"):
        eco = [economy(q, "sword_shield") for _ in range(100)]
        for t in range(TRIPS):
            def avg(f, t=t, eco=eco):
                return sum(f(e[t]) for e in eco) / len(eco)
            arm = [avg(lambda k_, k=k: k_.armor_plus[k]) for k in range(len(eco[0][t].armor_plus))]
            out.append(f"| {QUALITY[q]}·第 {t * TRIP + 1} 层 | {avg(lambda k: k.gold_left + k.spent_upgrade + k.potions * 24):.0f} | "
                       f"{avg(lambda k: k.potions):.1f} | {avg(lambda k: k.spent_upgrade):.0f} | "
                       f"+{avg(lambda k: k.weapon_plus[0]):.1f} | {' '.join(f'+{x:.1f}' for x in arm)} |")
    out.append("")

    # ---- 一趟一趟走 ----
    combos = []
    for q in ("poor", "normal", "fav"):
        for b in ("dual", "sword_shield", "sword_torch", "bow", "xbow", "hxb_sword", "sling_sword") \
                + (("mcb_sword",) if q == "fav" else ()):
            for size in (1, 2):
                combos.append((q, b, size))
        if q != "poor":
            combos.append((q, "sword_shield+bow", 2))
    # 真人档（只升武器、不买血药）和潜行打法（玩家A那样）
    combos += [("normal", "dual_sneak", 1), ("real", "dual", 1), ("real", "dual_sneak", 1), ("real", "sword_shield", 1),
               ("cap", "dual", 1), ("cap", "dual_sneak", 1)]
    table, room_log = {}, {}
    for q, b, size in combos:
        random.seed(f"{args.seed}-{q}-{b}-{size}")
        stats: dict[int, FloorStat] = {}
        if "+" in b:
            eco = [list(zip(*(economy(q, part) for part in b.split("+")))) for _ in range(30)]
        else:
            eco = [economy(q, b) for _ in range(30)]
        for t in range(START_TRIP.get(b, 0), TRIPS):
            for n in range(args.trips):
                run_trip(b, q, t, eco[n % len(eco)][t], size, stats,
                         room_log if (q, b, size) == ("normal", "sword_shield", 1) else None)
        table[(q, b, size)] = stats

    def seg(stats: dict, t: int) -> Optional[float]:
        if t * TRIP + 1 not in stats:
            return None                          # 这一段还拿不到这把弓
        surv = 1.0
        for d in range(t * TRIP + 1, t * TRIP + TRIP + 1):
            s = stats.get(d)
            if s and s.people:
                surv *= 1 - s.died / s.people
        return surv

    out.append("## 2. 走完一段（两块传送石之间）的存活率\n")
    out.append("目标（正常装备）：1–5 层约 99%、6–10 层约 85%、11–15 层约 65%、16–20 层约 50%；"
               "21 层以后深层装备、主题还没做（用第 16 层那套装备），只当预警\n")
    out.append("| 装备 | 打法 | 人数 | " + " | ".join(f"{t * TRIP + 1}–{t * TRIP + TRIP}" for t in range(TRIPS)) + " |")
    out.append("|---|---|---|" + "---|" * TRIPS)
    for (q, b, size), stats in table.items():
        out.append(f"| {QUALITY[q]} | {BUILDS[b]} | {size} | "
                   + " | ".join("-" if (v := seg(stats, t)) is None else pct(v) for t in range(TRIPS)) + " |")
    out.append("")

    def per_floor(title: str, fn, qs=("normal",)) -> None:
        out.append(title + "\n")
        out.append("| 装备 | 打法 | 人数 | " + " | ".join(str(d) for d in range(1, FLOORS + 1)) + " |")
        out.append("|---|---|---|" + "---|" * FLOORS)
        for (q, b, size), stats in table.items():
            if q in qs:
                out.append(f"| {QUALITY[q]} | {BUILDS[b]} | {size} | "
                           + " | ".join(fn(stats[d]) if d in stats and stats[d].people else "-" for d in range(1, FLOORS + 1)) + " |")
        out.append("")
    per_floor("## 3. 每层死亡率（目标：1–5 层 ≤1%，6–10 ≤3%，11–15 ≤8%，16–20 ≤13%；头目层可以再高 5 个百分点）",
              lambda s: pct(s.died / s.people), ("poor", "normal", "fav", "real"))
    per_floor("## 4. 每层掉血（这一层挨的伤害合计 ÷ 血量上限；目标 1–5 层 30–40%，6–10 约 50%，11–15 约 60%）",
              lambda s: pct(s.taken / s.people))
    per_floor("## 5. 每层用掉几瓶血药", lambda s: f"{s.potions / s.people:.1f}")
    per_floor("## 6. 每场平均几轮", lambda s: f"{s.rounds / max(1, s.fights):.1f}")

    out.append("## 7. 各种房间（正常装备、剑盾、单人，跟着一趟走下来的真实状态进场）：一场挨的伤害合计 / 团灭率\n")
    out.append("| 层 | 普通近战 | 有远程 | 有治疗 | 头目 |")
    out.append("|---|---|---|---|---|")
    for d in range(1, FLOORS + 1):
        cells = []
        for k in ("melee", "ranged", "healer", "boss"):
            v = room_log.get((d, k))
            cells.append(f"{pct(sum(x for x, _ in v) / len(v))} / {pct(sum(w for _, w in v) / len(v))}" if v else "-")
        out.append(f"| {d} | " + " | ".join(cells) + " |")
    out.append("")

    # ---- 担心会冒尖的几处：满血单独打一场，不喝药 ----
    random.seed(args.seed)
    out.append("## 8. 担心冒尖的几处（满血单独打一场、不喝血药；掉血按这一场挨的伤害合计，私酿回的不抵）：掉血 / 团灭率 / 轮数\n")
    out.append("| 场面 | 层 | 正常装备 | 好感全满 |")
    out.append("|---|---|---|---|")
    for depth, theme in ((5, "mine"), (10, "castle"), (15, "graveyard")):
        t = (depth - 1) // TRIP
        for size in (1, 2):
            cells = [" / ".join((pct(a), pct(w), f"{r:.1f}")) for a, w, r in
                     (fight_test("sword_shield", q, t, depth, [("boss", "boss")], theme, size=size) for q in ("normal", "fav"))]
            out.append(f"| 头目（剑盾，{size} 人） | {depth} | " + " | ".join(cells) + " |")
    for depth in (8, 12):
        t = (depth - 1) // TRIP
        base = fight_test("sword_shield", "normal", t, depth, [("animated_tome", "normal"), ("stone_gargoyle", "normal")], "library")
        combo = fight_test("sword_shield", "normal", t, depth, [("ink_scribe", "normal"), ("ink_shade", "normal")], "library")
        out.append(f"| 图书馆：书记 + 墨影（对照：活化书 + 石像鬼） | {depth} | "
                   f"{pct(combo[0])} / {pct(combo[1])} / {combo[2]:.1f}（对照 {pct(base[0])} / {pct(base[1])}） | - |")
        nun = fight_test("sword_shield", "normal", t, depth, [("grave_nun", "normal"), ("ghoul", "elite")], "graveyard")
        alone = fight_test("sword_shield", "normal", t, depth, [("ghoul", "elite")], "graveyard")
        out.append(f"| 招魂修女 + 精英食尸鬼（对照：精英食尸鬼单独） | {depth} | "
                   f"{pct(nun[0])} / {pct(nun[1])} / {nun[2]:.1f}（对照 {pct(alone[0])} / {pct(alone[1])}） | - |")
    for depth in (5, 10, 15):
        t = (depth - 1) // TRIP
        mcb = fight_test("mcb_sword", "fav", t, depth, [("kobold", "elite")], "mine")
        sw = fight_test("sword_shield", "fav", t, depth, [("kobold", "elite")], "mine")
        rb = "xbow" if depth >= 11 else "bow" if depth >= 6 else "hxb_sword"
        xb = fight_test(rb, "normal", t, depth, [("kobold", "elite")], "mine")
        out.append(f"| 精英狗头人：{BUILDS[rb]}（正常）；麦琪的弩配剑、剑盾（都是好感全满） | {depth} | "
                   f"{BUILDS[rb]} {pct(xb[0])}、{xb[2]:.1f} 轮 | 弩剑 {pct(mcb[0])}、{mcb[2]:.1f} 轮；剑盾 {pct(sw[0])}、{sw[2]:.1f} 轮 |")
    out.append("")
    out.append("### 六个主题的头目（正常装备、剑盾、单人，满血不喝血药）：挨的伤害 / 团灭率\n")
    out.append("| 头目 | 第 5 层 | 第 10 层 | 第 15 层 |")
    out.append("|---|---|---|---|")
    for key, th in THEMES.items():
        if th.get("min_depth"):
            continue                             # 深层主题的头目用 bosses.py --depths 16,20 看
        cells = []
        for depth in (5, 10, 15):
            a, w, _ = fight_test("sword_shield", "normal", (depth - 1) // TRIP, depth, [("boss", "boss")], key, n=300)
            cells.append(f"{pct(a)} / {pct(w)}")
        out.append(f"| {th['boss']['name']}（{key}） | " + " | ".join(cells) + " |")
    out.append("")

    # ---- 校准：装备差、剑盾单人的前 5 层，对照玩家A、玩家B 的真实记录 ----
    s = table[("poor", "sword_shield", 1)]
    out.append("## 9. 校准：模拟（装备差、剑盾、单人）对照真人（balance/calibrate.py）\n")
    out.append("| 层 | 模拟掉血 | 模拟死亡率 | 玩家A | 玩家B |")
    out.append("|---|---|---|---|---|")
    real = {1: ("35%", "105%"), 2: ("58%", "0%（只走了几步）"), 3: ("12%", "-"), 4: ("146%（倒下一次）", "-"), 5: ("69%", "-")}
    for d in range(1, 6):
        out.append(f"| {d} | {pct(s[d].taken / s[d].people)} | {pct(s[d].died / s[d].people)} | {real[d][0]} | {real[d][1]} |")
    text = "\n".join(out)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    open(args.out, "w", encoding="utf-8").write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
