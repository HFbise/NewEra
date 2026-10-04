"""Room environment: light, cover, ground, heat, fog, star cycle, noise, features, floor clocks. / 房间环境"""
import math
import random
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from uuid import UUID

from psycopg import Cursor
from psycopg.types.json import Jsonb

import dungeon
from rules import (
    BLIND_HIT, EFFECT_TURNS, ENDURE_HIT_CHANCE, LIGHT_BRIGHT, LIGHT_DARK, LIGHT_FULL, MELEE_HIT, SCALE, WEAK_MULT,
    dark_attack, dark_factor, heal_amount, light_hit, skill_chance, skill_level,
)
from schema import Effect, ItemInstance, Npc, Player, Room, RoomView, Status, Stealth

from ..base.core import (
    ActionError, DETECT_START, DRINK_WINDOW, DRUNK_CHANCE, DRUNK_PENALTY, DRUNK_RESIST, DRUNK_TIME, START_DISTANCE,
    TIER_RANGE,
)
from . import everyday
from ..base import helpers, loading
from ..fight import afflictions, bosses, combat, duels, equipment, ranged_combat, stealth


def feature_cfg(cur: Cursor, room_id: str, name: str) -> dict:
    """这个环境物件在主题里的配置（on_break、noise、clears_fog）"""
    if not dungeon.is_dungeon(room_id):
        return {}
    theme = dungeon.floor_info(cur, room_id)["theme"]
    return next((f for f in dungeon.data()["themes"][theme].get("features", []) if f["name"] == name), {})


# 整层一个声响值（dungeon_floors.state.noise），出声的动作往上加：说话 1、念书卷 2、砸碎重物 2、重伤以上的一下 1、
# 群伤 2、被撞倒 1、逃跑 1，铜钟这种物件自己定。房间也记一份（rooms.env.noise），到了石像的 wake_noise 它就醒。
# 满了（max，装备 noise_max_add 往上加）引来游荡的怪直接扑进这个房间（21 层起一群），声响回落到 reset_to。
# 大祭司在场时每个出声的动作给他回血量上限的 noise_heal
def make_noise(cur: Cursor, player: Player, what, heal: bool = True) -> list[str]:
    room = player.room_id
    if not what or not dungeon.is_dungeon(room):
        return []
    cfg = dungeon.data()["themes"][dungeon.floor_info(cur, room)["theme"]].get("noise")
    if not cfg:
        return []
    amount = int(cfg.get("add", {}).get(what, 0)) if isinstance(what, str) else int(what)
    if mults := [e.get("value", 1) for e in equipment.fx(helpers.worn(cur, player), "noise_mult")]:
        amount = round(amount * min(mults))         # 软底靴这类：声响 ×value（取最好的一件）
    if amount <= 0:
        return []
    top = int(cfg.get("max", 10) + equipment.gear_add(cur, player, "noise_max_add"))
    noise = int(dungeon.floor_state(cur, room).get("noise", 0)) + amount
    env = room_env(cur, room)
    here = int(env.get("noise", 0)) + amount
    cur.execute("update rooms set props = jsonb_set(props, '{env,noise}', to_jsonb(%s::int)) where id = %s", (here, room))
    facts = [f"（声响 {min(noise, top)}/{top}）"]
    for n in loading.load_npcs(cur, "n.room_id = %s and n.alive and (t.props->>'dormant')::boolean", (room,)):
        if not helpers.tally(cur, n, "awake") and here >= int(n.template.props.get("wake_noise", 3)):
            helpers.tally_merge(cur, n, {"awake": 1})
            facts.append(f"{n.name}眼皮上的石屑簌簌往下掉：它被吵醒了")
    if heal:
        for n in combat.enemies_in(cur, room):
            if (pct := n.template.props.get("noise_heal")) and n.hp < n.template.max_hp:
                gain = min(n.template.max_hp - n.hp, max(1, round(n.template.max_hp * pct)))
                cur.execute("update npcs set hp = hp + %s where id = %s", (gain, n.id))
                facts.append(f"{n.name}的银面具微微一亮，那一声让他回了 {gain} 点血")
    if noise >= top:
        full = cfg.get("full", {})
        noise = int(full.get("reset_to", 5))
        depth = dungeon.parse_room(room)[1]
        name = dungeon.spawn_wanderer(cur, room, 2 if depth >= 21 else 1)
        st = stealth.stealth_state(player)
        st.detected, st.hidden = True, False
        stealth.save_stealth(cur, player, st)
        facts.append(f"声音在整座神殿里回荡开去，远处传来急促的脚步：{name}循着声音直扑了进来")
    dungeon.set_floor_state(cur, room, {"noise": noise})
    return facts


def feature_break(cur: Cursor, room_id: str, name: str) -> list[str]:
    """环境物件用掉以后（on_break）：打碎晶簇，房间暗下来"""
    ft = feature_cfg(cur, room_id, name)
    if not (brk := ft.get("on_break")) or "light" not in brk:
        return []
    env = room_env(cur, room_id)
    new = max(0, min(100, int(env.get("light", 50)) + brk["light"]))
    cur.execute("update rooms set props = jsonb_set(props, '{env,light}', to_jsonb(%s::int)) where id = %s", (new, room_id))
    return [f"{name}碎了一地，房间{'暗' if brk['light'] < 0 else '亮'}了一截（光亮 {new}）"]


def holy(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """圣水：泼向亡灵或怕光的怪，按档位重创它；泼别的没用（水也没了）"""
    if not target or target not in view.refs:
        raise ActionError(f"{item.name}要泼向一个敌人")
    npc = helpers.room_npc(cur, view, player, target)
    consume(cur, item)
    facts = [f"{player.name}把{item.name}泼向{npc.name}"]
    if not (npc.template.props.get("undead") or npc.template.props.get("light_averse")):
        return facts + [f"{npc.name}只是被泼湿了，什么事也没有"]
    dmg = random.randint(*TIER_RANGE[helpers.prop(item, "holy")])
    if npc.template.props.get("weak") == "holy":
        dmg = math.ceil(dmg * WEAK_MULT)            # 守陵人怕圣物
    hurt, dead = combat.hurt_npc(cur, player, npc, dmg)
    return facts + [f"圣水在{npc.name}身上嘶嘶地冒起白烟，受到 {dmg} 点伤害"] + hurt


def stun(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """念能定身的东西（古书残卷：props.stun 是状态说明）：不用判定，房间里所有敌人一起失去战斗能力，东西用掉"""
    foes = combat.enemies_in(cur, player.room_id)
    if not foes:
        raise ActionError(f"这里没有敌人，{item.name}念了也没用")
    if who := bosses.silenced(cur, player.room_id, player):
        raise ActionError(f"{who}禁了声，{item.name}上的字都暗着，念不出来")
    # 这一仗已经被控过的头目不吃这一套；房间里只剩它的话书先不念（不白白用掉这一层的次数）
    immune = [n for n in foes if bosses.boss_resists(cur, n, mark=False)]
    if immune and len(immune) == len(foes):
        raise ActionError(f"{'、'.join(n.name for n in immune)}这一仗已经被困住过一回，有了防备，念了也定不住")
    foes = [n for n in foes if n not in immune]
    if helpers.prop(item, "per_floor"):
        if not equipment.once_per_floor(cur, player, "tome"):
            raise ActionError(f"{item.name}这一层已经念过了，书页上的字暗着，要到下一层才会再亮起来")
        label = str(helpers.prop(item, "stun"))[:20]
        for npc in foes:
            bosses.boss_resists(cur, npc)
            afflictions.set_status(cur, "npcs", npc.id, Status(kind="incapacitated", label=label, escape=everyday.STUN_ESCAPE,
                                                    since=datetime.now(timezone.utc).isoformat()))
        return [f"{player.name}翻开{item.name}，念出上面的古老文字，书页上的字一行行亮起来又暗下去（这一层用过了）",
                f"{'、'.join(n.name for n in foes)}{label}，失去了战斗能力"]
    everyday.use_up(cur, item)
    label = str(helpers.prop(item, "stun"))[:20]
    for npc in foes:
        bosses.boss_resists(cur, npc)
        afflictions.set_status(cur, "npcs", npc.id, Status(kind="incapacitated", label=label, escape=everyday.STUN_ESCAPE,
                                                since=datetime.now(timezone.utc).isoformat()))
    names = "、".join(n.name for n in foes)
    return [f"{player.name}翻开{item.name}，念出上面的古老文字，字句在空气里回荡，书页化成灰烬散开",
            f"{names}{label}，失去了战斗能力"]


RECALL_TO = "square"                    # 回城水晶把人传回这里


def recall(cur: Cursor, player: Player, item: ItemInstance) -> list[str]:
    """在地牢里捏碎回城水晶：传回村口广场。被怪缠着也能走，这就是它值钱的地方"""
    if not dungeon.is_dungeon(player.room_id):
        raise ActionError(f"{item.name}只有在远古地牢里才有用")
    if duels.active_duel(cur, player.id):
        raise ActionError(f"{player.name}正在决斗，走不了")
    consume(cur, item)
    dungeon.touch(cur, player.room_id)
    cur.execute("update players set room_id = %s, following = null, stealth = null, updated_at = now() where id = %s",
                (RECALL_TO, player.id))
    return [f"{player.name}捏碎了{item.name}，一阵淡蓝色的光裹住全身，再睁眼已经回到了{loading.load_room(cur, RECALL_TO).name}"]


# 消耗品的种类（模板 props.kind）：决定怎么用（吃、喝、敷）、算不算药。没写的按老办法猜：酒、水、汤是喝的，别的是吃的
USE_KINDS = {"food": "食物", "drink": "饮品", "potion": "药剂", "salve": "外用药"}


MEDICINE_KINDS = ("potion", "salve")


MEDIC_BONUS = 0.1                       # 给别人用药：用药的人医药每级多回 10%


MEDIC_CURE_LEVEL = 3                    # 医药到 3 级，给别人用药顺带解毒、止血


def use_kind(item: ItemInstance) -> str:
    if kind := helpers.prop(item, "kind"):
        return kind
    return "drink" if item.template.id == "made_drink" or is_alcohol(item) or any(c in item.name for c in "水汤茶奶") else "food"


def use_self(player: Player, item: ItemInstance) -> str:
    return {"food": f"{player.name}吃掉了{item.name}", "drink": f"{player.name}喝掉了{item.name}",
            "potion": f"{player.name}喝下了{item.name}", "salve": f"{player.name}给自己用上了{item.name}"}[use_kind(item)]


def _use_other(player: Player, target: Player, item: ItemInstance) -> str:
    return {"food": f"{player.name}喂{target.name}吃了{item.name}", "drink": f"{player.name}喂{target.name}喝了{item.name}",
            "potion": f"{player.name}给{target.name}灌下了{item.name}",
            "salve": f"{player.name}给{target.name}用上了{item.name}"}[use_kind(item)]


# 吃喝回血按血量上限的百分比算：rules.heal_amount
def is_alcohol(item: ItemInstance) -> bool:
    return bool(helpers.prop(item, "alcohol")) or "酒" in item.name


def eat_effect(cur: Cursor, eater: Player, item: ItemInstance, user: Player) -> list[str]:
    """吃喝下去的效果：有毒的掉血，蒙汗药这类把人放倒，正常的回血（倒下的人吃了回血药也能站起来）。
    回血按吃的人血量上限的百分比（heal_amount）；药草（模板 props.herbal）按用药的人（自己吃是自己，喂别人是喂的人）
    的自然等级多回。给别人用药（药剂、外用药）：用药的人医药每级多回 MEDIC_BONUS，用得上（回了血、治好了状态）练医药"""
    facts = []
    points = item.heal + (skill_level(user.skills.get("nature", 0)) if item.heal and item.template.props.get("herbal") else 0)
    medic = user.id != eater.id and use_kind(item) in MEDICINE_KINDS
    medic_level = skill_level(user.skills.get("medicine", 0)) if medic else 0
    heal = heal_amount(points, eater.max_hp, is_alcohol(item))
    if medic and heal:
        heal = round(heal * (1 + MEDIC_BONUS * medic_level))
    was_hp, was_effects = eater.hp, len(eater.effects)
    harm = item.harm
    if harm and (check := helpers.prop(item, "harm_check")):
        # 沼泽菇这类：认得出来（自然判定）就是好吃的，认不出来吃到有毒的
        if helpers.roll(skill_chance(skill_level(user.skills.get(check, 0)), 2)):
            harm = 0
            facts.append(f"{user.name}认出这是能吃的那种")
        else:
            facts.append(f"{user.name}没认出来，吃到了有毒的那种")
            if not afflictions.effect(eater, "poison"):
                eater.effects.append(Effect(kind="poison", value=1, left=EFFECT_TURNS["poison"], label="吃坏了肚子",
                                            source=item.name))
                afflictions.save_effects(cur, eater)
    hp = max(0, min(eater.max_hp, eater.hp + equipment.heal_scale(eater, heal) - harm))
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (hp, eater.id))
    cures = [helpers.prop(item, "cure")] + (["poison", "bleed"] if medic and medic_level >= MEDIC_CURE_LEVEL else [])
    for cure in dict.fromkeys(c for c in cures if c):
        if e := afflictions.effect(eater, cure):
            # 绷带止血、解毒苔解毒；医药练到家的人给别人用什么药都顺带解毒止血
            eater.effects.remove(e)
            afflictions.save_effects(cur, eater)
            facts.append(f"{eater.name}身上的{afflictions.EFFECT_NAMES[cure]}好了")
    practiced = medic and (hp > was_hp or len(eater.effects) < was_effects)
    if harm:
        facts.append(f"{item.name}有毒，{eater.name}掉了 {harm} 点 HP，当前 HP {hp}/{eater.max_hp}")
    elif item.heal:
        facts.append(f"{eater.name}恢复 {hp - eater.hp} 点 HP，当前 HP {hp}/{eater.max_hp}")
        if eater.hp <= 0 < hp:
            facts.append(f"{eater.name}醒了过来")
    if hp == 0:
        facts.append(f"{eater.name}倒下了")
        combat.downed_by(cur, eater, "poison", item.name)
    elif harm:
        eater.hp = hp
        facts += helpers.toughen(cur, eater)                   # 毒扛过去了，耐性一定涨
    if hp > 0 and item.knockout:
        knock_out(cur, "players", eater.id, item.knockout)
        facts.append(f"{eater.name}{item.knockout}，失去战斗能力")
    if (q := helpers.prop(item, "quench")) and hp > 0:
        cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::int) where id = %s",
                    (QUENCH, int(q), eater.id))
        facts.append(f"凉水下肚，{eater.name}接下来 {int(q)} 个动作不怕灼热")
    if helpers.prop(item, "sober"):
        cur.execute("update players set drunk_until = null where id = %s", (eater.id,))
        facts.append(f"{eater.name}酒醒了，脑子清楚了")
    elif helpers.prop(item, "alcohol") or "酒" in item.name:
        # 连着喝第几杯：上一杯在 DRINK_WINDOW 里就接着数，否则从 1 开始
        # 烈酒（props.strength）一杯顶几杯
        cur.execute(f"""update players set drinks = case when last_drink_at > now() - interval '{DRINK_WINDOW}'
                                                         then drinks else 0 end + %s, last_drink_at = now()
                        where id = %s returning drinks""", (helpers.prop(item, "strength") or 1, eater.id))
        n = cur.fetchone()["drinks"]
        resist = DRUNK_RESIST ** skill_level(eater.skills.get("endurance", 0))
        if hp > 0 and helpers.roll(min(1.0, DRUNK_CHANCE * 2 ** (n - 1) * resist)):
            cur.execute(f"update players set drunk_until = now() + interval '{DRUNK_TIME}' where id = %s", (eater.id,))
            facts.append(f"{eater.name}喝醉了（连着喝了 {n} 杯）：接下来一阵子说话含糊，做什么都不太利索")
        elif hp > 0 and n >= 2:
            eater.hp = hp
            facts += helpers.toughen(cur, eater, ENDURE_HIT_CHANCE)   # 连着喝还没醉，扛住了酒劲
    if practiced:
        facts += helpers.gain_skill(cur, user, "medicine")
    return facts


def drunk(player: Player) -> float:
    return DRUNK_PENALTY if player.drunk else 0.0


def slur(text: str) -> str:
    """醉话：在原话里随机插“嗝”和“……”"""
    out = []
    for ch in text:
        out.append(ch)
        if ch in "，。！？、,.!? " and random.random() < 0.6:
            out.append(random.choice(["……", "……嗝，", "嗝……"]))
        elif ch not in "，。！？、,.!? " and random.random() < 0.08:
            out.append(random.choice(["……", "嗝"]))
    return "".join(out) + random.choice(["……嗝", "……", ""])


def feed(cur: Cursor, player: Player, item: ItemInstance, name: str) -> list[str]:
    """喂同房间的玩家吃喝：倒下的、被放倒捆住的、队友都能喂。
    有毒、下了药的东西，清醒的外人不会乖乖吃下去（想害人得泼、得骗他自己吃）"""
    target, awake = duels.room_player(cur, player, name, lock=True)
    if not awake:
        raise ActionError(f"{target.name}睡着了，喂不进去")
    helpless = target.hp <= 0 or combat.subdued(target)
    mate = bool(player.party_id and player.party_id == target.party_id)
    if (item.harm or item.knockout) and not helpless and not mate:
        # 强行塞嘴里也一样：清醒的人会挣扎吐掉，得先把他放倒、捆住
        raise ActionError(f"{target.name}清醒着，不肯吃{item.name}，硬塞也会被吐掉，得先把他制住")
    # 喂毒掉血也是伤害，得在决斗里；只把人药倒不掉血的算整人
    if item.harm and not duels.active_duel(cur, player.id, target.id):
        raise ActionError(f"{item.name}有毒，{player.name}和{target.name}没有在决斗，不能拿它害人")
    consume(cur, item)
    return [_use_other(player, target, item)] + eat_effect(cur, target, item, player)


def consume_n(cur: Cursor, item: ItemInstance, n: int) -> None:
    """一次用掉 n 个（叠着的减 n，不够或刚好就删掉）"""
    if item.quantity > n:
        cur.execute("update item_instances set quantity = quantity - %s where id = %s", (n, item.id))
    else:
        cur.execute("delete from item_instances where id = %s", (item.id,))


def consume(cur: Cursor, item: ItemInstance) -> None:
    """用掉一个（叠着的减一，最后一个删掉）"""
    if item.quantity > 1:
        cur.execute("update item_instances set quantity = quantity - 1 where id = %s", (item.id,))
    else:
        cur.execute("delete from item_instances where id = %s", (item.id,))


def precious(cur: Cursor, item: ItemInstance) -> bool:
    """钥匙、任务要交的东西、只能拿一次的东西（护符）：不能被当成材料用掉"""
    if item.template.type == "key":
        return True
    cur.execute("""select exists (select 1 from quests where needs_item = %(t)s)
                       or exists (select 1 from rooms, jsonb_each(coalesce(props->'dispensers', '{}')) d
                                  where d.value->>'item' = %(t)s and coalesce((d.value->>'once')::boolean, false)
                                    and rooms.id not like 'dg-%%') as p""",
                {"t": item.template.id})
    return cur.fetchone()["p"]


def knock_out(cur: Cursor, table: str, target_id: UUID, label: str) -> None:
    """药倒：失去战斗能力，挣脱（醒来）难度普通"""
    afflictions.set_status(cur, table, target_id, Status(kind="incapacitated", label=label[:20], escape="normal",
                                              since=datetime.now(timezone.utc).isoformat()))


FOG_SPOT = 0.75                         # 浓雾里怪发现人的几率倍数（躲藏容易一级，双方都看不清）


FOG_FAR = 0.5                           # 浓雾里远程打"几步开外"的命中倍数（theme.fog.ranged_far_mult）


def star_dark(cur: Cursor, room_id: str) -> bool:
    """星界秘境的星光这会儿是暗的"""
    env = room_env(cur, room_id)
    return bool(dungeon.is_dungeon(room_id) and int(env.get("shift", 0)) < 0
                and dungeon.data()["themes"][dungeon.floor_info(cur, room_id)["theme"]].get("star_cycle"))


def away(cur: Cursor, npc: Npc) -> bool:
    """只在星光暗时出现的怪（虚空之眼 only_dark）：亮的时候不在场上"""
    return bool(npc.template.props.get("only_dark")) and not star_dark(cur, npc.room_id)


def void_fall(cur: Cursor, player: Player, add: int = 0, force: bool = False) -> list[str]:
    """虚空边缘（env.edge，theme.void）：被撞倒、推到边上时过一次运动，失败就坠入虚空：掉血量上限的 fall_damage_pct，
    被传回旁边一间不在边上的房间（这场仗等于重新进来）。星辰之靴这类不会掉"""
    env = room_env(cur, player.room_id)
    if player.hp <= 0 or not env.get("edge") or not dungeon.is_dungeon(player.room_id) or equipment.gear_has(cur, player, "void_immune"):
        return []
    run, depth = dungeon.parse_room(player.room_id)
    void = dungeon.data()["themes"][dungeon.floor_info(cur, player.room_id)["theme"]].get("void") or {}
    facts = []
    if not force:
        ok, facts = helpers.check(cur, player, SimpleNamespace(trained=[]), void.get("check", "athletics"),
                           int(void.get("base_difficulty", 2)) + depth // 10 + add)
        if ok:
            return facts + [f"{player.name}在石台边上晃了两下，稳住了"]
    dmg = max(SCALE, round(player.max_hp * void.get("fall_damage_pct", 0.15)))
    cur.execute("""select e.to_room from room_exits e join rooms r on r.id = e.to_room
                   where e.room_id = %s and e.direction in ('north', 'south', 'east', 'west')
                   order by coalesce((r.props->'env'->>'edge')::boolean, false), random() limit 1""", (player.room_id,))
    row = cur.fetchone()
    hurt, _ = combat.hurt_player(cur, player, dmg, "other", "虚空")
    facts += [f"{player.name}脚下一空，坠进了星海里，掉了 {dmg} 点血"] + hurt
    if row and player.hp > 0:
        cur.execute("update players set room_id = %s, status = null, stealth = %s where id = %s",
                    (row["to_room"], Jsonb(Stealth(room=row["to_room"], chance=DETECT_START).model_dump()), player.id))
        dungeon.mark_seen(cur, row["to_room"])
        cur.execute("select name from rooms where id = %s", (row["to_room"],))
        facts.append(f"一阵失重过后，{player.name}摔在了{cur.fetchone()['name']}（要回去得重新走过去）")
    return facts


def fog_here(cur: Cursor, room_id: str, player: Optional[Player] = None) -> Optional[dict]:
    """这个房间有没有浓雾（迷雾葬海 theme.fog）：灯塔点亮了（整层 fog_clear）、雾笛吹散了（env.fog_off）、
    带着不怕雾的东西的人不算"""
    if not dungeon.is_dungeon(room_id):
        return None
    cfg = dungeon.data()["themes"][dungeon.floor_info(cur, room_id)["theme"]].get("fog")
    if not cfg or dungeon.floor_state(cur, room_id).get("fog_clear") or int(room_env(cur, room_id).get("fog_off", 0)) > 0:
        return None
    if player and equipment.gear_has(cur, player, "fog_immune"):
        return None
    return cfg


def start_distance(cur: Cursor, room_id: str) -> int:
    """进门时怪离你几格：平时 START_DISTANCE，浓雾里 fog.start_distance，渡魂者吹熄了灯就贴身"""
    env = room_env(cur, room_id)
    if "fog_start" in env:
        return int(env["fog_start"])
    fog = fog_here(cur, room_id)
    return int(fog.get("start_distance", START_DISTANCE)) if fog else START_DISTANCE


# 光亮 0~100（村里没设环境的算 100）：低于 LIGHT_FULL 普通攻击命中按比例打折，0 时只剩 LIGHT_MIN_HIT；
# 低于 LIGHT_DARK 做花样难度 +1、躲藏容易 1 级。有人带火把 +TORCH_LIGHT；看不清（blind）的人光亮算 0。
# 越暗怪越凶、钱越多（_dark_factor）；怕光的怪在 LIGHT_BRIGHT 以上攻击 -1。
# 积水：闪避效果减半、逃跑难度 +1。掩体：躲藏容易 1 级
TORCH_LIGHT = 35                        # 说明用；实际按 world.yaml 火把（点燃）/（弱光）的 props.light


LIGHT_OLD = {"bright": 70, "dim": 40, "dark": 15}      # 旧存档里的文字写法


def room_env(cur: Cursor, room_id: str) -> dict:
    cur.execute("select props->'env' as env from rooms where id = %s", (room_id,))
    row = cur.fetchone()
    return (row and row["env"]) or {}


def base_light(env: dict) -> int:
    level = env.get("light", 100) if env else 100
    level = LIGHT_OLD.get(level, 40) if isinstance(level, str) else int(level)
    return max(0, min(100, level + int(env.get("shift", 0)))) if env else level       # 梦境的浮动、星光的明暗


def light_level(cur: Cursor, room_id: str, env: Optional[dict] = None, player: Optional[Player] = None) -> int:
    """房间现在的光亮 0~100：房间本身（含点着的灯）+ 有人带火把。看不清（blind）不在这里算，是命中减半（BLIND_HIT）"""
    env = room_env(cur, room_id) if env is None else env
    level = base_light(env)
    if env and level < 100:
        # 点着的火把（props.burning）取最亮的一支，永久光源（矿工灯、提灯）也只取最亮的一件，两样叠加
        cur.execute("""select max((t.props->>'light')::int) filter (where t.props ? 'burning') as torch,
                              greatest(max((t.props->>'light')::int) filter (where not t.props ? 'burning'),
                                       max((i.props->>'gem_light')::int)) as lamp
                       from item_instances i join item_templates t on t.id = i.template_id
                       join players p on p.id = i.player_id
                       where p.room_id = %s and (t.props ? 'light' or i.props ? 'gem_light') and i.equipped_slot is not null""",
                    (room_id,))
        row = cur.fetchone()
        torch, lamp = row["torch"] or 0, row["lamp"] or 0
        if (torch or lamp) and (fog := fog_here(cur, room_id)):
            cur.execute("""select 1 from item_instances i join item_templates t on t.id = i.template_id join players p on p.id = i.player_id
                           where p.room_id = %s and i.equipped_slot is not null and (t.props->>'fog_ignore')::boolean limit 1""",
                        (room_id,))
            mult = 1.0 if cur.fetchone() else fog.get("torch_mult", 0.5)        # 灯塔的提灯在雾里照常亮
            torch, lamp = round(torch * mult), round(lamp * mult)
        level += torch + lamp
    if player and env and env.get("shift") and equipment.gear_has(cur, player, "star_cycle_immune"):
        level -= int(env["shift"])              # 不受星光明暗（梦境浮动）影响，按房间本来的光亮算
    if env and (scroll := env.get("scroll")):
        cur.execute("select 1 from players where id = %s and room_id = %s", (scroll["by"], room_id))
        if cur.fetchone():
            level += scroll["value"]
    return max(0, min(100, level))


def light_info(cur: Cursor, player: Player, room: Room) -> Optional[dict]:
    """给界面看的光亮：数值、说法、这个亮度现在对这个人有什么影响、光从哪来。没设环境的房间（村里）是 None"""
    env = room.props.get("env")
    if not env:
        return None
    base = base_light(env)
    light = light_level(cur, room.id, env, player=player)
    lines = []
    if afflictions.effect(player, "blind"):
        lines.append(f"你看不清东西，这一回合命中 ×{BLIND_HIT}")
    hit_light = max(light, LIGHT_FULL) if equipment.gear_has(cur, player, "darkvision") and not afflictions.effect(player, "blind") else light
    hit = round(light_hit(hit_light, MELEE_HIT[0]) * ranged_combat.blind(player) * 100)
    lines.append(f"你贴身普通攻击的命中 {hit}%" + ("（夜视，不受暗处影响）" if hit_light > light
                                                  else "（亮度 50 以上不打折）" if light >= LIGHT_FULL else "（越暗越低，带火把能补）"))
    if light < LIGHT_DARK:
        lines.append("几乎漆黑：做花样难一级，躲起来容易一级")
    atk = dark_attack(light)
    lines.append("怪下手更狠（攻击 +10）" if atk > 0 else "怪被照得缩手缩脚（攻击 −10）" if atk < 0 else "怪的攻击正常")
    if light >= LIGHT_BRIGHT:
        lines.append("怕光的怪攻击再 −1")
    lines.append(f"打怪掉的钱 ×{1 + 0.5 * dark_factor(light):.2f}（越暗越多）")
    parts = [f"房间 {base}"]
    cur.execute("""select max((t.props->>'light')::int) filter (where t.props ? 'burning') as torch,
                          greatest(max((t.props->>'light')::int) filter (where not t.props ? 'burning'),
                                   max((i.props->>'gem_light')::int)) as lamp
                   from item_instances i join item_templates t on t.id = i.template_id join players p on p.id = i.player_id
                   where p.room_id = %s and (t.props ? 'light' or i.props ? 'gem_light') and i.equipped_slot is not null""",
                (room.id,))
    row = cur.fetchone()
    if row["torch"]:
        parts.append(f"火把 +{row['torch']}")
    if row["lamp"]:
        parts.append(f"随身的光 +{row['lamp']}")
    if (scroll := env.get("scroll")) and light > base:
        parts.append(f"光明卷轴 +{scroll['value']}")
    if env.get("ground") == "water":
        lines.append("地上积水：闪避效果减半，逃跑难一级")
    if env.get("cover"):
        lines.append("有掩体：躲起来容易一级")
    return {"value": light, "word": dungeon.light_word(light), "source": "，".join(parts), "lines": lines}


def env_text(cur: Cursor, room: Room) -> str:
    """给 AI 看的环境说明，没设环境的房间是空的"""
    env = room.props.get("env")
    return dungeon.env_text(env, light_level(cur, room.id, env)) if env else ""


# 沉睡的（石面守像，props.dormant）：房间声响到 wake_noise 或者挨了打才醒（npcs.tally.awake），醒之前不算敌人
AWAKE_SQL = "not (coalesce((t.props->>'dormant')::boolean, false) and not coalesce((n.tally->>'awake')::boolean, false))"


def sand_rules(cur: Cursor, player: Player) -> Optional[dict]:
    """这个人脚下是流沙（ground: sand，本主题 theme.sand 的规则），沙行靴不怕"""
    if room_env(cur, player.room_id).get("ground") != "sand" or not dungeon.is_dungeon(player.room_id) \
            or equipment.gear_has(cur, player, "sand_immune"):
        return None
    return dungeon.data()["themes"][dungeon.floor_info(cur, player.room_id)["theme"]].get("sand") \
        or {"move_max": 1, "stuck_chance": 0.3, "athletics_step": 0.05, "sink_after": 1}


SAND_STILL = "_sand_still"              # players.flags：在流沙里站着没挪窝几轮了


def sand_sink(cur: Cursor, player: Player, done: list) -> list[str]:
    """流沙：战斗里一轮都没挪（走、靠近退开、闪避、挣脱）就记一轮，超过 sink_after 轮往下陷（缠住）"""
    sand = sand_rules(cur, player)
    if not sand or player.hp <= 0:
        return []
    if any(a.action in ("move", "maneuver", "dodge", "struggle", "stand") for a, _ in done):
        cur.execute("update players set flags = flags - %s where id = %s", (SAND_STILL, player.id))
        return []
    still = int(player.flags.get(SAND_STILL, 0)) + 1
    if still <= sand.get("sink_after", 1):
        cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::int) where id = %s",
                    (SAND_STILL, still, player.id))
        return [f"（{player.name}脚下的沙子在慢慢往下陷：下一轮再不挪一挪就要陷进去了）"]
    cur.execute("update players set flags = flags - %s where id = %s", (SAND_STILL, player.id))
    return afflictions.inflict(cur, loading.load_player(cur, player.id), "restrained", "陷进了流沙，半截身子埋在沙里", 0, "流沙", escape=1)


def turn_over(cur: Cursor, player_id: UUID, room_id: str) -> list[str]:
    """一个敌人回合结束：闭眼、掐自己的清掉；梦境回廊的光亮浮动、距离打乱"""
    cur.execute("update players set flags = flags - %s - %s where id = %s", (combat.EYES_CLOSED, combat.PINCHED, player_id))
    return []


def room_turn(cur: Cursor, room_id: str) -> list[str]:
    """这个房间的敌人回合过了一轮：光亮浮动（light_jitter）、距离打乱（dislocate）"""
    if not dungeon.is_dungeon(room_id):
        return []
    theme = dungeon.data()["themes"][dungeon.floor_info(cur, room_id)["theme"]]
    env = room_env(cur, room_id)
    facts, upd = [], {}
    if j := theme.get("light_jitter"):
        upd["shift"] = random.randint(-j, j)
    if cyc := theme.get("star_cycle"):
        n = int(env.get("turns", 0)) + 1
        upd["turns"] = n
        period = int(env.get("period") or cyc.get("period", 4))
        bright = (n // period) % 2 == 0
        upd["shift"] = cyc.get("swing", 35) * (1 if bright else -1)
        if bright != (int(env.get("shift", 0)) >= 0):
            facts.append(f"头顶那颗魔星{'亮了起来，星光洒满石台' if bright else '暗了下去，四周一下子黑了'}（光亮 {light_level(cur, room_id, env | upd)}）")
        left = period - n % period
        for p in helpers.load_players_in(cur, room_id):
            if any(e.kind == "bless" and e.stat == "star_chart" for e in p.effects):
                facts.append(f"（{p.name}对着星图数了数：星光还有 {left} 个回合变{'暗' if bright else '亮'}）")
    if int(env.get("fog_off", 0)) > 0:
        upd["fog_off"] = int(env["fog_off"]) - 1
        if upd["fog_off"] == 0:
            facts.append("雾又慢慢合拢了")
    if dis := theme.get("dislocate"):
        n = int(env.get("turns", 0)) + 1
        upd["turns"] = n
        if n % dis.get("every", 3) == 0 and combat.enemies_in(cur, room_id):
            moved = []
            for p in helpers.load_players_in(cur, room_id):
                if equipment.gear_has(cur, p, "dislocate_immune"):
                    continue
                st = stealth.stealth_state(p)
                for npc in combat.enemies_in(cur, room_id):
                    stealth.set_distance(st, npc, random.randint(0, 2))
                stealth.save_stealth(cur, p, st)
                moved.append(p.name)
            if moved:
                facts.append("四周像梦一样晃了一下，再睁眼时每个人和怪的位置都变了（距离全乱了）")
    if upd:
        cur.execute("update rooms set props = jsonb_set(props, '{env}', coalesce(props->'env', '{}'::jsonb) || %s) where id = %s",
                    (Jsonb(upd), room_id))
    return facts


def glare(cur: Cursor, player: Player, room_id: str) -> list[str]:
    """炫光（水晶洞窟 env.glare）：光亮到 at 以上，每个敌人回合 chance 几率被晃得看不清一回合；闭着眼的不会"""
    g = room_env(cur, room_id).get("glare")
    if not g or player.hp <= 0 or player.flags.get(combat.EYES_CLOSED) or light_level(cur, room_id) < g.get("at", 70) \
            or equipment.gear_has(cur, player, "glare_immune"):
        return []
    if not helpers.roll(g.get("chance", 0.25)) or afflictions.effect(player, "blind"):
        return []
    return afflictions.inflict(cur, player, "blind", "被晶面反射的光晃花了眼", 0, "炫光")


HEAT_HINT = ("热浪扑面：这一层一直在烤人，每做一个动作都掉一点血（说话、看不算）；喝一口泉水能撑 4 个动作，"
             "这一层有两间冷却水池能接泉水")


HEAT_FREE = {"say", "talk", "look", "reject"}     # 灼热的楼层里不算动作、不掉血的


QUENCH = "_quench"                                # players.flags：喝了泉水，还有几个动作不怕灼热


def heat(cur: Cursor, player_id: UUID, action: str) -> list[str]:
    """灼热（地底熔炉，房间 env.heat）：常驻，这一层每做一个动作掉血量上限的 heat 比例（打不打仗都算，不看防御，最少 1 点）。
    喝泉水（props.quench）停几个动作；装备 heat_resist 减半或免疫；头目转阶段把房间的 heat_mult 翻倍"""
    if action in HEAT_FREE:
        return []
    cur.execute("select room_id from players where id = %s", (player_id,))
    room = cur.fetchone()["room_id"]
    env = room_env(cur, room)
    if not env.get("heat"):
        return []
    player = loading.load_player(cur, player_id)
    if player.hp <= 0:
        return []
    if (left := int(player.flags.get(QUENCH, 0))) > 0:
        cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::int) where id = %s",
                    (QUENCH, left - 1, player_id))
        return [] if left > 1 else [f"{player.name}喝下去的那点凉意散了，热浪又贴了上来"]
    resist = min([float(e.get("value", 1)) for e in equipment.fx(helpers.worn(cur, player), "heat_resist")] or [1.0])
    if resist <= 0:
        return []
    dmg = max(1, round(player.max_hp * env["heat"] * env.get("heat_mult", 1) * resist))
    hurt, _ = combat.hurt_player(cur, player, dmg, "other", "灼热")
    return [f"热浪烤得{player.name}头昏眼花，掉了 {dmg} 点血"] + hurt


NOISY = {"say": "talk", "talk": "talk", "flee": "flee"}


def floor_clock(cur: Cursor, player_id: UUID) -> list[str]:
    """地牢里每做一个动作：沙漠、梦境的出口到点重新连接；沙漏房里的沙子往下流"""
    player = loading.load_player(cur, player_id)
    room = player.room_id
    if not dungeon.is_dungeon(room):
        return []
    facts = []
    theme = dungeon.data()["themes"][dungeon.floor_info(cur, room)["theme"]]
    if shift := theme.get("shifting_exits"):
        n = int(dungeon.floor_state(cur, room).get("acts", 0)) + 1
        dungeon.set_floor_state(cur, room, {"acts": n})
        if n % shift.get("every", 8) == 0 and dungeon.shift_exits(cur, room, random.randint(2, 3)):
            facts.append(shift.get("label") or f"远处传来石墙挪动的闷响，{theme['name']}里有几条路变了（走过的地方可能不再相通）")
    glass = player.flags.get(helpers.HOURGLASS)
    if glass and glass.get("room") != room:
        cur.execute("update players set flags = flags - %s where id = %s", (helpers.HOURGLASS, player.id))    # 及时走出来了
        glass = None
    timed = (helpers.room_props(cur, room).get("dispensers") or {}).get("event") or {}
    if not glass and timed.get("service") == "timed":
        cur.execute("select 1 from dispenser_log where player_id = %s and room_id = %s and key = 'event'", (player.id, room))
        if not cur.fetchone():
            # 刚进沙漏房：身后的石门开始往下落
            glass = {"room": room, "left": int(timed.get("actions", 3)) + 1}
            cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::jsonb) where id = %s",
                        (helpers.HOURGLASS, Jsonb(glass), player.id))
    if glass:
        left = int(glass["left"]) - 1
        if left > 0:
            cur.execute("update players set flags = jsonb_set(flags, %s, to_jsonb(%s::int)) where id = %s",
                        ([helpers.HOURGLASS, "left"], left, player.id))
            facts.append(f"（沙漏里的沙子还够 {left} 个动作）")
        else:
            cur.execute("update players set flags = flags - %s where id = %s", (helpers.HOURGLASS, player.id))
            cur.execute("""insert into dispenser_log (player_id, room_id, key) values (%s, %s, 'event')
                           on conflict do nothing""", (player.id, room))
            name = dungeon.spawn_wanderer(cur, room)
            st = stealth.stealth_state(player)
            st.detected, st.hidden = True, False
            stealth.save_stealth(cur, player, st)
            facts.append(f"最后一粒沙子落了下去，石门轰地关死，墙里走出了{name}：得打一架才能出去")
    return facts
