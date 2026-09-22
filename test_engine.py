"""
规则引擎集成测试：不接 AI，手动构造动作，把老汉斯任务线跑一遍。
会重置世界（seed）和测试玩家，别对正式库跑。

用法: python test_engine.py
"""
import os
from uuid import UUID

import psycopg
import yaml
from dotenv import load_dotenv
from pydantic import TypeAdapter

import engine
from schema import PlayerAction, RoomView
from seed import seed

TEST_ID = UUID("00000000-0000-0000-0000-000000000001")
action = TypeAdapter(PlayerAction).validate_python


def reset(conn):
    with open("world.yaml", encoding="utf-8") as f:
        seed(conn, yaml.safe_load(f))
    with conn.transaction():
        conn.execute(
            """insert into auth.users (id, instance_id, aud, role, email)
               values (%s, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'test@ai-mud.local')
               on conflict (id) do nothing""",
            (TEST_ID,),
        )
        conn.execute(
            """insert into players (id, name, room_id, hp, max_hp, attack, defense)
               values (%s, '测试员', 'square', 20, 20, 2, 0)
               on conflict (id) do update set room_id = 'square', hp = 20, flags = '{}'""",
            (TEST_ID,),
        )
        conn.execute("delete from item_instances where player_id = %s", (TEST_ID,))
        conn.execute("delete from player_npc_relations where player_id = %s", (TEST_ID,))


def ref(view: RoomView, name: str) -> str:
    """按名字找短编号，测试里代替意图解析 AI"""
    for r, uid in view.refs.items():
        for obj in view.items + view.inventory + view.npcs:
            if obj.id == uid and obj.name == name:
                return r
    raise KeyError(name)


passed = failed = 0


def step(conn, view, act: dict, expect: bool = True):
    global passed, failed
    r = engine.execute(conn, view, action(act))
    ok = r.success == expect
    passed += ok
    failed += not ok
    print(f"{'✓' if ok else '✗ 预期' + ('成功' if expect else '失败')} {act}")
    for f in r.facts:
        print("    ", f)
    return r


def check(desc: str, cond: bool):
    global passed, failed
    passed += cond
    failed += not cond
    print(f"{'✓' if cond else '✗'} {desc}")


def main():
    load_dotenv(".env")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        reset(conn)
        v = lambda: engine.load_view(conn, TEST_ID)

        print("== 广场")
        view = v()
        step(conn, view, {"action": "look"})
        step(conn, view, {"action": "take", "item": ref(view, "面包")})
        step(conn, view, {"action": "take", "item": "i99"}, expect=False)
        step(conn, view, {"action": "move", "direction": "west"}, expect=False)

        print("\n== 先去酒馆：没打哥布林，老汉斯不给钥匙")
        step(conn, view, {"action": "move", "direction": "north"})
        view = v()
        hans = view.resolve(ref(view, "老汉斯"))
        check("giveable 为空", engine.giveable_items(conn, TEST_ID, hans) == [])
        key_id = engine.load_items(engine._cursor(conn), "i.npc_id = %s", (hans,))[0].id
        conn.commit()
        check("npc_give 被拒", not engine.npc_give(conn, TEST_ID, hans, key_id).success)
        step(conn, view, {"action": "attack", "target": ref(view, "老汉斯")}, expect=False)
        step(conn, view, {"action": "move", "direction": "down"}, expect=False)

        print("\n== 好感度限幅")
        r = engine.adjust_affinity(conn, TEST_ID, hans, 50)
        print("    ", r.facts)
        check("单次 +50 被限成 +5", engine.get_affinity(conn, TEST_ID, hans) == 5)
        engine.adjust_affinity(conn, TEST_ID, hans, -3)
        check("再 -3 变成 2", engine.get_affinity(conn, TEST_ID, hans) == 2)

        print("\n== 铁匠铺拿剑")
        step(conn, view, {"action": "move", "direction": "south"})
        step(conn, view, {"action": "move", "direction": "east"})
        view = v()
        sword = ref(view, "生锈的短剑")
        # 同一份 refs：拿起之后还能用同一个编号装备
        step(conn, view, {"action": "take", "item": sword})
        step(conn, view, {"action": "equip", "item": sword})
        step(conn, view, {"action": "equip", "item": sword}, expect=False)

        print("\n== 去森林打哥布林")
        for d in ("west", "south"):
            step(conn, view, {"action": "move", "direction": d})
        view = v()
        step(conn, view, {"action": "take", "item": ref(view, "药草")})
        step(conn, view, {"action": "move", "direction": "south"})
        view = v()
        gob = ref(view, "哥布林")
        step(conn, view, {"action": "look", "target": gob})
        for _ in range(5):
            r = step(conn, view, {"action": "attack", "target": gob})
            if not r.success or any("被击败" in f for f in r.facts):
                break
        step(conn, view, {"action": "attack", "target": gob}, expect=False)
        herb = ref(view, "药草")
        step(conn, view, {"action": "use", "item": herb})

        print("\n== 回酒馆拿钥匙")
        for d in ("north", "north", "north"):
            step(conn, view, {"action": "move", "direction": d})
        view = v()
        step(conn, view, {"action": "talk", "target": ref(view, "老汉斯"), "message": "哥布林我解决了"})
        giveable = engine.giveable_items(conn, TEST_ID, hans)
        check("giveable 里有钥匙", [i.name for i in giveable] == ["地窖钥匙"])
        r = engine.npc_give(conn, TEST_ID, hans, giveable[0].id)
        print("    ", r.facts)
        check("npc_give 成功", r.success)

        print("\n== 开地窖拿护符")
        view = v()
        key = ref(view, "地窖钥匙")
        step(conn, view, {"action": "use", "item": ref(view, "面包"), "target": "down"}, expect=False)
        step(conn, view, {"action": "use", "item": key, "target": "down"})
        step(conn, view, {"action": "move", "direction": "down"})
        view = v()
        amulet = ref(view, "古旧护符")
        step(conn, view, {"action": "take", "item": amulet})
        step(conn, view, {"action": "equip", "item": amulet})
        step(conn, view, {"action": "drop", "item": ref(view, "地窖钥匙")})
        step(conn, view, {"action": "freeform", "description": "对着酒桶唱歌"})

        print("\n== 最终状态")
        view = v()
        print("    背包:", [(i.name, i.quantity, i.equipped_slot) for i in view.inventory])
        print("    HP:", view.player.hp, "flags:", view.player.flags)
        check("身上有护符且已装备", any(i.name == "古旧护符" and i.equipped_slot == "armor" for i in view.inventory))

    print(f"\n通过 {passed}，失败 {failed}")


if __name__ == "__main__":
    main()
