"""Turn entry points: execute, execute_all, the action table. / 入口：执行一回合的动作"""
from typing import Callable
from uuid import UUID

from psycopg import Connection, errors as pg_errors

import dungeon
from rules import ENEMY_EVERY
from schema import ActionResult, PlayerAction, RoomView

from .core import ActionError, DOWNED_ALLOWED, PRONE_BLOCKED, RESTRAINED_BLOCKED
from . import (
    afflictions, bosses, combat, duels, environment, equipment, everyday, loading, npc_trade, parties,
    ranged_combat, smith, stealth,
)


HANDLERS: dict[str, Callable[..., list[str]]] = {
    "move": everyday.do_move, "look": everyday.do_look, "take": everyday.do_take, "drop": everyday.do_drop, "use": everyday.do_use,
    "equip": equipment.do_equip, "unequip": equipment.do_unequip, "attack": combat.do_attack, "talk": npc_trade.do_talk, "give": npc_trade.do_give, "freeform": everyday.do_freeform,
    "say": everyday.do_say, "revive": everyday.do_revive, "stunt": combat.do_stunt, "struggle": afflictions.do_struggle,
    "follow": parties.do_follow, "unfollow": parties.do_unfollow, "leave_party": parties.do_leave_party, "kick": parties.do_kick,
    "maneuver": combat.do_maneuver, "dodge": stealth.do_dodge, "tame": combat.do_tame, "uncurse": smith.do_uncurse, "reload": ranged_combat.do_reload, "refill": everyday.do_refill,
    "transfer": smith.do_transfer, "reroll": smith.do_reroll, "rename": smith.do_rename, "write": everyday.do_write, "socket": smith.do_socket, "unsocket": smith.do_unsocket, "refine": smith.do_refine, "donate": smith.do_donate, "take_donated": everyday.do_take_donated, "dismantle": smith.do_dismantle, "close_eyes": combat.do_close_eyes, "pinch": combat.do_pinch, "hide": stealth.do_hide, "search": stealth.do_search, "reject": everyday.do_reject,
    "upgrade": smith.do_upgrade, "pay": npc_trade.do_pay, "sell": npc_trade.do_sell, "respawn": everyday.do_respawn, "stand": afflictions.do_stand, "rest": everyday.do_rest, "camp": everyday.do_camp, "teleport": everyday.do_teleport, "challenge": duels.do_challenge, "accept_duel": duels.do_accept_duel, "decline_duel": duels.do_decline_duel, "flee": duels.do_flee,
}


def execute(conn: Connection, view: RoomView, action: PlayerAction) -> ActionResult:
    # 两个玩家同时互相动手（互殴、互相急救）会各自先锁自己再锁对方，Postgres 判死锁回滚其中一个，重来一次就行
    for attempt in range(2):
        try:
            with conn.transaction():
                cur = loading.cursor(conn)
                player = loading.load_player(cur, view.player.id, lock=True)
                if player.hp <= 0 and action.action not in DOWNED_ALLOWED:
                    raise ActionError(f"{player.name}已经倒下了，动弹不得，只能等人急救")
                st = player.status
                if st and (st.kind == "restrained" and action.action in RESTRAINED_BLOCKED
                           or st.kind == "prone" and action.action in PRONE_BLOCKED
                           or st.kind == "incapacitated" and action.action != "struggle"):
                    raise ActionError(f"{player.name}{st.label}，" + ("得先站起来" if st.kind == "prone" else "做不到"))
                if (grab := bosses.flight(cur, player, view, action)) is not None:
                    return ActionResult(action=action.action, success=False, facts=grab)
                facts = HANDLERS[action.action](cur, player, view, action)
                facts += environment.heat(cur, player.id, action.action)
            return ActionResult(action=action.action, success=True, facts=facts)
        except ActionError as e:
            return ActionResult(action=action.action, success=False, facts=[str(e)])
        except pg_errors.DeadlockDetected:
            if attempt:
                raise


def execute_all(conn: Connection, view: RoomView, actions: list[PlayerAction], enemies: bool = True) -> list[ActionResult]:
    """按顺序执行，前一步失败就中断。玩家每做 ENEMY_EVERY 个动作，同区域的敌人行动一次（发现、逼近、攻击、爬起来），
    一句话结尾不够数也行动一次；facts 接在那一轮最后一个动作后面，results 和执行了的 actions 一一对应。
    敌人有动静就打断剩下的。enemies=False 是战斗回合里：敌人等全队出完手统一行动（round_enemies）"""
    results, since = [], 0                  # since：上一轮敌人行动时玩家做到第几个动作
    pending = _tick(conn, view.player.id, "turn")
    for i, action in enumerate(actions, 1):
        pending += _tick(conn, view.player.id, "action")
        # 喝醉了说话含糊：改的是原话本身，叙事、旁人、NPC 听到的都是醉话
        if view.player.drunk and action.action in ("say", "talk"):
            action.message = environment.slur(action.message)
        result = execute(conn, view, action)
        result.facts[:0], pending = pending, []
        if result.success and dungeon.is_dungeon(view.room.id) and action.action not in ("look", "reject"):
            with conn.transaction():
                result.facts += environment.floor_clock(loading.cursor(conn), view.player.id)
        if result.success and action.action in ("use", "dodge", "maneuver", "flee") and dungeon.is_dungeon(view.room.id):
            with conn.transaction():
                result.facts += bosses.add_sin(loading.cursor(conn), view.player.id, action)
        if result.success and action.action in environment.NOISY and dungeon.is_dungeon(view.room.id):
            with conn.transaction():
                cur = loading.cursor(conn)
                result.facts += environment.make_noise(cur, loading.load_player(cur, view.player.id), environment.NOISY[action.action])
        if action.action in ("equip", "unequip", "drop", "give", "sell"):
            with conn.transaction():
                result.facts += equipment.sync_gear_hp(loading.cursor(conn), view.player.id)
        results.append(result)
        if not result.success:
            break
        # 在有看店 NPC 的地方伤了人：当场被轰出去，后面的动作不做了。那里不许决斗，一般伤不了人，
        # 整人（捆住、迷眼）不算
        if (action.action in ("attack", "stunt") and action.target not in view.refs
                and any("点伤害" in f for f in result.facts) and (kicked := npc_trade.keeper_eject(conn, view))):
            result.facts += kicked
            return results
        if enemies and i - since == ENEMY_EVERY:
            enemy, acted = combat.enemy_turn(conn, view.player.id, actions[since:i], results[since:i])
            since = i
            result.facts += enemy
            if acted and i < len(actions):
                result.facts.append(f"{view.player.name}被打断了，后面的动作没来得及做")
                return results
    if enemies and since < len(results):
        results[-1].facts += combat.enemy_turn(conn, view.player.id, actions[since:len(results)], results[since:])[0]
    return results


def _tick(conn: Connection, player_id: UUID, unit: str) -> list[str]:
    with conn.transaction():
        cur = loading.cursor(conn)
        player = loading.load_player(cur, player_id, lock=True)
        return afflictions.tick_effects(cur, player, unit) + (ranged_combat.recover_thrown(cur, player) if unit == "turn" else [])
