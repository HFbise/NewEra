"""Turn entry points: execute, execute_all, the action table. / 入口：执行一回合的动作"""
# 这个包里的模块共用一个命名空间：engine/__init__.py 按 MODULES 的顺序加载，每个模块都能直接用别的模块里的名字
# （跟拆分前在同一个文件里一样）。All modules in this package share one namespace; see engine/__init__.py.


HANDLERS: dict[str, Callable[..., list[str]]] = {
    "move": do_move, "look": do_look, "take": do_take, "drop": do_drop, "use": do_use,
    "equip": do_equip, "unequip": do_unequip, "attack": do_attack, "talk": do_talk, "give": do_give, "freeform": do_freeform,
    "say": do_say, "revive": do_revive, "stunt": do_stunt, "struggle": do_struggle,
    "follow": do_follow, "unfollow": do_unfollow, "leave_party": do_leave_party, "kick": do_kick,
    "maneuver": do_maneuver, "dodge": do_dodge, "tame": do_tame, "uncurse": do_uncurse, "reload": do_reload, "refill": do_refill,
    "transfer": do_transfer, "reroll": do_reroll, "rename": do_rename, "write": do_write, "socket": do_socket, "unsocket": do_unsocket, "refine": do_refine, "donate": do_donate, "take_donated": do_take_donated, "dismantle": do_dismantle, "close_eyes": do_close_eyes, "pinch": do_pinch, "hide": do_hide, "search": do_search, "reject": do_reject,
    "upgrade": do_upgrade, "pay": do_pay, "sell": do_sell, "respawn": do_respawn, "stand": do_stand, "rest": do_rest, "camp": do_camp, "teleport": do_teleport, "challenge": do_challenge, "accept_duel": do_accept_duel, "decline_duel": do_decline_duel, "flee": do_flee,
}


def execute(conn: Connection, view: RoomView, action: PlayerAction) -> ActionResult:
    # 两个玩家同时互相动手（互殴、互相急救）会各自先锁自己再锁对方，Postgres 判死锁回滚其中一个，重来一次就行
    for attempt in range(2):
        try:
            with conn.transaction():
                cur = _cursor(conn)
                player = load_player(cur, view.player.id, lock=True)
                if player.hp <= 0 and action.action not in DOWNED_ALLOWED:
                    raise ActionError(f"{player.name}已经倒下了，动弹不得，只能等人急救")
                st = player.status
                if st and (st.kind == "restrained" and action.action in RESTRAINED_BLOCKED
                           or st.kind == "prone" and action.action in PRONE_BLOCKED
                           or st.kind == "incapacitated" and action.action != "struggle"):
                    raise ActionError(f"{player.name}{st.label}，" + ("得先站起来" if st.kind == "prone" else "做不到"))
                if (grab := _flight(cur, player, view, action)) is not None:
                    return ActionResult(action=action.action, success=False, facts=grab)
                facts = HANDLERS[action.action](cur, player, view, action)
                facts += _heat(cur, player.id, action.action)
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
            action.message = slur(action.message)
        result = execute(conn, view, action)
        result.facts[:0], pending = pending, []
        if result.success and dungeon.is_dungeon(view.room.id) and action.action not in ("look", "reject"):
            with conn.transaction():
                result.facts += _floor_clock(_cursor(conn), view.player.id)
        if result.success and action.action in ("use", "dodge", "maneuver", "flee") and dungeon.is_dungeon(view.room.id):
            with conn.transaction():
                result.facts += _add_sin(_cursor(conn), view.player.id, action)
        if result.success and action.action in NOISY and dungeon.is_dungeon(view.room.id):
            with conn.transaction():
                cur = _cursor(conn)
                result.facts += _noise(cur, load_player(cur, view.player.id), NOISY[action.action])
        if action.action in ("equip", "unequip", "drop", "give", "sell"):
            with conn.transaction():
                result.facts += _sync_gear_hp(_cursor(conn), view.player.id)
        results.append(result)
        if not result.success:
            break
        # 在有看店 NPC 的地方伤了人：当场被轰出去，后面的动作不做了。那里不许决斗，一般伤不了人，
        # 整人（捆住、迷眼）不算
        if (action.action in ("attack", "stunt") and action.target not in view.refs
                and any("点伤害" in f for f in result.facts) and (kicked := _keeper_eject(conn, view))):
            result.facts += kicked
            return results
        if enemies and i - since == ENEMY_EVERY:
            enemy, acted = enemy_turn(conn, view.player.id, actions[since:i], results[since:i])
            since = i
            result.facts += enemy
            if acted and i < len(actions):
                result.facts.append(f"{view.player.name}被打断了，后面的动作没来得及做")
                return results
    if enemies and since < len(results):
        results[-1].facts += enemy_turn(conn, view.player.id, actions[since:len(results)], results[since:])[0]
    return results


def _tick(conn: Connection, player_id: UUID, unit: str) -> list[str]:
    with conn.transaction():
        cur = _cursor(conn)
        player = load_player(cur, player_id, lock=True)
        return _tick_effects(cur, player, unit) + (_recover_thrown(cur, player) if unit == "turn" else [])
