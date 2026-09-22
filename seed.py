"""
把 world.yaml 导入数据库。
- 静态内容（房间、模板）用 upsert，可以反复跑
- 世界里的 NPC 和物品实例会被重置（玩家身上的东西不动）

用法（DATABASE_URL 写在 .env 里）:
  python seed.py world.yaml
"""
import os
import sys

import psycopg
import yaml
from dotenv import load_dotenv
from psycopg.types.json import Jsonb


DEFAULT_RESPAWN = 300                   # 物品被拿走后默认多少秒重新出现


def seed(conn, world):
    with conn.transaction():
        cur = conn.cursor()

        for rid, r in world["rooms"].items():
            cur.execute(
                """insert into rooms (id, name, description, details) values (%s, %s, %s, %s)
                   on conflict (id) do update set name = excluded.name, description = excluded.description,
                     details = excluded.details""",
                (rid, r["name"], r["description"], r.get("details", "")),
            )

        for iid, it in world["items"].items():
            cur.execute(
                """insert into item_templates (id, name, description, type, takeable, stackable, damage, defense, heal, props)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (id) do update set
                     name = excluded.name, description = excluded.description, type = excluded.type,
                     takeable = excluded.takeable, stackable = excluded.stackable, damage = excluded.damage,
                     defense = excluded.defense, heal = excluded.heal, props = excluded.props""",
                (iid, it["name"], it["description"], it["type"], it.get("takeable", True),
                 it.get("stackable", it["type"] == "consumable"),
                 it.get("damage", 0), it.get("defense", 0), it.get("heal", 0), Jsonb(it.get("props", {}))),
            )

        # 出口依赖房间，放在房间后面
        for rid, r in world["rooms"].items():
            for direction, ex in r.get("exits", {}).items():
                cur.execute(
                    """insert into room_exits (room_id, direction, to_room, locked, key_item, relock_seconds)
                       values (%s, %s, %s, %s, %s, %s)
                       on conflict (room_id, direction) do update set
                         to_room = excluded.to_room, locked = excluded.locked, key_item = excluded.key_item,
                         relock_seconds = excluded.relock_seconds, unlocked_at = null""",
                    (rid, direction, ex["to"], ex.get("locked", False), ex.get("key"), ex.get("relock")),
                )

        for rid, r in world["rooms"].items():
            for key, ft in r.get("features", {}).items():
                cur.execute(
                    """insert into room_features (room_id, key, name, max_tier, max_uses, uses_left, respawn_seconds)
                       values (%(r)s, %(k)s, %(n)s, %(t)s, %(u)s, %(u)s, %(s)s)
                       on conflict (room_id, key) do update set
                         name = excluded.name, max_tier = excluded.max_tier, max_uses = excluded.max_uses,
                         uses_left = excluded.max_uses, respawn_seconds = excluded.respawn_seconds, used_at = null""",
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

        for rid, r in world["rooms"].items():
            for entry in r.get("items", []):
                # 写法：bread 或 {item: bread, respawn: 120}
                item = entry["item"] if isinstance(entry, dict) else entry
                respawn = entry.get("respawn", DEFAULT_RESPAWN) if isinstance(entry, dict) else DEFAULT_RESPAWN
                cur.execute("insert into item_instances (template_id, room_id) values (%s, %s)", (item, rid))
                cur.execute(
                    "insert into spawns (room_id, template_id, respawn_seconds) values (%s, %s, %s)",
                    (rid, item, respawn),
                )
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
    path = sys.argv[1] if len(sys.argv) > 1 else "world.yaml"
    with open(path, encoding="utf-8") as f:
        world = yaml.safe_load(f)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        seed(conn, world)
    print("导入完成")
