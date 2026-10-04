"""Dungeon event-room services (wish pool, mirror hall, memory door, ...). / 地牢事件房的服务"""
import random
import re

from psycopg import Cursor
from psycopg.types.json import Jsonb

import dungeon
from rules import SCALE, stat_text
from schema import Dispenser, Effect, Npc, Player

from .core import ActionError
from . import afflictions, equipment, everyday, helpers, loading, smith, stealth


WISHES = [("atk_pct", 15, "许愿池的祝福（攻击 +15%）"), ("dodge", 10, "许愿池的祝福（对方命中 -10%）"),
          ("regen", 3, "许愿池的祝福（每回合回 3% 血）")]


# 回忆之门里看到的往事（草稿，按 NPC 设定还会再改）
MEMORY_FRAGMENTS = {
    "innkeeper": ("挂着锡杯的门", "很多年前的野猪酒馆，一个扎着小辫的女孩垫着木箱才够得着酒桶。一个满脸伤疤的老冒险者把空锡杯推到她面前，"
                  "说以后这杯归她倒。女孩踮着脚倒满，洒了一半，老头哈哈大笑，一口喝干。那只锡杯后来一直挂在吧台后面"),
    "smith": ("挂着锻锤的门", "一间炉火很旺的铁匠铺，一个小女孩蹲在炉边，用捡来的碎铁敲出一把歪歪扭扭的小刀。一双大手把刀拿过去看了很久，"
              "没说话，第二天她的小板凳旁边多了一把刚好趁手的小锤"),
    "shopkeeper": ("挂着旧书的门", "一个下雨的傍晚，一个瘦小的女孩缩在旧书店最里面的书架之间，读到天都黑了也没发觉。店主没有赶她走，"
                   "只是悄悄在她身边放了一盏灯，又把门上的牌子翻成了“打烊”"),
}


def bottle(cur: Cursor, player: Player, d: Dispenser) -> list[str]:
    """漂流瓶：随机一张别人写过的纸条（没有就是空的），另外有几率捡到 bonus_item"""
    cur.execute("""select props from item_instances where props ? 'note' and coalesce(player_id::text, '') <> %s
                   order by random() limit 1""", (str(player.id),))
    row = cur.fetchone()
    cur.execute("select id from item_templates where props ? 'writable' limit 1")
    tpl = cur.fetchone()["id"]
    props = {k: v for k, v in (row["props"] if row else {}).items() if k in ("note", "writable", "name")}
    cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)", (tpl, player.id, Jsonb(props)))
    facts = [f"{player.name}拔开瓶塞，倒出一张" + (f"写了字的纸条：“{props['note']}”" if props.get("note") else "空白的纸条")]
    if (b := d.extra.get("bonus_item")) and helpers.roll(b.get("chance", 0)):
        everyday.give_player_new(cur, player, b["item"])
        cur.execute("select name from item_templates where id = %s", (b["item"],))
        facts.append(f"瓶身上挂着的{cur.fetchone()['name']}也一起到了手里")
    return facts


def memory_door(cur: Cursor, player: Player, d: Dispenser) -> list[str]:
    """回忆之门：推开一扇门，看见那个人很久以前的一段往事；第一次看到某个人的，好感 +3"""
    seen = set(player.flags.get("_memories") or [])
    keys = [k for k in d.extra.get("npcs", list(MEMORY_FRAGMENTS)) if k in MEMORY_FRAGMENTS]
    key = random.choice([k for k in keys if k not in seen] or keys)
    door, text = MEMORY_FRAGMENTS[key]
    cur.execute("select id, name from npc_templates where id = %s", (key,))
    t = cur.fetchone()
    facts = [f"{player.name}推开了{door}", text]
    if key not in seen and t:
        cur.execute("""insert into player_npc_relations (player_id, npc_template, affinity) values (%s, %s, %s)
                       on conflict (player_id, npc_template) do update set affinity = least(100, player_npc_relations.affinity + %s)""",
                    (player.id, key, 3, 3))
        cur.execute("update players set flags = jsonb_set(flags, '{_memories}', %s) where id = %s", (Jsonb(sorted(seen | {key})), player.id))
        facts.append(f"（{player.name}好像更懂{t['name']}了一点：好感 +3）")
    return facts


def wish(cur: Cursor, player: Player, d: Dispenser) -> list[str]:
    """许愿池：付 cost_gold × 层数的钱，这一层随机一个小祝福；整层声响已经到 fail_if_noise_at_least 就不灵（钱照付）"""
    depth = dungeon.parse_room(player.room_id)[1]
    cost = int(d.extra.get("cost_gold", 10)) * depth
    if player.gold < cost:
        raise ActionError(f"投一次要 {cost} 金币，{player.name}身上不够")
    cur.execute("update players set gold = gold - %s where id = %s", (cost, player.id))
    noise = int(dungeon.floor_state(cur, player.room_id).get("noise", 0))
    if noise >= int(d.extra.get("fail_if_noise_at_least", 99)):
        return [f"{player.name}往池里投了 {cost} 金币，钱币沉下去时却溅起了一圈水花：这一层太吵了，愿望不灵（声响 {noise}）"]
    stat, pct, label = random.choice(WISHES)
    afflictions.give_bless(cur, player, stat, pct, label)
    return [f"{player.name}往池里轻轻投了 {cost} 金币，钱币无声地沉了下去", f"{player.name}得到了{label}，出了这一层就散"]


def cleanse(cur: Cursor, player: Player) -> list[str]:
    """忏悔室：清掉身上所有减益（腐蚀扣的血量上限还回去），回血量上限的 15%"""
    bad = [e for e in player.effects if e.kind not in ("whet", "cheer", "bless")]
    back = sum(e.hp for e in bad)
    player.effects = [e for e in player.effects if e not in bad]
    afflictions.save_effects(cur, player)
    gain = round((player.max_hp + back) * 0.15)
    cur.execute("update players set max_hp = max_hp + %s, hp = least(max_hp + %s, hp + %s), status = null where id = %s",
                (back, back, gain, player.id))
    return [f"{player.name}在帘子后面轻声说完了自己做过的事，木板后面的人叹了口气",
            f"{player.name}身上的晦气散了" + (f"（{'、'.join(afflictions.EFFECT_NAMES[e.kind] for e in bad)}都消退了）" if bad else "")
            + f"，回了 {gain} 点血"]


def read_stele(cur: Cursor, player: Player, d: Dispenser) -> list[str]:
    """禁言碑：下一层进去时整层地图都知道；代价是接下来一阵子禁声（说不了话、念不了书卷）"""
    depth = dungeon.parse_room(player.room_id)[1]
    cur.execute("update players set flags = flags || jsonb_build_object('_reveal_next', %s::int) where id = %s", (depth + 1, player.id))
    ss = d.extra.get("self_status") or {}
    player.effects = [e for e in player.effects if e.kind != "silence"] + [
        Effect(kind="silence", value=1, left=int(ss.get("actions", 10)), label="读完碑文说不出话", source="禁言碑")]
    afflictions.save_effects(cur, player)
    return [f"{player.name}一行行读完了碑文，下一层的路都记在了心里", f"读完以后{player.name}喉咙发紧，很长一段时间都说不出话来（禁声）"]


def mirror_duel(cur: Cursor, player: Player, d: Dispenser) -> list[str]:
    """镜厅：镜子里走出一个照着他打出来的倒影（血六成，攻防跟他一样），打死了掉宝石和晶粉"""
    f = dungeon.floor_info(cur, player.room_id)
    atk, df = equipment.gear_totals(player.attack, player.defense, loading.load_items(cur, "i.player_id = %s", (player.id,)))
    cfg = next((e for e in dungeon.data()["events"].values() if e.get("take", {}).get("service") == "mirror_duel"), {})
    name = dungeon.spawn_mirror(cur, player.room_id, player.id, player.name, max(SCALE, round(player.max_hp * 0.6)), atk, df,
                                f["depth"], f["theme"], cfg.get("take", {}).get("reward", ["gem"]))
    st = stealth.stealth_state(player)
    st.detected, st.hidden = True, False
    stealth.save_stealth(cur, player, st)
    return [f"{player.name}对着镜子看了一眼，镜子里的人却没有跟着眨眼，一步跨了出来：是{name}"]


def free_upgrade(cur: Cursor, player: Player, d: Dispenser) -> list[str]:
    """未熄的锻炉（service: free_upgrade）：照着锻造纹敲，手上的主武器免费升一级；满级了、手上没武器就给一份碎铁"""
    weapon = next((w for w in helpers.weapons(cur, player) if w.equipped_slot == "right_hand"), None) \
        or next(iter(helpers.weapons(cur, player)), None)
    if weapon is None or weapon.props.get("plus", 0) >= smith.upgrade_cap(player, weapon):
        everyday.give_player_new(cur, player, smith.SCRAP)
        return [f"{player.name}照着锻造纹敲了一阵，" + ("手上的兵器已经锻到头了" if weapon else "手上没拿兵器")
                + "，只敲下来一份好铁（碎铁）"]
    stat, level = "damage", weapon.props.get("plus", 0) + 1
    name = re.sub(r" \+\d+$", "", weapon.name) + f" +{level}"
    new = weapon.damage + smith.step(weapon, stat)
    cur.execute("update item_instances set props = (props - 'upgrade_fails') || %s where id = %s",
                (Jsonb({"plus": level, stat: new, "name": name}), weapon.id))
    return [f"{player.name}照着铁砧上的锻造纹，把{weapon.name}放进炉火里敲打了一番",
            f"{weapon.name}变成了{name}，伤害 {stat_text(new)}（没花钱）"]


def stele(cur: Cursor, npc: Npc, player: Player) -> list[str]:
    """地窖石碑（world.yaml props.leaderboard）：看它、跟它说话，碑面上都会显出到过地牢最深处的人，
    还有这个人能直接传送到哪几层"""
    if not npc.template.props.get("leaderboard"):
        return []
    cur.execute("select waypoints from players where id = %s", (player.id,))
    points = cur.fetchone()["waypoints"]
    return [dungeon.leaderboard(cur), f"碑面上还浮现出给{player.name}的字：{dungeon.waypoints_text(points)}"]
