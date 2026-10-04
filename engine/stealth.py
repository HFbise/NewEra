"""Detection, hiding, distances, dodging, searching. / 被发现、躲藏、距离、闪避、搜索"""
from typing import Optional

from psycopg import Cursor
from psycopg.types.json import Jsonb

import dungeon
from rules import LIGHT_DARK, SMOKE_ROUNDS, skill_level
from schema import Dodge, Hide, ItemInstance, Npc, Player, RoomView, Search, Stealth

from .core import (
    ActionError, DETECT_START, DISTANCE_WORDS, DODGE_BONUS, DODGE_PER_LEVEL, FORAGE_CHANCE, FORAGE_COOLDOWN,
    MAX_DISTANCE,
)
from . import afflictions, combat, environment, everyday, helpers, loading


def keen_spotted(cur: Cursor, room_id: str) -> list[str]:
    """房间里警觉的怪（狼、恶犬、石像鬼、所有头目）：一进门就发现人，没有偷偷摸过去这回事"""
    return [n.name for n in combat.enemies_in(cur, room_id) if n.template.props.get("keen") and n.status is None]


def smoke_fades(cur: Cursor, room_id: str) -> list[str]:
    """敌人动了一次：烟散一点，散完了说一声"""
    env = environment.room_env(cur, room_id)
    if not env.get("smoke"):
        return []
    left = env["smoke"] - 1
    cur.execute("update rooms set props = jsonb_set(props, '{env,smoke}', to_jsonb(%s::int)) where id = %s", (left, room_id))
    return [] if left > 0 else ["呛人的灰烟慢慢散开了，又能看清远处了"]


def smoke(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """烟雾弹：摔碎了一大团灰烟，这一轮和下一轮远程命中减半，敌我都算"""
    environment.consume(cur, item)
    cur.execute("""update rooms set props = jsonb_set(props, '{env}', coalesce(props->'env', '{}'::jsonb) || jsonb_build_object('smoke', %s::int))
                   where id = %s""", (SMOKE_ROUNDS, player.room_id))
    return [f"{player.name}把{item.name}往地上一摔，一大团呛人的灰烟腾起来，谁也看不清谁（远程命中减半，撑 {SMOKE_ROUNDS} 轮）"]


def stealth_state(player: Player) -> Stealth:
    """玩家在当前区域的隐蔽情况；记录的是别的区域（刚被人带进来、刚登录）就当刚进门"""
    st = player.stealth
    return st if st and st.room == player.room_id else Stealth(room=player.room_id, chance=DETECT_START)


def distance_word(d: int) -> str:
    return f"{d} 格（{DISTANCE_WORDS.get(d, '离得很远')}）"


def distance(st: Stealth, npc: Npc) -> int:
    # 长在头目身上的（晶簇，props.host）：离头目多远就离它多远
    return st.distance.get(npc.template.props.get("host") or str(npc.id), st.start)


def set_distance(st: Stealth, npc: Npc, d: int) -> int:
    d = max(0, min(MAX_DISTANCE, d))
    st.distance[npc.template.props.get("host") or str(npc.id)] = d
    return d


def save_stealth(cur: Cursor, player: Player, st: Stealth) -> None:
    if st.detected:
        st.alerted = True                   # 发现过一次就一直戒备着，躲起来也只是暂时找不到，偷袭不了
    cur.execute("update players set stealth = %s where id = %s", (Jsonb(st.model_dump()), player.id))


def do_dodge(cur: Cursor, player: Player, view: RoomView, a: Dodge) -> list[str]:
    """闪避：打敌人时在 enemy_turn 里算；决斗中记下来，对手下一次攻击他时生效"""
    cur.execute("""update duels set dodging = array_append(array_remove(dodging, %(p)s), %(p)s)
                   where accepted and (challenger = %(p)s or target = %(p)s)""", {"p": player.id})
    return [f"{player.name}{a.description or '摆好架势，准备闪避'}"]


STILL_ACTIONS = {"hide", "look", "close_eyes", "reject"}     # 不算"动了身子"的（聆听者听不见）


def heard(enemies: list[Npc], done: list) -> Optional[str]:
    """聆听者（props.hearing）：躲着的人这一轮除了躲藏还动了身子，就听得出他在哪"""
    ears = [n for n in enemies if n.template.props.get("hearing") and n.status is None]
    if ears and any(a.action not in STILL_ACTIONS for a, _ in done):
        return f"{ears[0].name}的大耳朵一张，转向了{{who}}：它听见了动静"
    return None


def dodge_bonus(player: Player) -> float:
    """闪避让对方这一下命中率降多少：察觉越高降得越多"""
    return DODGE_BONUS + DODGE_PER_LEVEL * skill_level(player.skills.get("perception", 0))


def evade(player: Player, dodged: bool) -> float:
    """这一轮对方命中率降多少：闪避了按察觉算，另外加许愿池的闪避祝福"""
    return (dodge_bonus(player) if dodged else 0.0) + afflictions.bless(player, "dodge")


PERCEPTION_DETECT = {-1: 0.5, 0: 1.0, 1: 1.5}     # 怪的察觉（迟钝 / 普通 / 敏锐）：被发现的几率乘多少


def _perception(npc: Npc) -> int:
    return max(-1, min(1, int(npc.template.props.get("perception", 0))))


def sharpness(enemies: list[Npc]) -> float:
    """这群怪里最敏锐的那只决定被发现的几率倍数"""
    return PERCEPTION_DETECT[max([_perception(n) for n in enemies] or [0])]


def _keen_line(npc: Npc, who: str) -> str:
    """警觉的怪在场、藏不住：按它为什么警觉换说法（以前一律"鼻子灵"，骷髅也鼻子灵很出戏）"""
    p = npc.template.props
    if p.get("dungeon", {}).get("rank") == "boss":
        return f"{npc.name}早就察觉到{who}了，藏不住"
    if p.get("animal"):
        return f"鼻子灵的{npc.name}一直追着{who}的气味，藏不住"
    if "gargoyle" in npc.template.id:
        return f"{npc.name}一动不动地盯着{who}，藏不住"
    if p.get("undead"):
        return f"{npc.name}空洞的眼眶一直对着{who}，藏不住"
    return f"{npc.name}的眼睛一直跟着{who}，藏不住"


def do_hide(cur: Cursor, player: Player, view: RoomView, a: Hide) -> list[str]:
    """躲起来（隐匿）：成功了几率不再上涨；已经被发现的，躲成功就甩掉了（难度高一级）"""
    st = stealth_state(player)
    foes = [n for n in combat.enemies_in(cur, player.room_id) if n.status is None]
    if hunter := next((n for n in foes if n.template.props.get("hide_fails")), None):
        # 英仙座：在他面前躲藏必定失败，还让他下一击更狠
        helpers.tally_merge(cur, hunter, {"hide_punish": str(player.id)})
        st.detected, st.hidden = True, False
        save_stealth(cur, player, st)
        return [f"{player.name}想找地方藏起来", f"{hunter.name}笑了一声：“你藏的那点本事，是我玩剩下的。”（下一击冲着{player.name}，更狠）"]
    if keen := next((n for n in foes if n.template.props.get("keen")), None):
        raise ActionError(_keen_line(keen, player.name))
    env = environment.room_env(cur, player.room_id)
    easier = bool(env.get("cover")) + (environment.light_level(cur, player.room_id, env) < LIGHT_DARK)
    # 最敏锐的那只怪说了算（迟钝 -1、敏锐 +1）；手里拿着点着的火把 +1
    easier += int((environment.fog_here(cur, player.room_id) or {}).get("stealth_bonus", 0))       # 浓雾里好躲
    diff = a.difficulty + st.detected - easier + max([_perception(n) for n in foes] or [0]) + combat.holds_fire(cur, player)
    # 战斗中躲藏的下限：已经被发现至少 HIDE_SEEN，有怪贴身至少 HIDE_CLOSE（以前 AI 给 1–3，隐匿 4 级 95% 必成）
    if st.detected:
        diff = max(diff, combat.HIDE_SEEN)
    if any(distance(st, n) == 0 for n in foes):
        diff = max(diff, combat.HIDE_CLOSE)
    ok, rolled = helpers.check(cur, player, view, "stealth", max(1, min(10, diff)))
    facts = [f"{player.name}尝试：{a.description or '躲起来'}"] + rolled
    if not ok:
        return facts + [f"{player.name}没能藏好"]
    st.hidden, st.detected = True, False
    save_stealth(cur, player, st)
    for n in foes:
        if st.alerted and n.template.props.get("dungeon", {}).get("rank") in ("boss", "elite"):
            helpers.bump_tally(cur, n, "hides")             # fight_log：打它的时候躲过
    return facts + [f"{player.name}藏好了，暂时没人能发现"]


def do_search(cur: Cursor, player: Player, view: RoomView, a: Search) -> list[str]:
    """四处搜寻：死了的敌人不用等复活时间，直接找出来；房间 forage 里的东西（草药）找到放进背包，
    有冷却的每个人各算各的，不先到先得"""
    facts = [f"{player.name}尝试：{a.description or '四处搜寻'}"]
    # 地牢里走路时听见的怪声：循声找过去，引出一只游荡的怪，它直接扑上来（已经发现了人）
    if view.room.props.get("noise"):
        cur.execute("update rooms set props = props - 'noise' where id = %s", (player.room_id,))
        name = dungeon.spawn_wanderer(cur, player.room_id)
        st = stealth_state(player)
        st.detected, st.hidden = True, False
        save_stealth(cur, player, st)
        return facts + [f"{player.name}循着声音找过去，一只{name}从暗处扑了出来"]
    if found := loading.respawn_npcs(cur, player.room_id, found=True):
        facts.append(f"{player.name}发现了{'、'.join(found)}")
    elif here := [n.name for n in view.npcs if n.template.hostile]:
        facts.append(f"{'、'.join(here)}就在这里")
    for f in view.room.props.get("forage", []):
        cur.execute("select name from item_templates where id = %s", (f["item"],))
        name = cur.fetchone()["name"]
        cur.execute(
            """select 1 from forage_log where player_id = %s and room_id = %s and template_id = %s
               and found_at > now() - make_interval(secs => %s)""",
            (player.id, player.room_id, f["item"], f.get("cooldown", FORAGE_COOLDOWN)),
        )
        if cur.fetchone():
            facts.append(f"附近能找的{name}刚被{player.name}采过，一时找不到新的")
        elif (chance := f.get("chance", FORAGE_CHANCE)) >= 1 or helpers.roll(chance):
            everyday.give_player_new(cur, player, f["item"])
            cur.execute(
                """insert into forage_log (player_id, room_id, template_id) values (%s, %s, %s)
                   on conflict (player_id, room_id, template_id) do update set found_at = now()""",
                (player.id, player.room_id, f["item"]),
            )
            facts.append(f"{player.name}找到了{name}")
        else:
            facts.append(f"{player.name}没找到{name}")
    # 地牢空房里藏着的古币：搜的时候过调查判定，每人一次机会
    if stash := view.room.props.get("stash"):
        cur.execute("""insert into dispenser_log (player_id, room_id, key) values (%s, %s, 'stash')
                       on conflict do nothing returning 1""", (player.id, player.room_id))
        if cur.fetchone():
            ok, rolled = helpers.check(cur, player, view, "investigation", stash["difficulty"])
            facts += rolled
            if ok:
                cur.execute("update players set gold = gold + %s where id = %s", (stash["gold"], player.id))
                facts.append(f"{player.name}在不起眼的角落里发现了藏着的 {stash['gold']} 枚古币")
            else:
                facts.append("这里就算藏着什么，也没被找到")
    return facts if len(facts) > 1 else facts + ["什么也没找到"]
