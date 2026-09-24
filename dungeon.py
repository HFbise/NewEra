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

GATE = "dungeon_gate"                   # 地窖的入口、楼梯间往下都连到这个占位房间，引擎走到这里改由 through_gate 决定去哪
ENTRANCE = "cellar"                     # 地牢第 1 层往上回到这里
STALE = "30 minutes"                    # 没人在里面、这么久没动静的地牢删掉
GRID = 3                                # 一层 GRID × GRID 个房间
BOSS_EVERY = 5                          # 每几层楼梯间守着头目
THEME_GAP = 3                           # 主题不跟上面几层重复
TREASURE_GUARD = 0.5                    # 宝箱房有怪守着的几率
# 组队时多刷怪，不再给怪加血（战斗按回合结算，每只怪一轮只出手一次，以前是每个人出手它都还手）：
# 每只普通怪、精英变成"人数"只，一个房间最多 ROOM_CAP 只，多出来的折成血；钱按只数分、东西只有第一只带，
# 整个房间的收获跟以前一样。头目、楼梯间守卫还是一只，血 × (1 + BOSS_PARTY_HP × (人数 − 1))，
# 一轮出手"人数"次（跟以前每个人出手它都还手一样）
ROOM_CAP = 6
BOSS_PARTY_HP = 0.8

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
LOOT_NOT_YET = {"map_scrap", "warden_key", "cursed_twinblade", "greed_ring"}


def data() -> dict:
    global _data
    if _data is None:
        with open(os.path.join(os.path.dirname(__file__), "dungeon.yaml"), encoding="utf-8") as f:
            _data = yaml.safe_load(f)
    return _data


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


def _drops(kind: str, rank: str, theme: str, depth: int) -> list[str]:
    """一只怪身上带的东西（打死掉在地上）。精英专属掉率 × elite_mult，另外有几率从通用池抽；头目二选一必掉"""
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
    return out


def _put_item(cur: Cursor, template: str, depth: int, *, room: Optional[str] = None, npc: Optional[UUID] = None,
              boss: bool = False) -> None:
    """放一件东西。头目的招牌装备每深 boss_upgrade_every 层自带 +1（10 层 +1，15 层 +2）"""
    props = {}
    every = loot_data()["rules"].get("boss_upgrade_every", 0)
    if boss and every and (plus := depth // every - 1) > 0:
        cur.execute("select name, type, damage, defense from item_templates where id = %s", (template,))
        t = cur.fetchone()
        if t["type"] == "weapon":
            props = {"plus": plus, "damage": t["damage"] + plus, "name": f"{t['name']} +{plus}"}
        elif t["type"] == "armor" and t["defense"]:
            props = {"plus": plus, "defense": t["defense"] + plus, "name": f"{t['name']} +{plus}"}
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


def parse_room(room_id: str) -> tuple[UUID, int]:
    """地牢房间 id → (哪份地牢, 第几层)"""
    _, run, depth, _ = room_id.split("-")
    return UUID(run), int(depth)


# ============ 怪物数值 ============
# 第 n 层普通怪：血 6+n、攻击 2+n/3+n/12、防御 1+n/4（攻击跟着玩家大概的防御涨，见 memory/dungeon-design.md 的模拟）；
# 每种怪再按 dungeon.yaml 的倍率、加减调整。精英血 ×1.8 攻 +1，头目血 ×2 攻 +1 防 +1

def monster_stats(depth: int, mods: dict, rank: str = "normal") -> tuple[int, int, int]:
    hp = (6 + depth) * mods.get("hp", 1.0)
    atk = 2 + depth // 3 + depth // 12 + mods.get("atk", 0)
    df = 1 + depth // 4 + mods.get("def", 0)
    if rank == "elite":
        hp, atk = hp * 1.8, atk + 1
    elif rank == "boss":
        hp, atk, df = hp * 2, atk + 1, df + 1
    return max(2, round(hp)), max(1, atk), max(0, df)


def _gold(depth: int, rank: str) -> list[int]:
    """掉的金币跟着层数涨（约 1.2^层数），精英 ×2，头目 ×5"""
    scale = 1.2 ** depth * {"normal": 1, "elite": 2, "boss": 5}[rank]
    return [max(1, round(2 * scale)), max(2, round(5 * scale))]


def _template(cur: Cursor, depth: int, kind: str, rank: str, theme: str, share: int = 1, hp_mult: float = 1.0,
              minion: bool = False, attacks: int = 1) -> str:
    """这一层这种怪的 NPC 模板，没有就建。share：钱分给几只（组队多刷的同一群）；hp_mult：血的倍数；
    minion：头目带的小怪，不掉钱；attacks：战斗回合里一轮出手几次（房间满了折成血的，出手也跟着多）"""
    # 头目按主题分（每个主题的头目不一样），别的按怪的种类
    tid = (f"dg_{theme + '_' if rank == 'boss' else ''}{kind}_{depth}" + ("" if rank == "normal" else f"_{rank}")
           + (f"_g{share}" if share > 1 else "") + (f"_h{round(hp_mult * 10)}" if hp_mult != 1 else "")
           + ("_m" if minion else "") + (f"_a{attacks}" if attacks > 1 else ""))
    m = data()["themes"][theme]["boss"] if rank == "boss" else data()["monsters"][kind]
    hp, atk, df = monster_stats(depth, m, rank)
    hp = max(2, round(hp * hp_mult))
    name = m["name"] if rank != "elite" else f"凶悍的{m['name']}"
    description = m["description"] + ("它比同类更壮、更凶，身上带着好几道旧伤。" if rank == "elite" else "")
    gold = [max(1, round(g / share)) for g in _gold(depth, rank)]
    props = {"on_death": {} if minion else {"gold": gold}, "dungeon": {"depth": depth, "rank": rank}}
    if attacks > 1:
        props["attacks"] = attacks
    for flag in ("animal", "light_averse", "undead", "keen"):
        if m.get(flag):
            props[flag] = True
    if rank == "boss":
        props["keen"] = True                    # 头目都是警觉的：一进门就发现人，偷袭不了
    if m.get("on_hit"):
        props["on_hit"] = m["on_hit"]           # 打中时几率附带的效果（engine._on_hit）
    cur.execute(
        """insert into npc_templates (id, name, description, persona, hostile, max_hp, attack, defense, props)
           values (%s, %s, %s, %s, true, %s, %s, %s, %s) on conflict (id) do nothing""",
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
    kind = random.choice(data()["themes"][f["theme"]]["monsters"])
    _spawn_group(cur, room, f["depth"], kind, "normal", f["theme"], f["party_size"] or 1)
    return data()["monsters"][kind]["name"]


def _spawn_group(cur: Cursor, room: str, depth: int, kind: str, rank: str, theme: str, size: int, groups: int = 1) -> None:
    """放一群同种的怪：一个人一只，组队时"人数"只（这个房间一共 groups 群，总数不超过 ROOM_CAP，多的折成血）。
    钱按只数分，东西只有第一只带"""
    copies = max(1, min(size, ROOM_CAP // max(1, groups)))
    for i in range(copies):
        _spawn(cur, room, depth, kind, rank, theme, share=copies, hp_mult=size / copies, loot=i == 0,
               attacks=max(1, round(size / copies)))


def _spawn_boss(cur: Cursor, room: str, depth: int, kind: str, rank: str, theme: str, size: int) -> None:
    """头目、楼梯间守卫：只有一只，组队时多些血、一轮多动几次（不召小怪：组队时场面已经够乱）"""
    _spawn(cur, room, depth, kind, rank, theme, hp_mult=1 + BOSS_PARTY_HP * (size - 1), attacks=size)


def _spawn(cur: Cursor, room: str, depth: int, kind: str, rank: str, theme: str, share: int = 1, hp_mult: float = 1.0,
           minion: bool = False, loot: bool = True, attacks: int = 1) -> None:
    tid = _template(cur, depth, kind, rank, theme, share, hp_mult, minion, attacks)
    cur.execute("insert into npcs (template_id, room_id, hp) select id, %s, max_hp from npc_templates where id = %s"
                " returning id", (room, tid))
    npc_id = cur.fetchone()["id"]
    for item in _drops(kind, rank, theme, depth) if loot else []:     # 身上带的东西，打死了掉在地上
        _put_item(cur, item, depth, npc=npc_id, boss=rank == "boss")


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
                 "env": {"light": max(0, min(100, light)), "ground": text.get("ground", "normal"),
                         "cover": bool(text.get("cover"))}}
        if kind == "entry" and is_stone(depth):
            props["stone"] = True
        if kind == "empty":
            # 空房：搜索可能找到药草（和主题特产：森林的野莓、沼泽的解毒苔和沼泽菇）；调查可能翻出藏着的古币，每人一次
            props["forage"] = EMPTY_FORAGE + [{"item": e["item"], "chance": e.get("chance", 0.5), "cooldown": 86400}
                                              for e in loot_data().get("forage", {}).get(theme_key) or []]
            props["stash"] = {"gold": max(2, round(random.randint(4, 8) * 1.2 ** depth)) * size,
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
            count = random.randint(1, min(3, 1 + depth // 8))
            ranks = ["elite" if i == 0 and random.random() < min(0.35, 0.02 * depth) else "normal" for i in range(count)]
            for rank in ranks:
                _spawn_group(cur, rid, depth, random.choice(theme["monsters"]), rank, theme_key, size, len(ranks))
        elif kind == "stairs":
            boss = depth % BOSS_EVERY == 0
            _spawn_boss(cur, rid, depth, "boss" if boss else random.choice(theme["monsters"]), "boss" if boss else "elite",
                        theme_key, size)
            cur.execute("insert into room_exits (room_id, direction, to_room) values (%s, 'down', %s)", (rid, GATE))
        elif kind == "treasure":
            # 越暗的房间钱越多（按房间本来的光亮，不按后来点没点灯）
            dark = (50 - (theme.get("light", 35) + LIGHT_OFFSET.get(theme["treasure"].get("light", "dim"), 0))) / 50
            coins = round(random.randint(8, 15) * 1.2 ** depth * (1 + 0.5 * dark)) * size   # 捡的人会分给在场的队友
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
                    _put_item(cur, item, depth, room=rid)
            if guarded:
                # 有一半的宝箱房有怪守着（越深越可能是精英）
                rank = "elite" if random.random() < min(0.35, 0.02 * depth) else "normal"
                _spawn_group(cur, rid, depth, random.choice(theme["monsters"]), rank, theme_key, size)
    entry = _room_id(run, depth, start)
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
    """有人在地牢里走动：这份地牢还活着"""
    if is_dungeon(room_id):
        cur.execute("update dungeon_runs set last_active_at = now() where id = %s", (parse_room(room_id)[0],))


def cleanup(cur: Cursor) -> None:
    """删掉没人在里面、STALE 没动静的地牢：先删引用房间的事件和怪，再删房间（出口、地形、地上的东西跟着删）"""
    cur.execute(
        f"""select id from dungeon_runs r where last_active_at < now() - interval '{STALE}'
            and not exists (select 1 from players p where p.room_id like 'dg-' || replace(r.id::text, '-', '') || '-%')""")
    for row in cur.fetchall():
        prefix = f"dg-{row['id'].hex}-%"
        cur.execute("delete from events where room_id like %s", (prefix,))
        cur.execute("delete from npcs where room_id like %s", (prefix,))
        cur.execute("delete from rooms where id like %s", (prefix,))
        cur.execute("delete from dungeon_runs where id = %s", (row["id"],))


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
