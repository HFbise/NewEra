"""Upgrades, gems, refining, transfers, donations, dismantling, curses. / 升级、宝石、捐赠、拆解、诅咒"""
import random
import re
from typing import Optional

from psycopg import Cursor
from psycopg.types.json import Jsonb

import dungeon
from rules import (
    GEM_SLOT_WORDS, GEM_TIER_WORDS, GEM_TOP, SCALE, UPGRADE_DISCOUNT, UPGRADE_MAX, UPGRADE_MIN_COST, UPGRADE_PITY,
    UPGRADE_PITY_ARMOR, UPGRADE_SCRAP, UPGRADE_SCRAP_MAX, base_price, gem_category, gem_effects, gem_fits,
    refine_cap, refine_roll, stat_text, upgrade_chance, upgrade_cost, upgrade_step,
)
from schema import (
    Dismantle, Dispenser, Donate, ItemInstance, Npc, Player, Refine, Rename, Reroll, RoomView, Socket, Transfer,
    Uncurse, Unsocket, Upgrade,
)

from .core import ActionError
from . import environment, equipment, helpers, item_info, npc_social, npc_trade


# 铁匠升级武器：每级伤害 +1，名字后面标 +N。升到第 N 级有 N × UPGRADE_BREAK_STEP 的几率失败（最多 UPGRADE_BREAK_MAX），
# 失败不会碎，而是退一级（+3 升 +4 失败变 +2，+0 失败还是 +0），钱照收。
# 等级不封顶。费用是升级后和升级前建议价的差（至少 UPGRADE_MIN_COST），跟着伤害指数涨。
# 失败不掉级、钱照收；同一级连续失败有保底（rules.upgrade_chance）。
# 奥利哈刚（UPGRADE_ORE）：这一次成功率翻倍，用掉；碎铁（SCRAP，莉娜拆装备得来）每份 +5%，一次最多四份。
# 莉娜的淬火油（UPGRADE_OIL，好感 40 的回礼）：这一次必定成功，不收钱。等级上限 UPGRADE_MAX
UPGRADE_ORE = "ore"


UPGRADE_DEEP_MAX, DEEP_FORGE = 15, "lina_deep_forge"     # 深层锻造（拓片解锁）：深层装备（props.tier）升级上限


def upgrade_cap(player: Player, item: ItemInstance) -> int:
    """这件能升到几级：一般 +10；深层装备、而且莉娜学会了深层锻造（players.flags.lina_deep_forge）是 +15"""
    return UPGRADE_DEEP_MAX if helpers.prop(item, "tier") and player.flags.get(DEEP_FORGE) else UPGRADE_MAX


SCRAP = "scrap_iron"


UPGRADE_OIL = "lina_oil"


STAT_WORDS = {"damage": "伤害", "defense": "防御"}


def upgrade_stat(item: ItemInstance) -> Optional[str]:
    """升级加的是哪项：武器加伤害，有防御的防具（盔甲、盾、帽子……）加防御，别的升不了"""
    if item.template.type == "weapon":
        return "damage"
    if item.template.type == "armor" and item.defense > 0:
        return "defense"
    return None


def upgradable(items: list[ItemInstance]) -> list[ItemInstance]:
    return [i for i in items if upgrade_stat(i)]


def step(item: ItemInstance, stat: str) -> float:
    """这件升一级加多少（远程武器伤害 +15）"""
    return upgrade_step(stat, bool(helpers.prop(item, "ranged") or helpers.prop(item, "loads")))


def upgrade_terms(item: ItemInstance) -> tuple[int, int, float]:
    """(升到几级, 费用, 这次什么都不垫的失败几率，算上保底)：费用只看升到第几级（rules.upgrade_cost），稀有的东西乘 props.upgrade_mult"""
    level = item.props.get("plus", 0) + 1
    cost, _ = upgrade_cost(level, float(helpers.prop(item, "upgrade_mult") or 1))
    return level, cost, 1 - upgrade_chance(level, _upgrade_pity(item, level), armor=upgrade_stat(item) == "defense")


def _upgrade_pity(item: ItemInstance, level: int) -> int:
    """这件在这一级已经连续失败了几次（props.upgrade_fails = {"level", "n"}）"""
    f = item.props.get("upgrade_fails") or {}
    return f.get("n", 0) if f.get("level") == level else 0


def _upgrade_text(item: ItemInstance, price: Optional[int] = None, chance: Optional[float] = None) -> str:
    level, cost, risk = upgrade_terms(item)
    cost = price if price is not None else cost
    chance = 1 - risk if chance is None else chance
    stat = upgrade_stat(item)
    now = getattr(item, stat)
    pity = _upgrade_pity(item, level)
    return (f"升到 +{level}（{STAT_WORDS[stat]} {stat_text(now)} → {stat_text(now + step(item, stat))}）要 {cost} 金币，"
            f"成功率 {round(chance * 100)}%，失败不掉级、钱照收" + (f"（这一级失败过 {pity} 次，火候摸清楚了些）" if pity else ""))


def do_upgrade(cur: Cursor, player: Player, view: RoomView, a: Upgrade) -> list[str]:
    npc = helpers.room_npc(cur, view, player, a.target)
    if not npc.template.props.get("upgrades"):
        raise ActionError(f"{npc.name}不会升级装备")
    offers = npc_trade.offers_for(cur, player.id, npc)
    gear = upgradable(view.inventory)
    if a.quote:
        items = [helpers.inv_item(cur, view, player, a.item)] if a.item else gear
        if not items or not all(upgrade_stat(i) for i in items):
            raise ActionError(f"{player.name}身上没有能升级的武器、防具")
        disc = UPGRADE_DISCOUNT if "discount" in npc_social.perks(cur, player.id, npc.template.id) else 1.0
        return [f"{npc.name}看了看{player.name}的{i.name}：" + (f"已经 +{upgrade_cap(player, i)}，锻到头了" if i.props.get("plus", 0) >= upgrade_cap(player, i)
                                                             else _upgrade_text(i, max(1, round(upgrade_terms(i)[1] * disc)))
                                                             + ("（熟客价九折）" if disc < 1 else "")) for i in items]
    if a.item:
        item = helpers.inv_item(cur, view, player, a.item)
        if not upgrade_stat(item):
            raise ActionError(f"{item.name}不是武器，也不是带防御的防具，{npc.name}没法升级")
    else:
        # 没说哪件：刚问过要不要用矿石的那件（回"用""不用"），或者身上只有一件；不止一件就列出来问
        asked = next((i for i in gear if f"upgrade:{i.id}" in offers), None)
        if asked is None and a.ore is False:
            raise ActionError(f"{npc.name}没在问{player.name}用不用矿石")
        if asked is None and len(gear) != 1:
            if not gear:
                raise ActionError(f"{player.name}身上没有能升级的武器、防具")
            disc = UPGRADE_DISCOUNT if "discount" in npc_social.perks(cur, player.id, npc.template.id) else 1.0
            return ([f"{npc.name}问{player.name}要升级哪一件："]
                    + [f"{i.name}：{_upgrade_text(i, max(1, round(upgrade_terms(i)[1] * disc)))}" for i in gear]
                    + [f"{player.name}说「升级」加名字，{npc.name}就动手"])
        item = asked or gear[0]
    stat = upgrade_stat(item)
    now = getattr(item, stat)
    level, cost, risk = upgrade_terms(item)
    if level > upgrade_cap(player, item):
        raise ActionError(f"{item.name}已经 +{upgrade_cap(player, item)} 了，{npc.name}说再锻就要废了"
                          + ("（深层装备把矮人锻造纹拓片送给她，她能锻到 +15）" if helpers.prop(item, "tier") and not player.flags.get(DEEP_FORGE) else ""))
    if "discount" in npc_social.perks(cur, player.id, npc.template.id):
        cost = max(1, round(cost * UPGRADE_DISCOUNT))
    key = f"upgrade:{item.id}"
    ore = next((i for i in view.inventory if i.template.id == UPGRADE_ORE), None)
    oil = next((i for i in view.inventory if i.template.id == UPGRADE_OIL), None)
    scrap = next((i for i in view.inventory if i.template.id == SCRAP), None)
    if a.ore and ore is None:
        raise ActionError(f"{player.name}身上没有奥利哈刚矿石")
    if a.scrap and scrap is None:
        raise ActionError(f"{player.name}身上没有碎铁（找{npc.name}拆解用不上的地牢装备得来）")
    use_scrap = min(a.scrap, UPGRADE_SCRAP_MAX, scrap.quantity if scrap else 0)
    pity = _upgrade_pity(item, level)
    chance = upgrade_chance(level, pity, bool(a.ore), use_scrap, armor=stat == "defense")
    terms = _upgrade_text(item, cost, chance)
    if a.oil:
        # 莉娜的淬火油：必定成功，不收钱
        if oil is None:
            raise ActionError(f"{player.name}身上没有莉娜的淬火油")
        environment.consume(cur, oil)
        name = re.sub(r" \+\d+$", "", item.name) + f" +{level}"
        cur.execute("update item_instances set props = props || %s where id = %s",
                    (Jsonb({"plus": level, stat: now + step(item, stat), "name": name}), item.id))
        return [f"{player.name}递上莉娜的淬火油，{npc.name}把{item.name}烧红了往油里一浸，滋的一声冒起白烟",
                f"升级成功：{item.name}变成了{name}，{STAT_WORDS[stat]} {stat_text(now + step(item, stat))}（淬火油用掉了，没收钱）"]
    if cost > player.gold:
        raise ActionError(f"{npc.name}看了看{player.name}的{item.name}：{terms}。{player.name}身上只有 {player.gold} 金币，不够")
    if a.ore is None and not a.scrap and (ore is not None or scrap is not None) and chance < 1 and key not in offers:
        # 兜里有矿石、碎铁又没说用不用：先问一句，回"用""不用"（或者"垫两份碎铁"）才动手
        npc_trade.put_offer(cur, player.id, npc, key, cost)
        return [f"{npc.name}看了看{player.name}的{item.name}：{terms}",
                f"{npc.name}瞥见{player.name}带着"
                + "、".join(([f"奥利哈刚矿石（锻进去这次成功率翻倍到 {round(upgrade_chance(level, pity, True, armor=stat == "defense") * 100)}%，矿石用掉）"] if ore else [])
                            + ([f"{scrap.quantity} 份碎铁（每份 +{round(UPGRADE_SCRAP * 100)}%，一次最多垫 {UPGRADE_SCRAP_MAX} 份）"] if scrap else []))
                + (f"；还有莉娜的淬火油，用了必定成功、不收钱（说「用淬火油」）" if oil else ""),
                f"{player.name}说「用矿石」「垫两份碎铁」或者「不用」，{npc.name}就动手"]
    patron = npc_trade.pay(cur, player, cost, npc)
    cur.execute("update player_npc_relations set offers = offers - %s where player_id = %s and npc_template = %s",
                (key, player.id, npc.template.id))
    facts = [f"{player.name}付了 {cost} 金币，{npc.name}把{item.name}放进炉火里重新锻打（{terms}）"] + patron
    base = re.sub(r" \+\d+$", "", item.name)
    if a.ore:
        environment.consume(cur, ore)
        facts.append(f"{player.name}递上的奥利哈刚矿石化进了铁水里")
    if use_scrap:
        environment.consume_n(cur, scrap, use_scrap)
        facts.append(f"{npc.name}往炉子里添了 {use_scrap} 份碎铁")
    if not helpers.roll(chance):
        # 失败不掉级，钱照收；这一级连续失败的次数记在装备上，下一次成功率 +UPGRADE_PITY
        cur.execute("update item_instances set props = props || %s where id = %s",
                    (Jsonb({"upgrade_fails": {"level": level, "n": pity + 1}}), item.id))
        return facts + [f"淬火的时候火候没掌握好，{item.name}没升上去，好在也没伤着（钱照收）",
                        f"{npc.name}盯着炉火琢磨了一会儿：这把的火候摸清楚一点了（下次成功率 +{round((UPGRADE_PITY_ARMOR if stat == "defense" else UPGRADE_PITY) * 100)}%）"]
    name = base + f" +{level}"
    cur.execute("update item_instances set props = (props - 'upgrade_fails') || %s where id = %s",
                (Jsonb({"plus": level, stat: now + step(item, stat), "name": name}), item.id))
    return facts + [f"升级成功：{item.name}变成了{name}，{STAT_WORDS[stat]} {stat_text(now + step(item, stat))}"]


TRANSFER_SHARE = 0.5                    # 传承锻造：按"把接过去的那件从现在升到那么多级"的升级费的一半收


REROLL_BASE = 20                        # 刷新词条：参考价的一半（至少 20）×（这件刷过几次 + 1）


BAD_EFFECTS = {"self_damage", "cheat_death"}      # 刷词条、专属武器不会抽到的：咬手的、太稀有的


def _affix_pool(cur: Cursor, kind: str) -> list[dict]:
    """某一类装备（weapon / armor）能刷出来的词条：所有这类装备身上的特效（不含诅咒装备、坏效果、负数）"""
    cur.execute("""select props->'effects' as effects from item_templates
                   where type = %s and props ? 'effects' and not coalesce((props->>'cursed')::boolean, false)""", (kind,))
    seen, pool = set(), []
    for r in cur.fetchall():
        for e in r["effects"] or []:
            key = (e.get("when"), e.get("do"), e.get("kind"), tuple(e.get("vs") or []))
            if e.get("do") in BAD_EFFECTS or (isinstance(e.get("value"), (int, float)) and e["value"] < 0) or key in seen:
                continue
            seen.add(key)
            pool.append(e)
    return pool


def _lina(cur: Cursor, view: RoomView, player: Player, ref: str, perk: str, what: str) -> Npc:
    npc = helpers.room_npc(cur, view, player, ref)
    if not npc.template.props.get("upgrades"):
        raise ActionError(f"{npc.name}不会{what}")
    if perk not in npc_social.perks(cur, player.id, npc.template.id):
        raise ActionError(f"{npc.name}还没教过{player.name}这个（{what}要跟她交情更深才行）")
    return npc


def do_transfer(cur: Cursor, player: Player, view: RoomView, a: Transfer) -> list[str]:
    """传承锻造：A 的强化等级转到 B 上（同一类），A 变回 +0。按 B 从现在升到那一级的升级费的一半收钱"""
    npc = _lina(cur, view, player, a.target, "transfer", "传承锻造")
    src, dst = helpers.inv_item(cur, view, player, a.item), helpers.inv_item(cur, view, player, a.to)
    stat = upgrade_stat(src)
    ranged = lambda i: bool(helpers.prop(i, "ranged") or helpers.prop(i, "loads"))
    if not stat or upgrade_stat(dst) != stat or ranged(src) != ranged(dst):
        raise ActionError("传承只能在同一类装备之间：近战武器给近战武器、远程给远程、带防御的防具给防具")
    have, now = src.props.get("plus", 0), dst.props.get("plus", 0)
    if have <= now:
        raise ActionError(f"{src.name}的强化（+{have}）不比{dst.name}（+{now}）高，没什么可传的")
    price, probe = 0, dst.model_copy(deep=True)
    for lv in range(now + 1, have + 1):            # 按接过去那件一级一级升上去的费用算
        price += upgrade_terms(probe)[1]
        probe.props = {**probe.props, "plus": lv, stat: getattr(probe, stat) + step(probe, stat)}
    price = max(UPGRADE_MIN_COST, round(price * TRANSFER_SHARE))
    patron = npc_trade.pay(cur, player, price, npc)
    gain = have - now
    base_src, base_dst = re.sub(r" \+\d+$", "", src.name), re.sub(r" \+\d+$", "", dst.name)
    cur.execute("update item_instances set props = props || %s where id = %s",
                (Jsonb({"plus": 0, stat: getattr(src, stat) - have * step(src, stat), "name": base_src}), src.id))
    cur.execute("update item_instances set props = props || %s where id = %s",
                (Jsonb({"plus": have, stat: getattr(dst, stat) + gain * step(dst, stat), "name": f"{base_dst} +{have}"}), dst.id))
    return [f"{player.name}付了 {price} 金币，{npc.name}把{src.name}和{dst.name}一起放进炉火，锤了一整个下午",
            f"{src.name}上的锻纹褪了下去，变回了{base_src}；{base_dst}接过了这份锻打，变成了{base_dst} +{have}，"
            f"{STAT_WORDS[stat]} {stat_text(getattr(dst, stat) + gain * step(dst, stat))}"] + patron


def do_reroll(cur: Cursor, player: Player, view: RoomView, a: Reroll) -> list[str]:
    """刷新词条：带特效的装备重新锻出别的特效（条数不变），同一件越刷越贵"""
    npc = _lina(cur, view, player, a.target, "reroll", "刷新词条")
    item = helpers.inv_item(cur, view, player, a.item)
    old = helpers.prop(item, "effects") or []
    kind = item.template.type
    if item.template.props.get("cursed"):
        raise ActionError(f"{npc.name}摇摇头：{item.name}上的诅咒长在铁里，重淬也淬不掉，只会把诅咒一起锻进去")
    if item.template.id in dungeon.boss_items():
        raise ActionError(f"{npc.name}摸着{item.name}看了半天：这是头目身上的东西，那股劲是它自己的，重淬会毁了它")
    if not old or kind not in ("weapon", "armor"):
        raise ActionError(f"{item.name}身上没有特效，没什么可刷的（只有带特效的武器、防具能刷）")
    times = item.props.get("rerolls", 0)
    price = max(REROLL_BASE, base_price(npc_trade.item_stats(item)) // 2) * (times + 1)
    pool = [e for e in _affix_pool(cur, kind) if e not in old]
    if len(pool) < len(old):
        raise ActionError(f"{npc.name}想不出还能给{item.name}锻出什么别的特效了")
    patron = npc_trade.pay(cur, player, price, npc)
    new = random.sample(pool, len(old))
    cur.execute("update item_instances set props = props || %s where id = %s",
                (Jsonb({"effects": new, "rerolls": times + 1}), item.id))
    return ([f"{player.name}付了 {price} 金币（这件第 {times + 1} 次刷），{npc.name}说了声「重淬」，把{item.name}烧红了重新锻打，"
             f"原来的特效随着火星散掉了"] + patron
            + [f"{item.name}新的特效：{'；'.join(item_info.effect_line(e) for e in new)}"])


def exclusive_blade(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """莉娜的回礼（好感 100）：一把专属武器。伤害按他走到过的最深层数（(5 + 层数/3，最多 12) × 10），带一个武器词条"""
    cur.execute("select deepest_floor from players where id = %s", (player.id,))
    deep = cur.fetchone()["deepest_floor"] or 0
    dmg = min(12, 5 + deep // 3) * SCALE
    affix = random.choice(_affix_pool(cur, "weapon") or [{}])
    props = {"damage": dmg, "effects": [affix] if affix else []}
    cur.execute("insert into item_instances (template_id, player_id, props) values ('lina_blade', %s, %s)",
                (player.id, Jsonb(props)))
    return [f"无铭：伤害 {dmg}（跟着走过的最深层数一起长，最多 {12 * SCALE}）" + (f"，{item_info.effect_line(affix)}" if affix else ""),
            f"{npc.name}只说了一句：名字，你起"]


def do_rename(cur: Cursor, player: Player, view: RoomView, a: Rename) -> list[str]:
    """给专属武器起名（莉娜打的那把）"""
    item = helpers.inv_item(cur, view, player, a.item)
    if not helpers.prop(item, "exclusive"):
        raise ActionError(f"{item.name}不是你的专属武器，名字改不了")
    name = a.name.strip().strip("「」“”\"'")
    if not (1 <= len(name) <= 10) or re.search(r"[<>]", name):
        raise ActionError("名字 1 到 10 个字")
    plus = item.props.get("plus", 0)
    full = name + (f" +{plus}" if plus else "")
    cur.execute("update item_instances set props = props || %s where id = %s", (Jsonb({"name": full}), item.id))
    return [f"{player.name}给{item.name}起了名字：{full}"]


def _regem(cur: Cursor, item: ItemInstance, gems: list[dict]) -> None:
    """重算装备上宝石的效果（按品质、装备类型落成 effects 存 props.gem_fx，萤石的光存 props.gem_light）"""
    cat = gem_category(item.template.type, item.template.slot)
    cur.execute("select id, props from item_templates where id = any(%s)", ([g["id"] for g in gems],))
    tpl = {r["id"]: r["props"] for r in cur.fetchall()}
    fx = [e for g in gems for e in gem_effects(tpl[g["id"]], g["tier"], cat)]
    light = max([int(e["value"]) for e in fx if e.get("do") == "light"] or [0])
    cur.execute("update item_instances set props = (props - 'gem_light') || %s where id = %s",
                (Jsonb({"gems": gems, "gem_fx": fx} | ({"gem_light": light} if light else {})), item.id))


def _smith(cur: Cursor, view: RoomView, player: Player, ref: str, what: str) -> Npc:
    npc = helpers.room_npc(cur, view, player, ref)
    if not npc.template.props.get("upgrades"):
        raise ActionError(f"{npc.name}不会{what}，找铁匠莉娜")
    return npc


def do_socket(cur: Cursor, player: Player, view: RoomView, a: Socket) -> list[str]:
    """镶宝石：装备上有空孔、宝石对得上这类装备（武器、护具、饰品），免费"""
    npc = _smith(cur, view, player, a.target, "镶宝石")
    item, gem = helpers.inv_item(cur, view, player, a.item), helpers.inv_item(cur, view, player, a.gem)
    if gem.template.type != "gem":
        raise ActionError(f"{gem.name}不是宝石")
    cat = gem_category(item.template.type, item.template.slot)
    holes, gems = item.props.get("sockets", 0), list(item.props.get("gems") or [])
    if not holes:
        raise ActionError(f"{item.name}上没有孔，镶不了（只有地牢里掉的装备才带孔）")
    if len(gems) >= holes:
        raise ActionError(f"{item.name}的 {holes} 个孔都镶满了，先取下来一颗（说「把{item.name}上的{gems[0]['name']}取下来」）")
    if not gem_fits(gem.template.props.get("gem_slot", "any"), cat):
        raise ActionError(f"{gem.name}只能镶在{GEM_SLOT_WORDS.get(gem.template.props.get('gem_slot'), '')}上，{item.name}镶不了")
    gems.append({"id": gem.template.id, "tier": gem.props.get("tier", 1), "name": gem.name})
    environment.consume(cur, gem)
    _regem(cur, item, gems)
    facts = [f"{npc.name}把{gem.name}按进{item.name}的孔里，用小锤敲了几下固定好（镶宝石不收钱）",
             f"{item.name}：宝石孔 {len(gems)}/{holes}"] + [item_info.effect_line(e) for e in gem_effects(
                 gem.template.props, gem.props.get("tier", 1), cat)]
    return facts + (equipment.sync_gear_hp(cur, player.id) if item.equipped_slot else [])


def do_unsocket(cur: Cursor, player: Player, view: RoomView, a: Unsocket) -> list[str]:
    """取宝石：宝石还给玩家，按品质收钱；诅咒装备解咒前取不出来"""
    npc = _smith(cur, view, player, a.target, "取宝石")
    item = helpers.inv_item(cur, view, player, a.item)
    gems = list(item.props.get("gems") or [])
    if not gems:
        raise ActionError(f"{item.name}上没有镶宝石")
    if _cursed(item):
        raise ActionError(f"{item.name}被诅咒了，宝石跟它长在了一起，得先找诺艾尔解咒才取得下来")
    want = (a.gem or "").strip()
    pick = next((g for g in gems if want and (want == g["name"] or want in g["name"])), None) \
        or (gems[0] if not want or len(gems) == 1 else None)
    if pick is None:
        raise ActionError(f"{item.name}上镶的是{'、'.join(g['name'] for g in gems)}，要取哪一颗？")
    fee = dungeon.gem_rules()["unsocket_fee"][pick["tier"] - 1]
    patron = npc_trade.pay(cur, player, fee, npc)
    gems.remove(pick)
    _regem(cur, item, gems)
    cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                (pick["id"], player.id, Jsonb(dungeon.gem_props(cur, pick["id"], pick["tier"]))))
    facts = [f"{player.name}付了 {fee} 金币，{npc.name}用细錾子把{pick['name']}从{item.name}上撬了下来，完好无损"] + patron
    return facts + (equipment.sync_gear_hp(cur, player.id) if item.equipped_slot else [])


# 老手把用不上的武器护具放进武器桶留给新人：强化等级清掉（伤害防御回到原样），镶的宝石退回捐的人背包；
# 诅咒装备、礼物（NPC 回礼给的、送 NPC 的）、委托要交的东西、钥匙不能放。拿的人每人每天一件，拿到的标 donated，NPC 不收
DONATE_TAKE_WINDOW = "1 day"


UPGRADE_PROPS = ("plus", "damage", "defense", "name", "gems", "gem_fx", "gem_light")


def _gift_templates(cur: Cursor) -> set[str]:
    """NPC 回礼送的东西（连同弩空着、装好的另一个模板）"""
    cur.execute("""select g.value->>'item' as item from npc_templates t, jsonb_each(coalesce(t.props->'return_gifts', '{}')) g
                   where g.value ? 'item'""")
    ids = {r["item"] for r in cur.fetchall()}
    cur.execute("""select id from item_templates where props->>'loads' = any(%(i)s) or props->>'unloaded' = any(%(i)s)
                   or props->>'refill_to' = any(%(i)s)""", {"i": list(ids)})
    return ids | {r["id"] for r in cur.fetchall()}


def donation_box(view: RoomView, ref: Optional[str]) -> Dispenser:
    boxes = [d for d in view.dispensers if d.donate]
    if ref:
        uid = view.resolve(ref)
        if box := next((d for d in boxes if d.id == uid), None):
            return box
    if len(boxes) == 1:
        return boxes[0]
    raise ActionError("这里没有能放东西的地方（酒馆门边的武器桶可以）" if not boxes else "要放进哪里？")


def do_donate(cur: Cursor, player: Player, view: RoomView, a: Donate) -> list[str]:
    box = donation_box(view, a.target)
    item = helpers.inv_item(cur, view, player, a.item)
    if item.template.type not in box.donate:
        raise ActionError(f"{box.container}只收武器和护具，{item.name}放不进去")
    if item.equipped_slot:
        raise ActionError(f"{item.name}还装备在身上，先卸下来再放")
    if helpers.prop(item, "cursed"):                   # 没穿上时诅咒不发作，但也不能坑新人
        raise ActionError(f"{item.name}被诅咒了，不能留给别人（先找诺艾尔解咒）")
    if environment.precious(cur, item) or helpers.prop(item, "gift") or item.template.id in _gift_templates(cur):
        raise ActionError(f"{item.name}是别人的心意或者要交的东西，不能放进{box.container}")
    facts = []
    if item.props.get("plus"):
        facts.append(f"{item.name}的强化没了，放进去的是一把普通的{item.template.name}")
    for g in item.props.get("gems") or []:
        cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                    (g["id"], player.id, Jsonb(dungeon.gem_props(cur, g["id"], g["tier"]))))
        facts.append(f"镶在上面的{g['name']}撬下来，退回了{player.name}的背包")
    props = {k: v for k, v in item.props.items() if k not in UPGRADE_PROPS + ("donated",)}
    cur.execute("insert into donations (room_id, container, template_id, props, donor) values (%s, %s, %s, %s, %s)",
                (box.room, box.key, item.template.id, Jsonb(props), player.id))
    cur.execute("delete from item_instances where id = %s", (item.id,))
    return [f"{player.name}把{item.template.name}放进了{box.container}，留给缺家伙的人"] + facts


SCRAP_BY_RARITY = {"common": 1, "uncommon": 2, "rare": 3}


def _not_material(cur: Cursor, item: ItemInstance, what: str) -> None:
    """诅咒装备、礼物（NPC 回礼、送 NPC 的）、委托物品、钥匙：不能捐、不能拆"""
    if helpers.prop(item, "cursed"):
        raise ActionError(f"{item.name}被诅咒了，{what}不了（先找诺艾尔解咒）")
    if environment.precious(cur, item) or helpers.prop(item, "gift") or item.template.id in _gift_templates(cur):
        raise ActionError(f"{item.name}是别人的心意或者要交的东西，{what}不了")


def do_dismantle(cur: Cursor, player: Player, view: RoomView, a: Dismantle) -> list[str]:
    """莉娜拆解用不上的地牢装备：出碎铁（垫升级用），镶的宝石退回背包"""
    npc = _smith(cur, view, player, a.target, "拆解")
    item = helpers.inv_item(cur, view, player, a.item)
    if item.template.id not in dungeon.dungeon_items() or not gem_category(item.template.type, item.template.slot):
        raise ActionError(f"{npc.name}只拆地牢里带出来的武器、护具、饰品，{item.name}拆不出好铁")
    if item.equipped_slot:
        raise ActionError(f"{item.name}还装备在身上，先卸下来")
    if helpers.prop(item, "donated"):
        raise ActionError(f"{item.name}是酒馆武器桶里别人留给新人的，{npc.name}不拆")
    _not_material(cur, item, "拆")
    n = SCRAP_BY_RARITY[dungeon.item_rarity(item.template.id, item.props)] + item.props.get("plus", 0) // 2
    facts = [f"{npc.name}把{item.name}拆开，挑出了 {n} 份碎铁（升级时垫上，每份成功率 +{round(UPGRADE_SCRAP * 100)}%）"]
    for g in item.props.get("gems") or []:
        cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                    (g["id"], player.id, Jsonb(dungeon.gem_props(cur, g["id"], g["tier"]))))
        facts.append(f"镶在上面的{g['name']}撬下来，还给了{player.name}")
    cur.execute("delete from item_instances where id = %s", (item.id,))
    cur.execute("""update item_instances set quantity = quantity + %s
                   where player_id = %s and template_id = %s and equipped_slot is null returning id""", (n, player.id, SCRAP))
    if not cur.fetchone():
        cur.execute("insert into item_instances (template_id, player_id, quantity) values (%s, %s, %s)", (SCRAP, player.id, n))
    return facts


def do_refine(cur: Cursor, player: Player, view: RoomView, a: Refine) -> list[str]:
    """诺艾尔刷宝石品质：只升不降（loot.yaml gems.refine），宝石得在背包里、没镶上去；最深层数不够时封顶，封顶了不收钱"""
    npc = helpers.room_npc(cur, view, player, a.target)
    if not npc.template.props.get("refine"):
        raise ActionError(f"{npc.name}不懂宝石，找杂货铺的诺艾尔")
    gem = helpers.inv_item(cur, view, player, a.item)
    if gem.template.type != "gem":
        if gem.props.get("gems"):
            raise ActionError(f"宝石镶在{gem.name}上刷不了，先找莉娜取下来（说「把{gem.name}上的{gem.props['gems'][0]['name']}取下来」）")
        raise ActionError(f"{gem.name}不是宝石")
    rules = dungeon.gem_rules()["refine"]
    tier = gem.props.get("tier", 1)
    if tier >= GEM_TOP:
        raise ActionError(f"{gem.name}已经是完美的了，再唤醒也不会更好")
    cur.execute("select deepest_floor from players where id = %s", (player.id,))
    deepest = cur.fetchone()["deepest_floor"]
    cap = refine_cap(bool(gem.template.props.get("numeric")), deepest, rules)
    if tier >= cap:
        need = rules["min_deepest_floor"]["shiny"] if cap < 3 else rules["min_deepest_floor"]["perfect_numeric"]
        raise ActionError(f"{npc.name}翻了半天书，小声说书上写着……还差一点什么（最深走到第 {need} 层以后，才唤得醒更好的{GEM_TIER_WORDS[tier + 1]}品质）")
    catalyst = None
    if a.catalyst:
        catalyst = min((i for i in view.inventory if i.template.id == gem.template.id and i.id != gem.id),
                       key=lambda i: i.props.get("tier", 1), default=None)      # 垫子先用品质最低的
        if catalyst is None:
            catalyst = next((i for i in view.inventory if helpers.prop(i, "refine_catalyst")), None)       # 晶粉：什么宝石都能垫
        if catalyst is None:
            raise ActionError(f"身上没有另一颗{gem.template.name}（或者晶粉）能当垫子")
    cost = rules["cost"][tier - 1]
    patron = npc_trade.pay(cur, player, cost, npc)
    if catalyst:
        environment.consume(cur, catalyst)
    dust = float(helpers.prop(catalyst, "refine_catalyst") or 0) if catalyst else 0
    new = refine_roll(tier, cap, rules, bool(catalyst) and not dust, dust or 1.0)
    facts = [f"{player.name}付了 {cost} 金币，{npc.name}翻开一本旧书，照着上面的法子对着{gem.name}念念有词"
             + (f"，{catalyst.name}当了垫子，化成一小撮粉末" if catalyst else "")] + patron
    if new == tier:
        return facts + [f"{gem.name}闪了一下又暗了下去，品质没变（{npc.name}一个劲地小声道歉）"]
    props = dungeon.gem_props(cur, gem.template.id, new)
    cur.execute("update item_instances set props = props || %s where id = %s", (Jsonb(props), gem.id))
    return facts + [f"{gem.name}亮了起来，变成了{props['name']}（{GEM_TIER_WORDS[new]}品质）"
                    + ("，一下跳了两档！" if new - tier == 2 else "") + f"（{npc.name}忍不住小声欢呼了一下）"]


# 诅咒装备（props.cursed）：装上就粘在身上，卸不下、扔不掉、给不出、卖不掉，别的东西也顶不掉它。
# 找诺艾尔（props.uncurse）按参考价付钱解咒：解了就是普通装备（实例上记 cursed: false），特效还在

def _cursed(item: ItemInstance) -> bool:
    return bool(item.equipped_slot and helpers.prop(item, "cursed"))


def no_curse(item: Optional[ItemInstance], what: str = "卸") -> None:
    if item is not None and _cursed(item):
        raise ActionError(f"{item.name}被诅咒了，粘在身上{what}不下来（找杂货铺的诺艾尔付钱解咒）")


def do_uncurse(cur: Cursor, player: Player, view: RoomView, a: Uncurse) -> list[str]:
    npc = helpers.room_npc(cur, view, player, a.target)
    if not npc.template.props.get("uncurse"):
        raise ActionError(f"{npc.name}不会解咒")
    if a.item:
        item = helpers.inv_item(cur, view, player, a.item)
        if not _cursed(item):
            raise ActionError(f"{item.name}身上没有诅咒" if not helpers.prop(item, "cursed") else f"{item.name}没戴在身上，用不着解咒，直接扔掉就行")
    else:
        worn = [i for i in helpers.worn(cur, player) if _cursed(i)]
        if not worn:
            raise ActionError(f"{player.name}身上没有被诅咒的装备")
        item = worn[0]
    price = base_price(npc_trade.item_stats(item))
    patron = npc_trade.pay(cur, player, price, npc)
    cur.execute("update item_instances set props = props || '{\"cursed\": false}'::jsonb where id = %s", (item.id,))
    return ([f"{player.name}付了 {price} 金币，{npc.name}翻开怀里那本旧书，小声念了一段古老的句子，"
             f"{item.name}上缠着的诅咒散开了：现在能卸下来了"] + patron)
