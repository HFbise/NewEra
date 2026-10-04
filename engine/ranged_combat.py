"""Ranged weapons, ammo, reloading, cover and smoke. / 远程武器、弹药、装填、掩体和烟雾"""
from typing import Optional

from psycopg import Cursor
from psycopg.types.json import Jsonb

from rules import BLIND_HIT, LIGHT_BRIGHT, MELEE_HIT, RANGED_COVER, RANGED_HIT, SMOKE_HIT, STEADY_HIT
from schema import ItemInstance, Npc, Player, Reload, RoomView

from .core import ActionError
from . import afflictions, combat, environment, equipment, helpers, loading, stealth


# props.ranged 的武器：贴身难射，离得越远越准（RANGED_HIT）；steady 的（麦琪的弩）远近都是 STEADY_HIT。
# 射完换成 props.unloaded 那个"空"的样子（弩没弦），说"装填"换回来（props.loads），算一个动作。
# 射的时候只算这件远程武器的伤害；"空"的远程武器不算近战武器
# 命中表：rules.RANGED_HIT、STEADY_HIT、BLIND_HIT、SMOKE_*


WEAK_WORDS = {"fire": "怕火", "pierce": "甲缝", "light": "怕光", "bright": "怕光", "holy": "怕圣物", "poison": "怕毒", "water": "怕水"}


def weak_spot(cur: Cursor, player: Player, npc: Npc, melee: list[ItemInstance], shooter: Optional[ItemInstance],
               loaded: Optional[str], fired: list[dict]) -> Optional[str]:
    """这一下打没打在弱点上（头目的 props.weak）：带火的（火把、火油箭、余烬长剑）、破甲的、强光下（光亮 70 以上）"""
    weak = npc.template.props.get("weak")
    if not weak or weak in (npc.template.props.get("resist_element") or []):
        return None
    hit = False
    if any(e.get("do") == "element" and e.get("kind") == weak for e in fired):
        return WEAK_WORDS.get(weak, weak)       # 余烬石：这一击算作火
    if weak == "fire":
        ammo_fire = False
        if loaded:
            cur.execute("select (props->>'fire')::boolean as f from item_templates where id = %s", (loaded,))
            ammo_fire = bool((cur.fetchone() or {}).get("f"))
        hit = ammo_fire or any(helpers.prop(w, "fire") for w in ([shooter] if shooter else melee))
    elif weak == "pierce":
        hit = any(e.get("do") == "pierce" for e in fired)
    elif weak in ("light", "bright"):
        hit = environment.light_level(cur, npc.room_id) >= LIGHT_BRIGHT
    return WEAK_WORDS.get(weak, weak) if hit else None


def choose_weapons(weapons: list[ItemInstance], d: int, want: Optional[ItemInstance]
                    ) -> tuple[list[ItemInstance], Optional[ItemInstance]]:
    """这一下用什么打：(近战用的武器, 射的远程武器)。说了用哪件就用哪件；
    没说：手上有装好的远程武器，对方又没贴身（或者这件远近都准、或者手上没有别的武器）就射，不然近战"""
    loaded = [w for w in weapons if helpers.prop(w, "ranged")]
    melee = [w for w in weapons if not helpers.prop(w, "ranged") and not helpers.prop(w, "loads")]
    if want is not None:
        if helpers.prop(want, "loads"):
            raise ActionError(f"{want.name}还没装填，射不了（先说「装填」）")
        if helpers.prop(want, "ranged"):
            return [], want
        # 说了用哪把近战武器也按面板打：双持两把都算；AI 解析时指了背包里没拿着的那把，也照手上的打
        # （以前报"没拿在手上"，这一轮白白浪费）
        return melee, None
    if loaded and (d > 0 or not melee or helpers.prop(loaded[0], "steady")):
        return [], loaded[0]
    return melee, None


def how(player: Player, melee: list[ItemInstance], shooter: Optional[ItemInstance]) -> tuple[str, int]:
    """(怎么打的说法, 攻击力)"""
    cheer = 1 + (afflictions.effect(player, "cheer").value / 100 if afflictions.effect(player, "cheer") else 0) + afflictions.bless(player, "atk_pct")
    if shooter:
        verb = f"从{shooter.name}里抽出一把掷向" if helpers.prop(shooter, "thrown") else f"端起{shooter.name}射向"
        return verb, int((player.attack + shooter.damage) * cheer + 0.5)
    return f"用{equipment.wielding(melee)}攻击", int((player.attack + equipment.weapon_damage(melee)) * cheer + 0.5)


def hit_base(shooter: Optional[ItemInstance], d: int) -> float:
    if shooter is None:
        return MELEE_HIT.get(d, 0)
    return STEADY_HIT if helpers.prop(shooter, "steady") else RANGED_HIT.get(d, RANGED_HIT[max(RANGED_HIT)])


def swap_template(cur: Cursor, item: ItemInstance, template_id: str) -> str:
    """换成另一个模板（弩：装好的 ↔ 空的），升级过的名字跟着换（麦琪的轻弩 +1 ↔ 麦琪的轻弩（空） +1），返回新名字。
    装着的弹药、弹匣剩几发、绞盘摇了几圈这些一换就清掉"""
    cur.execute("select name from item_templates where id = %s", (template_id,))
    base = cur.fetchone()["name"]
    props = {k: v for k, v in item.props.items() if k not in ("loaded_ammo", "shots", "wound")}
    if props.get("plus"):
        props["name"] = f"{base} +{props['plus']}"
    else:
        props.pop("name", None)
    cur.execute("update item_instances set template_id = %s, props = %s where id = %s", (template_id, Jsonb(props), item.id))
    return props.get("name", base)


def spend(cur: Cursor, player: Player, shooter: Optional[ItemInstance]) -> list[str]:
    """射出去了：弹匣（props.magazine，连弩、飞刀）还有剩就少一发；空了换成空的样子，要重新装填。
    箭袋腰带（free_reload）每场白给一次装填：第一次射空时顺手就装好了"""
    if shooter is None or not (empty := helpers.prop(shooter, "unloaded")):
        return []
    if mag := helpers.prop(shooter, "magazine"):
        left = shooter.props.get("shots", mag) - 1
        if left > 0:
            cur.execute("update item_instances set props = props || %s where id = %s", (Jsonb({"shots": left}), shooter.id))
            return [f"{shooter.name}还剩 {left} 发"]
    st = stealth.stealth_state(player)
    if not mag and not st.reloaded and equipment.gear_has(cur, player, "free_reload"):
        st.reloaded = True
        stealth.save_stealth(cur, player, st)
        if shooter.props.get("loaded_ammo"):
            cur.execute("update item_instances set props = props - 'loaded_ammo' where id = %s", (shooter.id,))
        return [f"{player.name}手一垂就从箭袋腰带里摸出一支，顺手又装好了（这一场用过了）"]
    name = swap_template(cur, shooter, empty)
    if helpers.prop(shooter, "thrown"):
        return [f"{name}：都扔出去了，打完这一架（房间里没有敌人了）自然会捡回来"]
    return [f"{name}射空了，要重新装填（说「装填」）才能再射"]


def ammo_fx(cur: Cursor, shooter: Optional[ItemInstance], npc: Optional[Npc]) -> list[dict]:
    """装着的特殊弹药（钩索箭、火油箭、铅弹）这一发带上的效果：只对某类怪（vs）的对不上就不算"""
    if shooter is None or not (ammo := shooter.props.get("loaded_ammo")):
        return []
    cur.execute("select props->'effects' as fx from item_templates where id = %s", (ammo,))
    row = cur.fetchone()
    return [e for e in (row["fx"] if row else None) or []
            if not e.get("vs") or (npc and any(npc.template.props.get(t) for t in e["vs"]))]


def weapon_fit(fired: list[dict], shooter: Optional[ItemInstance]) -> list[dict]:
    """只对远程（weapon: ranged）或只对近战（weapon: melee）的效果（猎手之戒）：这一下对不上就不算"""
    kind = "ranged" if shooter else "melee"
    return [e for e in fired if e.get("weapon") in (None, kind)]


def cover(cur: Cursor, room_id: str, shooter) -> float:
    """有掩体的房间，远程的命中 −RANGED_COVER（对双方都一样）；近战不管"""
    return RANGED_COVER if shooter and environment.room_env(cur, room_id).get("cover") else 0.0


def _smoky(cur: Cursor, room_id: str) -> bool:
    return (environment.room_env(cur, room_id).get("smoke") or 0) > 0


def shot_smoke(cur: Cursor, room_id: str, shooter) -> float:
    """烟雾弹的烟没散：远程命中减半（对双方都一样）"""
    return SMOKE_HIT if shooter and _smoky(cur, room_id) else 1.0


def blind(player: Player) -> float:
    return BLIND_HIT if afflictions.effect(player, "blind") else 1.0


def recover_thrown(cur: Cursor, player: Player) -> list[str]:
    """扔出去的飞刀（空了、props.recover）：房间里没有敌人了就捡回来"""
    thrown = [i for i in loading.load_items(cur, "i.player_id = %s", (player.id,)) if helpers.prop(i, "recover") and helpers.prop(i, "loads")]
    if not thrown or combat.enemies_in(cur, player.room_id):
        return []
    return [f"{player.name}把扔出去的{swap_template(cur, i, helpers.prop(i, 'loads'))}一把把捡了回来" for i in thrown]


def do_reload(cur: Cursor, player: Player, view: RoomView, a: Reload) -> list[str]:
    """装填：空的换回装好的。带了特殊弹药就装上它（下一发带它的效果，弹药用掉一个）；
    要摇好几次的（绞盘重弩 props.reload_steps）每次摇一点；飞刀扔完了要打完这一架才能捡回来"""
    ammo = helpers.inv_item(cur, view, player, a.ammo) if a.ammo else None
    if ammo and not helpers.prop(ammo, "ammo"):
        raise ActionError(f"{ammo.name}不是能装的弹药")
    if a.item:
        item = helpers.inv_item(cur, view, player, a.item)
        if not helpers.prop(item, "loads"):
            if helpers.prop(item, "ranged") and ammo:
                raise ActionError(f"{item.name}已经装好了，射出去才能换装{ammo.name}")
            raise ActionError(f"{item.name}用不着装填" if not helpers.prop(item, "ranged") else f"{item.name}已经装好了")
    else:
        empties = [i for i in view.inventory if helpers.prop(i, "loads")]
        if ammo:
            empties = [i for i in empties if helpers.prop(i, "ammo_kind") == helpers.prop(ammo, "ammo")] or empties
        if not empties:
            if ammo and any(helpers.prop(i, "ammo_kind") == helpers.prop(ammo, "ammo") and helpers.prop(i, "ranged") for i in view.inventory):
                raise ActionError(f"已经装好了，射出去才能换装{ammo.name}")
            raise ActionError(f"{player.name}身上没有要装填的东西")
        item = sorted(empties, key=lambda i: i.equipped_slot is None)[0]     # 拿在手上的先装
    if helpers.prop(item, "recover"):
        raise ActionError(f"{item.name}都扔出去了，打完这一架（房间里没有敌人了）自然会捡回来")
    if ammo and helpers.prop(item, "ammo_kind") != helpers.prop(ammo, "ammo"):
        raise ActionError(f"{ammo.name}装不进{item.name}")
    steps = helpers.prop(item, "reload_steps") or 1
    wound = item.props.get("wound", 0) + 1
    if wound < steps:
        cur.execute("update item_instances set props = props || %s where id = %s", (Jsonb({"wound": wound}), item.id))
        return [f"{player.name}咬着牙摇了几圈绞盘，弦才上到一半（还要再装填 {steps - wound} 次）"]
    name = swap_template(cur, item, helpers.prop(item, "loads"))
    if ammo:
        environment.consume(cur, ammo)
        cur.execute("update item_instances set props = props || %s where id = %s", (Jsonb({"loaded_ammo": ammo.template.id}), item.id))
        return [f"{player.name}把一支{ammo.name}装了上去，{name}又能射了（下一发带上它的效果）"]
    return [f"{player.name}拉开弦、装好了，{name}又能射了"]
