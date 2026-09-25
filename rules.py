"""
纯规则：命中、减伤、伤害、负面效果的数值、回血、扎营、技能判定、价钱、升级费用和失败率、怪物数值和掉钱。
不碰数据库、不调 AI。引擎（engine.py、dungeon.py）和数值模拟（balance/sim.py）共用这一份，改一次两边一起变。
随机只用 random 模块：模拟里 random.seed 固定下来就能复现。
"""
import math
import random
from typing import Optional

# 战斗数值（血、伤害、防御、效果里的点数）统一放大 10 倍：防具升级 +0.5、远程 +1.5、宝石这些小数都成了整数。
# 几率、抗性倍数、光亮、金币、价格、升级费用、技能难度不跟着放大；回血本来就按血量上限的百分比算
SCALE = 10

# ============ 命中 ============
# 近战按距离（格）：贴身、一步之遥、几步开外；够不着的格数是 0
MELEE_HIT = {0: 0.95, 1: 0.45, 2: 0.05}
# 远程武器贴身难射，离远了准；steady 的（麦琪的弩、绞盘重弩）远近都是 STEADY_HIT
RANGED_HIT = {0: 0.45, 1: 0.75, 2: 0.9, 3: 0.9, 4: 0.85}
STEADY_HIT = 0.9
BLIND_HIT = 0.5                         # 看不清：命中减半（以前当一片漆黑只剩 5%，被墨咒书记远远致盲就追不上了）
SMOKE_HIT = 0.5                         # 烟雾弹的烟里：远程命中减半，敌我都算
SMOKE_ROUNDS = 2                        # 烟能撑几轮（敌人动几次）
# 远程的怪：离人一步以上就射，有掩体的房间 −RANGED_COVER（对双方都一样）
ENEMY_RANGED_HIT = {0: 0.5, 1: 0.75, 2: 0.85, 3: 0.85, 4: 0.75}
RANGED_COVER = 0.15
RANGED_BACKS = 2                        # 远程怪一场最多往后跳两次，再贴上去它就"背后撞上了石壁"，跳不开了
AVOID_CAP = 0.30                        # 闪避（影步靴）和盾的格挡（props.block）合计最多这么多，免得叠太高
RELOAD_BACKS = 2                        # 只给模拟试用：装填时被贴身顺势退一步（试过没用，近战怪当轮就跟上来，没上线）
HEALER_BELOW = 0.6
HEALER_CHANCE = 0.5                     # 有同伴要治时一半几率治疗（另一半照常出手）。治疗量按本层普通怪的血量 × props.healer，不看治的是谁
HEALER_MAX = 3                          # 每只一场最多治三次，"先杀她"和"耗光她"都是办法
# 玩家每做这么多个动作，敌人行动一次；战斗回合里就是每人每轮最多做几件事
ENEMY_EVERY = 2

# ============ 光亮 ============
# 光亮 0~100：低于 LIGHT_FULL 普通攻击命中按比例打折，0 时只剩 LIGHT_MIN_HIT；低于 LIGHT_DARK 做花样难一级；
# 越暗怪越凶、钱越多（dark_factor）；怕光的怪在 LIGHT_BRIGHT 以上攻击 -1
LIGHT_FULL, LIGHT_MIN_HIT, LIGHT_DARK, LIGHT_BRIGHT = 50, 0.05, 20, 70


def light_hit(light: int, chance: float) -> float:
    """命中按光亮打折：LIGHT_FULL 以上不打折，往下按比例降，最低 LIGHT_MIN_HIT（够不着的还是 0）"""
    return 0.0 if chance <= 0 else max(LIGHT_MIN_HIT, chance * min(1.0, light / LIGHT_FULL))


def dark_factor(light: int) -> float:
    """-1（光亮 100）到 +1（光亮 0）：越暗越大。怪的攻击、掉的钱跟着它走"""
    return (LIGHT_FULL - light) / LIGHT_FULL


def dark_attack(light: int) -> int:
    """暗处怪的攻击加成：很暗 +10，很亮 −10，中间 0"""
    return math.floor(dark_factor(light) + 0.5) * SCALE


# ============ 伤害怎么被防御挡掉 ============
# 按挨打的一方分：挨打的是玩家（怪打人、NPC 打人、决斗）按比例减伤，每一点防御都有用、越往上越少；
# 挨打的是怪和 NPC 还是减法（怪的防御是我们定的，不会像玩家那样被装备叠上去，重甲怪就该硬，破甲才有价值）。
# 中毒流血每回合掉的血、陷阱、事件失败、吃了有毒的东西都是固定值，不看防御（这正是对付高防玩家的手段）。
# 玩家挨打的结算顺序（以后加效果照这个顺序插）：
#   1 攻击方的破甲先扣掉防御  2 伤害 = 攻击 × K ÷ (K + 防御)  3 小数按几率进位（2.5 就一半 2 一半 3）
#   4 防守方的减伤（guard，比如项圈 -1）  5 最少 1 点
DEF_K = 4 * SCALE


def def_k(depth: int = 0) -> int:
    """比例减伤的常数：越大防御越不顶用。先写死，以后当深层难度的旋钮（比如 4 + 层数/5）"""
    return DEF_K


def hurt_player_by(atk: int, defense: int, depth: int = 0, pierce: int = 0, guard: int = 0) -> int:
    """玩家挨一下打实际掉多少血（顺序见上面）"""
    k = def_k(depth)
    x = atk * k / (k + max(0, defense - pierce))
    dmg = int(x) + (random.random() < x - int(x))
    return max(SCALE, dmg - guard)                  # 最少 1 点（放大前的 1 点）


def hurt_npc_by(power: int, armor: int) -> int:
    """怪挨一下打掉多少血：攻击减护甲（破甲、腐蚀已经从 armor 里扣掉了），最少 1"""
    return max(SCALE, power - max(0, armor))


OFFHAND_SHARE = 0.5                     # 双持时副手（左手）武器只加这么多伤害，向下取整；只拿一把的不管在哪只手都算主手。
                                        # 以前是 1/4，副手 7 才 +1；一半时两把精钢短剑 7+3 = 10，跟双手战锤一样

# ============ 负面效果 ============
# 玩家身上的：中毒每回合掉血、命中和判定 -POISON_HIT；流血每个动作掉血、打出的伤害 ×BLEED_DAMAGE；
# 看不清命中 ×BLIND_HIT；腐蚀防御 -value、血量上限临时扣 CORRODE_HP
EFFECT_TURNS = {"poison": 3, "bleed": 4, "blind": 1, "corrode": 3}
POISON_HIT, BLEED_DAMAGE, CORRODE_HP = 0.15, 0.75, 0.15
# 怪身上的（玩家装备打上去的）
NPC_EFFECT_TURNS = {"poison": 3, "bleed": 3, "blind": 2, "corrode": 3}
NPC_BLIND_HIT, NPC_CORRODE_DEF = 0.25, 2 * SCALE


def effect_value(kind: str, depth: int) -> int:
    """怪打上来的效果有多重：中毒、流血每次掉的血，腐蚀扣的防御，跟着层数涨"""
    return (1 + depth // 6 if kind == "corrode" else 1 + depth // 5) * SCALE


# ============ 回血、扎营、酒劲 ============
# 吃喝回血按血量上限的百分比算，耐性练高了药也跟着管用：heal 每 1 点回 HEAL_PCT%（血量 20 时正好 1 点），
# 酒回得少一点，每点 HEAL_PCT_ALCOHOL%。毒（harm）还是按点数掉血
HEAL_PCT = 5
HEAL_PCT_ALCOHOL = 3


def heal_amount(points: int, max_hp: int, alcohol: bool = False) -> int:
    """heal 点数 → 这个人实际回多少血"""
    return max(SCALE, round(max_hp * points * (HEAL_PCT_ALCOHOL if alcohol else HEAL_PCT) / 100)) if points > 0 else 0


# 扎营：回 max_hp × CAMP_BASE，带帐篷再加 CAMP_TENT（用掉一顶），生存判定成功再加 CAMP_SURVIVAL，空房里整体 ×CAMP_EMPTY
CAMP_BASE, CAMP_TENT, CAMP_SURVIVAL, CAMP_EMPTY = 0.3, 0.3, 0.15, 1.5
CHEER_ATTACK, CHEER_FLOORS = 25, 1     # 麦琪的私酿：喝了这一层攻击 +25%（下一层就过去了）

# ============ 技能 ============
# 技能判定：难度减技能等级的差值 → 成功率，差值不超过 0 是 SKILL_SURE，比表里最大的还大就必定失败
SKILL_SURE = 0.95
SKILL_GAP_CHANCE = {1: 0.9, 2: 0.6, 3: 0.3}
# 从 n 级升到 n+1 级要攒 SKILL_STEP * (n+1) 次熟练；只有难度高于当前等级的成功才算熟练
SKILL_STEP = 3
# 耐性：每升一级 HP 上限 +ENDURANCE_HP；挨打（活下来的）有 ENDURE_HIT_CHANCE 的几率涨熟练
ENDURANCE_HP = 3 * SCALE
ENDURE_HIT_CHANCE = 0.25


def skill_total(level: int) -> int:
    """升到 level 级一共要攒几次熟练：3、9、18、30……"""
    return SKILL_STEP * level * (level + 1) // 2


def skill_level(count: int) -> int:
    level = 0
    while count >= skill_total(level + 1):
        level += 1
    return level


def skill_progress(count: int) -> tuple[int, int, int]:
    """(等级, 这一级攒了几次, 升下一级要几次)"""
    level = skill_level(count)
    return level, count - skill_total(level), SKILL_STEP * (level + 1)


def skill_chance(level: int, difficulty: int) -> float:
    gap = difficulty - level
    return SKILL_SURE if gap <= 0 else SKILL_GAP_CHANCE.get(gap, 0.0)


def escape_chance(escape: int, attempts: int) -> float:
    """挣脱、醒来不靠技能等级的那种（NPC 的、昏过去的）：施加时的难度，每失败一次降一级"""
    return skill_chance(0, max(1, escape - attempts))


# ============ 价钱 ============
# 建议价按效果指数增长：好东西贵得快。NPC 报价在建议价的 OFFER_BAND 倍之间浮动；没有数值的东西（绳子、字条）
# 没有建议价，价钱全由 AI 定，限在 FREE_PRICE 之间
GEAR_PRICE = (2, 1.6)                   # 武器护甲：2 × 1.6^(伤害+防御)，伤害 3 约 8，伤害 5 约 21，伤害 6 约 34
POTION_PRICE = (0.8, 1.4)               # 吃喝：0.8 × 1.4^(回血+毒)，回 3 约 2，回 6 约 6
KNOCKOUT_PRICE = 5
OFFER_BAND = (0.5, 2.0)
FREE_PRICE = (1, 200)


def base_price(stats: dict) -> int:
    if stats.get("price"):
        return stats["price"]               # 模板写死了建议价（解酒药这类没数值的）
    gear = (stats.get("damage", 0) + stats.get("defense", 0)) / SCALE       # 定价曲线按放大前的数值算
    potion = stats.get("heal", 0) + stats.get("harm", 0) / SCALE
    price = ((GEAR_PRICE[0] * GEAR_PRICE[1] ** gear if gear else 0)
             + (POTION_PRICE[0] * POTION_PRICE[1] ** potion if potion else 0)
             + (KNOCKOUT_PRICE if stats.get("knockout") else 0))
    return max(1, round(price))


def has_stats(stats: dict) -> bool:
    return any(stats.get(k) for k in ("damage", "defense", "heal", "harm", "knockout", "price"))


def clamp_price(stats: dict, price: int, markup: float = 1.0) -> int:
    """AI 报的价限在建议价的一半到两倍；没数值的东西只限一个大范围。
    markup 是这家店加价卖的货（麦琪后厨的麻绳，props.markup）：建议价乘上它，而且不能往下砍（不然比本行的店还便宜）"""
    if not has_stats(stats):
        return max(FREE_PRICE[0], min(FREE_PRICE[1], price))
    base = base_price(stats) * markup
    low = math.ceil(base) if markup > 1 else math.ceil(base * OFFER_BAND[0])
    return max(low, min(math.floor(base * OFFER_BAND[1]), price))


# ============ 升级 ============
# 铁匠升级：武器每级伤害 +1，防具每级防御 +0.5（防具件数多，按件叠上去太快：以前第 11 层全身 32 防，怪只打得动 1 点）。
# 升到第 N 级有 N × UPGRADE_BREAK_STEP 的几率失败（最多 UPGRADE_BREAK_MAX），失败退一级，钱照收。
# 费用只看升到第几级，武器防具同一条曲线（以前看这件现在的数值：基础伤害高的弓、弩升一级比短剑贵 5 到 10 倍，
# 防具又便宜得离谱）；稀有的东西可以在物品 props.upgrade_mult 里给个倍数。
# 用奥利哈刚：失败不掉级、矿石不用掉，成功那次才用掉。淬火油必成、不收钱
UPGRADE_STEP = {"damage": 1 * SCALE, "defense": SCALE // 2}
RANGED_UPGRADE_STEP = 15               # 远程武器每级伤害 +15（放大前 +1.5）：射一发要搭一次装填，不补的话深层跟不上近战


def upgrade_step(stat: str, ranged: bool) -> float:
    """升一级加多少：武器 +1（远程 +1.5），防具 +0.5"""
    return RANGED_UPGRADE_STEP if ranged and stat == "damage" else UPGRADE_STEP[stat]


UPGRADE_COST = (12.5, 1.6)              # 升到第 N 级：12.5 × 1.6^N，+1 要 20、+3 要 51、+5 要 131、+8 要 537、+10 要 1374
UPGRADE_BREAK_STEP = 0.10
UPGRADE_BREAK_MAX = 0.90
UPGRADE_MIN_COST = 5
UPGRADE_MAX = 10
UPGRADE_DISCOUNT = 0.9                  # 莉娜的回礼（好感 20）：熟客价九折


def upgrade_cost(level: int, mult: float = 1.0) -> tuple[int, float]:
    """升到第 level 级的 (费用, 失败的几率)；mult 是这件东西的稀有度倍数"""
    cost = UPGRADE_COST[0] * UPGRADE_COST[1] ** level * mult
    return max(UPGRADE_MIN_COST, round(cost)), min(UPGRADE_BREAK_MAX, UPGRADE_BREAK_STEP * level)


def stat_text(v: float) -> str:
    """数值显示：整数不带小数点（防具升级有 0.5）"""
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


# ============ 地牢里的怪 ============
BOSS_EVERY = 5                          # 每几层楼梯间守着头目
TREASURE_GUARD = 0.5                    # 宝箱房有怪守着的几率
# 组队时多刷怪：每只普通怪、精英变成"人数"只，一个房间最多 ROOM_CAP 只，多出来的折成血；钱按只数分、东西只有第一只带。
# 头目、楼梯间守卫还是一只，血 × (1 + BOSS_PARTY_HP × (人数 − 1))，一轮出手"人数"次
ROOM_CAP = 6
BOSS_PARTY_HP = 0.8


ELITE_HP = 1.5                          # 精英的血（以前 1.8，现在每只精英带一个词缀，血降下来补偿）


def monster_stats(depth: int, mods: dict, rank: str = "normal") -> tuple[int, int, int]:
    """(血, 攻, 防)：随层数涨，mods 是这种怪的倍数和加减（dungeon.yaml），精英、头目再加"""
    hp = (6 + depth) * SCALE * mods.get("hp", 1.0)
    atk = (2 + depth // 3 + depth // 12) * SCALE + mods.get("atk", 0)
    df = (1 + depth // 4) * SCALE + mods.get("def", 0)
    if rank == "elite":
        hp, atk = hp * ELITE_HP, atk + SCALE
    elif rank == "boss":
        hp, atk, df = hp * 2, atk + SCALE, df + SCALE
    return max(2 * SCALE, round(hp)), max(SCALE, atk), max(0, df)


def monster_gold(depth: int, rank: str) -> list[int]:
    """掉的金币范围跟着层数涨（约 1.2^层数），精英 ×2，头目 ×5"""
    scale = 1.2 ** depth * {"normal": 1, "elite": 2, "boss": 5}[rank]
    return [max(1, round(2 * scale)), max(2, round(5 * scale))]


def elite_chance(depth: int) -> float:
    """一群怪是精英的几率（战斗房第一群、有怪守着的宝箱房）"""
    return min(0.35, 0.02 * depth)


def max_groups(depth: int) -> int:
    """战斗房最多几群怪：1 到 7 层一群，8 层起最多两群，16 层起最多三群（每群随机 1 到这个数）"""
    return min(3, 1 + depth // 8)


def party_copies(size: int, groups: int) -> int:
    """组队时一群怪刷几只（多出来的折成血）"""
    return max(1, min(size, ROOM_CAP // max(1, groups)))


def treasure_gold(depth: int, dark: float, size: int) -> int:
    """宝箱房的钱袋：越深越多，越暗越多（按房间本来的光亮），组队按人数"""
    return round(random.randint(8, 15) * 1.2 ** depth * (1 + 0.5 * dark)) * size


def stash_gold(depth: int, size: int) -> int:
    """空房里藏着的古币（调查判定，难度 2 + 层数/3）"""
    return max(2, round(random.randint(4, 8) * 1.2 ** depth)) * size


# ============ 精英词缀 ============
# 每只精英从这里随机抽一个，名字前缀跟着变。同一只狗头人矿工，这次是迅捷的，下次是坚甲的，打法就不一样
ELITE_AFFIXES = {
    "swift": {"name": "迅捷的", "attacks": 2, "extra_chance": 0.5, "dmg_mult": 0.7, "hp_mult": 0.75,
              "note": "一轮常常出手两次（第二下一半几率），每下轻一点，身子也脆一点"},
    "armored": {"name": "坚甲的", "def": 3 * SCALE, "hp_mult": 0.75, "note": "防御高、血少一些"},
    "frenzied": {"name": "狂暴的", "frenzy": 3 * SCALE, "note": "血量低于一半时攻击 +30"},
    "bloodthirsty": {"name": "嗜血的", "lifesteal": 0.5, "note": "打中人就回造成伤害的一半"},
    "commanding": {"name": "号令的", "minions": 1, "note": "带着一只同类小怪（小怪下手减半、不掉东西，主子一死就跑）"},
    "thorny": {"name": "荆棘的", "thorns": 1 * SCALE, "thorns_chance": 0.5, "note": "近战砍它一半几率被扎回 10 点"},
    "keen": {"name": "警觉的", "keen": True, "note": "一进门就发现人，偷袭不了"},
    "plagued": {"name": "瘟疫的", "plague": 0.3, "note": "打中人 30% 附带这一带的毒害"},
}
# 瘟疫的精英附带什么：跟着主题走（主题头目那一手）
THEME_PLAGUE = {"mine": "prone", "graveyard": "corrode", "castle": "restrained", "forest": "prone",
                "swamp": "poison", "library": "blind"}


def scale_damage(dmg: int, mult: float) -> int:
    """伤害乘个倍数（迅捷的精英每下 ×0.7），小数按几率进位，最少 1"""
    if mult == 1:
        return dmg
    x = dmg * mult
    return max(SCALE, int(x) + (random.random() < x - int(x)))


# ============ 头目技能 ============
# 头目的技能写在 dungeon.yaml 的 boss.skills 里，跟玩家装备一样是 when / do：
#   when：hp_below（血掉到这条线，每条线一次）/ every（每出手 N 次一次）/ fight_start（第一次出手）
#   do：summon（叫帮手）/ status_all（全场上状态）/ effect_all（全场中毒、腐蚀……）/ telegraph（预告，下一次出手放 then 里的招）
#       / mark（判罪：标记一个人，打他 +bonus，打中一次就消）/ self_heal（回血）/ silence（禁声：几次出手之内不能念书、卷轴）
# 技能按层数解锁：第 5 层的头目只会第一招，第 10 层两招，第 15 层起全套，外加血少时狂暴。
# 放技能就是这一次出手（mark 除外，标完照样打）。技能换数值：加了技能的头目，血或攻击要相应扣一点（dungeon.yaml 里调）
SUMMON_MAX = 2                          # 场上同时最多几只召唤来的小怪
SUMMON_HP = 0.5                         # 召唤的小怪血量：本层普通怪的一半，不掉钱也不掉东西，不召治疗怪
SUMMON_DMG = 0.5                        # 召唤的小怪下手也只有一半（全额的话场上一下多出两个人出手，太容易打出爆发）
INTERRUPT_SHARE = 0.15                  # 预告大招那一轮对它打出这么多（血量上限的比例）就打断了：打断靠重创，不算控制
ENRAGE_FROM, ENRAGE_BELOW, ENRAGE_ATK = 15, 0.3, 2 * SCALE      # 第 15 层起的头目血量低于三成狂暴，攻击 +20
WEAK_MULT = 1.5                         # 打中弱点（火、破甲、圣水、强光……）伤害 ×1.5


def summon_count(skill: dict, depth: int) -> int:
    """叫几只帮手：第 5 层的入门版只叫一只，第 10 层起按技能写的数"""
    return min(skill.get("count", 1), 1 if depth < 10 else skill.get("count", 1))


def unlocked_skills(skills: list[dict], depth: int) -> list[dict]:
    """这一层的头目会哪几招（按顺序解锁）"""
    n = 1 if depth < 10 else 2 if depth < ENRAGE_FROM else len(skills)
    return skills[:n]


def due_skill(skills: list[dict], hp_ratio: float, acts: int, used: set) -> Optional[int]:
    """这一次出手该放哪一招（技能的下标），没有就是 None。acts 是这一场已经出手过几次，used 是放过的一次性技能"""
    for i, s in enumerate(skills):
        when = s.get("when")
        if when == "hp_below" and hp_ratio < s["value"] and i not in used:
            return i
        if when == "fight_start" and acts == 0 and i not in used:
            return i
        if when == "every" and acts > 0 and acts % s["value"] == 0:
            return i
    return None


def enraged(depth: int, hp_ratio: float) -> bool:
    return depth >= ENRAGE_FROM and hp_ratio < ENRAGE_BELOW


# ============ 宝石 ============
# 规则数字在 loot.yaml 的 gems（孔位、品质权重、取出费用、全身上限），宝石本身在 items_dungeon.yaml（type: gem）
GEM_TIER_PREFIX = {1: "碎裂的", 2: "", 3: "闪亮的", 4: "完美的"}
GEM_TIER_WORDS = {1: "碎裂", 2: "普通", 3: "闪亮", 4: "完美"}
GEM_TOP = 4
GEM_SLOT_WORDS = {"weapon": "武器", "armor": "护具", "trinket": "饰品", "any": "什么都能镶"}


def gem_category(item_type: str, slot: Optional[str]) -> Optional[str]:
    """装备镶宝石时算哪一类：武器 / 护具（头、胸、腿、脚、盾）/ 饰品（戒指、项链、腰带）；别的镶不了"""
    if item_type == "weapon":
        return "weapon"
    if item_type == "armor":
        return "trinket" if slot in ("ring", "neck", "belt") else "armor"
    return None


def gem_fits(gem_slot: str, category: Optional[str]) -> bool:
    return category is not None and gem_slot in ("any", category)


def gem_effects(gem_props: dict, tier: int, category: str) -> list[dict]:
    """这颗宝石按品质、镶在哪一类装备上，落成跟装备一样的 effects（values / chances 取这一档的值）"""
    specs = [gem_props["adapts"][category]] if gem_props.get("adapts") else gem_props.get("effects") or []
    out = []
    for s in specs:
        e = {k: v for k, v in s.items() if k not in ("values", "chances")}
        if "values" in s:
            e["value"] = s["values"][tier - 1]
        if "chances" in s:
            e["chance"] = s["chances"][tier - 1]
        e["gem"] = True
        out.append(e)
    return out


def gem_tier(depth: int, gem_rules: dict, numeric: bool) -> int:
    """掉落时的品质：按这一层的权重抽；数值宝石没到第 numeric_perfect_min_floor 层，完美降成闪亮"""
    table = gem_rules["tier_by_floor"]
    key = max([int(k) for k in table if int(k) <= depth] or [min(int(k) for k in table)])
    tier = random.choices([1, 2, 3, 4], table[key] if key in table else table[str(key)])[0]
    if numeric and tier == GEM_TOP and depth < gem_rules.get("numeric_perfect_min_floor", 15):
        tier = GEM_TOP - 1
    return tier


def refine_cap(numeric: bool, deepest: int, refine: dict) -> int:
    """诺艾尔最多能把这颗刷到第几档：闪亮要最深到过第 10 层，完美要第 10 层（数值宝石第 15 层）"""
    gate = refine["min_deepest_floor"]
    if deepest < gate["shiny"]:
        return 2
    return GEM_TOP if deepest >= gate["perfect_numeric" if numeric else "perfect_other"] else 3


def refine_roll(tier: int, cap: int, refine: dict, catalyst: bool) -> int:
    """刷一次：只升不降。先掷跳两档（封顶在 cap），再掷升一档（交了垫子几率翻倍），都没中就不变"""
    if random.random() < refine["double_up_chance"] and tier + 2 <= cap:
        return tier + 2
    chance = refine["up_chance"][tier - 1] * (refine["catalyst_mult"] if catalyst else 1)
    return tier + 1 if tier < cap and random.random() < chance else tier


def roll_sockets(rarity: str, gem_rules: dict) -> int:
    return random.choices([0, 1, 2, 3], gem_rules["sockets"][rarity])[0]


def whole(v: float) -> int:
    """小数的加成按几率进位（+0.5 就是一半几率多 1 点）"""
    return int(v) + (random.random() < v - int(v))
