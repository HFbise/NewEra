"""Everyday actions: moving, looking, taking, using and consuming items, resting, writing. / 日常动作"""
import random
import re
from datetime import datetime, timezone
from typing import Optional

from psycopg import Cursor
from psycopg.types.json import Jsonb

import dungeon
from rules import (
    CAMP_BASE, CAMP_EMPTY, CAMP_SURVIVAL, CAMP_TENT, CHEER_ATTACK, CHEER_FLOORS, SCALE, gold_scale, heal_amount,
    skill_level, steps,
)
from schema import (
    Camp, Dispenser, Drop, Effect, Freeform, ItemInstance, Look, Move, Player, Refill, Reject, Respawn, Rest,
    Revive, RoomView, SLOT_NAMES, Say, Status, Stealth, Take, TakeDonated, Teleport, Use, Write, dir_name,
)

from ..base.core import ActionError, DETECT_START, ONLINE_WINDOW, RESPAWN_ROOM, REVIVE_DIFFICULTY
from . import dungeon_events, environment
from ..base import helpers, loading
from ..fight import afflictions, bosses, combat, duels, equipment, ranged_combat, stealth
from ..npc import npc_social, npc_trade, smith


# 签名统一: (cur, player, view, action) -> facts

def do_move(cur: Cursor, player: Player, view: RoomView, a: Move) -> list[str]:
    ex = helpers.find_exit(cur, player.room_id, a.direction, lock=True)
    if ex is None:
        raise ActionError(f"这里没有往{dir_name(a.direction)}的路")
    facts = []
    # 决斗中：发起者得先逃跑成功才能走；被挑战的一方走开，决斗就结束
    if duel := duels.active_duel(cur, player.id):
        if duel["challenger"] == player.id:
            raise ActionError(f"{player.name}发起的决斗还没结束，得先逃跑成功才能离开")
        cur.execute("delete from duels where challenger = %s", (duel["challenger"],))
        facts.append(f"{player.name}走开了，和{duel['opponent']}的决斗结束了")
    # 楼梯间的守卫（精英、头目）活着就下不去：得打倒它，或者把它定住、捆住趁机溜下去
    if a.direction == "down" and dungeon.is_dungeon(player.room_id) and (guards := [
            n.name for n in combat.enemies_in(cur, player.room_id)
            if n.template.props.get("dungeon", {}).get("rank") in ("elite", "boss")
            and (n.status is None or n.status.kind == "prone")]):
        raise ActionError(f"{'、'.join(guards)}守在楼梯口，不打倒它（或者把它定住、捆住）下不去")
    if a.direction == "down" and (gate := next((n for n in combat.enemies_in(cur, player.room_id) if n.template.props.get("gate")), None)) \
            and npc_social.player_deepest(cur, player) <= dungeon.parse_room(player.room_id)[1] \
            and str(dungeon.parse_room(player.room_id)[1]) not in (player.flags.get("gates") or {}):
        raise ActionError(f"{gate.name}守着这一关：不打倒它，谁也下不去")
    if environment.room_env(cur, player.room_id).get("sealed") and combat.enemies_in(cur, player.room_id):
        raise ActionError("门被封死了，打完之前谁也出不去")
    # 被敌人发现、正在交手：得先逃跑成功（甩开了就不算被发现）才能离开
    if stealth.stealth_state(player).detected and (foes := [n.name for n in combat.enemies_in(cur, player.room_id) if n.status is None]):
        raise ActionError(f"{'、'.join(foes)}正缠着{player.name}，得先逃跑成功才能离开")
    if ex["locked"]:
        # 身上带着对应的钥匙就顺手打开，不用玩家专门说"用钥匙开门"
        keys = loading.load_items(cur, "i.player_id = %s and i.template_id = %s", (player.id, ex["key_item"])) \
            if ex["key_item"] else []
        if not keys:
            raise ActionError(f"往{dir_name(a.direction)}的门锁着" + ("（要典狱长的大钥匙）" if ex["key_item"] == "warden_key" else ""))
        facts += _unlock(cur, player, keys[0], a.direction)
    # 地窖的漆黑入口、地牢楼梯间往下：去哪一层由地牢决定（第一次到的那层当场生成）
    to, arrived = ex["to_room"], []
    if to == dungeon.GATE:
        to, arrived = dungeon.through_gate(cur, player, player.room_id, ONLINE_WINDOW)
    dungeon.touch(cur, player.room_id)
    cur.execute("""update rooms set props = props #- '{env,scroll}'
                   where id = %s and props->'env'->'scroll'->>'by' = %s""", (player.room_id, str(player.id)))
    # 进门前没人在的区域先把该回来的敌人刷出来（走进去才碰上），进门时还没被发现
    loading.respawn_npcs(cur, to)
    # 自己走就不再跟着别人
    spotted = stealth.keen_spotted(cur, to)
    cur.execute("update players set room_id = %s, following = null, stealth = %s, updated_at = now() where id = %s",
                (to, Jsonb(Stealth(room=to, chance=DETECT_START, detected=bool(spotted), alerted=bool(spotted),
                                   start=environment.start_distance(cur, to)).model_dump()), player.id))
    room = loading.load_room(cur, to)
    if room.props.get("gate_stone"):
        d = dungeon.parse_room(to)[1]
        cur.execute("""update players set waypoints = array_append(waypoints, %s)
                       where id = %s and not (%s = any(waypoints)) returning 1""", (d, player.id, d))
        if cur.fetchone():
            facts.append(f"前厅里的传送石亮了起来：以后在地窖里说「传送到第 {d} 层」就能直接到这里，倒下了也能马上回来重来")
    if (fog := environment.fog_here(cur, to)) and (foes := combat.enemies_in(cur, to)):
        # 浓雾：进门过一次察觉，过了就先听见怪在哪
        ok, _ = helpers.check(cur, player, view, "perception", int(fog.get("perception_reveal", 2)) + dungeon.parse_room(to)[1] // 6)
        facts.append(f"雾里传来{'、'.join(n.name for n in foes)}的动静，{player.name}听出了它们在哪" if ok
                     else f"浓雾把四周裹得严严实实，{player.name}什么都看不清，只觉得雾里有东西")
    dungeon.mark_seen(cur, to)
    facts.append(f"{player.name}往{dir_name(a.direction)}走，来到了{room.name}")
    stride = helpers.stride(cur, player, view, "walk", 1)          # 走路也练运动（攒满了这条消息末尾报熟练）
    if heal := sum(int(e.get("value", 0)) for e in equipment.fire(cur, player, "enter", "heal")):
        facts += equipment.heal_player(cur, player, heal)
    facts += arrived + (_torch_floor(cur, [player.name], to) if arrived else [])
    if arrived and environment.room_env(cur, to).get("heat"):
        facts.append(environment.HEAT_HINT)
    # 同房间跟着他的人一起走：睡着、倒下、带着负面状态的跟不上
    cur.execute(
        f"""update players set room_id = %s, updated_at = now()
            where following = %s and room_id = %s and hp > 0 and status is null
              and last_active_at > now() - interval '{ONLINE_WINDOW}'
              and id not in (select challenger from duels where accepted)
            returning name""",
        (to, player.id, player.room_id),
    )
    names = [r["name"] for r in cur.fetchall()]
    # 地牢里两个房间之间走动：可能碰上陷阱、零钱、跟进来的怪、怪声
    if (not arrived and dungeon.is_dungeon(player.room_id) and dungeon.is_dungeon(to)
            and dungeon.CELL_INDEX not in (dungeon._cell_of(player.room_id), dungeon._cell_of(to))      # 进出牢房只是一扇门
            and helpers.roll(dungeon.ROAD_EVENT_CHANCE)):
        facts += _road_event(cur, player, view, to)
    if names:
        facts.append(f"{'、'.join(names)}跟着{player.name}一起来到了{room.name}")
        if spotted:
            cur.execute("update players set stealth = %s where name = any(%s)",
                        (Jsonb(Stealth(room=to, chance=DETECT_START, detected=True, alerted=True).model_dump()), names))
        if arrived:
            facts += dungeon.arrived(cur, names, dungeon.parse_room(to)[1]) + _torch_floor(cur, names, to)
    if spotted:
        facts.append(f"{'、'.join(spotted)}一下子就察觉到了来人")
    for npc in loading.load_npcs(cur, "n.room_id = %s and n.alive", (to,)):
        facts += npc_social.upgrade_nudge(cur, player, npc)       # 走进铁匠铺：莉娜看不下去没升过的武器
        facts += npc_social.bow_nudge(cur, player, npc)           # 拿着弓进酒馆：麦琪提一句找人挡在前面
        facts += npc_social.armor_nudge(cur, player, npc) + npc_social.gem_nudge(cur, player, npc) + npc_social.potion_nudge(cur, player, npc)
        facts += npc_social.news(cur, player, npc)                # 上线后第一次进酒馆：麦琪八卦最近的更新
    if room.props.get("rest"):
        facts.append(REST_TEXT)
    return facts + combat.beast_hint(cur, to) + stride


# 走路随机事件的累计几率：陷阱、零钱、路边小木匣，剩下是怪声（循声去找就会碰上游荡的怪）
ROAD_TRAP, ROAD_COINS, ROAD_CHEST = 0.45, 0.65, 0.8


ROAD_CHEST_ITEMS = ["herb", "bread", "torch", "rope"]     # 路边小木匣里的东西：普通补给，不给好东西


TRAP_EASE = "_trap_ease"                 # players.flags：这一层踩中过几次陷阱 {"floor": "run:层", "n": 次数}


TRAP_LEARN = 0.25                       # 踩中陷阱涨察觉熟练的几率


def _road_event(cur: Cursor, player: Player, view: RoomView, to: str) -> list[str]:
    """地牢里走路时碰上的事。陷阱：察觉发现就绕过去，没发现挨一下（活下来耐性一定涨）。
    怪声：在房间上记一笔，有人循声去找（搜索）就引出一只游荡的怪"""
    f = dungeon.floor_info(cur, to)
    depth, r = f["depth"], random.random()
    if r < ROAD_TRAP:
        trap = random.choice(dungeon.data()["themes"].get(f["theme"], {}).get("traps") or dungeon.data()["traps"])
        sense = equipment.carries(cur, player, "trap_sense")          # 麦琪的陷阱图：难度降几级
        here = ":".join(map(str, dungeon.parse_room(to)))
        ease = player.flags.get(TRAP_EASE, {})
        ease = ease.get("n", 0) if ease.get("floor") == here else 0
        diff = 2 + round(steps(depth, 6)) - (helpers.prop(sense, "trap_sense") if sense else 0) - ease
        ok, rolled = helpers.check(cur, player, view, "perception", max(1, diff))
        # 同一层每踩中一次，下一个陷阱难度 -1（吃过亏就留神了），躲开一次就恢复
        cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::jsonb) where id = %s",
                    (TRAP_EASE, Jsonb({"floor": here, "n": 0 if ok else ease + 1}), player.id))
        dmg = 0 if ok else round((2 + steps(depth, 3)) * SCALE)
        cur.execute("insert into trap_log (player_id, depth, difficulty, avoided, dmg) values (%s, %s, %s, %s, %s)",
                    (player.id, depth, max(1, diff), ok, dmg))
        if ok:
            return [f"路上{trap}"] + rolled + [f"{player.name}{'想起陷阱图上画过这种地方，' if sense else ''}及时察觉，躲了过去"]
        hurt, down = combat.hurt_player(cur, player, dmg, "other", "陷阱")
        # 踩中了也长记性：几率涨一点察觉熟练（不然察觉 0 级的人永远判不成，永远练不上去）
        learn = helpers.gain_skill(cur, player, "perception") if not down and "perception" not in view.trained and helpers.roll(TRAP_LEARN) else []
        if learn:
            view.trained.append("perception")
        return [f"路上{trap}"] + rolled + [f"{player.name}没能躲开，受到 {dmg} 点伤害"] + hurt \
            + ([] if down else helpers.toughen(cur, player)) + learn
    if r < ROAD_COINS:
        coins = max(1, round(random.randint(1, 3) * gold_scale(depth)))
        cur.execute("update players set gold = gold + %s where id = %s", (coins, player.id))
        return [f"{player.name}在路边的碎石里踢到了 {coins} 枚古币，顺手捡了起来"]
    if r < ROAD_CHEST:
        item = dungeon.road_box_item(f["theme"], depth) or random.choice(ROAD_CHEST_ITEMS)
        give_player_new(cur, player, item)
        cur.execute("select name from item_templates where id = %s", (item,))
        return [f"{player.name}在路边的碎石下翻出一只烂木匣，里面有一件{cur.fetchone()['name']}，顺手收下了"]
    cur.execute("""update rooms set props = props || '{"noise": true}' where id = %s""", (to,))
    return [random.choice(dungeon.data()["themes"][f["theme"]]["eerie"]),
            "声音像是就在这附近，要是循声去找（搜索），也许能找到它的来源"]


def do_teleport(cur: Cursor, player: Player, view: RoomView, a: Teleport) -> list[str]:
    """传送石边上：回到地窖。地窖里：传送到到过的传送石那一层。同房间跟着他的人一起走"""
    here = player.room_id
    if duels.active_duel(cur, player.id):
        raise ActionError(f"{player.name}正在决斗，走不了")
    if dungeon.is_dungeon(here) and view.room.props.get("stone"):
        to, facts = dungeon.ENTRANCE, [f"{player.name}把手按在传送石上，纹路里的银光一下子亮起来裹住全身，"
                                       f"再睁眼已经站在了{loading.load_room(cur, dungeon.ENTRANCE).name}的漆黑入口前"]
        dungeon.touch(cur, here)
    elif here == dungeon.ENTRANCE:
        cur.execute("select waypoints from players where id = %s", (player.id,))
        points = cur.fetchone()["waypoints"]
        cur.execute("select deepest_floor from players where id = %s", (player.id,))
        if a.floor and dungeon.gate_depth(a.floor) and cur.fetchone()["deepest_floor"] >= a.floor:
            points = list(points) + [a.floor]           # 关卡层的前厅：到过这么深的人都能回去挑战
        if a.floor is None or a.floor not in points:
            raise ActionError((f"{player.name}还没到过第 {a.floor} 层的传送石。" if a.floor else "")
                              + dungeon.waypoints_text(points))
        to, facts = dungeon.teleport_to(cur, player, a.floor, ONLINE_WINDOW)
        facts = [f"{player.name}站在漆黑的入口前默念第 {a.floor} 层，一阵银光闪过"] + facts
    else:
        raise ActionError("只有在地牢的传送石边上、或者地窖里才能传送")
    cur.execute("update players set room_id = %s, following = null, stealth = null, updated_at = now() where id = %s",
                (to, player.id))
    dungeon.mark_seen(cur, to)
    cur.execute(
        f"""update players set room_id = %s, updated_at = now()
            where following = %s and room_id = %s and hp > 0 and status is null
              and last_active_at > now() - interval '{ONLINE_WINDOW}' returning name""", (to, player.id, here))
    names = [r["name"] for r in cur.fetchall()]
    if names:
        facts.append(f"{'、'.join(names)}跟着{player.name}一起传了过去")
        if dungeon.is_dungeon(to):
            facts += dungeon.arrived(cur, names, dungeon.parse_room(to)[1])
    return facts + _torch_floor(cur, [player.name] + names, to)


# 扎营：回多少见 rules.CAMP_*


REST_TEXT = "门后传来沉重的呼吸声。这里是头目门前最后能喘口气的地方：在这儿能多扎一次营，不算这一层的扎营次数"


def do_camp(cur: Cursor, player: Player, view: RoomView, a: Camp) -> list[str]:
    """地牢里清完怪的房间扎营，每层每人一次"""
    if not dungeon.is_dungeon(player.room_id):
        raise ActionError("只有在远古地牢里才用得着扎营，村里可以去酒馆住店")
    if foes := [n.name for n in combat.enemies_in(cur, player.room_id)]:
        raise ActionError(f"{'、'.join(foes)}还在这里，没法扎营")
    run, depth = dungeon.parse_room(player.room_id)
    rest = False
    if view.room.props.get("rest"):
        # 头目门前的休息点：每人一次，记在楼梯间的房间上，不占这一层的扎营
        cur.execute("""update rooms r set props = jsonb_set(r.props, '{rested}', coalesce(r.props->'rested', '[]'::jsonb) || to_jsonb(%s::text))
                       from dungeon_floors f where f.run_id = %s and f.depth = %s and r.id = f.stairs_room
                         and not coalesce(r.props->'rested', '[]'::jsonb) ? %s returning 1""",
                    (str(player.id), run, depth, str(player.id)))
        rest = cur.fetchone() is not None
    if not rest:
        cur.execute("""update dungeon_floors set camped = array_append(camped, %s)
                       where run_id = %s and depth = %s and not (%s = any(camped)) returning 1""",
                    (player.id, run, depth, player.id))
        if not cur.fetchone():
            raise ActionError(f"{player.name}在这一层已经扎过营了，再歇也缓不过来，得往下走")
    tent = next((i for i in view.inventory if helpers.prop(i, "camp")), None)
    facts = [f"{player.name}在{view.room.name}扎营休息" + (f"，搭起了{tent.name}" if tent else "")
             + ("（头目门前的休息点，不算这一层的扎营）" if rest else "")]
    share = CAMP_BASE + (CAMP_TENT if tent else 0)
    if tent:
        environment.consume(cur, tent)
    ok, rolled = helpers.check(cur, player, view, "survival", 1 + depth // 3)
    facts += rolled
    if ok:
        share += CAMP_SURVIVAL
        facts.append("营地收拾得很妥当，睡得更安稳")
    if view.room.props.get("dungeon", {}).get("kind") == "empty":
        share *= CAMP_EMPTY
        facts.append("这里安静空旷，是个扎营的好地方")
    if player.effects:
        # 好好歇一觉，身上的毒、伤口、腐蚀都缓过来了
        player.max_hp += sum(e.hp for e in player.effects)
        facts.append(f"{player.name}身上的{'、'.join(afflictions.EFFECT_NAMES[e.kind] for e in player.effects)}都缓过来了")
        player.effects = []
        cur.execute("update players set max_hp = %s, effects = coalesce((select jsonb_agg(e) from jsonb_array_elements(effects) e where e->>'kind' in ('whet', 'cheer')), '[]'::jsonb) where id = %s", (player.max_hp, player.id))
    hp = min(player.max_hp, player.hp + round(player.max_hp * share))
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (hp, player.id))
    return facts + [f"{player.name}恢复了 {hp - player.hp} 点 HP，当前 HP {hp}/{player.max_hp}"]


def do_look(cur: Cursor, player: Player, view: RoomView, a: Look) -> list[str]:
    if a.target is None:
        room = loading.load_room(cur, player.room_id)
        facts = [f"{room.name}：{room.description}"] + ([env] if (env := environment.env_text(cur, room)) else [])
        exits = loading.load_exits(cur, player.room_id)
        if exits:
            facts.append("出口：" + "、".join(
                f"{dir_name(e.direction)}（通往{loading.load_room(cur, e.to_room).name}" + ("，门锁着" if e.locked else "") + "）"
                for e in exits))
        items = loading.load_items(cur, "i.room_id = %s", (player.room_id,))
        ground = [helpers.item_label(i) for i in items] + view.forage
        if ground:
            facts.append("地上有：" + "、".join(ground))
        npcs = loading.load_npcs(cur, "n.room_id = %s and n.alive", (player.room_id,))
        here = [n.name + (f"（{n.status.describe()}）" if n.status else "") for n in npcs]             + [f"{d.container}（{d.where}面有{d.item_name}）" if d.available else d.container
               for d in view.dispensers]
        if here:
            facts.append("这里有：" + "、".join(here))
        cur.execute(
            f"""select name, hp, status, coalesce(last_active_at > now() - interval '{ONLINE_WINDOW}', false) as awake
                from players where room_id = %s and id <> %s order by name""",
            (player.room_id, player.id),
        )
        others = [r["name"] + ("（倒在地上，等人急救）" if r["hp"] <= 0
                               else f"（{Status(**r['status']).describe()}）" if r["status"]
                               else "" if r["awake"] else "（在原地睡着了，不会动也不会回应）")
                  for r in cur.fetchall()]
        if others:
            facts.append("其他玩家：" + "、".join(others))
        return facts

    ex = helpers.find_exit(cur, player.room_id, a.target)
    if ex is not None:
        room = loading.load_room(cur, ex["to_room"])
        return [f"往{dir_name(a.target)}通往{room.name}" + ("，门锁着" if ex["locked"] else "")]

    # 看同房间的玩家：HP、倒下、负面状态、手里拿着什么（背包里别的东西看不到）
    if a.target not in view.refs:
        try:
            other, awake = duels.room_player(cur, player, a.target)
        except ActionError:
            # 不是人：环境里写着的东西（石碑、壁画、井）照环境细节看，地窖石碑把排行榜念出来
            room = view.room
            seen = a.target.strip("的")
            if not seen or seen not in room.description + room.details:
                raise
            return [f"{player.name}仔细看了看{seen}"]
        worn = helpers.worn(cur, other)
        facts = [f"{other.name}：HP {other.hp}/{other.max_hp}"
                 + ("，倒在地上" if other.hp <= 0 else "") + ("" if awake else "，睡着了")]
        if other.status:
            facts.append(f"{other.name}{other.status.describe()}")
        if worn:
            facts.append(f"{other.name}身上装备着" + "、".join(f"{i.name}（{SLOT_NAMES[i.equipped_slot]}）" for i in worn))
        return facts

    uid = helpers.resolve(view, a.target)
    if d := next((d for d in view.dispensers if d.id == uid), None):
        return [f"{d.container}：{d.description}".rstrip("：")] \
            + ([f"{d.container}{d.where}有{d.item_name}"] if d.available else []) \
            + ([f"{d.container}里还有别人留下的：{'、'.join(x['name'] for x in d.donated)}（说「从{d.container}里拿某某」，每人每天一件）"]
               if d.donated else []) \
            + ([f"用不上的武器、护具可以放进{d.container}留给新人（说「把某某放进{d.container}」；强化清零，镶的宝石退回自己背包）"]
               if d.donate else [])
    items = loading.load_items(cur, "i.id = %s and (i.room_id = %s or i.player_id = %s)",
                       (uid, player.room_id, player.id))
    if items:
        return [f"{items[0].name}：{items[0].description}"]
    npcs = loading.load_npcs(cur, "n.id = %s and n.room_id = %s and n.alive", (uid, player.room_id))
    if npcs:
        n = npcs[0]
        facts = [f"{n.name}：{n.template.description}"] + dungeon_events.stele(cur, n, player)
        if n.combatable:
            facts.append(f"{n.name} HP {n.hp}/{n.template.max_hp}")
        if n.status:
            facts.append(f"{n.name}{n.status.describe()}")
        return facts
    raise ActionError("这里看不到那个东西")


def give_player_new(cur: Cursor, player: Player, template_id: str) -> None:
    """凭空给玩家一件新东西（武器桶、搜索找到的），可叠加的并进已有的那堆"""
    cur.execute(
        """update item_instances i set quantity = quantity + 1 from item_templates t
           where t.id = i.template_id and t.stackable and i.template_id = %s and i.player_id = %s
             and i.equipped_slot is null
           returning i.id""",
        (template_id, player.id),
    )
    if not cur.fetchone():
        cur.execute("insert into item_instances (template_id, player_id) values (%s, %s)", (template_id, player.id))


def _take_from(cur: Cursor, player: Player, view: RoomView, d: Dispenser) -> list[str]:
    """从武器桶这类地方拿一件：身上已经有同类的（锈剑、磨亮的剑）就不能拿；once 的拿过一次就再也不能拿。
    要技能判定的（挖矿、挑酒）先判，失败了这次拿不到（可能挨一下），下次还能再试"""
    cur.execute(
        """select t.name from item_instances i join item_templates t on t.id = i.template_id
           where i.player_id = %s and i.template_id = any(%s) limit 1""",
        (player.id, [d.item] + d.unless),
    )
    if (row := cur.fetchone()) and not d.repeat:
        raise ActionError(f"{player.name}身上已经有{row['name']}了，{d.container}{d.where}的留给别人")
    if d.once:
        cur.execute("select 1 from dispenser_log where player_id = %s and room_id = %s and key = %s",
                    (player.id, d.room, d.key))
        if cur.fetchone():
            raise ActionError(f"{d.container}{d.where}已经没有{player.name}能拿的东西了")
    facts = []
    if d.skill:
        # 跟挣脱一样：每失败一次难度降一级，多试几次总能成（最多挨几下）
        cur.execute("select fails from dispenser_log where player_id = %s and room_id = %s and key = %s",
                    (player.id, d.room, f"{d.key}#fails"))
        fails = (cur.fetchone() or {}).get("fails", 0)
        ok, facts = helpers.check(cur, player, view, d.skill, max(1, d.difficulty - fails))
        if not ok:
            cur.execute("""insert into dispenser_log (player_id, room_id, key, fails) values (%s, %s, %s, 1)
                           on conflict (player_id, room_id, key) do update set fails = dispenser_log.fails + 1""",
                        (player.id, d.room, f"{d.key}#fails"))
            if d.fail == "void_fall":
                return [f"{player.name}助跑一下跳向{d.container}"] + facts + ["差了一点点，脚尖擦着石台边滑了下去"] \
                    + environment.void_fall(cur, player, force=True)
            facts = [f"{player.name}想从{d.container}{d.where}取{d.item_name}"] + facts + [d.fail or "没能成功"]
            if d.fail_damage:
                hurt, _ = combat.hurt_player(cur, player, d.fail_damage, "other", d.container)
                facts += [f"{player.name}受到 {d.fail_damage} 点伤害"] + hurt
            return facts + ["摸到了点门道，下次会顺手一些"]
    if d.once:
        # 拿过一次就没了：并发时靠主键挡住第二次
        cur.execute("""insert into dispenser_log (player_id, room_id, key) values (%s, %s, %s)
                       on conflict do nothing returning 1""", (player.id, d.room, d.key))
        if not cur.fetchone():
            raise ActionError(f"{d.container}{d.where}已经没有{player.name}能拿的东西了")
    if d.service == "free_upgrade":
        return facts + dungeon_events.free_upgrade(cur, player, d)
    if d.service == "mirror_duel":
        return facts + dungeon_events.mirror_duel(cur, player, d)
    if d.service == "wish":
        return facts + dungeon_events.wish(cur, player, d)
    if d.service == "clear_fog":
        dungeon.set_floor_state(cur, player.room_id, {"fog_clear": True})
        return facts + [f"{player.name}点亮了灯塔，一束光扫过海面，这一层的雾散开了（远程、火把照常，怪不再一进门就在跟前）"]
    if d.service == "player_note":
        return facts + dungeon_events.bottle(cur, player, d)
    if d.service == "buff":
        b = d.extra.get("buff") or {}
        for stat in ("atk_pct", "hit_pct", "dodge"):
            if b.get(stat):
                pct = round(b[stat] * 100)
                afflictions.give_bless(cur, player, stat, pct, {"atk_pct": f"清醒梦（攻击 +{pct}%）", "hit_pct": f"读懂的星图（命中 +{pct}%）",
                                                      "dodge": f"祝福（对方命中 -{pct}%）"}[stat])
        if b.get("reveal") == "star_cycle":
            afflictions.give_bless(cur, player, "star_chart", 0, "读懂的星图（知道星光什么时候变）")
        return facts + [f"{player.name}{d.extra.get('done') or '心里一下子亮堂了'}：{'、'.join(e.label for e in player.effects if e.kind == 'bless')}，出了这一层就散"]
    if d.service == "shortcut":
        f = dungeon.floor_info(cur, player.room_id)
        cur.execute("select stairs_room from dungeon_floors where run_id = %s and depth = %s", dungeon.parse_room(player.room_id))
        stairs = cur.fetchone()["stairs_room"]
        cur.execute("update players set room_id = %s, stealth = null where id = %s", (stairs, player.id))
        dungeon.mark_seen(cur, stairs)
        return facts + [f"{player.name}找出了楼梯的规律，最后一遍走到底，推开门就到了第 {f['depth']} 层的楼梯间"]
    if d.service == "memory":
        return facts + dungeon_events.memory_door(cur, player, d)
    if d.service == "cleanse":
        return facts + dungeon_events.cleanse(cur, player)
    if d.service == "reveal_next_floor":
        return facts + dungeon_events.read_stele(cur, player, d)
    if d.item == "treasure_pool":
        f = dungeon.floor_info(cur, player.room_id)
        item = dungeon._pick(dungeon.loot_data()["treasure"].get(f["theme"]), f["depth"]) or "herb"
        dungeon._put_item(cur, item, f["depth"], player=player.id, rarity="uncommon")
        cur.execute("select name from item_templates where id = %s", (item,))
        return facts + [f"{player.name}从{d.container}{d.where}拿到了{cur.fetchone()['name']}"]
    if d.service == "timed":
        # 沙漏房：拿了钱就得在沙子流完之前走出去
        f = dungeon.floor_info(cur, player.room_id)
        coins = dungeon.treasure_gold(f["depth"], 0.3, f["party_size"] or 1)
        cur.execute("update players set gold = gold + %s where id = %s", (coins, player.id))
        return facts + [f"{player.name}一把抓起石台上的钱袋（{coins} 金币）：沙子流完之前得走出这间屋子"]
    if d.item == "gem":
        f = dungeon.floor_info(cur, player.room_id)
        gem = dungeon.put_gem(cur, dungeon.pick_gem(f["theme"]), f["depth"], player=player.id,
                              min_tier=int(d.extra.get("gem_min_tier", 1)))
        return facts + [f"{player.name}从{d.container}{d.where}撬下来一颗{gem}"]
    give_player_new(cur, player, d.item)
    facts = facts + [f"{player.name}从{d.container}{d.where}拿了一件{d.item_name}"]
    if d.bonus_gem and dungeon.is_dungeon(player.room_id) and helpers.roll(d.bonus_gem):
        f = dungeon.floor_info(cur, player.room_id)
        gem = dungeon.put_gem(cur, dungeon.pick_gem(f["theme"]), f["depth"], player=player.id)
        facts.append(f"敲下来的碎块里还滚出一颗{gem}")
    if d.item == smith.UPGRADE_ORE and dungeon.is_dungeon(player.room_id) and helpers.roll(dungeon.gem_rules()["drops"]["mining"]):
        depth = dungeon.parse_room(player.room_id)[1]
        gem = dungeon.put_gem(cur, dungeon.pick_gem("mine", 0.5), depth, player=player.id)
        facts.append(f"敲下来的碎石里还滚出一颗{gem}")
    return facts


def do_take(cur: Cursor, player: Player, view: RoomView, a: Take) -> list[str]:
    uid = helpers.resolve(view, a.item)
    if d := next((d for d in view.dispensers if d.id == uid), None):
        return _take_from(cur, player, view, d)
    item = helpers.get_item(cur, view, a.item)
    if item.player_id == player.id:
        # "从背包里拿出钥匙开门"会解析成先 take，东西本来就在身上，算成功，后面的动作照常执行
        return [f"{item.name}就在{player.name}身上"]
    if item.room_id != player.room_id:
        raise ActionError(f"这里没有{item.name}")
    if not item.template.takeable:
        raise ActionError(f"{item.name}拿不起来")
    if coins := item.props.get("gold"):
        cur.execute("delete from item_instances where id = %s", (item.id,))
        cur.execute("select id, name from players where party_id = %s and room_id = %s and id <> %s order by name",
                    (player.party_id, player.room_id, player.id))
        mates = cur.fetchall() if player.party_id else []
        share = coins // (len(mates) + 1)
        for m in mates:
            cur.execute("update players set gold = gold + %s where id = %s", (share, m["id"]))
        mine = coins - share * len(mates)
        cur.execute("update players set gold = gold + %s where id = %s", (mine, player.id))
        return [f"{player.name}捡起{item.name}，倒出了 {coins} 枚金币"
                + (f"，跟{'、'.join(m['name'] for m in mates)}平分，每人 {share} 枚" if mates else "")]
    helpers.move_item(cur, item, player_id=player.id)
    return [f"{player.name}从地上捡起了{helpers.item_label(item)}"]


def do_drop(cur: Cursor, player: Player, view: RoomView, a: Drop) -> list[str]:
    item = helpers.inv_item(cur, view, player, a.item)
    smith.no_curse(item, "扔")
    if burning(item):
        return douse(cur, player, item)
    facts = [f"{player.name}卸下了{item.name}"] if item.equipped_slot else []
    helpers.move_item(cur, item, room_id=player.room_id)
    return facts + [f"{player.name}把{helpers.item_label(item)}放在了地上"]


def do_use(cur: Cursor, player: Player, view: RoomView, a: Use) -> list[str]:
    item = helpers.inv_item(cur, view, player, a.item)
    if helpers.prop(item, "water") and a.target and a.target in view.refs:
        return bosses.douse_npc(cur, player, view, item, a.target)
    if helpers.prop(item, "stun"):
        return environment.stun(cur, player, view, item, a.target)
    for key, fn in (("refuel", _refuel), ("whet", _whet), ("room_light", _room_light), ("holy", environment.holy), ("drug", _drug),
                    ("smoke", stealth.smoke)):
        if helpers.prop(item, key):
            return fn(cur, player, view, item, a.target)

    ex = helpers.find_exit(cur, player.room_id, a.target, lock=True) if a.target is not None else None
    if a.target is not None and ex is None and item.template.type == "consumable":
        return environment.feed(cur, player, item, a.target)
    if a.target is not None:
        if ex is None:
            raise ActionError(f"不知道怎么对那个东西使用{item.name}")
        if not ex["locked"]:
            raise ActionError(f"往{dir_name(a.target)}的门没有锁")
        if ex["key_item"] != item.template.id:
            raise ActionError(f"{item.name}打不开往{dir_name(a.target)}的门")
        return _unlock(cur, player, item, a.target)

    if helpers.prop(item, "reveal_floor"):
        return _read_map(cur, player, item)
    if helpers.prop(item, "flask"):
        return _flask(cur, player, item)
    if helpers.prop(item, "antidote"):
        return _antidote(cur, player, item)
    if helpers.prop(item, "refill_by"):
        raise ActionError(f"{item.name}已经空了，回酒馆找麦琪说「续杯」")
    if helpers.prop(item, "recall"):
        return environment.recall(cur, player, item)
    if item.template.type != "consumable":
        raise ActionError(f"{item.name}不能直接使用")
    environment.consume(cur, item)
    return [environment.use_self(player, item)] + environment.eat_effect(cur, player, item, player)


def _unlock(cur: Cursor, player: Player, key: ItemInstance, direction: str) -> list[str]:
    """用钥匙开门。只开一次的钥匙（典狱长的大钥匙，props.opens）转开以后就卡在锁里拔不出来了"""
    cur.execute("update room_exits set locked = false, unlocked_at = now() where room_id = %s and direction = %s",
                (player.room_id, direction))
    facts = [f"{player.name}用{key.name}打开了往{dir_name(direction)}的门"]
    if helpers.prop(key, "opens"):
        environment.consume(cur, key)
        facts.append(f"{key.name}转到底就卡死在锁里，拔不出来了")
    return facts


def _empty(cur: Cursor, item: ItemInstance) -> str:
    """麦琪的酒壶、药用完了：换成空的样子，返回新名字"""
    return ranged_combat.swap_template(cur, item, helpers.prop(item, "empty"))


def _flask(cur: Cursor, player: Player, item: ItemInstance) -> list[str]:
    """麦琪的私酿：一口回满血，浑身是劲（这一层攻击 +CHEER_ATTACK%）。喝完就空了，找她续杯"""
    if player.hp >= player.max_hp:
        raise ActionError(f"{player.name}没受伤，舍不得喝（这一壶喝完就得回酒馆续了）")
    cur.execute("update players set hp = max_hp where id = %s", (player.id,))
    facts = [f"{player.name}拧开{item.name}灌了一大口，一股辛辣的暖流冲遍全身，壶底的纸条晃了晃",
             f"{player.name}恢复到满血，HP {player.max_hp}/{player.max_hp}"]
    player.effects = [e for e in player.effects if e.kind != "cheer"] + [
        Effect(kind="cheer", value=CHEER_ATTACK, left=CHEER_FLOORS, label="私酿的酒劲", source=item.name)]
    afflictions.save_effects(cur, player)
    facts.append(f"{player.name}浑身是劲，攻击 +{CHEER_ATTACK}%（这一层）")
    return facts + [f"{_empty(cur, item)}，回酒馆找麦琪续杯"]


def _antidote(cur: Cursor, player: Player, item: ItemInstance) -> list[str]:
    """麦琪的解毒酒壶：解毒、回一点血，喝完就空了"""
    poison = afflictions.effect(player, "poison")
    if not poison and player.hp >= player.max_hp:
        raise ActionError(f"{player.name}没中毒也没受伤，用不着喝")
    facts = [f"{player.name}灌了一口{item.name}，又苦又冲，呛得直咳嗽"]
    if poison:
        player.effects.remove(poison)
        afflictions.save_effects(cur, player)
        facts.append(f"{player.name}身上的中毒好了")
    heal = equipment.heal_scale(player, heal_amount(int(helpers.prop(item, "antidote")), player.max_hp))
    hp = min(player.max_hp, player.hp + heal)
    cur.execute("update players set hp = %s where id = %s", (hp, player.id))
    return facts + [f"{player.name}恢复 {hp - player.hp} 点 HP，当前 HP {hp}/{player.max_hp}", f"{_empty(cur, item)}，回酒馆找麦琪续杯"]


def _drug(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """麦琪的特制迷药：泼向一个敌人，它醉倒动弹不得（不用判定），瓶子空了"""
    foes = combat.enemies_in(cur, player.room_id)
    if target and target in view.refs:
        npc = helpers.room_npc(cur, view, player, target)
    elif len(foes) == 1:
        npc = foes[0]
    else:
        raise ActionError("这里没有敌人" if not foes else f"要泼向谁？（{'、'.join(n.name for n in foes)}）")
    if not npc.template.hostile:
        raise ActionError(f"不能拿{item.name}对付{npc.name}")
    if bosses.boss_resists(cur, npc):
        raise ActionError(f"{npc.name}这一仗已经被放倒过一回，有了防备，{item.name}泼过去也不管用（留着吧）")
    label = str(helpers.prop(item, "drug"))[:20]
    afflictions.set_status(cur, "npcs", npc.id, Status(kind="incapacitated", label=label, escape=2,
                                            since=datetime.now(timezone.utc).isoformat()))
    return [f"{player.name}拔开{item.name}的塞子，朝{npc.name}泼了过去，一股甜腻的酒气散开",
            f"{npc.name}{label}", f"{_empty(cur, item)}，回酒馆找麦琪续上"]


def do_refill(cur: Cursor, player: Player, view: RoomView, a: Refill) -> list[str]:
    """找麦琪续杯：她给的酒壶、药空了的都灌满，不收钱"""
    npc = helpers.room_npc(cur, view, player, a.target)
    empties = [i for i in view.inventory if helpers.prop(i, "refill_by") == npc.template.id]
    if not empties:
        mine = [i for i in view.inventory if helpers.prop(i, "empty") and any(
            t.get("item") == i.template.id for t in (npc.template.props.get("return_gifts") or {}).values())]
        raise ActionError(f"{player.name}身上{'的' + '、'.join(i.name for i in mine) + '都还满着' if mine else f'没有{npc.name}能续的东西'}")
    # 续杯是她的心意：交情掉到送这件东西那一档以下，她就不给续了
    tiers = {g.get("item"): int(t) for t, g in (npc.template.props.get("return_gifts") or {}).items()}
    aff = npc_trade.affinity_of(cur, player, npc)
    if cold := [i for i in empties if aff < tiers.get(helpers.prop(i, "refill_to"), 0)]:
        raise ActionError(f"{npc.name}瞥了一眼{'、'.join(i.name for i in cold)}，没接：想续？先把交情补回来再说")
    names = [ranged_combat.swap_template(cur, i, helpers.prop(i, "refill_to")) for i in empties]
    return [f"{npc.name}接过{'、'.join(i.name for i in empties)}，哼了一声，背过身去一样一样灌满了又塞回{player.name}手里",
            f"{'、'.join(names)}又满了"]


def _read_map(cur: Cursor, player: Player, item: ItemInstance) -> list[str]:
    """地图残片：显出这一层所有房间，小地图上能看到楼梯间、宝箱房在哪（全队共用）"""
    if not dungeon.is_dungeon(player.room_id):
        raise ActionError(f"{item.name}得在地牢里展开，墨迹才会动")
    if not dungeon.reveal(cur, player.room_id):
        raise ActionError("这一层的地图已经全显出来了，用不着再展开一张")
    environment.consume(cur, item)
    return [f"{player.name}展开{item.name}，兽皮上的墨迹自己游动起来，画出了这一层所有的房间：侧栏的小地图上能看到楼梯间和宝箱房在哪了"]


STUN_ESCAPE = 3                         # 古书残卷念出来的定身：敌人挣脱的难度（每回合 30%、60%、90% 醒过来）


def use_up(cur: Cursor, item: ItemInstance) -> None:
    """用掉一次：能用好几次的（props.uses，借阅簿 3 次）记在实例上，用完才没"""
    total = helpers.prop(item, "uses")
    left = item.props.get("uses_left", total or 1) - 1
    if left > 0:
        cur.execute("update item_instances set props = props || jsonb_build_object('uses_left', %s::int) where id = %s",
                    (left, item.id))
    else:
        environment.consume(cur, item)


def _refuel(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """灯油：倒在弱光的火把上，让它重新烧旺"""
    torch = next((i for i in helpers.worn(cur, player) if i.template.id == "torch_dim"), None)
    if torch is None:
        raise ActionError(f"手上没有快灭的火把，{item.name}用不上")
    environment.consume(cur, item)
    cur.execute("update item_instances set template_id = %s, props = '{}' where id = %s", (helpers.prop(item, "refuel"), torch.id))
    return [f"{player.name}把{item.name}倒在火把上，火苗呼地一下又旺了起来"]


def _whet(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """磨刀石：这一层普通攻击伤害 +value（换了层就没了）"""
    if not dungeon.is_dungeon(player.room_id):
        raise ActionError(f"{item.name}的锋利劲撑不过一层地牢，下了地牢再磨吧")
    run, depth = dungeon.parse_room(player.room_id)
    environment.consume(cur, item)
    player.effects = [e for e in player.effects if e.kind != "whet"] + [
        Effect(kind="whet", value=int(helpers.prop(item, "whet")), left=1, label="刃口磨得雪亮", source=f"{run.hex}:{depth}")]
    afflictions.save_effects(cur, player)
    return [f"{player.name}用{item.name}把武器磨得雪亮（这一层普通攻击伤害 +{helpers.prop(item, 'whet')}）"]


def _room_light(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """光明卷轴：这个房间光亮 +N，走出房间就散了"""
    if not view.room.props.get("env"):
        raise ActionError("这里够亮了，用不着")
    if who := bosses.silenced(cur, player.room_id, player):
        raise ActionError(f"{who}禁了声，{item.name}上的字都暗着，念不出来")
    environment.consume(cur, item)
    noise = environment.make_noise(cur, player, "read")
    cur.execute("""update rooms set props = jsonb_set(props, '{env,scroll}', %s) where id = %s""",
                (Jsonb({"value": int(helpers.prop(item, "room_light")), "by": str(player.id)}), player.room_id))
    return [f"{player.name}展开{item.name}，上面的字一个接一个亮起来，把整个房间照得雪亮"] + noise


def do_rest(cur: Cursor, player: Player, view: RoomView, a: Rest) -> list[str]:
    """住店：这里有开店的 NPC（world.yaml 的 props.inn）就付钱睡一觉，回满血、醒酒、状态清掉"""
    host = next((n for n in view.npcs if n.template.props.get("inn") and n.status is None), None)
    if host is None:
        raise ActionError("这里没有能住的地方")
    price = host.template.props["inn"].get("price", 0)
    patron = npc_trade.pay(cur, player, price, host)
    cur.execute(f"""update players set hp = max_hp + {afflictions.RESTORE_MAX_HP}, max_hp = max_hp + {afflictions.RESTORE_MAX_HP}, effects = coalesce((select jsonb_agg(e) from jsonb_array_elements(effects) e where e->>'kind' in ('whet', 'cheer')), '[]'::jsonb),
                   status = null, drunk_until = null, drinks = 0, updated_at = now()
                   where id = %s""", (player.id,))
    return [f"{player.name}付了 {price} 金币，在{host.name}这儿要了间房，美美睡了一觉",
            f"{player.name}精神饱满，HP {player.max_hp}/{player.max_hp}"
            + ("，酒也醒了" if player.drunk else "")] + patron


def do_revive(cur: Cursor, player: Player, view: RoomView, a: Revive) -> list[str]:
    """急救倒下的人（救起来 1 HP），也能帮人松绑、叫醒"""
    target, _ = duels.room_player(cur, player, a.target, lock=True)
    if target.hp > 0 and target.status is None:
        raise ActionError(f"{target.name}没有倒下也没被困住，用不着急救")
    facts = []
    if target.hp <= 0:
        # 医药判定：救醒回 1 点血，医药每级多回 1 点
        ok, facts = helpers.check(cur, player, view, "medicine", REVIVE_DIFFICULTY)
        if not ok:
            return facts + [f"{player.name}给{target.name}做了急救，但没能救醒"]
        hp = min(target.max_hp, (1 + skill_level(player.skills.get("medicine", 0))) * SCALE)
        cur.execute(f"""update players set hp = %s, max_hp = max_hp + {afflictions.RESTORE_MAX_HP}, effects = coalesce((select jsonb_agg(e) from jsonb_array_elements(effects) e where e->>'kind' in ('whet', 'cheer')), '[]'::jsonb), updated_at = now()
                        where id = %s""", (hp, target.id))
        facts += [f"{player.name}给{target.name}做了急救，{target.name}醒了过来", f"{target.name} HP {hp}/{target.max_hp}"]
    if target.status:
        afflictions.set_status(cur, "players", target.id, None)
        facts.append(f"{player.name}帮{target.name}摆脱了“{target.status.label}”的状态")
    return facts


def do_take_donated(cur: Cursor, player: Player, view: RoomView, a: TakeDonated) -> list[str]:
    box = smith.donation_box(view, a.target)
    want = a.name.strip()
    pick = next((d for d in box.donated if d["name"] == want), None) or next((d for d in box.donated if want and want in d["name"]), None)
    if pick is None:
        raise ActionError(f"{box.container}里没有{want}" + (f"（里面有：{'、'.join(d['name'] for d in box.donated)}）" if box.donated else "（里面没有别人放的东西）"))
    cur.execute(f"""select 1 from dispenser_log where player_id = %s and room_id = %s and key = %s
                    and taken_at > now() - interval '{smith.DONATE_TAKE_WINDOW}'""", (player.id, box.room, f"{box.key}#donated"))
    if cur.fetchone():
        raise ActionError(f"{player.name}今天已经从{box.container}拿过一件了，留点给别人（明天再来）")
    cur.execute("delete from donations where id = %s returning template_id, props", (pick["id"],))
    row = cur.fetchone()
    if row is None:
        raise ActionError(f"{pick['name']}刚被别人拿走了")
    cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                (row["template_id"], player.id, Jsonb(row["props"] | {"donated": True})))
    cur.execute("""insert into dispenser_log (player_id, room_id, key) values (%s, %s, %s)
                   on conflict (player_id, room_id, key) do update set taken_at = now()""", (player.id, box.room, f"{box.key}#donated"))
    return [f"{player.name}从{box.container}{box.where}挑了一件{pick['name']}（别人留下的，店里不收）"]


NOTE_MAX = 100


def do_write(cur: Cursor, player: Player, view: RoomView, a: Write) -> list[str]:
    """在纸条（props.writable）上写字：文字存进这一张的描述，名字变成"写了字的纸条"，写过的不能再改。
    玩家之间留言用：给队友、丢在地牢房间里提醒后来的人、当信物。字是玩家写的，不是 AI 编的"""
    item = helpers.inv_item(cur, view, player, a.item)
    if item.props.get("note"):
        raise ActionError(f"{item.name}上已经写过字了，改不了")
    if not helpers.prop(item, "writable"):
        raise ActionError(f"{item.name}上写不了字")
    text = re.sub(r"\s+", " ", a.message).strip().strip("「」“”\"'").strip()
    if not text:
        raise ActionError("要写什么？（说「在纸条上写……」）")
    if len(text) > NOTE_MAX:
        raise ActionError(f"纸条就这么大，最多写 {NOTE_MAX} 个字（这段有 {len(text)} 个）")
    if item.quantity > 1:                   # 叠着的只写最上面一张
        cur.execute("update item_instances set quantity = quantity - 1 where id = %s", (item.id,))
        cur.execute("insert into item_instances (template_id, player_id) values (%s, %s) returning id",
                    (item.template.id, player.id))
        note_id = cur.fetchone()["id"]
    else:
        note_id = item.id
    cur.execute("update item_instances set props = props || %s where id = %s",
                (Jsonb({"note": text, "writable": False, "name": "写了字的纸条",
                        "description": f"{item.template.description}上面用炭笔写着：“{text}”（{player.name}写的）"}), note_id))
    return [f"{player.name}在纸条上写下：“{text}”"]


def do_respawn(cur: Cursor, player: Player, view: RoomView, a: Respawn) -> list[str]:
    """倒下的人被抬回有看店 NPC 的地方（默认酒馆），NPC 当场扶起来回满血，按倒下的原因说句话"""
    if player.hp > 0:
        raise ActionError(f"{player.name}没有倒下，用不着复活")
    cur.execute(
        """select distinct r.id, r.name from rooms r join npcs n on n.room_id = r.id join npc_templates t on t.id = n.template_id
           where n.alive and not t.hostile and t.props ? 'revive_lines'""")
    havens = cur.fetchall()
    named = next((r for r in havens if a.target and (a.target in r["name"] or r["name"] in a.target)), None)
    dest = named or next((r for r in havens if r["id"] == RESPAWN_ROOM), None)
    if dest is None:
        raise ActionError(f"没有能把{player.name}抬过去照看的地方" + (f"（{a.target}）" if a.target else ""))
    if dest["id"] == player.room_id:
        raise ActionError(f"{player.name}已经在{dest['name']}了，等人来扶")
    cur.execute("update players set room_id = %s, following = null, stealth = null, updated_at = now() where id = %s",
                (dest["id"], player.id))
    facts = [f"有人把{player.name}带回了{dest['name']}"]
    # 那边的人先看到被抬进来，再看到扶起来
    cur.execute("insert into events (room_id, kind, observer) values (%s, 'carried_in', %s)",
                (dest["id"], f"有人把倒下的{player.name}抬了进来。"))
    revived = loading.keeper_revive(cur, dest["id"])
    return facts + (revived or [f"{dest['name']}里没人照看，{player.name}还躺着"])


def do_say(cur: Cursor, player: Player, view: RoomView, a: Say) -> list[str]:
    if afflictions.effect(player, "silence"):
        raise ActionError(f"{player.name}喉咙像被堵住了，一个字也说不出来（禁声，过一会儿才好）")
    if a.target is None:
        return [f"{player.name}说：“{a.message}”"]
    cur.execute("select 1 from players where room_id = %s and name = %s and id <> %s",
                (player.room_id, a.target, player.id))
    if not cur.fetchone():
        raise ActionError(f"{a.target}不在这里")
    return [f"{player.name}对{a.target}说：“{a.message}”"]


def do_freeform(cur: Cursor, player: Player, view: RoomView, a: Freeform) -> list[str]:
    # 不改任何状态，叙事 AI 自由发挥；需要本事的（辨认草药、翻墙、查线索）判一次技能，叙事照成败写
    facts = [f"{player.name}尝试：{a.description}"]
    if a.skill and a.difficulty:
        ok, rolled = helpers.check(cur, player, view, a.skill, a.difficulty)
        facts += rolled + ["成功了" if ok else "没有成功"]
    return facts


def do_reject(cur: Cursor, player: Player, view: RoomView, a: Reject) -> list[str]:
    raise ActionError(a.reason)


# 火把：背包里的火把（props.lights）拿到手上就换成点燃的样子；点燃的（props.burning）带到地牢下一层换成弱光的，
# 弱光的再下一层就烧完了。点燃的只能拿在手上：卸下、扔掉要再说一次确认，确认了就熄灭没了；不能给人、不能卖
DOUSE_CONFIRM = 120                     # 秒：说了一次要收火把，这么久内再说一次就真的熄灭


def burning(item: ItemInstance) -> bool:
    return "burning" in item.template.props


def _torch_floor(cur: Cursor, names: list[str], room_id: str) -> list[str]:
    """这些人拿着点着的火把下到了地牢新的一层（走下来、传送来）：火光变小，或者烧完"""
    if not dungeon.is_dungeon(room_id) or not names:
        return []
    cur.execute("""select i.id, t.name as item, t.props->>'burning' as next, p.name as who
                   from item_instances i join item_templates t on t.id = i.template_id join players p on p.id = i.player_id
                   where p.name = any(%s) and t.props ? 'burning' and i.equipped_slot is not null""", (names,))
    facts = []
    for r in cur.fetchall():
        if r["next"]:
            cur.execute("update item_instances set template_id = %s, props = '{}' where id = %s", (r["next"], r["id"]))
            facts.append(f"{r['who']}手上的火把火光变小了，照不了那么亮了，再下一层就会烧完")
        else:
            cur.execute("delete from item_instances where id = %s", (r["id"],))
            facts.append(f"{r['who']}手上的火把烧到了头，熄灭了")
    # 私酿的劲头：下到下一层就过去了（CHEER_FLOORS 层）
    for p in [loading.load_player(cur, r["id"]) for r in helpers.rows_by_names(cur, names)]:
        if e := afflictions.effect(p, "cheer"):
            e.left -= 1
            if e.left <= 0:
                p.effects.remove(e)
                facts.append(f"{p.name}身上那股浑身是劲的感觉过去了")
            afflictions.save_effects(cur, p)
    return facts


def douse(cur: Cursor, player: Player, item: ItemInstance) -> list[str]:
    """想把点着的火把收起来、扔掉：第一次只提醒，DOUSE_CONFIRM 秒内再说一次才熄灭（东西没了）"""
    asked = item.props.get("douse_asked", 0)
    cur.execute("select extract(epoch from now())::float as t")
    now = cur.fetchone()["t"]
    if now - asked > DOUSE_CONFIRM:
        cur.execute("update item_instances set props = props || jsonb_build_object('douse_asked', %s::float) where id = %s",
                    (now, item.id))
        return [f"{item.name}收起来就灭了，灭了就没法再点，只能扔掉",
                f"{player.name}还没动手：真要熄掉就再说一次"]
    cur.execute("delete from item_instances where id = %s", (item.id,))
    return [f"{player.name}把{item.name}按在地上掐灭了，烧过的火把没法再用了"]
