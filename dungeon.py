"""
远古地牢：酒馆地窖里的漆黑入口通往一座无限往下的地牢。每支队伍（没组队就是自己）各有一份，
一层 3×3 九个房间，按需生成：第一次走进入口生成第 1 层，走到楼梯间往下才生成下一层。
房间、出口、怪物、地上的东西都是普通的世界数据（房间 id 以 dg- 开头），移动、潜行、战斗照常走引擎；
没人在里面超过 STALE 就整份删掉，下次进来重新生成。

怪物数值按层数算（monster_stats），同一种怪同一层共用一个 NPC 模板（dg_种类_层数），引擎不用改。
内容（主题、房间描写、怪物）在 dungeon.yaml。
"""

import os
import random
from typing import Optional
from uuid import UUID, uuid4

import yaml
from psycopg import Cursor
from psycopg.types.json import Jsonb

from rules import (BOSS_EVERY, BOSS_PARTY_HP, ELITE_AFFIXES, GEM_TIER_PREFIX, ROOM_CAP, SCALE, SUMMON_DMG, SUMMON_HP,  # noqa: F401
                   THEME_PLAGUE, TREASURE_GUARD, UPGRADE_STEP, elite_chance, gem_category, gem_tier, max_groups, roll_groups, gold_scale,
                   roll_sockets, unlocked_skills,
                   monster_gold, monster_stats, party_copies, stash_gold, treasure_gold)

GATE = "dungeon_gate"                   # 地窖的入口、楼梯间往下都连到这个占位房间，引擎走到这里改由 through_gate 决定去哪
ENTRANCE = "cellar"                     # 地牢第 1 层往上回到这里
STALE = "30 minutes"                    # 没人在、这么久没动静的一层删掉（按层算，有人在的那一层留着）
GRID = 3                                # 一层 GRID × GRID 个房间
THEME_GAP = 3                           # 主题不跟上面几层重复
# 组队时多刷怪，不再给怪加血（战斗按回合结算，每只怪一轮只出手一次，以前是每个人出手它都还手）：
# 每只普通怪、精英变成"人数"只，一个房间最多 ROOM_CAP 只，多出来的折成血；钱按只数分、东西只有第一只带，
# 整个房间的收获跟以前一样。头目、楼梯间守卫还是一只，血 × (1 + BOSS_PARTY_HP × (人数 − 1))，
# 一轮出手"人数"次（跟以前每个人出手它都还手一样）

DIRS = {"north": (-1, 0), "south": (1, 0), "west": (0, -1), "east": (0, 1)}
BACK = {"north": "south", "south": "north", "west": "east", "east": "west"}

# 房间里的补给：空房搜索可能找到药草（每人每层一次），宝箱房有一袋古币和药草
EMPTY_FORAGE = [{"item": "herb", "chance": 0.5, "cooldown": 86400}]
FEATURE_RESPAWN = 86400                 # 地牢里的环境物件用掉就没了（地牢活不到这么久）
# 光亮 0~100：主题的自然光亮（dungeon.yaml themes.light）+ 房间明暗（bright +30 / dim 0 / dark -15），
# 点着的灯（火盆、烛台）每盏 +LAMP_LIGHT，带火把的人在场 +engine.TORCH_LIGHT。越暗怪越强、钱越多（engine._dark_factor）
LIGHT_OFFSET = {"bright": 30, "dim": 0, "dark": -15}
LAMP_LIGHT = 20
STONE_LIGHT = 90
ROAD_EVENT_CHANCE = 0.15                # 地牢里两个房间之间走动时碰上随机事件的几率（engine._road_event）

_data: Optional[dict] = None
_loot: Optional[dict] = None
# 掉落表里暂时不出的东西：它们的机制还没做（地图残片要小地图，大钥匙要上锁的牢房，诅咒要诺艾尔解咒）
LOOT_NOT_YET: set[str] = set()         # 机制还没做的物品先不掉（第三批做完以后都放出来了）


def data() -> dict:
    global _data
    if _data is None:
        with open(os.path.join(os.path.dirname(__file__), "dungeon.yaml"), encoding="utf-8") as f:
            _data = yaml.safe_load(f)
    return _data


_dungeon_items: Optional[set] = None


def dungeon_items() -> set[str]:
    """items_dungeon.yaml 里的物品 id（地牢掉的东西；村里店里卖的不在里面）"""
    global _dungeon_items
    if _dungeon_items is None:
        with open(os.path.join(os.path.dirname(__file__), "items_dungeon.yaml"), encoding="utf-8") as f:
            _dungeon_items = set((yaml.safe_load(f) or {}).get("items", {}))
    return _dungeon_items


def item_rarity(template: str, props: dict) -> str:
    """一件地牢装备的稀有度：掉的时候记在实例上的为准；以前掉的按来源猜（头目招牌装备稀有、普通怪专属掉落普通、别的精良）"""
    if props.get("rarity"):
        return props["rarity"]
    if template in boss_items():
        return "rare"
    if any(d.get("item") == template for drops in loot_data()["monsters"].values() for d in drops or []):
        return "common"
    return "uncommon"


def loot_data() -> dict:
    """掉落表 loot.yaml：普通怪专属掉落、头目二选一、通用池、宝箱房主题池、路边小木匣、空房搜索"""
    global _loot
    if _loot is None:
        with open(os.path.join(os.path.dirname(__file__), "loot.yaml"), encoding="utf-8") as f:
            _loot = yaml.safe_load(f)
    return _loot


def _pick(entries: list[dict], depth: int) -> Optional[str]:
    """按权重从池子里抽一件（到了 min_floor 的、机制做好了的）"""
    ok = [e for e in entries or [] if depth >= e.get("min_floor", 1) and e["item"] not in LOOT_NOT_YET]
    return random.choices([e["item"] for e in ok], [e.get("weight", 1) for e in ok])[0] if ok else None


def boss_items() -> set[str]:
    """头目的招牌装备（二选一必掉的、额外掉的）：刷新词条刷不了"""
    return {i for b in loot_data()["bosses"].values() for i in b.get("pick_one", []) + [e["item"] for e in b.get("extra", [])]}


def _drops(kind: str, rank: str, theme: str, depth: int, stair: bool = False) -> list[str]:
    """一只怪身上带的东西（打死掉在地上）。精英专属掉率 × elite_mult，另外有几率从通用池抽；头目二选一必掉；
    楼梯间守卫（stair）一件都没掷中就从 stair_guard 补给池保底抽一件"""
    loot, out = loot_data(), []
    rules = loot["rules"]
    if rank == "boss":
        b = loot["bosses"].get(theme, {})
        if pick := [i for i in b.get("pick_one", []) if i not in LOOT_NOT_YET]:
            out.append(random.choice(pick))
        out += [e["item"] for e in b.get("extra", []) if random.random() < e.get("chance", 1)]
        return out
    mult = rules.get("elite_mult", 1) if rank == "elite" else 1
    out += [d["item"] for d in loot["monsters"].get(kind) or []
            if depth >= d.get("min_floor", 1) and d["item"] not in LOOT_NOT_YET and random.random() < d["chance"] * mult]
    if rank == "elite" and random.random() < rules.get("elite_pool_chance", 0) and (p := _pick(loot["pool"], depth)):
        out.append(p)
    if stair and not out and (p := _pick(loot.get("stair_guard"), depth)):
        out.append(p)
    return out


def gem_rules() -> dict:
    return loot_data()["gems"]


def gem_props(cur: Cursor, gem: str, tier: int) -> dict:
    """一颗宝石实例的 props：品质、带前缀的名字（碎裂的、完美的）、按品质的参考价"""
    cur.execute("select name from item_templates where id = %s", (gem,))
    name = cur.fetchone()["name"]
    return {"tier": tier, "name": GEM_TIER_PREFIX[tier] + name, "price": gem_rules()["tier_price"][tier - 1]}


def put_gem(cur: Cursor, gem: str, depth: int, *, room: Optional[str] = None, npc: Optional[UUID] = None,
            player: Optional[UUID] = None) -> str:
    """掉一颗宝石（品质按层数抽），返回名字"""
    cur.execute("select props from item_templates where id = %s", (gem,))
    tier = gem_tier(depth, gem_rules(), bool(cur.fetchone()["props"].get("numeric")))
    props = gem_props(cur, gem, tier)
    cur.execute("insert into item_instances (template_id, room_id, npc_id, player_id, props) values (%s, %s, %s, %s, %s)",
                (gem, room, npc, player, Jsonb(props)))
    return props["name"]


def deep_mult(depth: int) -> float:
    """深层（gems.drops.deep_from_floor 起）宝石掉率的倍数"""
    d = gem_rules()["drops"]
    return d.get("deep_mult", 1) if depth >= d.get("deep_from_floor", 999) else 1


def gem_count(rank: str, depth: int) -> int:
    """这只怪身上带几颗宝石：头目必带（深层两颗），别的按掉率（深层翻倍）"""
    d = gem_rules()["drops"]
    if rank == "boss":
        return d.get("boss_deep", 1) if depth >= d.get("deep_from_floor", 999) else 1
    return int(random.random() < d.get(rank, 0) * deep_mult(depth))


def pick_gem(theme: Optional[str], common_share: float = 0.0) -> str:
    """从这个主题的宝石池抽一颗（common_share 的几率改从通用池抽）"""
    pools = gem_rules()["pools"]
    pool = pools["common"] if not theme or theme not in pools or random.random() < common_share else pools[theme]
    return random.choice(pool)


def _put_item(cur: Cursor, template: str, depth: int, *, room: Optional[str] = None, npc: Optional[UUID] = None,
              boss: bool = False, rarity: Optional[str] = None) -> None:
    """放一件东西。头目的招牌装备每深 boss_upgrade_every 层自带 +1（10 层 +1，15 层 +2）；
    rarity 给了的是地牢掉落：武器、护具、饰品按稀有度随机带孔（common 普通怪 / uncommon 精英宝箱 / rare 头目）"""
    props = {}
    every = loot_data()["rules"].get("boss_upgrade_every", 0)
    cur.execute("select name, type, slot, damage, defense from item_templates where id = %s", (template,))
    t = cur.fetchone()
    if rarity and gem_category(t["type"], t["slot"]):
        props["rarity"] = rarity                # 拆解按稀有度出碎铁（engine.do_dismantle）
        if n := roll_sockets(rarity, gem_rules()):
            props["sockets"] = n
    if boss and every and (plus := depth // every - 1) > 0:
        if t["type"] == "weapon":
            props |= {"plus": plus, "damage": t["damage"] + plus * UPGRADE_STEP["damage"], "name": f"{t['name']} +{plus}"}
        elif t["type"] == "armor" and t["defense"]:
            props |= {"plus": plus, "defense": t["defense"] + plus * UPGRADE_STEP["defense"], "name": f"{t['name']} +{plus}"}
    cur.execute("insert into item_instances (template_id, room_id, npc_id, props) values (%s, %s, %s, %s)",
                (template, room, npc, Jsonb(props)))


def road_box_item(theme: str, depth: int) -> Optional[str]:
    """路边小木匣里除了面包药草火把麻绳，还可能是这些（各自掷几率，先中先得）"""
    box = loot_data().get("road_box", {})
    for e in (box.get("all") or []) + (box.get(theme) or []):
        if depth >= e.get("min_floor", 1) and e["item"] not in LOOT_NOT_YET and random.random() < e.get("chance", 0):
            return e["item"]
    return None


def is_dungeon(room_id: str) -> bool:
    return room_id.startswith("dg-")


def _room_id(run: UUID, depth: int, cell: tuple[int, int]) -> str:
    return f"dg-{run.hex}-{depth}-{cell[0] * GRID + cell[1]}"


OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east"}
FLOOR_HOOK = None                        # 新生成了一层时调用 (run, depth)：server 挂上后台 AI 写房间


# ============ AI 写的房间 ============
# 一层先用模板生成（玩家不用等），后台让 AI 重写战斗房、空房、宝箱房、楼梯间的名字、描写、环境（亮暗、积水、掩体）
# 和能利用的环境物品。入口（玩家已经站在里面）、事件房（文字跟事件绑着）、传送石、牢房不改；已经有人进过的房间也不改
AI_KINDS = ("combat", "empty", "treasure", "stairs")


def rooms_for_ai(cur: Cursor, run: UUID, depth: int) -> Optional[dict]:
    """给 AI 写的材料：这一层的主题，和还没人进过、可以重写的房间（格子、种类、现在的模板文字）"""
    cur.execute("select theme, seen from dungeon_floors where run_id = %s and depth = %s", (run, depth))
    f = cur.fetchone()
    if f is None:
        return None
    cur.execute("select id, name, description, props from rooms where id like %s", (f"dg-{run.hex}-{depth}-%",))
    rooms = [{"cell": _cell_of(r["id"]), "kind": r["props"]["dungeon"]["kind"], "name": r["name"].split("·", 1)[-1],
              "description": r["description"]}
             for r in cur.fetchall()
             if r["props"].get("dungeon", {}).get("kind") in AI_KINDS and _cell_of(r["id"]) not in (f["seen"] or [])]
    t = data()["themes"][f["theme"]]
    return {"theme": f["theme"], "theme_name": t["name"], "intro": t["intro"], "depth": depth,
            "monsters": [data()["monsters"][m]["name"] for m in t["monsters"]], "rooms": sorted(rooms, key=lambda r: r["cell"])}


def apply_ai_rooms(cur: Cursor, run: UUID, depth: int, theme: str, texts: list[dict]) -> int:
    """把 AI 写好的房间写进去（期间有人进去了的跳过），返回改了几间。
    环境物品换成 AI 给的（点着的灯让房间更亮），光亮按主题的自然光亮 + 亮暗档重新算"""
    cur.execute("select seen from dungeon_floors where run_id = %s and depth = %s", (run, depth))
    seen = set(cur.fetchone()["seen"] or [])
    base = data()["themes"][theme].get("light", 35)
    done = 0
    for t in texts:
        rid = f"dg-{run.hex}-{depth}-{t['cell']}"
        cur.execute("select 1 from players where room_id = %s limit 1", (rid,))
        if t["cell"] in seen or cur.fetchone():
            continue
        light = max(0, min(100, base + LIGHT_OFFSET[t["light"]]))
        lamps = [f"f{i}" for i, ft in enumerate(t["features"]) if ft["lamp"]]
        env = {"light": min(100, light + LAMP_LIGHT * len(lamps)), "ground": t["ground"], "cover": t["cover"]}
        if lamps:
            env["lamps"] = lamps
        cur.execute("""update rooms set name = %s, description = %s, details = %s, props = jsonb_set(props, '{env}', %s)
                       where id = %s""",
                    (f"第 {depth} 层·{t['name']}", t["description"], t["details"], Jsonb(env), rid))
        cur.execute("select count(*) as n from room_features where room_id = %s", (rid,))
        if cur.fetchone()["n"]:         # 模板放了环境物品的房间（战斗房、楼梯间、有守卫的宝箱房）才换
            cur.execute("delete from room_features where room_id = %s", (rid,))
            for i, ft in enumerate(t["features"]):
                cur.execute("""insert into room_features (room_id, key, name, max_tier, max_uses, uses_left, respawn_seconds)
                               values (%s, %s, %s, %s, 1, 1, %s)""", (rid, f"f{i}", ft["name"], ft["max_tier"], FEATURE_RESPAWN))
        done += 1
    return done
CELL_INDEX = GRID * GRID                # 城堡层的牢房接在九宫格外面，编号在格子后面


def _prison_cell(cur: Cursor, run: UUID, depth: int, theme: dict, cells: list, start: tuple, size: int) -> None:
    """城堡层：挑一个靠边的格子，往外开一扇锁着的门（典狱长的大钥匙开），门后是上锁的牢房：
    一袋比宝箱房多一倍的钱、一件通用池的东西、一件城堡宝箱池的东西，没有怪"""
    sides = [(c, d) for c in cells if c != start for d, (dr, dc) in DIRS.items()
             if not (0 <= c[0] + dr < GRID and 0 <= c[1] + dc < GRID)]
    parent, d = random.choice(sides)
    text = theme["cell"]
    rid = f"dg-{run.hex}-{depth}-{CELL_INDEX}"
    light = theme.get("light", 35) + LIGHT_OFFSET.get(text.get("light", "dark"), 0)
    props = {"dungeon": {"depth": depth, "theme": "castle", "kind": "cell"},
             "env": {"light": max(0, min(100, light)), "ground": "normal", "cover": False}}
    cur.execute("insert into rooms (id, name, description, details, props) values (%s, %s, %s, %s, %s)",
                (rid, f"第 {depth} 层·{text['name']}", text["description"], text.get("details", ""), Jsonb(props)))
    cur.execute("""insert into room_exits (room_id, direction, to_room, locked, key_item) values (%s, %s, %s, true, 'warden_key')""",
                (_room_id(run, depth, parent), d, rid))
    cur.execute("insert into room_exits (room_id, direction, to_room) values (%s, %s, %s)",
                (rid, OPPOSITE[d], _room_id(run, depth, parent)))
    dark = (50 - light) / 50
    coins = round(random.randint(8, 15) * gold_scale(depth) * (1 + 0.5 * dark)) * size * 2
    cur.execute("insert into item_instances (template_id, room_id, props) values ('gold_pouch', %s, %s)", (rid, Jsonb({"gold": coins})))
    for item in (_pick(loot_data()["pool"], depth), _pick(loot_data()["treasure"].get("castle"), depth)):
        if item:
            _put_item(cur, item, depth, room=rid, rarity="uncommon")


# ============ 小地图 ============
# 地牢一层是 3×3 的九宫格：走过的格子记在 dungeon_floors.seen（全队共用一份地牢），用地图残片显出整层（revealed）。
# 走过的格子标房间种类，显出来但没走过的只标入口、楼梯间、宝箱房；门只画走过的格子的（显出整层就全画）

KIND_MARK = {"entry": "入", "stairs": "梯", "treasure": "宝", "empty": "空", "event": "事", "combat": "战"}


def _cell_of(room_id: str) -> int:
    """地牢房间在这一层的格子编号；不是地牢房间（地窖、入口）是 -1"""
    tail = room_id.rsplit("-", 1)[-1]
    return int(tail) if is_dungeon(room_id) and tail.isdigit() else -1


def mark_seen(cur: Cursor, room_id: str) -> None:
    """有人走进了地牢的这个房间：记进这一层走过的格子"""
    if not is_dungeon(room_id):
        return
    run, depth = parse_room(room_id)
    cell = _cell_of(room_id)
    cur.execute("""update dungeon_floors set seen = array_append(seen, %s)
                   where run_id = %s and depth = %s and not (%s = any(seen))""", (cell, run, depth, cell))


def reveal(cur: Cursor, room_id: str) -> bool:
    """用地图残片显出这一层：已经显出过就返回 False"""
    run, depth = parse_room(room_id)
    cur.execute("update dungeon_floors set revealed = true where run_id = %s and depth = %s and not revealed returning 1",
                (run, depth))
    return cur.fetchone() is not None


def minimap(cur: Cursor, room_id: str) -> Optional[dict]:
    """给界面画小地图：每个格子的标记、是不是走过、是不是现在所在、往东往南有没有门（画格子之间的连线）"""
    if not is_dungeon(room_id):
        return None
    run, depth = parse_room(room_id)
    cur.execute("select seen, revealed from dungeon_floors where run_id = %s and depth = %s", (run, depth))
    f = cur.fetchone()
    if f is None:
        return None
    prefix = f"dg-{run.hex}-{depth}-"
    cur.execute("select id, props from rooms where id like %s", (prefix + "%",))
    props = {_cell_of(r["id"]): r["props"] for r in cur.fetchall()}
    cur.execute("select room_id, direction, to_room, locked from room_exits where room_id like %s", (prefix + "%",))
    exits = cur.fetchall()
    seen, revealed, here = set(f["seen"] or []), f["revealed"], _cell_of(room_id)
    cells = []
    for i in range(GRID * GRID):
        kind = (props.get(i) or {}).get("dungeon", {}).get("kind", "")
        stone = (props.get(i) or {}).get("stone")
        walked = i in seen or i == here
        known = walked or revealed
        mark = ("石" if stone else KIND_MARK.get(kind, "")) if walked else \
            ("石" if stone else KIND_MARK[kind] if kind in ("entry", "stairs", "treasure") else "?") if revealed else ""
        # 只看通往这一层别的格子的门（往上回上一层、往下的楼梯不算）
        mine = [e for e in exits if _cell_of(e["room_id"]) == i and e["to_room"].startswith(prefix)]
        cells.append({
            "mark": mark, "walked": walked, "known": known, "here": i == here,
            # 往东、往南的门（格子之间的连线只画一次）；往九宫格外面的门（城堡的牢房）标在格子上
            "east": known and any(e["direction"] == "east" and _cell_of(e["to_room"]) < CELL_INDEX for e in mine),
            "south": known and any(e["direction"] == "south" and _cell_of(e["to_room"]) < CELL_INDEX for e in mine),
            "door": next((e["direction"] for e in mine if known and _cell_of(e["to_room"]) == CELL_INDEX), None),
        })
    return {"depth": depth, "grid": GRID, "cells": cells, "revealed": revealed, "in_cell": here == CELL_INDEX}


def parse_room(room_id: str) -> tuple[UUID, int]:
    """地牢房间 id → (哪份地牢, 第几层)"""
    _, run, depth, _ = room_id.split("-")
    return UUID(run), int(depth)


# ============ 怪物数值 ============
# 第 n 层普通怪：血 6+n、攻击 2+n/3+n/12、防御 1+n/4（攻击跟着玩家大概的防御涨，见 memory/dungeon-design.md 的模拟）；
# 每种怪再按 dungeon.yaml 的倍率、加减调整。精英血 ×1.8 攻 +1，头目血 ×2 攻 +1 防 +1

def _template(cur: Cursor, depth: int, kind: str, rank: str, theme: str, share: int = 1, hp_mult: float = 1.0,
              minion: bool = False, attacks: int = 1, affix: Optional[str] = None) -> str:
    """这一层这种怪的 NPC 模板，没有就建。share：钱分给几只（组队多刷的同一群）；hp_mult：血的倍数；
    minion：头目叫来的、精英带着的小怪，不掉钱也不掉东西；attacks：战斗回合里一轮出手几次（房间满了折成血的，出手也跟着多）；
    affix：精英的词缀（rules.ELITE_AFFIXES）"""
    # 头目按主题分（每个主题的头目不一样），别的按怪的种类
    tid = (f"dg_{theme + '_' if rank == 'boss' else ''}{kind}_{depth}" + ("" if rank == "normal" else f"_{rank}")
           + (f"_{affix}" if affix else "")
           + (f"_g{share}" if share > 1 else "") + (f"_h{round(hp_mult * 10)}" if hp_mult != 1 else "")
           + ("_m" if minion else "") + (f"_a{attacks}" if attacks > 1 else ""))
    m = data()["themes"][theme]["boss"] if rank == "boss" else data()["monsters"][kind]
    hp, atk, df = monster_stats(depth, m, rank)
    fx = ELITE_AFFIXES.get(affix, {}) if rank == "elite" else {}
    hp = max(2 * SCALE, round(hp * hp_mult * fx.get("hp_mult", 1)))
    df += fx.get("def", 0)
    name = m["name"] if rank != "elite" else f"{fx.get('name', '凶悍的')}{m['name']}"
    description = m["description"] + ("它比同类更壮、更凶，身上带着好几道旧伤。" if rank == "elite" else "")
    gold = [max(1, round(g / share)) for g in monster_gold(depth, rank)]
    props = {"on_death": {} if minion else {"gold": gold}, "dungeon": {"depth": depth, "rank": rank, "theme": theme}}
    if fx.get("extra_chance"):
        props["extra_chance"], props["base_attacks"] = fx["extra_chance"], attacks     # 迅捷的：多出来的那几下有几率落空
    attacks *= fx.get("attacks", 1)
    if attacks > 1:
        props["attacks"] = attacks
    if affix:
        props["affix"] = affix
        for key in ("dmg_mult", "frenzy", "lifesteal", "thorns", "thorns_chance"):
            if fx.get(key):
                props[key] = fx[key]
    if rank == "boss":
        # 技能按层数解锁（第 5 层一招、第 10 层两招、第 15 层全套），免疫、弱点
        if skills := unlocked_skills(m.get("skills") or [], depth):
            props["skills"] = skills
        for key in ("immune", "weak"):
            if m.get(key):
                props[key] = m[key]
    for flag in ("animal", "light_averse", "undead", "keen", "ranged"):
        if m.get(flag):
            props[flag] = True
    if m.get("perception"):
        props["perception"] = m["perception"]   # 察觉：迟钝 -1 / 敏锐 +1（进门被发现的几率、躲藏难度）
    for key in ("healer", "verb", "guard_allies"):   # 治疗的比例、出手的说法（"甩出一颗石子"）、盾卫护同伴的几率
        if m.get(key):
            props[key] = m[key]
    if rank == "boss" or fx.get("keen"):
        props["keen"] = True                    # 头目、警觉的精英：一进门就发现人，偷袭不了
    if m.get("on_hit"):
        props["on_hit"] = m["on_hit"]           # 打中时几率附带的效果（engine._on_hit）
    if plague := fx.get("plague"):
        # 瘟疫的精英：打中附带这一带的毒害；本来就有附带效果的，几率再加上去
        props["on_hit"] = ({**props["on_hit"], "chance": props["on_hit"].get("chance", 0.25) + plague} if props.get("on_hit")
                           else {"kind": THEME_PLAGUE.get(theme, "poison"), "chance": plague})
    if minion:
        props["minion"] = True
        props["dmg_mult"] = props.get("dmg_mult", 1) * SUMMON_DMG      # 小怪：血一半、下手一半
        if props.get("on_hit"):                                       # 附带的中毒、流血也减半
            props["on_hit"] = {**props["on_hit"], "value_mult": props["on_hit"].get("value_mult", 1) * SUMMON_DMG}
    # 已经有了也按现在的数值更新：不然改了 dungeon.yaml、rules.monster_stats，以前生成过的层数、种类还是旧数值
    cur.execute(
        """insert into npc_templates (id, name, description, persona, hostile, max_hp, attack, defense, props)
           values (%s, %s, %s, %s, true, %s, %s, %s, %s)
           on conflict (id) do update set name = excluded.name, description = excluded.description,
               persona = excluded.persona, max_hp = excluded.max_hp, attack = excluded.attack,
               defense = excluded.defense, props = excluded.props""",
        (tid, name, description, m["persona"], hp, atk, df, Jsonb(props)))
    return tid


def floor_info(cur: Cursor, room_id: str) -> dict:
    """地牢房间所在的那一层：depth、theme、party_size"""
    run, depth = parse_room(room_id)
    cur.execute("select depth, theme, party_size from dungeon_floors where run_id = %s and depth = %s", (run, depth))
    return cur.fetchone()


def spawn_wanderer(cur: Cursor, room: str) -> str:
    """游荡的怪跟进了房间（走路时的随机事件）：这一层主题里的普通怪，返回名字"""
    f = floor_info(cur, room)
    kind = random.choice(_kinds(data()["themes"][f["theme"]], f["depth"]))
    _spawn_group(cur, room, f["depth"], kind, "normal", f["theme"], f["party_size"] or 1)
    return data()["monsters"][kind]["name"]


def _kinds(theme: dict, depth: int) -> list[str]:
    """这一层能刷的怪（有的要到 min_floor 才出，比如狗头人盾卫）"""
    return [k for k in theme["monsters"] if data()["monsters"][k].get("min_floor", 1) <= depth] or theme["monsters"]


def _spawn_group(cur: Cursor, room: str, depth: int, kind: str, rank: str, theme: str, size: int, groups: int = 1) -> None:
    """放一群同种的怪：一个人一只，组队时"人数"只（这个房间一共 groups 群，总数不超过 ROOM_CAP，多的折成血）。
    钱按只数分，东西只有第一只带"""
    copies = party_copies(size, groups)
    affix = random.choice(list(ELITE_AFFIXES)) if rank == "elite" else None      # 同一群精英同一个词缀
    for i in range(copies):
        _spawn(cur, room, depth, kind, rank, theme, share=copies, hp_mult=size / copies, loot=i == 0,
               attacks=max(1, round(size / copies)), affix=affix)


def _spawn_boss(cur: Cursor, room: str, depth: int, kind: str, rank: str, theme: str, size: int) -> None:
    """头目、楼梯间守卫：只有一只，组队时多些血、一轮多动几次（不召小怪：组队时场面已经够乱）"""
    _spawn(cur, room, depth, kind, rank, theme, hp_mult=1 + BOSS_PARTY_HP * (size - 1), attacks=size, stair=True,
           affix=random.choice(list(ELITE_AFFIXES)) if rank == "elite" else None)


def spawn_minions(cur: Cursor, room: str, depth: int, kind: str, theme: str, count: int) -> list[str]:
    """头目叫来的、号令的精英带着的小怪：本层普通怪一半的血，不掉钱也不掉东西。返回名字"""
    names = []
    for _ in range(count):
        _spawn(cur, room, depth, kind, "normal", theme, hp_mult=SUMMON_HP, minion=True, loot=False)
        names.append(data()["monsters"][kind]["name"])
    return names


def _spawn(cur: Cursor, room: str, depth: int, kind: str, rank: str, theme: str, share: int = 1, hp_mult: float = 1.0,
           minion: bool = False, loot: bool = True, attacks: int = 1, stair: bool = False, affix: Optional[str] = None) -> None:
    tid = _template(cur, depth, kind, rank, theme, share, hp_mult, minion, attacks, affix)
    cur.execute("insert into npcs (template_id, room_id, hp) select id, %s, max_hp from npc_templates where id = %s"
                " returning id", (room, tid))
    npc_id = cur.fetchone()["id"]
    for item in _drops(kind, rank, theme, depth, stair) if loot else []:     # 身上带的东西，打死了掉在地上
        _put_item(cur, item, depth, npc=npc_id, boss=rank == "boss",
                  rarity={"boss": "rare", "elite": "uncommon"}.get(rank, "common"))
    if loot:
        for _ in range(gem_count(rank, depth)):
            put_gem(cur, pick_gem(theme), depth, npc=npc_id)
    for _ in range(ELITE_AFFIXES.get(affix, {}).get("minions", 0)):
        spawn_minions(cur, room, depth, kind, theme, 1)             # 号令的精英：带着一只同类小怪


# ============ 地图 ============

def _neighbors(cell: tuple[int, int]) -> list[tuple[str, tuple[int, int]]]:
    r, c = cell
    return [(d, (r + dr, c + dc)) for d, (dr, dc) in DIRS.items() if 0 <= r + dr < GRID and 0 <= c + dc < GRID]


def layout() -> tuple[tuple[int, int], tuple[int, int], set[frozenset]]:
    """(入口格, 楼梯格, 相连的格子对)。先随机走一棵连通所有格子的树，再多开一两扇门成环；楼梯在离入口最远的格子"""
    start = random.choice([(0, 0), (0, GRID - 1), (GRID - 1, 0), (GRID - 1, GRID - 1)])
    edges, seen, stack = set(), {start}, [start]
    while stack:
        options = [n for _, n in _neighbors(stack[-1]) if n not in seen]
        if not options:
            stack.pop()
            continue
        n = random.choice(options)
        edges.add(frozenset((stack[-1], n)))
        seen.add(n)
        stack.append(n)
    extra = [frozenset((a, b)) for a in seen for _, b in _neighbors(a) if frozenset((a, b)) not in edges]
    edges |= set(random.sample(sorted(set(extra), key=sorted), min(len(set(extra)), random.randint(1, 2))))
    # 按走过去要几步找最远的格子
    dist, queue = {start: 0}, [start]
    for cell in queue:
        for _, n in _neighbors(cell):
            if n not in dist and frozenset((cell, n)) in edges:
                dist[n] = dist[cell] + 1
                queue.append(n)
    far = max(dist.values())
    stairs = random.choice([c for c, d in dist.items() if d == far])
    return start, stairs, edges


def is_stone(depth: int) -> bool:
    """打完头目之后的那一层（6、11、16……），入口是传送石"""
    return depth > 1 and (depth - 1) % BOSS_EVERY == 0


def _make_floor(cur: Cursor, run: UUID, depth: int, above: Optional[str], size: int) -> str:
    """生成第 depth 层，返回入口房间 id。above 是往上回去的房间（第 1 层是地窖，往后是上一层的楼梯间；
    从地窖直接传送来的没有上一层，就不开往上的路）；size 是队伍人数：怪的血量、钱袋里的钱跟着涨"""
    themes = data()["themes"]
    # 主题随机，但不跟上面 THEME_GAP 层重复，连着下几层都是不同的地方
    cur.execute("select theme from dungeon_floors where run_id = %s and depth between %s and %s",
                (run, depth - THEME_GAP, depth - 1))
    recent = {r["theme"] for r in cur.fetchall()}
    theme_key = random.choice([k for k in themes if k not in recent] or list(themes))
    theme = themes[theme_key]
    start, stairs, edges = layout()
    cells = [(r, c) for r in range(GRID) for c in range(GRID)]
    rest = [c for c in cells if c not in (start, stairs)]
    random.shuffle(rest)
    # 七个普通格子：一个宝箱房、一个空房、一个随机事件房、其余战斗房
    kinds = {start: "entry", stairs: "stairs", rest[0]: "treasure", rest[1]: "empty", rest[2]: "event"}
    kinds |= {c: "combat" for c in rest[3:]}
    pool = random.sample(theme["rooms"], len(theme["rooms"]))
    for cell in cells:
        kind = kinds[cell]
        event = data()["events"][random.choice(theme["events"])] if kind == "event" else None
        text = (event if event else data()["stone"] if kind == "entry" and is_stone(depth)
                else theme[kind] if kind in ("entry", "stairs", "treasure")
                else pool.pop() if pool else random.choice(theme["rooms"]))
        rid = _room_id(run, depth, cell)
        light = (STONE_LIGHT if kind == "entry" and is_stone(depth)
                 else theme.get("light", 35) + LIGHT_OFFSET.get(text.get("light", "dim"), 0))
        props = {"dungeon": {"depth": depth, "theme": theme_key, "kind": kind},
                 # 头目层：楼梯间隔壁的房间是休息点，能多扎一次营（engine.do_camp）
                 **({"rest": True} if depth % BOSS_EVERY == 0 and kind != "stairs" and frozenset((cell, stairs)) in edges else {}),
                 "env": {"light": max(0, min(100, light)), "ground": text.get("ground", "normal"),
                         "cover": bool(text.get("cover"))}}
        if kind == "entry" and is_stone(depth):
            props["stone"] = True
        if kind == "empty":
            # 空房：搜索可能找到药草（和主题特产：森林的野莓、沼泽的解毒苔和沼泽菇）；调查可能翻出藏着的古币，每人一次
            props["forage"] = EMPTY_FORAGE + [{"item": e["item"], "chance": e.get("chance", 0.5), "cooldown": 86400}
                                              for e in loot_data().get("forage", {}).get(theme_key) or []]
            props["stash"] = {"gold": stash_gold(depth, size),
                              "difficulty": 2 + depth // 3}
        if event:
            # 事件房的东西跟酒馆武器桶一样是"取用处"：每人一次，要过技能判定（难度随层数涨）
            take = dict(event["take"], description=event["description"], once=True, repeat=True)
            if take.get("skill"):
                # 每 6 层难一级：大约跟玩家走到这层时的技能等级对齐（比等级高 2，起步 60%）
                take["difficulty"] = take.get("difficulty", 2) + depth // 6
            props["dispensers"] = {"event": take}
        cur.execute("insert into rooms (id, name, description, details, props) values (%s, %s, %s, %s, %s)",
                    (rid, f"第 {depth} 层·{text['name']}", text["description"], text.get("details", ""), Jsonb(props)))
    for cell in cells:
        rid = _room_id(run, depth, cell)
        for direction, n in _neighbors(cell):
            if frozenset((cell, n)) in edges:
                cur.execute("insert into room_exits (room_id, direction, to_room) values (%s, %s, %s)",
                            (rid, direction, _room_id(run, depth, n)))
        kind = kinds[cell]
        guarded = kind == "treasure" and random.random() < TREASURE_GUARD
        if kind in ("combat", "stairs") or guarded:
            lamps = []
            for i, ft in enumerate(random.sample(theme["features"], random.randint(1, 2))):
                cur.execute(
                    """insert into room_features (room_id, key, name, max_tier, max_uses, uses_left, respawn_seconds)
                       values (%s, %s, %s, %s, 1, 1, %s)""", (rid, f"f{i}", ft["name"], ft["max_tier"], FEATURE_RESPAWN))
                if ft.get("lamp"):
                    lamps.append(f"f{i}")
            if lamps:
                # 点着的火盆、烛台让房间更亮；灯被拿去砸人就暗下来（engine.do_stunt）
                cur.execute("""update rooms set props = jsonb_set(jsonb_set(props, '{env,lamps}', %s), '{env,light}',
                                 to_jsonb(least(100, (props->'env'->>'light')::int + %s)))
                               where id = %s""", (Jsonb(lamps), LAMP_LIGHT * len(lamps), rid))
        if kind == "combat":
            count = roll_groups(depth)
            ranks = ["elite" if i == 0 and random.random() < elite_chance(depth) else "normal" for i in range(count)]
            for rank in ranks:
                _spawn_group(cur, rid, depth, random.choice(_kinds(theme, depth)), rank, theme_key, size, len(ranks))
        elif kind == "stairs":
            boss = depth % BOSS_EVERY == 0
            _spawn_boss(cur, rid, depth, "boss" if boss else random.choice(_kinds(theme, depth)), "boss" if boss else "elite",
                        theme_key, size)
            cur.execute("insert into room_exits (room_id, direction, to_room) values (%s, 'down', %s)", (rid, GATE))
        elif kind == "treasure":
            # 越暗的房间钱越多（按房间本来的光亮，不按后来点没点灯）
            dark = (50 - (theme.get("light", 35) + LIGHT_OFFSET.get(theme["treasure"].get("light", "dim"), 0))) / 50
            coins = treasure_gold(depth, dark, size)    # 捡的人会分给在场的队友
            cur.execute("insert into item_instances (template_id, room_id, props) values ('gold_pouch', %s, %s)",
                        (rid, Jsonb({"gold": coins})))
            cur.execute("insert into item_instances (template_id, room_id) values ('herb', %s)", (rid,))
            rules = loot_data()["rules"]
            if random.random() < rules.get("treasure_item_chance", 0):
                # 一半从这个主题的宝箱池抽，一半从通用池抽
                themed = random.random() < rules.get("treasure_theme_share", 0.5)
                item = (_pick(loot_data()["treasure"].get(theme_key), depth) if themed else None) \
                    or _pick(loot_data()["pool"], depth)
                if item:
                    _put_item(cur, item, depth, room=rid, rarity="uncommon")
            if random.random() < gem_rules()["drops"]["treasure"] * deep_mult(depth):
                put_gem(cur, pick_gem(theme_key, 0.5), depth, room=rid)        # 宝箱房额外一颗宝石：一半本主题、一半通用
            if guarded:
                # 有一半的宝箱房有怪守着（越深越可能是精英）
                rank = "elite" if random.random() < elite_chance(depth) else "normal"
                _spawn_group(cur, rid, depth, random.choice(_kinds(theme, depth)), rank, theme_key, size)
    if theme_key == "castle" and theme.get("cell"):
        _prison_cell(cur, run, depth, theme, cells, start, size)
    entry = _room_id(run, depth, start)
    if FLOOR_HOOK:
        FLOOR_HOOK(run, depth)              # 后台让 AI 重写还没人进过的房间（server._describe_floor）
    if above:
        cur.execute("insert into room_exits (room_id, direction, to_room) values (%s, 'up', %s)", (entry, above))
    cur.execute("""insert into dungeon_floors (run_id, depth, theme, entry_room, stairs_room, party_size)
                   values (%s, %s, %s, %s, %s, %s)""", (run, depth, theme_key, entry, _room_id(run, depth, stairs), size))
    return entry


# ============ 进出 ============

def _floor(cur: Cursor, run: UUID, depth: int, above: Optional[str], size: int) -> tuple[str, str]:
    """(这一层的入口房间, 主题名)，还没有就按现在的队伍人数生成"""
    cur.execute("select 1 from dungeon_floors where run_id = %s and depth = %s", (run, depth))
    if not cur.fetchone():
        _make_floor(cur, run, depth, above, size)
    cur.execute("select entry_room, theme from dungeon_floors where run_id = %s and depth = %s", (run, depth))
    row = cur.fetchone()
    return row["entry_room"], row["theme"]


def _run_for(cur: Cursor, player) -> UUID:
    """这个人（的队伍）的地牢：组了队就用队伍那份，没组队用自己那份，都没有就新开一份"""
    if player.party_id:
        cur.execute("select id from dungeon_runs where party_id = %s", (player.party_id,))
    else:
        cur.execute("select id from dungeon_runs where owner = %s and party_id is null", (player.id,))
    row = cur.fetchone()
    if row:
        return row["id"]
    run = uuid4()
    cur.execute("insert into dungeon_runs (id, owner, party_id) values (%s, %s, %s)", (run, player.id, player.party_id))
    return run


def _party_size(cur: Cursor, player, online: str) -> int:
    """队伍里在线的人数（自己总算一个）"""
    if not player.party_id:
        return 1
    cur.execute(f"""select count(*) as n from players where party_id = %s
                    and (id = %s or last_active_at > now() - interval '{online}')""", (player.party_id, player.id))
    return max(1, cur.fetchone()["n"])


def _arrive(cur: Cursor, player, run: UUID, depth: int, above: Optional[str], online: str) -> tuple[str, list[str]]:
    """到第 depth 层（没有就按现在的队伍人数生成）：记最深层数、到了传送石就记下来。返回 (入口房间, facts)"""
    entry, theme = _floor(cur, run, depth, above, _party_size(cur, player, online))
    cur.execute("update dungeon_runs set last_active_at = now() where id = %s", (run,))
    t = data()["themes"][theme]
    facts = [f"{player.name}来到了远古地牢第 {depth} 层：{t['name']}。{t['intro']}"] + arrived(cur, [player.name], depth)
    if depth % BOSS_EVERY == 0:
        facts.append(f"这一层的楼梯间守着{t['boss']['name']}")
    return entry, facts


def arrived(cur: Cursor, names: list[str], depth: int) -> list[str]:
    """这些人到了第 depth 层：更新最深层数；这层有传送石就记进他们能传送的层（players.waypoints）"""
    cur.execute("update players set deepest_floor = greatest(deepest_floor, %s) where name = any(%s)", (depth, names))
    # 莉娜打的专属剑（lina_blade）跟着人一起变强：伤害 (5 + 最深层数/3，最多 12) × 10，再加升级的等级 × 10
    cur.execute("""update item_instances i set props = i.props || jsonb_build_object('damage',
                     10 * least(12, 5 + p.deepest_floor / 3) + 10 * coalesce((i.props->>'plus')::int, 0))
                   from players p where i.player_id = p.id and p.name = any(%s) and i.template_id = 'lina_blade'""", (names,))
    if not is_stone(depth):
        return []
    cur.execute("""update players set waypoints = array_append(waypoints, %s)
                   where name = any(%s) and not (%s = any(waypoints)) returning name""", (depth, names, depth))
    new = [r["name"] for r in cur.fetchall()]
    return [f"传送石的纹路亮了一下，记住了{'、'.join(new)}：以后在地窖里说「传送到第 {depth} 层」就能直接过来；"
            f"在传送石边说「回城」就能回到地面"] if new else []


def through_gate(cur: Cursor, player, from_room: str, online: str) -> tuple[str, list[str]]:
    """走进地窖的漆黑入口、或者从楼梯间往下：返回 (要去的房间, facts)。第一次去的那层当场生成，
    难度按队伍里在线的人数（online 是算在线的时间窗）"""
    if is_dungeon(from_room):
        run, depth = parse_room(from_room)
        return _arrive(cur, player, run, depth + 1, from_room, online)
    cleanup(cur)
    return _arrive(cur, player, _run_for(cur, player), 1, from_room, online)


def teleport_to(cur: Cursor, player, depth: int, online: str) -> tuple[str, list[str]]:
    """从地窖直接传送到有传送石的那一层（自己或队伍那份地牢，没有就生成；上面几层不生成）"""
    cleanup(cur)
    return _arrive(cur, player, _run_for(cur, player), depth, None, online)


def waypoints_text(waypoints: list[int]) -> str:
    if not waypoints:
        return "还没有到过任何一块传送石（打倒第 5 层的头目，下到第 6 层就能找到第一块）"
    return "能直接传送到：" + "、".join(f"第 {d} 层" for d in sorted(waypoints)) + "（在地窖里说\"传送到第 N 层\"）"


def touch(cur: Cursor, room_id: str) -> None:
    """有人在地牢里走动：这份地牢、这一层还活着"""
    if is_dungeon(room_id):
        run, depth = parse_room(room_id)
        cur.execute("update dungeon_runs set last_active_at = now() where id = %s", (run,))
        cur.execute("update dungeon_floors set last_active_at = now() where run_id = %s and depth = %s", (run, depth))


def log_fights(cur: Cursor, npc_ids: list, outcome: str) -> None:
    """头目、精英打完（或者这一层回收时还活着）记一行战斗记录（fight_log）：放了哪些招、有没有被打断、
    打了玩家多少、打倒几次人。只记真交过手的（出过手或者打中过人）"""
    cur.execute(
        """insert into fight_log (npc_template, name, rank, affix, theme, depth, players, casts, interrupted, acts, dealt, downs,
                                 sneaks, hides, outcome)
           select t.id, t.name, t.props->'dungeon'->>'rank', t.props->>'affix', t.props->'dungeon'->>'theme',
                  (t.props->'dungeon'->>'depth')::int,
                  coalesce(array(select jsonb_array_elements_text(n.tally->'foes')), '{}'),
                  coalesce(array(select jsonb_array_elements_text(n.tally->'casts')), '{}'),
                  coalesce((n.tally->>'interrupted')::int, 0), coalesce((n.tally->>'acts')::int, 0),
                  coalesce((n.tally->>'dealt')::int, 0), coalesce((n.tally->>'downs')::int, 0),
                  coalesce((n.tally->>'sneaks')::int, 0), coalesce((n.tally->>'hides')::int, 0), %s
           from npcs n join npc_templates t on t.id = n.template_id
           where n.id = any(%s) and t.props->'dungeon'->>'rank' in ('boss', 'elite')
             and (n.tally ? 'acts' or n.tally ? 'dealt' or n.tally ? 'foes')""", (outcome, list(npc_ids)))


def cleanup(cur: Cursor) -> None:
    """按层删：没人在、STALE 没动静的一层删掉（先删引用房间的事件、回合、怪，再删房间，出口、地形、地上的东西跟着删）。
    一份地牢的层都删光了，这份地牢也删掉。上面的层删了，从下面往上走的路就断了（传送石、回城水晶、复活照常）。
    进地牢、传送时顺手清一次，服务器也每隔几分钟清一次（server._sweep_dungeons）"""
    cur.execute(
        f"""select f.run_id, f.depth from dungeon_floors f where f.last_active_at < now() - interval '{STALE}'
            and not exists (select 1 from players p
                            where p.room_id like 'dg-' || replace(f.run_id::text, '-', '') || '-' || f.depth || '-%')""")
    for row in cur.fetchall():
        prefix = f"dg-{row['run_id'].hex}-{row['depth']}-%"
        cur.execute("select id from npcs where room_id like %s and alive", (prefix,))
        log_fights(cur, [r["id"] for r in cur.fetchall()], "unfinished")     # 打了一半的头目、精英也记一笔
        cur.execute("delete from events where room_id like %s", (prefix,))
        cur.execute("delete from combat_rounds where room_id like %s", (prefix,))
        cur.execute("delete from npcs where room_id like %s", (prefix,))
        cur.execute("delete from rooms where id like %s", (prefix,))
        cur.execute("delete from dungeon_floors where run_id = %s and depth = %s", (row["run_id"], row["depth"]))
    cur.execute(f"""delete from dungeon_runs r where last_active_at < now() - interval '{STALE}'
                    and not exists (select 1 from dungeon_floors f where f.run_id = r.id)""")


def light_word(light: int) -> str:
    return ("明亮" if light >= 70 else "昏暗但看得清" if light >= 50 else "昏暗，看东西吃力" if light >= 20
            else "几乎一片漆黑")


def env_text(env: dict, light: int) -> str:
    """给 AI 看的环境说明（写进房间细节）：light 是算上灯、火把之后的光亮"""
    parts = [f"光亮 {light}/100，{light_word(light)}"]
    if env.get("ground") == "water":
        parts.append("地上积水泥泞，行动不便，很难灵活闪躲，也不好逃跑")
    if env.get("cover"):
        parts.append("有能躲藏的掩体")
    return "【环境】" + "；".join(p for p in parts if p)


def leaderboard(cur: Cursor, limit: int = 10) -> str:
    """地窖石碑上刻的：到过地牢最深处的人"""
    cur.execute("select name, deepest_floor from players where deepest_floor > 0 order by deepest_floor desc, name limit %s",
                (limit,))
    rows = cur.fetchall()
    if not rows:
        return "石碑上还是一片空白，没有刻下任何人的名字。"
    return "石碑上按深浅刻着到过远古地牢深处的人：" + "；".join(f"{r['name']}，第 {r['deepest_floor']} 层" for r in rows)
