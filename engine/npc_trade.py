"""Talking, paying, buying and selling, NPC giving and making items. / 跟 NPC 说话、买卖、给予、现做东西"""
import math
import random
import re
from typing import Any, Optional
from uuid import UUID

from psycopg import Connection, Cursor
from psycopg.types.json import Jsonb

from commands import REST_TALK_RE
from rules import base_price, clamp_price
from schema import ActionResult, Give, ItemInstance, Npc, Pay, Player, Rest, RoomView, Sell, Talk, Upgrade

from .core import ActionError
from . import duels, dungeon_events, environment, equipment, everyday, helpers, item_info, loading, npc_social, smith


def affinity_of(cur: Cursor, player: Player, npc: Npc) -> int:
    cur.execute("select affinity from player_npc_relations where player_id = %s and npc_template = %s",
                (player.id, npc.template.id))
    row = cur.fetchone()
    return row["affinity"] if row else 0


def do_talk(cur: Cursor, player: Player, view: RoomView, a: Talk) -> list[str]:
    # 这里只校验对象在场，NPC 怎么回、给不给东西由对话步骤决定（见 giveable_items / npc_give）
    # 武器桶这类物件跟 NPC 列在一起，但不会说话，叙事按它的描述写
    if d := next((d for d in view.dispensers if d.id == view.resolve(a.target)), None):
        return [f"{player.name}对{d.container}说：“{a.message}”", f"{d.container}只是个物件，不会回应"]
    npc = helpers.room_npc(cur, view, player, a.target)
    # AI 解析偶尔把"对麦琪 来杯酒"整句当成说的话：只去掉"对麦琪"这种指令开头；"麦琪大人""麦琪，救我"是喊人，留着。
    # 外面多包的一层引号（玩家自己打了引号）也去掉
    message = re.sub(rf"^(对|跟|和|向){re.escape(npc.name)}(说|讲|问)?[\s，,：:]*", "", a.message).strip() or a.message
    message = message.strip("\"“”'‘’「」").strip() or message
    facts = ([f"{player.name}对{npc.name}说：“{message}”"] + dungeon_events.stele(cur, npc, player) + npc_social.upgrade_nudge(cur, player, npc)
             + npc_social.bow_nudge(cur, player, npc))
    offers = offers_for(cur, player.id, npc)
    # 刚说要白给她钱、她问了"真要给我？"，这句回"是""给你"就给
    if (tip := offers.get("tip")) and TIP_CONFIRM_RE.search(message) and not re.search(r"不|算了|别", message[:4]):
        if tip["price"] > player.gold:
            return facts + [f"{player.name}身上只有 {player.gold} 金币，给不出 {tip['price']}"]
        return facts + give_tip(cur, player, npc, tip["price"], tip)
    # 问住店（"住店多少钱"）：记一笔住店的报价，接着给钱就是付房钱
    if (inn := npc.template.props.get("inn")) and REST_TALK_RE.search(message):
        put_offer(cur, player.id, npc, "inn", inn.get("price", 0))
    return facts


TIP_CONFIRM_RE = re.compile(r"^(是|对|嗯|确认|真的|真给|给你|收下|拿着|收着|当然|没错|给|要)")


def do_pay(cur: Cursor, player: Player, view: RoomView, a: Pay) -> list[str]:
    if a.amount > player.gold:
        raise ActionError(f"{player.name}身上只有 {player.gold} 金币，不够 {a.amount}")
    if a.target not in view.refs:
        other, _ = duels.room_player(cur, player, a.target, lock=True)
        cur.execute("update players set gold = gold - %s where id = %s", (a.amount, player.id))
        cur.execute("update players set gold = gold + %s where id = %s", (a.amount, other.id))
        return [f"{player.name}给了{other.name} {a.amount} 金币"]
    npc = helpers.room_npc(cur, view, player, a.target)
    if npc.template.props.get("fare"):
        due = helpers.tally(cur, npc, "fare_due")
        if not due:
            raise ActionError(f"{npc.name}这会儿没伸手要钱")
        if a.amount < due:
            raise ActionError(f"{npc.name}要的是 {due} 金币，{player.name}只给了 {a.amount}")
        cur.execute("update players set gold = gold - %s where id = %s", (a.amount, player.id))
        helpers.tally_merge(cur, npc, {"fare_paid": 1})
        return [f"{player.name}把 {a.amount} 金币放进了{npc.name}干枯的手心"]
    offers = offers_for(cur, player.id, npc)
    deals = {k: v for k, v in offers.items() if k != "tip"}
    if deals:
        # 刚谈过买卖：付的就是最近那笔的钱
        key, offer = max(deals.items(), key=lambda kv: kv[1].get("at", 0))
        price = offer["price"]
        if a.amount < price:
            raise ActionError(f"{npc.name}要的是 {price} 金币，{player.name}只给了 {a.amount}")
        facts = _close_deal(cur, player, view, npc, key)
        if extra := a.amount - price:
            player = loading.load_player(cur, player.id, lock=True)
            facts += [f"多给的 {extra} 金币{npc.name}当小费收下了"] + pay(cur, player, extra, npc)
        return facts
    return give_tip(cur, player, npc, a.amount, offers.get("tip"))


def _close_deal(cur: Cursor, player: Player, view: RoomView, npc: Npc, key: str) -> list[str]:
    """付钱成交一笔报过价的买卖：住店、升级、墙上的货、现做的东西"""
    if key == "inn":
        cur.execute("update player_npc_relations set offers = offers - 'inn' where player_id = %s and npc_template = %s",
                    (player.id, npc.template.id))
        return everyday.do_rest(cur, player, view, Rest(action="rest"))
    if key.startswith("upgrade:"):
        ref = next((r for r, uid in view.refs.items() if str(uid) == key[8:]), None)
        if ref is None:
            raise ActionError("要升级的武器不在身上了")
        return smith.do_upgrade(cur, player, view, Upgrade(action="upgrade", item=ref, target=next(
            r for r, uid in view.refs.items() if uid == npc.id)))
    r = (npc_buy_made if key.startswith("made:") else npc_sell)(cur.connection, player.id, npc, key)
    if not r.success:
        raise ActionError(r.facts[0])
    return r.facts


def give_tip(cur: Cursor, player: Player, npc: Npc, amount: int, pending: Optional[dict]) -> list[str]:
    """没在做买卖就给钱：第一次 NPC 只问一句，同样的数目再给一次（或者回一句"是""给你"）才收"""
    if pending is None or pending["price"] != amount:
        put_offer(cur, player.id, npc, "tip", amount)
        return [f"{player.name}想白给{npc.name} {amount} 金币（不是买东西），{npc.name}还没收",
                f"{npc.name}要先问一句是不是真要给她，{player.name}确认了才收"]
    cur.execute("update player_npc_relations set offers = offers - 'tip' where player_id = %s and npc_template = %s",
                (player.id, npc.template.id))
    return ([f"{player.name}把 {amount} 金币塞给了{npc.name}，{npc.name}收下了（白给的心意，不是买东西，她不用拿东西给他）"]
            + pay(cur, player, amount, npc))


# 收购：做生意的 NPC（props.buys.likes 是她用得上的种类）什么都收，按参考价（base_price）的一定比例给钱：
# 用得上的 BUY_LIKED，别的 BUY_OTHER，好感每 10 点再多 BUY_AFFINITY（最多 ±BUY_AFFINITY_MAX）。钥匙、任务要交的东西不收
BUY_LIKED, BUY_OTHER = 0.6, 0.3


BUY_GIFT = 0.2                          # 礼物道具（矿石、古酒、古书）谁收都只给两成：它的价值在送人换好感


BUY_AFFINITY, BUY_AFFINITY_MAX = 0.02, 0.1


def item_kinds(item: ItemInstance) -> set[str]:
    """东西属于哪几类（收购时看她用不用得上）：weapon / armor / food / drink / herb / 礼物种类（ore wine book） / misc"""
    kinds = {item.template.type if item.template.type in ("weapon", "armor", "misc") else ""}
    if item.template.type == "consumable":
        kinds.add("drink" if environment.is_alcohol(item) or any(c in item.name for c in "水汤茶奶") else "food")
    if helpers.prop(item, "herbal"):
        kinds.add("herb")
    if gift := helpers.prop(item, "gift"):
        kinds.add(gift)
    return kinds - {""}


def item_stats(item: ItemInstance) -> dict:
    return {"damage": item.damage, "defense": item.defense, "heal": item.heal, "harm": item.harm,
            "knockout": item.knockout, "price": helpers.prop(item, "price")}


def buy_price(npc: Npc, item: ItemInstance, affinity: int) -> int:
    """这个 NPC 收这件东西给多少钱"""
    liked = item_kinds(item) & set(npc.template.props.get("buys", {}).get("likes", []))
    rate = (BUY_GIFT if helpers.prop(item, "gift") else BUY_LIKED if liked else BUY_OTHER) \
        + max(-BUY_AFFINITY_MAX, min(BUY_AFFINITY_MAX, affinity // 10 * BUY_AFFINITY))
    return max(1, round(base_price(item_stats(item)) * rate)) * item.quantity


def buy_quotes(conn: Connection, player_id: UUID, npc: Npc, items: list[ItemInstance]) -> list[tuple[str, int]]:
    """给 AI 看的：她收玩家身上这些东西各给多少（玩家问收不收、值多少时照这个说）"""
    if not npc.template.props.get("buys"):
        return []
    with conn.transaction():
        cur = loading.cursor(conn)
        affinity = affinity_of(cur, loading.load_player(cur, player_id), npc)
        return [(i.name, buy_price(npc, i, affinity)) for i in items if not environment.precious(cur, i) and not helpers.prop(i, "donated")]


def do_sell(cur: Cursor, player: Player, view: RoomView, a: Sell) -> list[str]:
    npc = helpers.room_npc(cur, view, player, a.target)
    if not npc.template.props.get("buys"):
        raise ActionError(f"{npc.name}不收东西")
    item = helpers.inv_item(cur, view, player, a.item)
    smith.no_curse(item, "拿")
    if everyday.burning(item):
        raise ActionError(f"点着的{item.name}{npc.name}可不收")
    if environment.precious(cur, item):
        raise ActionError(f"{item.name}太要紧了，{npc.name}不收")
    if helpers.prop(item, "donated"):
        raise ActionError(f"{item.name}是酒馆武器桶里别人留下的，{npc.name}不收（用不上了可以放回桶里）")
    price = buy_price(npc, item, affinity_of(cur, player, npc))
    cur.execute("delete from item_instances where id = %s", (item.id,))
    cur.execute("update players set gold = gold + %s where id = %s", (price, player.id))
    return [f"{player.name}把{helpers.item_label(item)}卖给了{npc.name}，得到 {price} 金币"]


LIKED_GIFT = 10                      # 送心爱的礼物加的好感（不受花钱加好感的上限）


GIFT_FACT = "心爱的礼物"                # 台词那边认这几个字，演出特别的反应


RETURNED_FACT = "她自己卖出去的那件"     # 从她那买的礼物又送回给她（诺艾尔的古书），台词那边演认出来


def do_give(cur: Cursor, player: Player, view: RoomView, a: Give) -> list[str]:
    item = helpers.inv_item(cur, view, player, a.item)
    smith.no_curse(item, "拿")
    if everyday.burning(item):
        raise ActionError(f"点着的{item.name}只能拿在自己手上，递不出去")
    if a.target not in view.refs:
        # 给同房间的玩家：target 是名字。睡着、倒下的也能收（东西放进他背包）
        other, _ = duels.room_player(cur, player, a.target, lock=True)
        helpers.move_item(cur, item, player_id=other.id)          # 装备着的会自动卸下
        return [f"{player.name}把{helpers.item_label(item)}交给了{other.name}"]
    npc = helpers.room_npc(cur, view, player, a.target)
    # 正好是这个 NPC 的委托要的东西（锈剑交给莉娜）：先留在身上，接下来的 quest_turn 收走并发奖励，
    # 不然东西进了 NPC 背包，委托就再也触发不了
    cur.execute(
        """select 1 from quests q left join player_quests pq on pq.quest_id = q.id and pq.player_id = %s
           where q.giver = %s and q.needs_item = %s and pq.status is distinct from 'rewarded'""",
        (player.id, npc.template.id, item.template.id),
    )
    if cur.fetchone():
        return [f"{player.name}把{item.name}递给了{npc.name}"]
    # 送她心爱的礼物（world.yaml props.gift_likes 里的种类）：好感固定 +LIKED_GIFT，东西她收下（从世界里拿走）
    if (gift := helpers.prop(item, "gift")) and (gift == "any" or gift in npc.template.props.get("gift_likes", [])):
        # 小礼物（props.gift_value，村里互相卖的怪酒、木炭、游记）：好感加得少，每人每天只收一件，跨得过好感的坎
        if (unlock := helpers.prop(item, "unlock")) and npc.template.props.get("upgrades") and not player.flags.get(unlock):
            environment.consume(cur, item)
            new = min(100, affinity_of(cur, player, npc) + int(helpers.prop(item, "gift_value") or 0))
            cur.execute("""insert into player_npc_relations (player_id, npc_template, affinity) values (%s, %s, %s)
                           on conflict (player_id, npc_template) do update set affinity = excluded.affinity""",
                        (player.id, npc.template.id, new))
            cur.execute("update players set flags = flags || jsonb_build_object(%s::text, true) where id = %s", (unlock, player.id))
            return [f"{player.name}把{item.name}送给了{npc.name}",
                    f"{npc.name}盯着拓片上的锻造纹看了很久，手指跟着纹路一圈圈描过去，最后低声说了句：“……原来是这么打的。”",
                    f"（莉娜学会了深层锻造：深层装备（第 16 层以后掉的）能升到 +{smith.UPGRADE_DEEP_MAX}）",
                    f"{npc.name}对{player.name}的好感上升（当前 {new}）"]
        if small := helpers.prop(item, "gift_value"):
            cur.execute("""select 1 from player_npc_relations where player_id = %s and npc_template = %s
                           and gift_day = (now() at time zone 'Asia/Shanghai')::date""", (player.id, npc.template.id))
            if cur.fetchone():
                raise ActionError(f"{npc.name}今天已经收过{player.name}的小礼物了，明天再送")
            environment.consume(cur, item)
            new = min(100, affinity_of(cur, player, npc) + int(small))
            cur.execute("""insert into player_npc_relations (player_id, npc_template, affinity, gift_day)
                           values (%s, %s, %s, (now() at time zone 'Asia/Shanghai')::date)
                           on conflict (player_id, npc_template) do update
                           set affinity = excluded.affinity, gift_day = excluded.gift_day""",
                        (player.id, npc.template.id, new))
            facts = [f"{player.name}把{item.name}送给了{npc.name}，是她喜欢的小东西",
                     f"{npc.name}对{player.name}的好感上升（当前 {new}，收到小礼物）"]
            if (lore := equipment.LORE.get(helpers.prop(item, "lore"))) and lore[0] == npc.template.id:
                # 壁画拓片、船牌这类：每送一件，她多讲一段（讲什么由叙事写，按第几段往下接）
                seen = dict(player.flags.get("_lore") or {})
                seen[helpers.prop(item, "lore")] = n = int(seen.get(helpers.prop(item, "lore"), 0)) + 1
                cur.execute("update players set flags = flags || jsonb_build_object('_lore', %s::jsonb) where id = %s",
                            (Jsonb(seen), player.id))
                cur.execute("select text from lore_texts where key = %s and n = %s", (helpers.prop(item, "lore"), n))
                told = cur.fetchone()
                facts.append(f"（{npc.name}看着它出了一会儿神，讲起了{lore[1]}的第 {n} 段往事）")
                facts.append(f"{npc.name}讲的往事：{told['text']}" if told else f"{equipment.LORE_MARK}{helpers.prop(item, 'lore')}::{n}::{npc.name}")
            return facts
        back = item.props.get("sold_by") == npc.template.id
        environment.consume(cur, item)                 # 叠着的古酒只送出一瓶
        new = min(100, affinity_of(cur, player, npc) + LIKED_GIFT)
        cur.execute("""insert into player_npc_relations (player_id, npc_template, affinity) values (%s, %s, %s)
                       on conflict (player_id, npc_template) do update set affinity = excluded.affinity""",
                    (player.id, npc.template.id, new))
        return [f"{player.name}把{item.name}送给了{npc.name}，这正是她最想要的东西（{GIFT_FACT}）"
                + (f"，而且是{RETURNED_FACT}" if back else ""),
                f"{npc.name}对{player.name}的好感上升（当前 {new}，收到心爱的礼物）"]
    helpers.move_item(cur, item, npc_id=npc.id)
    return [f"{player.name}把{helpers.item_label(item)}交给了{npc.name}"]


def keeper_eject(conn: Connection, view: RoomView) -> list[str]:
    """房间里有能轰人的 NPC（麦琪、莉娜：配了 eject_to，醒着）就把动手打人的玩家轰出去，记进 NPC 对他的记忆。
    NPC 被放倒、捆住了就管不了"""
    keeper = next((n for n in view.npcs if can_eject(n) and not n.template.hostile), None)
    if keeper is None:
        return []
    with conn.transaction():
        cur = loading.cursor(conn)
        fresh = loading.load_npcs(cur, "n.id = %s and n.alive and n.status is null", (keeper.id,))
    if not fresh or not (kicked := npc_eject(conn, view.player.id, fresh[0])):
        return []
    npc_social.add_npc_log(conn, view.player.id, fresh[0], f"{view.player.name}在你这里对别的客人动手，你把他轰了出去")
    with conn.transaction():
        revived = loading.keeper_revive(loading.cursor(conn), view.room.id)      # 被打倒的客人当场扶起来
    return [f"{keeper.name}看见{view.player.name}动手打人"] + kicked.facts + revived


def _give_rule_ok(cur: Cursor, npc: Npc, player: Player, item: ItemInstance) -> bool:
    # 玩家身上已经有同样的东西就不再给（钥匙这类任务物品，不然每次聊天都塞一把）
    cur.execute("select 1 from item_instances where player_id = %s and template_id = %s", (player.id, item.template.id))
    if cur.fetchone():
        return False
    rule: Any = npc.template.props.get("gives", {}).get(item.template.id)
    if rule == "ai":
        return True
    if not isinstance(rule, dict) or not rule:
        return False                    # 没配置就是不给
    if "requires" in rule and not player.flags.get(rule["requires"]):
        return False
    if "min_affinity" in rule and affinity_of(cur, player, npc) < rule["min_affinity"]:
        return False
    return True


def giveable_items(conn: Connection, player_id: UUID, npc_id: UUID) -> list[ItemInstance]:
    """NPC 现在允许给这个玩家的物品，告诉对话 AI 它可以给哪些"""
    with conn.transaction():
        cur = loading.cursor(conn)
        player = loading.load_player(cur, player_id)
        npcs = loading.load_npcs(cur, "n.id = %s", (npc_id,))
        if not npcs:
            return []
        items = loading.load_items(cur, "i.npc_id = %s", (npc_id,))
        return [i for i in items if _give_rule_ok(cur, npcs[0], player, i)]


# 在 NPC 那儿花钱加好感：每次成交 +1，每满 10 金币再 +1，一次最多 PATRON_MAX；
# 光靠花钱最多到 PATRON_CAP，再往上（白送东西的交情）得靠聊天、帮忙、做委托
PATRON_MAX = 5


PATRON_CAP = 30


def pay(cur: Cursor, player: Player, price: int, npc: Optional[Npc] = None) -> list[str]:
    """交易扣钱：价钱是 AI 定的，这里只管够不够、扣掉。钱不够整笔交易回滚。
    给了 npc 就是在他那儿消费，好感跟着涨，返回好感变化的 fact"""
    price = max(0, price)
    if price > player.gold:
        raise ActionError(f"{player.name}的钱不够，要 {price} 金币，身上只有 {player.gold} 金币")
    if not price:
        return []
    cur.execute("update players set gold = gold - %s where id = %s", (price, player.id))
    if npc is None:
        return []
    now = affinity_of(cur, player, npc)
    new = max(now, min(PATRON_CAP, now + min(PATRON_MAX, 1 + price // 10)))
    if new == now:
        return []                       # 已经是熟客了，再花钱也不涨
    cur.execute(
        """insert into player_npc_relations (player_id, npc_template, affinity) values (%s, %s, %s)
           on conflict (player_id, npc_template) do update set affinity = excluded.affinity""",
        (player.id, npc.template.id, new))
    return [f"{npc.name}对{player.name}的好感上升（当前 {new}，照顾生意）"]


def _deal(npc: Npc, player: Player, name: str, price: int) -> str:
    return (f"{npc.name}把{name}卖给了{player.name}，收了 {price} 金币" if price > 0
            else f"{npc.name}把{name}交给了{player.name}")


def npc_give(conn: Connection, player_id: UUID, npc_id: UUID, item_id: UUID, price: int = 0) -> ActionResult:
    """对话 AI 提议 NPC 给玩家东西时调用，规则不满足就当没说。price 是 AI 定的价钱，0 就是白给"""
    try:
        with conn.transaction():
            cur = loading.cursor(conn)
            player = loading.load_player(cur, player_id, lock=True)
            npcs = loading.load_npcs(cur, "n.id = %s", (npc_id,), lock=True)
            if not npcs or not npcs[0].alive or npcs[0].room_id != player.room_id:
                raise ActionError("对方不在这里")
            npc = npcs[0]
            items = loading.load_items(cur, "i.id = %s and i.npc_id = %s", (item_id, npc.id), lock=True)
            if not items or not _give_rule_ok(cur, npc, player, items[0]):
                raise ActionError(f"{npc.name}不会给这个")
            patron = pay(cur, player, price, npc)
            helpers.move_item(cur, items[0], player_id=player.id)
        return ActionResult(action="npc_give", success=True, facts=[_deal(npc, player, helpers.item_label(items[0]), price)] + patron)
    except ActionError as e:
        return ActionResult(action="npc_give", success=False, facts=[str(e)])


def shop_directory(conn: Connection, npc: Npc) -> list[dict]:
    """村里别的店卖什么（不含这个 NPC 自己）：[{npc, room, items}]。客人找她要别家的货，她指路，不自己报价"""
    with conn.transaction():
        cur = loading.cursor(conn)
        cur.execute("""select distinct t.id, t.name, r.name as room, t.props->'sells' as sells from npcs n
                       join npc_templates t on t.id = n.template_id join rooms r on r.id = n.room_id
                       where n.alive and t.props ? 'sells' and t.id <> %s""", (npc.template.id,))
        shops = cur.fetchall()
        ids = [i for s in shops for i in s["sells"] or []]
        cur.execute("select id, name from item_templates where id = any(%s)", (ids,))
        names = dict((r["id"], r["name"]) for r in cur.fetchall())
    return [{"npc": s["name"], "room": s["room"], "items": [names[i] for i in s["sells"] or [] if i in names]} for s in shops]


def sellable(conn: Connection, npc: Npc, rare: Optional[str] = None, player_id: Optional[UUID] = None) -> list[dict]:
    """NPC 能卖的货（world.yaml 的 sells），给交易 AI 看：物品 id、名字、说明、伤害防御、按效果算的原价。
    rare 是这会儿有的稀罕货（rare_stock），标 rare，价钱固定；给了 player_id 就加上回礼解锁给他的货"""
    extra = []
    if player_id:
        with conn.transaction():
            extra = perk_sells(loading.cursor(conn), player_id, npc)
    ids = npc.template.props.get("sells", []) + extra + ([rare] if rare else [])
    if not ids:
        return []
    with conn.transaction():
        cur = loading.cursor(conn)
        cur.execute("select id, name, description, type, damage, defense, heal, props->'price' as price,"
                    " coalesce(props->>'kind', case when (props->>'alcohol')::boolean then 'drink' end) as kind"
                    " from item_templates where id = any(%s)", (ids,))
        return [r | ({"base_price": _rare_price(npc, r), "rare": True} if r["id"] == rare
                     else {"base_price": math.ceil(base_price(r) * markup(npc, r["id"]))})
                for r in cur.fetchall()]


LIKE_WORDS = {"food": "吃的", "drink": "酒水", "herb": "草药", "wine": "酒", "weapon": "武器", "armor": "护甲", "ore": "矿石",
              "misc": "杂物", "book": "书"}


def npc_menu(cur: Cursor, player_id: UUID, npc: Npc) -> Optional[dict]:
    """侧栏里店主下面的提示：这儿能办的事、墙上的货和标价（不用每次去问）。点一下把命令填进输入框。
    稀罕货只有问出来以后才列（问货时才判有没有），回礼解锁的本事和货也列上"""
    p = npc.template.props
    if npc.template.hostile:
        return None
    name, services = npc.name, []
    if p.get("leaderboard"):
        services += [{"text": "看排行榜", "fill": f"看{name}"},
                     {"text": "在地窖里说「传送到第 N 层」，直接去到过的传送石（每 5 层一块）", "fill": "传送到第 层"}]
    cur.execute("""select q.goal, q.done_flag, r.name as reward from quests q
                   left join item_templates r on r.id = q.reward_item
                   left join player_quests pq on pq.quest_id = q.id and pq.player_id = %s
                   where q.giver = %s and not q.hidden and coalesce(pq.status, '') <> 'rewarded' order by q.id""",
                (player_id, npc.template.id))
    for q in cur.fetchall():
        cur.execute("select flags ? %s as done from players where id = %s", (q["done_flag"] or "", player_id))
        done = bool(q["done_flag"]) and cur.fetchone()["done"]
        services.append({"text": f"委托：{q['goal']}" + (f"（奖励{q['reward']}）" if q["reward"] else "")
                         + ("，做完了，跟她说一声领奖" if done else ""), "fill": f"对{name} "})
    if likes := p.get("gift_likes"):
        services.append({"text": "喜欢收到：" + "、".join(LIKE_WORDS.get(k, k) for k in likes), "fill": f"给{name}"})
    cur.execute("""select t.name from item_instances i join item_templates t on t.id = i.template_id
                   where i.player_id = %s and t.props->>'refill_by' = %s""", (player_id, npc.template.id))
    if empties := [r["name"] for r in cur.fetchall()]:
        services.append({"text": f"续杯：{'、'.join(empties)}空了，找她灌满（不收钱）", "fill": f"对{name} 续杯"})
    if buys := p.get("buys"):
        services.append({"text": "收东西：" + "、".join(LIKE_WORDS.get(k, k) for k in buys.get("likes", [])) + "给价高",
                         "fill": f"把 卖给{name}"})
    if inn := p.get("inn"):
        services.append({"text": f"住店 {inn.get('price', 0)} 金币：回满血、醒酒", "fill": "住店"})
    if p.get("upgrades"):
        services += [{"text": "升级武器、防具（最多 +10）", "fill": f"对{name} 升级"},
                     {"text": "镶宝石 · 取宝石（地牢里掉的装备才带孔）", "fill": "把 镶到 上"}]
    if p.get("refine"):
        services.append({"text": "刷宝石品质（20 / 60 / 150 金币，宝石要先取下来）", "fill": "刷"})
    if p.get("uncurse"):
        services.append({"text": "解除装备上的诅咒", "fill": f"对{name} 解咒"})
    if p.get("lore"):
        services.append({"text": "讲地牢怪物的习性和打法", "fill": f"对{name} 怎么打"})
    table = p.get("return_gifts") or {}
    mine = npc_social.perks(cur, player_id, npc.template.id)
    services += [{"text": g["text"], "fill": f"对{name} "} for g in table.values()
                 if g.get("perk") in mine and g.get("text") and g["perk"] not in ("map_scrap",)]
    ids = p.get("sells", []) + perk_sells(cur, player_id, npc)
    rare = _rare_now(cur, player_id, npc)
    goods = []
    if ids or rare:
        cur.execute("select id, name, damage, defense, heal, props->'price' as price from item_templates where id = any(%s)",
                    (ids + ([rare] if rare else []),))
        rows = {r["id"]: r for r in cur.fetchall()}
        for i in dict.fromkeys(ids + ([rare] if rare else [])):
            if r := rows.get(i):
                price = _rare_price(npc, r) if i == rare else clamp_price(r, base_price(r), markup(npc, i))
                goods.append({"name": r["name"], "price": price, "rare": i == rare, "fill": f"对{name} 买{r['name']}"})
    return {"services": services, "goods": goods} if services or goods else None


OFFER_WINDOW = "10 minutes"             # NPC 报的价多久内有效


# 偶尔进的稀罕货（world.yaml rare_sells）：客人问有什么卖的时判一次，不管中没中这段时间里不再判
RARE_WINDOW = "1 hour"


RARE_ASK_RE = re.compile(r"卖什么|卖啥|卖些|卖点|有什么|有啥|有没有|什么货|新货|进货|进了|看看货|货架|稀罕|好东西|宝贝|东西卖|什么可以买|能买")


RELUCTANT_FACT = "舍不得卖"              # 台词那边认这几个字，演舍不得又礼貌道谢


def rare_stock(conn: Connection, player_id: UUID, npc: Npc, text: str) -> Optional[str]:
    """这个客人现在能在 NPC 这儿买到的稀罕货（物品 id），没有就是 None。这段时间还没判过、他又在问有什么卖，就判一次"""
    rare = npc.template.props.get("rare_sells")
    if not rare:
        return None
    with conn.transaction():
        cur = loading.cursor(conn)
        if (row := _rare_row(cur, player_id, npc)) is not None:
            return row.get("item")
        if not RARE_ASK_RE.search(text):
            return None
        item = random.choice(rare["items"]) if helpers.roll(rare.get("chance", 0.15)) else None
        cur.execute(
            """insert into player_npc_relations (player_id, npc_template, rare)
               values (%s, %s, jsonb_build_object('at', extract(epoch from now()), 'item', %s::text))
               on conflict (player_id, npc_template) do update set rare = excluded.rare""",
            (player_id, npc.template.id, item))
        return item


def _rare_row(cur: Cursor, player_id: UUID, npc: Npc) -> Optional[dict]:
    """这段时间里判过的稀罕货 {at, item}；没判过或者过期了是 None"""
    cur.execute(
        f"""select rare from player_npc_relations where player_id = %s and npc_template = %s and rare is not null
              and to_timestamp((rare->>'at')::float) > now() - interval '{RARE_WINDOW}'""",
        (player_id, npc.template.id))
    row = cur.fetchone()
    return row["rare"] if row else None


def _rare_now(cur: Cursor, player_id: UUID, npc: Npc) -> Optional[str]:
    return (_rare_row(cur, player_id, npc) or {}).get("item") if npc.template.props.get("rare_sells") else None


def _rare_price(npc: Npc, stats: dict) -> int:
    return round(base_price(stats) * npc.template.props["rare_sells"].get("markup", 2))


PERK_SELLS = {"map_scrap": "map_scrap"}  # 回礼解锁的货：本事 → 物品（诺艾尔好感 20 以后卖地图残片）


def perk_sells(cur: Cursor, player_id: UUID, npc: Npc) -> list[str]:
    return [item for perk, item in PERK_SELLS.items() if perk in npc_social.perks(cur, player_id, npc.template.id)]


def _sells(cur: Cursor, player_id: UUID, npc: Npc) -> list[str]:
    """这个客人能在 NPC 这儿买的货：墙上的 + 回礼解锁的 + 这会儿有的稀罕货"""
    rare = _rare_now(cur, player_id, npc)
    return npc.template.props.get("sells", []) + perk_sells(cur, player_id, npc) + ([rare] if rare else [])


def _sell_rare(cur: Cursor, player: Player, npc: Npc, template_id: str, name: str, price: int) -> list[str]:
    """卖出稀罕货：只有一件，卖掉就没了；舍不得卖的那几样多一句，买走的东西记上是谁卖的"""
    cur.execute("update player_npc_relations set rare = rare || '{\"item\": null}' where player_id = %s and npc_template = %s",
                (player.id, npc.template.id))
    cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                (template_id, player.id, Jsonb({"sold_by": npc.template.id})))
    facts = [_deal(npc, player, name, price)]
    if template_id in npc.template.props["rare_sells"].get("reluctant", []):
        facts.insert(0, f"{npc.name}抱着{name}犹豫了好一会儿，{RELUCTANT_FACT}，最后还是依依不舍地推了过去")
    return facts


def get_offers(conn: Connection, player_id: UUID, npc: Npc) -> dict[str, dict]:
    """NPC 给这个玩家报过、还没过期的价 {key: {price, spec}}。
    key 是卖货的物品 id，或者 "made:名字"（现造的东西，spec 是报价时定好的规格）"""
    with conn.transaction():
        return offers_for(loading.cursor(conn), player_id, npc)


def offers_for(cur: Cursor, player_id: UUID, npc: Npc) -> dict[str, dict]:
    cur.execute(
        f"""select o.key, o.value from player_npc_relations r, jsonb_each(r.offers) o
            where r.player_id = %s and r.npc_template = %s
              and to_timestamp((o.value->>'at')::float) > now() - interval '{OFFER_WINDOW}'""",
        (player_id, npc.template.id),
    )
    return {r["key"]: r["value"] for r in cur.fetchall()}


def put_offer(cur: Cursor, player_id: UUID, npc: Npc, key: str, price: int, spec: Optional[dict] = None) -> None:
    """记一条报价；已有的就改价钱（砍价让步），现造东西的规格不变"""
    value = {"price": max(0, price)} | ({"spec": spec} if spec else {})
    cur.execute(
        """insert into player_npc_relations (player_id, npc_template, offers)
           values (%(p)s, %(t)s, jsonb_build_object(%(k)s::text, %(v)s::jsonb || jsonb_build_object('at', extract(epoch from now()))))
           on conflict (player_id, npc_template) do update set offers = player_npc_relations.offers
             || jsonb_build_object(%(k)s::text, coalesce(player_npc_relations.offers->%(k)s, '{}'::jsonb) || %(v)s::jsonb
                                   || jsonb_build_object('at', extract(epoch from now())))""",
        {"p": player_id, "t": npc.template.id, "k": key, "v": Jsonb(value)},
    )


def set_offer(conn: Connection, player_id: UUID, npc: Npc, key: str, price: int) -> None:
    """叙事里 NPC 报了价、砍价让了步就记下来：卖货清单里的物品 id，或者已经报过价的现造东西"""
    offers = get_offers(conn, player_id, npc)
    with conn.transaction():
        unlocked = perk_sells(loading.cursor(conn), player_id, npc)
    if key not in npc.template.props.get("sells", []) + unlocked and key not in offers:
        return                              # 稀罕货不在这里：价钱固定，不砍价
    with conn.transaction():
        cur = loading.cursor(conn)
        if key.startswith("made:"):
            stats = offers[key].get("spec", {})
        else:
            cur.execute("select damage, defense, heal, props->'price' as price from item_templates where id = %s", (key,))
            stats = cur.fetchone()
        put_offer(cur, player_id, npc, key, clamp_price(stats, price, markup(npc, key)))


def npc_hand(conn: Connection, player_id: UUID, npc: Npc, key: str, price: int, affinity: int) -> ActionResult:
    """叙事里 NPC 把货递给了玩家（交易那步没给）：照做。key 是卖货的物品 id 或 "made:名字"（做过的货）。
    叙事写了收钱就按那个价（限在建议价一半到两倍）；没写收钱的，交情够就白送，不够按建议价收；钱不够就没给成"""
    try:
        with conn.transaction():
            cur = loading.cursor(conn)
            player = loading.load_player(cur, player_id, lock=True)
            if key.startswith("made:"):
                cur.execute("select spec from npc_goods where npc_template = %s and name = %s", (npc.template.id, key[5:]))
                row = cur.fetchone()
                if row is None:
                    raise ActionError(f"{npc.name}没做过{key[5:]}")
                stats, name = row["spec"], f"{row['spec']['name']}（{item_info.effect_text(row['spec'])}）"
            else:
                if key not in _sells(cur, player_id, npc):
                    raise ActionError(f"{npc.name}不卖这个")
                cur.execute("select name, damage, defense, heal, props->'price' as price from item_templates where id = %s",
                            (key,))
                stats = cur.fetchone()
                name = stats["name"]
                if key == _rare_now(cur, player_id, npc):
                    price = _rare_price(npc, stats)
                    patron = pay(cur, player, price, npc)
                    return ActionResult(action="npc_sell", success=True,
                                        facts=_sell_rare(cur, player, npc, key, name, price) + patron)
            listed = clamp_price(stats, base_price(stats), markup(npc, key))
            price = 0 if price <= 0 and can_gift(affinity) else (
                min(listed, clamp_price(stats, price, markup(npc, key))) if price > 0 and not key.startswith("made:")
                else clamp_price(stats, price if price > 0 else base_price(stats), markup(npc, key)))       # 墙上的货不超过标价
            patron = pay(cur, player, price, npc)
            if key.startswith("made:"):
                _make(cur, player_id, stats)
            else:
                cur.execute("insert into item_instances (template_id, player_id) values (%s, %s)", (key, player_id))
        return ActionResult(action="npc_sell", success=True, facts=[_deal(npc, player, name, price)] + patron)
    except ActionError as e:
        return ActionResult(action="npc_sell", success=False, facts=[str(e)])


SELL_MAX_COUNT = 10                     # 墙上的货一次最多买几件


def npc_sell(conn: Connection, player_id: UUID, npc: Npc, template_id: str, price: Optional[int] = None,
             count: int = 1) -> ActionResult:
    """NPC 卖一件货给玩家（货不限量，每卖一件新造一件）。报过价就按报的价收；玩家直接下单（没报过价）就按 AI 这回合
    给的价，限在建议价的一半到两倍（没给就按建议价）"""
    try:
        with conn.transaction():
            cur = loading.cursor(conn)
            if template_id not in _sells(cur, player_id, npc):
                raise ActionError(f"{npc.name}不卖这个")
            if template_id == _rare_now(cur, player_id, npc):
                # 稀罕货：只有一件，价钱固定
                cur.execute("select name, damage, defense, heal, props->'price' as price from item_templates where id = %s",
                            (template_id,))
                stats = cur.fetchone()
                player = loading.load_player(cur, player_id, lock=True)
                price = _rare_price(npc, stats)
                patron = pay(cur, player, price, npc)
                return ActionResult(action="npc_sell", success=True,
                                    facts=_sell_rare(cur, player, npc, template_id, stats["name"], price) + patron)
        offer = get_offers(conn, player_id, npc).get(template_id)
        with conn.transaction():
            cur = loading.cursor(conn)
            if offer is None:
                # 墙上的货直接下单：按标价收；AI 这回合给的价只能往下（交情好打个折），不能往上抬
                # （以前限在标价的两倍：诺艾尔嘴上说 20，交易那步收了 40）
                cur.execute("select damage, defense, heal, props->'price' as price from item_templates where id = %s",
                            (template_id,))
                stats = cur.fetchone()
                listed = clamp_price(stats, base_price(stats), markup(npc, template_id))
                offer = {"price": min(listed, clamp_price(stats, price or listed, markup(npc, template_id)))}
            player = loading.load_player(cur, player_id, lock=True)
            count = max(1, min(SELL_MAX_COUNT, count))
            patron = pay(cur, player, offer["price"] * count, npc)
            # 成交了这个报价就作废，再买要重新谈
            cur.execute("update player_npc_relations set offers = offers - %s where player_id = %s and npc_template = %s",
                        (template_id, player_id, npc.template.id))
            for _ in range(count):
                cur.execute("insert into item_instances (template_id, player_id) values (%s, %s)", (template_id, player_id))
            cur.execute("select name from item_templates where id = %s", (template_id,))
            name = cur.fetchone()["name"] + (f" ×{count}" if count > 1 else "")
        return ActionResult(action="npc_sell", success=True, facts=[_deal(npc, player, name, offer["price"] * count)] + patron)
    except ActionError as e:
        return ActionResult(action="npc_sell", success=False, facts=[str(e)])


MADE_TEMPLATES = {"food": "made_food", "drink": "made_drink", "misc": "made_misc", "weapon": "made_weapon"}


CREATE_COOLDOWN = "3 minutes"           # 同一个 NPC 白送现造东西给同一个玩家的间隔


# 建议价、报价区间：rules.base_price、clamp_price

def markup(npc: Npc, key: str) -> float:
    """这家店对这样货的加价倍数（props.markup），不加价是 1"""
    return float((npc.template.props.get("markup") or {}).get(key, 1))


# 白送的门槛：NPC 现做的东西、AI 决定给不给的东西（地图），好感够高才白送，不然模型第一次见面就把剑白送了。
# 任务奖励、按条件给的（打完哥布林给钥匙）不受这个限制
GIFT_AFFINITY = 50


def can_gift(affinity: int) -> bool:
    return affinity >= GIFT_AFFINITY


def create_limits(npc: Npc) -> dict[str, dict]:
    """NPC 能现造的种类和上限（world.yaml 的 creates）。老写法 [food, drink] 当成没有数值上限"""
    cfg = npc.template.props.get("creates", {})
    if isinstance(cfg, list):
        cfg = {k: {} for k in cfg}
    return {k: (v or {}) for k, v in cfg.items() if k in MADE_TEMPLATES}


def made_allowed(npc: Npc, spec: dict) -> bool:
    """这个 NPC 现在还做这种东西吗。knockout_only：只调下了药的特调（麦琪放倒闹事的人），别的酒水不现做"""
    caps = create_limits(npc).get(spec.get("kind"))
    return caps is not None and (not caps.get("knockout_only") or bool(spec.get("knockout")))


def made_spec(npc: Npc, kind: str, name: str, description: str, heal: int = 0, harm: int = 0,
              damage: int = 0, knockout: Optional[str] = None, alcohol: bool = False) -> dict:
    """AI 提议的现造东西，按 NPC 的上限裁剪成规格。种类不允许就抛 ActionError"""
    caps = create_limits(npc).get(kind)
    if caps is None:
        raise ActionError(f"{npc.name}做不了这种东西")
    spec = {"kind": kind, "name": name.strip()[:12] or "小玩意", "description": description.strip()[:120]}
    clamp = lambda v, key: max(0, min(int(v or 0), caps.get(key, 0)))
    if kind in ("food", "drink"):
        spec["harm"] = clamp(harm, "harm")
        if knockout and caps.get("knockout"):
            spec["knockout"] = knockout.strip()[:20]
        # 有毒、下了药的就不回血
        spec["heal"] = 0 if spec["harm"] or spec.get("knockout") else clamp(heal, "heal")
        if alcohol and kind == "drink":
            spec["alcohol"] = True              # 酒：喝了可能醉
        if caps.get("knockout_only") and not spec.get("knockout"):
            raise ActionError(f"{npc.name}不现做酒水，吧台上有什么卖什么")
    elif kind == "weapon":
        spec["damage"] = max(1, clamp(damage, "damage"))
    return spec


def creatable_kinds(conn: Connection, player_id: UUID, npc: Npc) -> dict[str, dict]:
    """NPC 能给这个玩家现造的种类和上限。白送有冷却，但买卖（先报价）不受冷却限制，所以这里总是返回"""
    return create_limits(npc)


def _gift_cooldown(cur: Cursor, player_id: UUID, npc: Npc) -> None:
    cur.execute(
        f"""insert into player_npc_relations (player_id, npc_template, last_created_at) values (%s, %s, now())
            on conflict (player_id, npc_template) do update set last_created_at = now()
            where player_npc_relations.last_created_at is null
               or player_npc_relations.last_created_at < now() - interval '{CREATE_COOLDOWN}'
            returning 1""",
        (player_id, npc.template.id),
    )
    if not cur.fetchone():
        raise ActionError(f"{npc.name}刚白送过东西，过一会儿再说")


# 现做的杂物（made_misc）什么用处都没有：名字像有用的东西就不做，不然玩家拿到"麻绳"爬不了、"火把"照不亮
FAKE_MISC_WORDS = ("绳", "钥匙", "火把", "火炬", "灯", "卷轴", "箭", "弹", "药", "水晶", "地图", "绷带", "帐篷", "磨刀石")


def _no_fake(cur: Cursor, npc: Npc, spec: dict) -> None:
    name = spec["name"]
    if spec["kind"] != "misc":
        # 吃的喝的、武器可以现做，但不能跟正式的东西同名（两杯效果不一样的"矮人黑啤"）
        cur.execute("select 1 from item_templates where id not like 'made%%' and name = %s", (name,))
        if cur.fetchone():
            raise ActionError(f"{name}是正经的货，{npc.name}不另做一份")
        return
    cur.execute("""select name from item_templates where id not like 'made%%' and length(name) >= 2
                   and strpos(%s, name) > 0 limit 1""", (name,))
    if cur.fetchone() or any(w in name for w in FAKE_MISC_WORDS):
        raise ActionError(f"{npc.name}现做不出能用的{name}：有用处的东西得是店里正经的货")


def _make(cur: Cursor, player_id: UUID, spec: dict) -> None:
    cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                (MADE_TEMPLATES[spec["kind"]], player_id, Jsonb({k: v for k, v in spec.items() if k != "kind"})))


def npc_gift(conn: Connection, player_id: UUID, npc: Npc, spec: dict) -> ActionResult:
    """NPC 高兴了现造一件白送（请客），不用报价；同一个玩家 3 分钟一次"""
    try:
        with conn.transaction():
            cur = loading.cursor(conn)
            player = loading.load_player(cur, player_id, lock=True)
            _no_fake(cur, npc, spec)
            _gift_cooldown(cur, player_id, npc)
            _make(cur, player_id, spec)
            _remember_goods(cur, npc, spec)
        return ActionResult(action="npc_create", success=True,
                            facts=[f"{npc.name}把{spec['name']}（{item_info.effect_text(spec)}）送给了{player.name}"])
    except ActionError as e:
        return ActionResult(action="npc_create", success=False, facts=[str(e)])


def quote_made(conn: Connection, player_id: UUID, npc: Npc, spec: dict, price: Optional[int] = None) -> ActionResult:
    """要收钱的现造东西先报价：AI 开的价按建议价限幅（没给就按建议价），连同规格记进报价，玩家同意了再照这个规格做"""
    price = clamp_price(spec, price or base_price(spec))
    with conn.transaction():
        cur = loading.cursor(conn)
        player = loading.load_player(cur, player_id)
        _no_fake(cur, npc, spec)
        put_offer(cur, player_id, npc, "made:" + spec["name"], price, spec)
        _remember_goods(cur, npc, spec, price)
    return ActionResult(action="quote", success=True,
                        facts=[f"{npc.name}给{player.name}开价：{spec['name']}（{item_info.effect_text(spec)}），{price} 金币"])


def npc_buy_made(conn: Connection, player_id: UUID, npc: Npc, key: str) -> ActionResult:
    """玩家同意了现造东西的报价：按报价收钱，照报价时的规格做"""
    try:
        offer = get_offers(conn, player_id, npc).get(key)
        if not offer or not offer.get("spec"):
            raise ActionError(f"{npc.name}还没给这件东西报价")
        spec = offer["spec"]
        if not made_allowed(npc, spec):
            raise ActionError(f"{npc.name}现在不做{spec['name']}了")     # 现做关掉以前开过的价
        with conn.transaction():
            cur = loading.cursor(conn)
            player = loading.load_player(cur, player_id, lock=True)
            patron = pay(cur, player, offer["price"], npc)
            _make(cur, player_id, spec)
            _remember_goods(cur, npc, spec, offer["price"])
            cur.execute("update player_npc_relations set offers = offers - %s where player_id = %s and npc_template = %s",
                        (key, player_id, npc.template.id))
        return ActionResult(action="npc_create", success=True,
                            facts=[_deal(npc, player, f"{spec['name']}（{item_info.effect_text(spec)}）", offer["price"])] + patron)
    except ActionError as e:
        return ActionResult(action="npc_create", success=False, facts=[str(e)])


# NPC 做过的东西记下来（按名字），下次有人要同一样就照原来的规格做，不会这回有下回没有
GOODS_SHOWN = 20                        # 给 AI 看最近做过的几样


def _remember_goods(cur: Cursor, npc: Npc, spec: dict, price: Optional[int] = None) -> None:
    cur.execute(
        """insert into npc_goods (npc_template, name, spec, price) values (%s, %s, %s, %s)
           on conflict (npc_template, name) do update set spec = excluded.spec,
               price = coalesce(excluded.price, npc_goods.price), updated_at = now()""",
        (npc.template.id, spec["name"], Jsonb(spec), price))


def known_goods(conn: Connection, npc: Npc) -> list[dict]:
    """NPC 做过、现在还做的东西 [{name, spec, price}]，最近的在前"""
    with conn.transaction():
        cur = loading.cursor(conn)
        cur.execute("select name, spec, price from npc_goods where npc_template = %s order by updated_at desc limit %s",
                    (npc.template.id, GOODS_SHOWN))
        return [g for g in cur.fetchall() if made_allowed(npc, g["spec"] or {})]


def can_eject(npc: Npc) -> bool:
    """world.yaml 里配了 eject_to 的 NPC 能把闹事的玩家轰出去"""
    return bool(npc.template.props.get("eject_to"))


def npc_eject(conn: Connection, player_id: UUID, npc: Npc) -> Optional[ActionResult]:
    """把玩家从 NPC 所在房间的 eject_to 出口轰出去（叙事 AI 判定玩家闹事时调用）"""
    direction = npc.template.props.get("eject_to")
    with conn.transaction():
        cur = loading.cursor(conn)
        player = loading.load_player(cur, player_id, lock=True)
        ex = helpers.find_exit(cur, npc.room_id, direction) if direction else None
        if ex is None or player.room_id != npc.room_id:
            return None
        cur.execute("update players set room_id = %s, following = null, updated_at = now() where id = %s",
                    (ex["to_room"], player_id))
        room = loading.load_room(cur, ex["to_room"])
        cur.execute("insert into events (room_id, player_id, kind, observer) values (%s, %s, 'ejected', %s)",
                    (ex["to_room"], player_id, f"{player.name}被{npc.name}从{loading.load_room(cur, npc.room_id).name}轰了出来。"))
    return ActionResult(action="npc_eject", success=True, facts=[f"{npc.name}把{player.name}轰出了门，{player.name}来到了{room.name}"])
