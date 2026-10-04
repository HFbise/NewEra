"""Equipment: passive and triggered item effects, defense, equip / unequip. / 装备：被动和触发效果、防御、穿脱"""
import math
import random
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from psycopg import Cursor
from psycopg.types.json import Jsonb

import dungeon
from rules import NPC_CORRODE_DEF, NPC_EFFECT_TURNS, OFFHAND_SHARE, SCALE, WEAK_MULT, steps
from schema import Effect, Equip, ItemInstance, Npc, Player, RoomView, SLOT_CHOICES, SLOT_NAMES, Status, Unequip

from ..base.core import ActionError
from . import afflictions, bosses, combat, stealth
from ..base import helpers, loading
from ..world import environment, everyday
from ..npc import item_info, smith


# 叠加规则：固定数值加起来（血量上限、技能、逃跑难度、伤害加成、减伤、破甲）；
# 百分比和几率只取最好的一件（金币加成、闪避、吸血、抗性、光亮）
def _effects(item: ItemInstance) -> list[dict]:
    """这件装备的效果：自己的，加上镶在上面的宝石的（props.gem_fx）"""
    return (helpers.prop(item, "effects") or []) + (item.props.get("gem_fx") or [])


def _gem_total(items: list[ItemInstance], do: str) -> float:
    """宝石加的数值合计（防御、血量上限），全身有上限（loot.yaml gems.body_caps），免得件件都镶又叠爆"""
    total = sum(e.get("value", 0) for i in items for e in i.props.get("gem_fx") or []
                if e.get("when") == "passive" and e.get("do") == do)
    return min(total, dungeon.gem_rules()["body_caps"].get(do, total))


def fx(items: list[ItemInstance], do: str, when: str = "passive", kind: Optional[str] = None) -> list[dict]:
    return [e for i in items for e in _effects(i)
            if e.get("when") == when and e.get("do") == do and (kind is None or e.get("kind") == kind)]


def gear_add(cur: Cursor, player: Player, do: str, kind: Optional[str] = None) -> float:
    return sum(e.get("value", 0) for e in fx(helpers.worn(cur, player), do, kind=kind))


def gear_has(cur: Cursor, player: Player, do: str) -> bool:
    return bool(fx(helpers.worn(cur, player), do))


def gear_resist(cur: Cursor, player: Player, kind: str) -> float:
    """状态几率乘多少：0 是免疫，0.5 减半；几件取最好的"""
    return min([1.0] + [e.get("value", 1) for e in fx(helpers.worn(cur, player), "resist", kind=kind)])


def gear_gold(cur: Cursor, player: Player) -> float:
    return max([0.0] + [e.get("value", 0) for e in fx(helpers.worn(cur, player), "gold")])


def _ally_down(cur: Cursor, player: Player) -> bool:
    if not player.party_id:
        return False
    cur.execute("select 1 from players where party_id = %s and room_id = %s and hp <= 0 and id <> %s limit 1",
                (player.party_id, player.room_id, player.id))
    return cur.fetchone() is not None


def fire(cur: Cursor, player: Player, when: str, do: Optional[str] = None, npc: Optional[Npc] = None,
          roll: bool = True) -> list[dict]:
    """这一刻（when）身上装备触发了哪些效果：对得上 vs 标签、满足 if 条件、掷中 chance 的（roll=False 不掷，只看条件）"""
    out = []
    for e in (e for i in helpers.worn(cur, player) for e in _effects(i)
              if e.get("when") == when and (do is None or e.get("do") == do)):
        if (vs := e.get("vs")) and not (npc and any(npc.template.props.get(t) for t in vs)):
            continue
        cond = e.get("if") or {}
        if "hp_below" in cond and not player.hp < player.max_hp * cond["hp_below"]:
            continue
        if "target_hp_below" in cond and not (npc and npc.combatable and npc.hp < npc.template.max_hp * cond["target_hp_below"]):
            continue
        if cond.get("ally_down") and not _ally_down(cur, player):
            continue
        if roll and e.get("chance", 1) < 1 and not helpers.roll(e["chance"]):
            continue
        out.append(e)
    return out


def labels(effects: list[dict]) -> list[str]:
    return [e["label"] for e in effects if e.get("label")]


def aura(cur: Cursor, player: Player, kind: str) -> int:
    """军团徽记这类光环：同房间的队友（和自己）身上最强的一件"""
    cur.execute("""select i.id from item_instances i join players p on p.id = i.player_id
                   where i.equipped_slot is not null and p.room_id = %s
                     and (p.id = %s or (p.party_id is not null and p.party_id = %s))""",
                (player.room_id, player.id, player.party_id))
    ids = [r["id"] for r in cur.fetchall()]
    items = loading.load_items(cur, "i.id = any(%s)", (ids,)) if ids else []
    return max([0] + [int(e.get("value", 0)) for e in fx(items, "aura", kind=kind)])


# 装备打到怪身上的状态：怪也能中毒、流血（每轮敌人行动掉血）、看不清（命中 ×NPC_BLIND_HIT）、腐蚀（防御 -NPC_CORRODE_DEF）；
# 定身 stun、缠住、撞倒用怪原来的状态


def npc_effect(npc: Npc, kind: str) -> Optional[Effect]:
    return next((e for e in npc.effects if e.kind == kind), None)


def _save_npc_effects(cur: Cursor, npc: Npc) -> None:
    cur.execute("update npcs set effects = %s where id = %s", (Jsonb([e.model_dump() for e in npc.effects]), npc.id))


def _npc_affect(cur: Cursor, player: Player, npc: Npc, kind: str, label: str) -> list[str]:
    """给怪上一个状态（装备触发的）"""
    if not npc.alive or npc.hp is None or npc.hp <= 0:
        return []
    if kind in (npc.template.props.get("immune") or []):
        return [f"{npc.name}不吃这一套（免疫{afflictions.EFFECT_NAMES.get(kind, item_info.STATE_NAMES.get(kind, kind))}）"]
    if kind == "wrapped":
        kind = "restrained"
    if kind == "silence":
        helpers.tally_merge(cur, npc, {"muted": 2})
        return [f"{npc.name}{label or '被捂住了嘴'}（接下来两次出手放不了招）"]
    if kind in ("stun", "restrained", "prone"):
        if npc.status or bosses.boss_resists(cur, npc):
            return []
        st = Status(kind="incapacitated" if kind == "stun" else kind, label=(label or "动弹不得")[:20], escape=2,
                    since=datetime.now(timezone.utc).isoformat())
        afflictions.set_status(cur, "npcs", npc.id, st)
        npc.status = st
        return [f"{npc.name}{label}" if label else f"{npc.name}{st.describe()}"]
    depth = npc.template.props.get("dungeon", {}).get("depth", 1)
    value = NPC_CORRODE_DEF if kind == "corrode" else round((1 + steps(depth, 5)) * SCALE)
    npc.effects = [e for e in npc.effects if e.kind != kind] + [
        Effect(kind=kind, value=value, left=NPC_EFFECT_TURNS[kind], label=(label or afflictions.EFFECT_NAMES[kind])[:20], source=player.name)]
    _save_npc_effects(cur, npc)
    return [f"{npc.name}{label or ''}（{afflictions.EFFECT_NAMES[kind]}）"]


def tick_npc_effects(cur: Cursor, player: Player, npcs: list[Npc]) -> list[str]:
    """敌人每轮行动前：身上的中毒、流血掉血，时间到了消退"""
    facts = []
    for npc in npcs:
        if not npc.effects:
            continue
        keep = []
        for e in npc.effects:
            if e.left <= 0:
                facts.append(f"{npc.name}身上的{afflictions.EFFECT_NAMES[e.kind]}消退了")
                continue
            if e.kind in ("poison", "bleed") and npc.hp > 0:
                v = math.ceil(e.value * WEAK_MULT) if npc.template.props.get("weak") == e.kind else e.value     # 典狱长怕毒
                hurt, dead = combat.hurt_npc(cur, player, npc, v)
                npc.hp = max(0, npc.hp - v)
                facts += [f"{npc.name}{afflictions.EFFECT_NAMES[e.kind]}，掉了 {v} 点血"] + hurt
                if dead:
                    npc.alive = False
                    break
            e.left -= 1
            keep.append(e)
        if npc.alive:
            npc.effects = keep
            _save_npc_effects(cur, npc)
    return facts


def assassinated(cur: Cursor, player: Player, view: RoomView, npc: Npc, dead: bool, unseen: bool) -> list[str]:
    """敌人还没发现你就被解决了（暗杀）：隐匿熟练 +1，一条消息最多一次"""
    if not (dead and unseen and npc.template.hostile) or "stealth" in view.trained:
        return []
    view.trained.append("stealth")
    return [f"{npc.name}到死都没发现{player.name}"] + helpers.gain_skill(cur, player, "stealth")


def pvp_hit_extras(cur: Cursor, player: Player, target: Player, dmg: int, down: bool, fired: list[dict]) -> list[str]:
    """决斗里打中对手以后装备触发的：上状态（对手的装备抗性照算）、吸血；打倒了就是杀敌效果。
    打群体、连锁这种对怪的效果决斗里没有"""
    facts = []
    if down:
        for e in fire(cur, player, "kill"):
            if e["do"] == "heal":
                facts += labels([e]) + heal_player(cur, player, int(e.get("value", 1)))
        return facts
    depth = dungeon.parse_room(player.room_id)[1] if dungeon.is_dungeon(player.room_id) else 1
    for e in fired:
        if e["do"] == "status" and (resist := gear_resist(cur, target, e["kind"])) > 0 and (resist >= 1 or helpers.roll(resist)):
            facts += afflictions.inflict(cur, target, e["kind"], e.get("label", ""), depth, player.name)
    if leech := max([0] + [e.get("value", 0) for e in fired if e["do"] == "leech"]):
        facts += heal_player(cur, player, math.ceil(dmg * leech))
    return facts


def pvp_self_damage(cur: Cursor, player: Player, swung: list[dict]) -> list[str]:
    """决斗里挥出去就触发的：握柄的刃咬手（打怪时的横扫到别的敌人，决斗里没有）"""
    facts = []
    for e in swung:
        if e["do"] == "self_damage":
            hurt, _ = combat.hurt_player(cur, player, int(e.get("value", 1)), "other", "手里的凶器")
            facts += labels([e]) + [f"{player.name}自己掉了 {e.get('value', 1)} 点血"] + hurt
    return facts


def attack_extras(cur: Cursor, player: Player, npc: Npc, fired: list[dict]) -> list[str]:
    """普通攻击出手时（打没打中都算）触发的：甩开铁链扫到别的敌人、握柄的刃咬手"""
    facts = []
    for e in fired:
        if e["do"] == "splash":
            for other in [n for n in combat.enemies_in(cur, player.room_id) if n.id != npc.id]:
                v = int(e.get("value", 1)) * (2 if other.template.props.get("swarm") else 1)     # 虫群怕横扫
                hurt, _ = combat.hurt_npc(cur, player, other, v)
                facts += [f"{other.name}被扫到，受到 {v} 点伤害"] + hurt
            facts += environment.make_noise(cur, player, "aoe")
        elif e["do"] == "self_damage":
            hurt, _ = combat.hurt_player(cur, player, int(e.get("value", 1)), "other", "手里的凶器")
            facts += [f"{player.name}自己掉了 {e.get('value', 1)} 点血"] + hurt
    return labels(fired) + facts


def hit_extras(cur: Cursor, player: Player, npc: Npc, dmg: int, dead: bool, fired: list[dict]) -> list[str]:
    """普通攻击打中以后触发的：上状态、全体上状态、连锁、吸血；杀死了就是杀敌效果"""
    facts = []
    if dead:
        for e in fire(cur, player, "kill", npc=npc):
            if e["do"] == "heal":
                facts += labels([e]) + heal_player(cur, player, int(e.get("value", 1)))
        return facts
    for e in fired:
        if e["do"] == "status":
            facts += _npc_affect(cur, player, npc, e["kind"], e.get("label", ""))
        elif e["do"] == "status_all":
            facts += labels([e])
            for other in combat.enemies_in(cur, player.room_id):
                facts += _npc_affect(cur, player, other, e["kind"], "")
        elif e["do"] == "knockback":
            st = stealth.stealth_state(player)
            d = stealth.set_distance(st, npc, stealth.distance(st, npc) + 1)
            stealth.save_stealth(cur, player, st)
            facts += [f"{npc.name}被打得往后退了一步（离{player.name} {stealth.distance_word(d)}）"]
        elif e["do"] == "chain":
            others = [n for n in combat.enemies_in(cur, player.room_id) if n.id != npc.id]
            if others:
                other = random.choice(others)
                hurt, _ = combat.hurt_npc(cur, player, other, int(e.get("value", 1)))
                facts += labels([e]) + [f"{other.name}受到 {e.get('value', 1)} 点伤害"] + hurt
    if leech := max([0] + [e.get("value", 0) for e in fired if e["do"] == "leech"]):      # 吸血取最好的一件
        facts += heal_player(cur, player, math.ceil(dmg * leech))
    return facts


def heal_scale(player: Player, amount: int) -> int:
    """重伤（wound）：受到的治疗 × value%（药草、绷带解不了）"""
    w = next((e for e in player.effects if e.kind == "wound"), None)
    return round(amount * w.value / 100) if w else amount


def heal_player(cur: Cursor, player: Player, amount: int) -> list[str]:
    amount = heal_scale(player, amount)
    hp = min(player.max_hp, player.hp + amount)
    if hp == player.hp:
        return []
    cur.execute("update players set hp = %s where id = %s", (hp, player.id))
    gained, player.hp = hp - player.hp, hp
    return [f"{player.name}回了 {gained} 点血，当前 HP {hp}/{player.max_hp}"]


def sync_gear_hp(cur: Cursor, player_id: UUID) -> list[str]:
    """装备带的血量上限（活力之戒 +3、贪婪之戒 -3）：跟 players.gear_hp 比，差多少就改多少，当前血量不超过上限"""
    player = loading.load_player(cur, player_id, lock=True)
    worn = helpers.worn(cur, player)
    bonus = round(sum(e.get("value", 0) for e in fx(worn, "max_hp") if not e.get("gem")) + _gem_total(worn, "max_hp"))
    cur.execute("select gear_hp from players where id = %s", (player_id,))
    had = cur.fetchone()["gear_hp"]
    if bonus == had:
        return []
    cur.execute("""update players set max_hp = greatest(1, max_hp + %s), gear_hp = %s,
                   hp = least(hp, greatest(1, max_hp + %s)) where id = %s returning max_hp""",
                (bonus - had, bonus, bonus - had, player_id))
    return [f"{player.name}的血量上限{'+' if bonus > had else ''}{bonus - had}（身上的装备），现在上限 {cur.fetchone()['max_hp']}"]


def total_defense(cur: Cursor, player: Player) -> int:
    """基础防御加上所有装备的防御（护甲、护符、以后的盾）"""
    corrode = afflictions.effect(player, "corrode")
    worn = helpers.worn(cur, player)
    return max(0, player.defense + sum(i.defense for i in worn) + _gem_total(worn, "defense") + aura(cur, player, "defense")
               - (corrode.value if corrode else 0))


def gear_totals(attack: int, defense: int, equipped: list[ItemInstance]) -> tuple[int, int]:
    """算上装备的攻、防（侧栏、后台看的）：双持时副手武器按 OFFHAND_SHARE，所有装备的防御相加"""
    weapons = [i for i in equipped if i.equipped_slot in ("left_hand", "right_hand") and i.template.type == "weapon"
               and not helpers.prop(i, "ranged") and not helpers.prop(i, "loads")]            # 远程武器射的时候单算
    worn = [i for i in equipped if i.equipped_slot]
    return attack + weapon_damage(weapons), defense + sum(i.defense for i in worn) + _gem_total(worn, "defense")


def weapon_damage(weapons: list[ItemInstance]) -> int:
    """手上武器加的伤害：主手（右手，右手空着就是那唯一一把）全额，副手按 OFFHAND_SHARE"""
    main = next((w for w in weapons if w.equipped_slot == "right_hand"), weapons[0] if weapons else None)
    return sum(w.damage if w is main else int(w.damage * OFFHAND_SHARE) for w in weapons)


def wielding(weapons: list[ItemInstance]) -> str:
    return "和".join(i.name for i in weapons) or "拳头"


def do_equip(cur: Cursor, player: Player, view: RoomView, a: Equip) -> list[str]:
    """装上：武器左右手都行（双持），戒指两个位都行。玩家说了哪只手就放哪只，没说就放空着的，都满了换掉第一个"""
    item = helpers.inv_item(cur, view, player, a.item)
    kind = item.template.slot
    if not kind:
        raise ActionError(f"{item.name}不能装备")
    choices = SLOT_CHOICES.get(kind, [kind])
    worn = {i.equipped_slot: i for i in helpers.worn(cur, player)}
    if a.slot in choices:
        slot = a.slot
    elif item.equipped_slot:
        raise ActionError(f"{item.name}已经装备在{SLOT_NAMES[item.equipped_slot]}了")
    else:
        # 两只手都占着时，火把默认换掉左手（副手），别把主手的武器换下来
        # 盾默认拿左手（副手），武器默认右手
        prefer = ["left_hand", "right_hand"] if kind == "hand" and item.template.type == "armor" else choices
        slot = next((c for c in prefer if c not in worn), "left_hand" if helpers.prop(item, "lights") else prefer[0])
    if item.equipped_slot == slot:
        raise ActionError(f"{item.name}已经装备在{SLOT_NAMES[slot]}了")
    facts = []
    if kind == "hand":
        # 双手武器（props.two_handed）拿在右手，左手得空出来；拿着双手武器时再往手上拿别的，先把它放下
        if helpers.prop(item, "two_handed"):
            slot = "right_hand"
            other = worn.get("left_hand")
            if other and other.id != item.id:
                smith.no_curse(other, "放")
                if everyday.burning(other):
                    raise ActionError(f"{item.name}要两只手拿，左手的{other.name}点着，收起来就灭了；先把火把熄掉再换")
                cur.execute("update item_instances set equipped_slot = null where id = %s", (other.id,))
                facts.append(f"{player.name}放下了{other.name}，腾出两只手")
                worn.pop("left_hand")
        elif (big := next((i for i in worn.values() if helpers.prop(i, "two_handed") and i.id != item.id), None)):
            smith.no_curse(big, "放")
            cur.execute("update item_instances set equipped_slot = null where id = %s", (big.id,))
            facts.append(f"{player.name}放下了要两只手拿的{big.name}")
            worn = {s: i for s, i in worn.items() if i.id != big.id}
    old = worn.get(slot)
    if old and not item.equipped_slot:
        smith.no_curse(old, "换")
    if old and everyday.burning(old) and not item.equipped_slot:
        raise ActionError(f"{SLOT_NAMES[slot]}拿着点着的{old.name}，收起来就灭了；要换就先把火把熄掉（卸下火把），或者说换到另一只手")
    prev = item.equipped_slot
    if prev:
        # 已经装着的换个位置（右手的斧子换到左手）：先腾出来，免得撞上"每格一件"的唯一索引
        cur.execute("update item_instances set equipped_slot = null where id = %s", (item.id,))
    if old and prev:
        # 两只手互换：原来那格的东西挪到空出来的这格
        cur.execute("update item_instances set equipped_slot = %s where id = %s", (prev, old.id))
        facts.append(f"{player.name}把{old.name}换到了{SLOT_NAMES[prev]}")
    elif old:
        cur.execute("update item_instances set equipped_slot = null where id = %s", (old.id,))
        facts.append(f"{player.name}卸下了{old.name}")
    cur.execute("update item_instances set equipped_slot = %s where id = %s", (slot, item.id))
    if helpers.prop(item, "cursed"):
        facts.append(f"{item.name}一上身就像长在了{player.name}身上，怎么也甩不掉：它被诅咒了（找杂货铺的诺艾尔解咒）")
    if lit := helpers.prop(item, "lights"):
        cur.execute("update item_instances set template_id = %s, props = '{}' where id = %s", (lit, item.id))
        return facts + [f"{player.name}把{item.name}拿在{SLOT_NAMES[slot]}点着了，火光一下子亮起来"]
    return facts + [f"{player.name}把{item.name}装备在{SLOT_NAMES[slot]}"]


def do_unequip(cur: Cursor, player: Player, view: RoomView, a: Unequip) -> list[str]:
    item = helpers.inv_item(cur, view, player, a.item)
    if not item.equipped_slot:
        # "卸下火把"：背包里没点的火把名字一模一样，要卸的其实是手上点着的那支
        item = next((i for i in helpers.worn(cur, player) if i.name.startswith(item.name)), item)
    if not item.equipped_slot:
        raise ActionError(f"{item.name}没有装备着")
    smith.no_curse(item)
    if everyday.burning(item):
        return everyday.douse(cur, player, item)
    cur.execute("update item_instances set equipped_slot = null where id = %s", (item.id,))
    return [f"{player.name}卸下了{SLOT_NAMES[item.equipped_slot]}的{item.name}"]


# 送了会讲往事的礼物（props.lore）：谁讲、讲什么
LORE = {"upon_mountain": ("shopkeeper", "“在其山岳之上者”"), "maggie_past": ("innkeeper", "她自己年轻时")}


LORE_MARK = "LORE::"                    # 这一段还没写过：server 生成、存下来再换成正文（server._fill_lore）


def once_per_fight(cur: Cursor, player: Player, key: str) -> bool:
    """这一场（这个房间里的这一仗）还没用过 key 就记下并返回 True"""
    f = player.flags.get("_fight") or {}
    used = f.get("used", []) if f.get("room") == player.room_id else []
    if key in used:
        return False
    f = {"room": player.room_id, "used": used + [key]}
    player.flags["_fight"] = f
    cur.execute("update players set flags = flags || jsonb_build_object('_fight', %s::jsonb) where id = %s", (Jsonb(f), player.id))
    return True


def cheat_death_ready(cur: Cursor, player: Player) -> bool:
    """不熄的羽毛：每层地牢一次（记在 players.flags 的 cheat_death 里）"""
    return once_per_floor(cur, player, "cheat_death")


def once_per_floor(cur: Cursor, player: Player, key: str) -> bool:
    """每层地牢一次的东西（不熄的羽毛、无底酒壶、守护书签、诺艾尔的古书）：这一层用过了就是 False，没用过记下来返回 True"""
    here = ":".join(map(str, dungeon.parse_room(player.room_id))) if dungeon.is_dungeon(player.room_id) else "surface"
    if player.flags.get(key) == here:
        return False
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::text) where id = %s", (key, here, player.id))
    player.flags[key] = here
    return True


def carries(cur: Cursor, player: Player, prop: str) -> Optional[ItemInstance]:
    """身上（背包里、装备着都算）带着有这个特性的东西"""
    cur.execute("""select i.id from item_instances i join item_templates t on t.id = i.template_id
                   where i.player_id = %s and (t.props ? %s or i.props ? %s) limit 1""", (player.id, prop, prop))
    row = cur.fetchone()
    return loading.load_items(cur, "i.id = %s", (row["id"],))[0] if row else None
