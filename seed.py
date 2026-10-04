"""
把 data/world.yaml 导入数据库。
- 静态内容（房间、模板）用 upsert，可以反复跑
- seed：同步设定，再重置世界里的 NPC 和物品实例（玩家身上的东西不动）
- sync：只同步设定（房间描述、物品说明、NPC 人设、出口、地形、任务），世界状态不动，改人设后用这个；
  新加的 NPC、房间里新加的物品要 seed 才会出现

用法（DATABASE_URL 写在 .env 里）:
  python seed.py               （默认 data/world.yaml）
"""
import os
import sys

import psycopg
import yaml
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from paths import data_path


DEFAULT_RESPAWN = 300                   # 物品被拿走后默认多少秒重新出现


EXTRA_ITEMS = "items_dungeon.yaml"      # 地牢物品单独放一个文件，跟 world.yaml 的 items 合在一起入库


DEEP_ITEMS = "deep_items.yaml"          # 深层物品：主文件没有的才补进来（泉水这种已有的以主文件为准）


def _all_items(world) -> dict:
    items = dict(world["items"])
    for name, override in ((EXTRA_ITEMS, True), (DEEP_ITEMS, False)):
        path = data_path(name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for key, value in ((yaml.safe_load(f) or {}).get("items") or {}).items():
                    if override or key not in items:
                        items[key] = value
    return items


def _content(cur, world, reset: bool) -> None:
    """房间、物品模板、出口、地形、NPC 模板。reset=False 时不动门锁状态和地形剩余次数"""
    for rid, r in world["rooms"].items():
        props = {k: r[k] for k in ("forage", "dispensers") if k in r}
        cur.execute(
            """insert into rooms (id, name, description, details, props) values (%s, %s, %s, %s, %s)
               on conflict (id) do update set name = excluded.name, description = excluded.description,
                 details = excluded.details, props = excluded.props""",
            (rid, r["name"], r["description"], r.get("details", ""), Jsonb(props)),
        )

    for iid, it in _all_items(world).items():
        cur.execute(
            """insert into item_templates (id, name, description, type, takeable, stackable, damage, defense, heal, slot, props)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (id) do update set
                 name = excluded.name, description = excluded.description, type = excluded.type,
                 takeable = excluded.takeable, stackable = excluded.stackable, damage = excluded.damage,
                 defense = excluded.defense, heal = excluded.heal, slot = excluded.slot, props = excluded.props""",
            (iid, it["name"], it["description"], it["type"], it.get("takeable", True),
             it.get("stackable", it["type"] == "consumable"),
             it.get("damage", 0), it.get("defense", 0), it.get("heal", 0),
             # 武器默认拿在手上，护甲默认穿在身上，别的写明 slot（护符 neck、戒指 ring）
             it.get("slot", {"weapon": "hand", "armor": "chest"}.get(it["type"])), Jsonb(it.get("props", {}))),
        )

    # 出口依赖房间，放在房间后面
    for rid, r in world["rooms"].items():
        for direction, ex in r.get("exits", {}).items():
            cur.execute(
                """insert into room_exits (room_id, direction, to_room, locked, key_item, relock_seconds)
                   values (%s, %s, %s, %s, %s, %s)
                   on conflict (room_id, direction) do update set
                     to_room = excluded.to_room, key_item = excluded.key_item, relock_seconds = excluded.relock_seconds"""
                + (", locked = excluded.locked, unlocked_at = null" if reset else ""),
                (rid, direction, ex["to"], ex.get("locked", False), ex.get("key"), ex.get("relock")),
            )

    for rid, r in world["rooms"].items():
        for key, ft in r.get("features", {}).items():
            cur.execute(
                """insert into room_features (room_id, key, name, max_tier, max_uses, uses_left, respawn_seconds)
                   values (%(r)s, %(k)s, %(n)s, %(t)s, %(u)s, %(u)s, %(s)s)
                   on conflict (room_id, key) do update set
                     name = excluded.name, max_tier = excluded.max_tier, max_uses = excluded.max_uses,
                     respawn_seconds = excluded.respawn_seconds"""
                + (", uses_left = excluded.max_uses, used_at = null" if reset
                   else ", uses_left = least(room_features.uses_left, excluded.max_uses)"),
                {"r": rid, "k": key, "n": ft["name"], "t": ft["max_tier"], "u": ft.get("uses", 1),
                 "s": ft.get("respawn", DEFAULT_RESPAWN)},
            )
        keys = list(r.get("features", {}))
        cur.execute("delete from room_features where room_id = %s and key <> all(%s)", (rid, keys))

    for nid, n in world["npcs"].items():
        s = n.get("stats") or {}
        props = dict(n.get("props", {}))
        if "respawn" in n:
            props["respawn_seconds"] = n["respawn"]
        cur.execute(
            """insert into npc_templates (id, name, description, persona, hostile, max_hp, attack, defense, props)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (id) do update set
                 name = excluded.name, description = excluded.description, persona = excluded.persona,
                 hostile = excluded.hostile, max_hp = excluded.max_hp, attack = excluded.attack,
                 defense = excluded.defense, props = excluded.props""",
            (nid, n["name"], n["description"], n["persona"], n.get("hostile", False),
             s.get("max_hp"), s.get("attack", 0), s.get("defense", 0), Jsonb(props)),
        )

    # 任务依赖 NPC 模板和物品模板，放最后；yaml 里删掉的任务连同玩家进度一起删
    quests = world.get("quests", {})
    for qid, q in quests.items():
        cur.execute(
            """insert into quests (id, giver, name, hook, goal, done_flag, needs_item, hidden, reward_item, after)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (id) do update set giver = excluded.giver, name = excluded.name, hook = excluded.hook,
                 goal = excluded.goal, done_flag = excluded.done_flag, needs_item = excluded.needs_item,
                 hidden = excluded.hidden, reward_item = excluded.reward_item, after = excluded.after""",
            (qid, q["giver"], q["name"], q["hook"], q["goal"], q.get("done"), q.get("needs"), q.get("hidden", False),
             q.get("reward"), q.get("after", "")),
        )
    cur.execute("delete from quests where id <> all(%s)", (list(quests),))


def _room_items(world) -> dict[tuple[str, str], int]:
    """房间地上的刷新点 {(房间, 物品): 刷新秒数}。写法：bread 或 {item: bread, respawn: 120}"""
    return {(rid, e["item"] if isinstance(e, dict) else e):
            e.get("respawn", DEFAULT_RESPAWN) if isinstance(e, dict) else DEFAULT_RESPAWN
            for rid, r in world["rooms"].items() for e in r.get("items", [])}


def sync(conn, world) -> None:
    """只同步设定，不动世界状态。房间地上的刷新点对齐 yaml：删掉的连地上那件一起收走，新加的马上放一件"""
    with conn.transaction():
        cur = conn.cursor()
        _content(cur, world, reset=False)
        wanted = _room_items(world)
        cur.execute("select id, room_id, template_id from spawns where room_id is not null")
        for sid, rid, item in cur.fetchall():
            if (rid, item) in wanted:
                cur.execute("update spawns set respawn_seconds = %s where id = %s", (wanted.pop((rid, item)), sid))
            else:
                cur.execute("delete from spawns where id = %s", (sid,))
                cur.execute("delete from item_instances where room_id = %s and template_id = %s", (rid, item))
        for (rid, item), respawn in wanted.items():
            cur.execute("insert into spawns (room_id, template_id, respawn_seconds) values (%s, %s, %s)",
                        (rid, item, respawn))
            cur.execute("insert into item_instances (template_id, room_id) values (%s, %s)", (item, rid))
        # 新加的 NPC（房间 npcs 里有、库里还没有这个模板的实例）：放进去，身上的东西和补货规则一起建好
        for rid, r in world["rooms"].items():
            for npc_tid in r.get("npcs", []):
                cur.execute("select 1 from npcs where template_id = %s", (npc_tid,))
                if cur.fetchone():
                    continue
                n = world["npcs"][npc_tid]
                cur.execute("insert into npcs (template_id, room_id, hp) values (%s, %s, %s) returning id",
                            (npc_tid, rid, (n.get("stats") or {}).get("hp")))
                npc_id = cur.fetchone()[0]
                for item in n.get("inventory", []):
                    cur.execute("insert into item_instances (template_id, npc_id) values (%s, %s)", (item, npc_id))
                    cur.execute("insert into spawns (npc_template, template_id, respawn_seconds) values (%s, %s, %s)",
                                (npc_tid, item, n.get("restock", DEFAULT_RESPAWN)))


def seed(conn, world) -> None:
    with conn.transaction():
        cur = conn.cursor()
        _content(cur, world, reset=True)

        # 重置世界里的实例（不碰玩家背包）和刷新点
        cur.execute("delete from item_instances where player_id is null")
        cur.execute("delete from npcs")
        cur.execute("delete from spawns")

        for nid, n in world["npcs"].items():
            for item in n.get("inventory", []):
                cur.execute(
                    "insert into spawns (npc_template, template_id, respawn_seconds) values (%s, %s, %s)",
                    (nid, item, n.get("restock", DEFAULT_RESPAWN)),
                )

        for (rid, item), respawn in _room_items(world).items():
            cur.execute("insert into item_instances (template_id, room_id) values (%s, %s)", (item, rid))
            cur.execute("insert into spawns (room_id, template_id, respawn_seconds) values (%s, %s, %s)",
                        (rid, item, respawn))
        for rid, r in world["rooms"].items():
            for npc_tid in r.get("npcs", []):
                s = world["npcs"][npc_tid].get("stats") or {}
                cur.execute(
                    "insert into npcs (template_id, room_id, hp) values (%s, %s, %s) returning id",
                    (npc_tid, rid, s.get("hp")),
                )
                npc_id = cur.fetchone()[0]
                for item in world["npcs"][npc_tid].get("inventory", []):
                    cur.execute("insert into item_instances (template_id, npc_id) values (%s, %s)", (item, npc_id))


if __name__ == "__main__":
    load_dotenv()
    path = sys.argv[1] if len(sys.argv) > 1 else data_path("world.yaml")
    with open(path, encoding="utf-8") as f:
        world = yaml.safe_load(f)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        seed(conn, world)
    print("导入完成")
