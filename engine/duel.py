"""Player-versus-player duels. / 玩家决斗"""
# 这个包里的模块共用一个命名空间：engine/__init__.py 按 MODULES 的顺序加载，每个模块都能直接用别的模块里的名字
# （跟拆分前在同一个文件里一样）。All modules in this package share one namespace; see engine/__init__.py.


def _end_stale_duels(cur: Cursor) -> list[str]:
    """清掉已经结束的决斗：有一方离开了决斗的地方、倒下了，或者申请过期了。返回结束了的决斗说明"""
    cur.execute(
        f"""delete from duels d using players a, players b
            where a.id = d.challenger and b.id = d.target
              and (a.room_id <> d.room_id or b.room_id <> d.room_id or a.hp <= 0 or b.hp <= 0
                   or not d.accepted and d.created_at < now() - interval '{DUEL_WINDOW}')
            returning d.accepted, a.name as a, b.name as b""")
    return [f"{r['a']}和{r['b']}的决斗结束了" for r in cur.fetchall() if r["accepted"]]


def _active_duel(cur: Cursor, player_id: UUID, other_id: Optional[UUID] = None) -> Optional[dict]:
    """player 正在打的决斗（已接受）：challenger、target、distance、opponent（对手名字）；
    给了 other_id 就只认跟这个人的"""
    cur.execute(
        """select d.challenger, d.target, d.distance, d.dodging, o.name as opponent from duels d
           join players o on o.id = case when d.challenger = %(p)s then d.target else d.challenger end
           where d.accepted and (d.challenger = %(p)s or d.target = %(p)s)
             and (%(o)s::uuid is null or o.id = %(o)s::uuid)""",
        {"p": player_id, "o": other_id})
    return cur.fetchone()


def _duel_over(cur: Cursor, down: bool) -> list[str]:
    """有人被打倒了：决斗当场结束"""
    return _end_stale_duels(cur) if down else []


def _keeper_here(cur: Cursor, room_id: str) -> Optional[Npc]:
    """这里有管事的 NPC（能把人轰出去的麦琪、莉娜，醒着）就不许决斗"""
    return next((n for n in load_npcs(cur, "n.room_id = %s and n.alive and n.status is null", (room_id,))
                 if can_eject(n) and not n.template.hostile), None)


def do_challenge(cur: Cursor, player: Player, view: RoomView, a: Challenge) -> list[str]:
    target, awake = _room_player(cur, player, a.target)
    if keeper := _keeper_here(cur, player.room_id):
        raise ActionError(f"这里是{keeper.name}的地盘，她不许有人在这里决斗")
    if not awake:
        raise ActionError(f"{target.name}睡着了，没法接受决斗")
    if target.hp <= 0 or player.hp <= 0:
        raise ActionError(f"{target.name}已经倒下了")
    if player.party_id and player.party_id == target.party_id:
        raise ActionError(f"{target.name}是{player.name}的队友，要决斗得先退队")
    if _active_duel(cur, player.id):
        raise ActionError(f"{player.name}正在决斗，打完才能再申请")
    cur.execute(
        """insert into duels (challenger, target, room_id) values (%s, %s, %s)
           on conflict (challenger) do update set target = excluded.target, room_id = excluded.room_id,
                                                  accepted = false, created_at = now()""",
        (player.id, target.id, player.room_id))
    return [f"{player.name}向{target.name}申请决斗，等{target.name}接受", DUEL_RULES]


def _pending_from(cur: Cursor, player: Player, name: Optional[str]) -> dict:
    """别人向自己发起、还没回应的决斗申请；没给名字就只能有一个"""
    cur.execute(
        f"""select d.challenger, p.name from duels d join players p on p.id = d.challenger
            where d.target = %s and not d.accepted and d.created_at > now() - interval '{DUEL_WINDOW}'
              and (%s::text is null or p.name = %s)""",
        (player.id, name, name))
    rows = cur.fetchall()
    if not rows:
        raise ActionError(f"{name}没有向{player.name}申请决斗，或者申请已经过期" if name
                          else f"没有人向{player.name}申请决斗，或者申请已经过期")
    if len(rows) > 1:
        raise ActionError(f"好几个人都向{player.name}申请了决斗（{'、'.join(r['name'] for r in rows)}），得说清楚是谁")
    return rows[0]


def do_accept_duel(cur: Cursor, player: Player, view: RoomView, a: AcceptDuel) -> list[str]:
    row = _pending_from(cur, player, a.target)
    challenger, _ = _room_player(cur, player, row["name"], lock=True)
    if _active_duel(cur, player.id) or _active_duel(cur, challenger.id):
        raise ActionError("已经有一方在决斗了，打完才能再接受")
    if keeper := _keeper_here(cur, player.room_id):
        raise ActionError(f"这里是{keeper.name}的地盘，她不许有人在这里决斗")
    cur.execute("update duels set accepted = true, distance = %s, room_id = %s, created_at = now() where challenger = %s",
                (DUEL_DISTANCE, player.room_id, challenger.id))
    return [f"{player.name}接受了{challenger.name}的决斗，两人相隔 {distance_word(DUEL_DISTANCE)}", DUEL_RULES]


def do_decline_duel(cur: Cursor, player: Player, view: RoomView, a: DeclineDuel) -> list[str]:
    row = _pending_from(cur, player, a.target)
    cur.execute("delete from duels where challenger = %s", (row["challenger"],))
    return [f"{player.name}拒绝了{row['name']}的决斗申请"]


def do_flee(cur: Cursor, player: Player, view: RoomView, a: Flee) -> list[str]:
    """逃跑（运动，难度按距离）：决斗的发起者逃掉了决斗就结束；被挑战的一方不用判定。
    被敌人缠住时按离得最近的敌人算，逃掉了就甩开了（不再算被发现），可以离开"""
    duel = _active_duel(cur, player.id)
    facts = [f"{player.name}尝试：{a.description or '逃跑'}"]
    if duel is None:
        st = _stealth(player)
        foes = [n for n in _enemies(cur, player.room_id) if n.status is None]
        if not (st.detected and foes):
            raise ActionError(f"{player.name}没有在决斗，也没被敌人缠住，用不着逃跑")
        near = min(_distance(st, n) for n in foes)
        if diff := FLEE_DIFFICULTY.get(near):
            diff += _room_env(cur, player.room_id).get("ground") == "water" and not gear_has(cur, player, "wade")
            diff = max(1, diff + int(gear_add(cur, player, "flee")))           # 狼皮靴 -1、铁皮甲 +1
            ok, rolled = (True, [f"（{player.name}脚下的飞鞋一振，怎么都追不上）"]) if gear_has(cur, player, "flee_always") \
                else _check(cur, player, view, "athletics", diff)
            facts += rolled
            if not ok:
                return facts + [f"{player.name}没能甩开{'、'.join(n.name for n in foes)}"]
        st.detected = st.hidden = False
        st.chance = DETECT_START
        _save_stealth(cur, player, st)
        return facts + [f"{player.name}甩开了{'、'.join(n.name for n in foes)}，可以离开了"]
    if duel["target"] == player.id:
        return facts + [f"{player.name}是被挑战的一方，随时可以直接走开，离开这里决斗就结束"]
    if diff := FLEE_DIFFICULTY.get(duel["distance"]):
        ok, rolled = _check(cur, player, view, "athletics", diff)
        facts += rolled
        if not ok:
            return facts + [f"隔着 {distance_word(duel['distance'])}，{player.name}没能从{duel['opponent']}面前脱身，决斗还在继续"]
    cur.execute("delete from duels where challenger = %s", (player.id,))
    return facts + [f"{player.name}从{duel['opponent']}面前脱身了，决斗结束，可以离开了"]


def _room_player(cur: Cursor, player: Player, name: str, lock: bool = False) -> tuple[Player, bool]:
    """同房间的其他玩家（按名字），返回 (玩家, 是否醒着)"""
    if name == player.name:
        raise ActionError(f"{player.name}没法对自己这么做")
    cur.execute(
        f"""select id, coalesce(last_active_at > now() - interval '{ONLINE_WINDOW}', false) as awake
            from players where room_id = %s and name = %s""",
        (player.room_id, name),
    )
    row = cur.fetchone()
    if row is None:
        raise ActionError(f"{name}不在这里")
    return load_player(cur, row["id"], lock=lock), row["awake"]


def _pvp_target(cur: Cursor, player: Player, name: str) -> Player:
    """PvP 能打的玩家：不能打倒下的、睡着的、队友"""
    target, awake = _room_player(cur, player, name, lock=True)
    if target.hp <= 0:
        raise ActionError(f"{target.name}已经倒下了")
    if not awake:
        raise ActionError(f"{target.name}睡着了，不能趁人睡着下手")
    if player.party_id and player.party_id == target.party_id:
        raise ActionError(f"{target.name}是{player.name}的队友，不能攻击队友")
    return target
