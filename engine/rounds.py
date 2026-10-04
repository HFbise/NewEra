"""Party combat rounds: queueing, resolving, stuck-round recovery. / 组队战斗回合"""
# 这个包里的模块共用一个命名空间：engine/__init__.py 按 MODULES 的顺序加载，每个模块都能直接用别的模块里的名字
# （跟拆分前在同一个文件里一样）。All modules in this package share one namespace; see engine/__init__.py.


# 房间里有怪、有人被怪发现了就是在战斗：队员的命令先排队，全队都出完手才一起结算（没有倒计时，纯等：文字游戏不该催人；
# 掉线的人（ONLINE_WINDOW 没心跳）不算在"全队"里，不会卡住）。
# 队员按出手先后执行，然后每只怪出手一次（props.attacks 的出手几次），挑谁打看 _pick_target。
# 说话、查看不用排队，马上生效
ROUND_INSTANT = {"say", "look", "reject"}


ROUND_ACTIONS = ENEMY_EVERY             # 一轮里每人最多做几件事（跟平时"做两件事敌人动一次"一样），多的不做


def in_combat(cur: Cursor, room_id: str) -> bool:
    """这个房间在打：有怪发现了人，或者有两个人在决斗"""
    return _monster_fight(cur, room_id) or _duel_here(cur, room_id)


def _duel_here(cur: Cursor, room_id: str) -> bool:
    cur.execute("""select 1 from duels d join players a on a.id = d.challenger join players b on b.id = d.target
                   where d.accepted and a.room_id = %(r)s and b.room_id = %(r)s limit 1""", {"r": room_id})
    return cur.fetchone() is not None


def in_round(cur: Cursor, room_id: str, player_id: UUID) -> bool:
    """这个人的命令要不要排队：房间里有活着的敌人（没被发现也算：偷袭、躲藏在回合里照样判，
    不然每场第一刀即时、被发现以后才变回合，玩家看着一会儿单独一会儿回合），或者他在这里决斗"""
    if _monster_fight(cur, room_id):
        return True
    cur.execute("""select 1 from duels d join players a on a.id = d.challenger join players b on b.id = d.target
                   where d.accepted and %(p)s in (d.challenger, d.target) and a.room_id = %(r)s and b.room_id = %(r)s""",
                {"p": player_id, "r": room_id})
    return cur.fetchone() is not None


def _monster_fight(cur: Cursor, room_id: str) -> bool:
    """房间里有活着的敌人"""
    cur.execute("""select 1 from npcs n join npc_templates t on t.id = n.template_id
                   where n.room_id = %s and n.alive and t.hostile and t.max_hp is not null and """ + AWAKE_SQL + " limit 1",
                (room_id,))
    return cur.fetchone() is not None


def round_members(cur: Cursor, room_id: str) -> list[dict]:
    """这一轮要等谁出手：在这个房间、没倒下、在线，而且在打：被怪发现了的人和他们的队友（队友藏着也等他）、
    这一轮已经出了手的人，或者在这里决斗的两个人。路过的、没被发现、没出手又不是队友的人不等，不然他发呆就把别人的仗卡住了"""
    cur.execute(f"""select id, name from players p where room_id = %(r)s and hp > 0
                    and last_active_at > now() - interval '{ONLINE_WINDOW}'
                    and ((%(fight)s and (p.stealth->>'room' = %(r)s and (p.stealth->>'detected')::boolean
                                         or p.party_id in (select q.party_id from players q where q.room_id = %(r)s
                                                           and q.party_id is not null and q.stealth->>'room' = %(r)s
                                                           and (q.stealth->>'detected')::boolean)
                                         or exists (select 1 from combat_queue cq where cq.room_id = %(r)s and cq.player_id = p.id)))
                         or exists (select 1 from duels d where d.accepted and p.id in (d.challenger, d.target)))
                    order by name""", {"r": room_id, "fight": _monster_fight(cur, room_id)})
    return cur.fetchall()


def queue_round(conn: Connection, player_id: UUID, room_id: str, text: str, actions: list[dict], source: str,
                notes: list[str]) -> None:
    """战斗中出手：先排队（这一轮里再说一次就换成新的）。deadline 在这里只记这一轮什么时候有人先出的手"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute(
            """insert into combat_queue (room_id, player_id, text, actions, source, notes) values (%s, %s, %s, %s, %s, %s)
               on conflict (room_id, player_id) do update
                 set text = excluded.text, actions = excluded.actions, source = excluded.source, notes = excluded.notes,
                     created_at = now()""",
            (room_id, player_id, text, Jsonb(actions), source, Jsonb(notes)))
        cur.execute(
            """insert into combat_rounds (room_id, deadline) values (%s, now())
               on conflict (room_id) do update set deadline = coalesce(combat_rounds.deadline, now())
                 where not combat_rounds.resolving""", (room_id,))


ROUND_STUCK = "3 minutes"               # 结算（连写叙事）超过这么久还没收尾，当成卡住了（服务器中途重启、出错）


def unstick_rounds(conn: Connection, room_id: Optional[str] = None) -> list[str]:
    """卡在"结算中"的回合收掉进下一轮：room_id 给了只看这个房间、超过 ROUND_STUCK 的；
    不给就是服务器刚启动，所有还在结算中的都是上一个进程没做完的，全收掉。返回收掉的房间"""
    with conn.transaction():
        rows = conn.execute(
            f"""select room_id from combat_rounds where resolving
                and (%(room)s::text is null or (room_id = %(room)s
                     and coalesce(resolving_at, deadline, now() - interval '1 day') < now() - interval '{ROUND_STUCK}'))""",
            {"room": room_id}).fetchall()
    rooms = [r[0] for r in rows]
    for r in rooms:
        end_round(conn, r)
    return rooms


def round_due(conn: Connection, room_id: str) -> bool:
    """这一轮该结算了：有人出了手，而且在场（在线、没倒下）的人都出手了"""
    unstick_rounds(conn, room_id)
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("select 1 from combat_rounds where room_id = %s and not resolving and deadline is not null", (room_id,))
        if cur.fetchone() is None:
            return False
        cur.execute("select player_id from combat_queue where room_id = %s", (room_id,))
        queued = {r["player_id"] for r in cur.fetchall()}
        return all(m["id"] in queued for m in round_members(cur, room_id))


def claim_round(conn: Connection, room_id: str) -> Optional[tuple[int, list[dict]]]:
    """开始结算这一轮：(第几轮, 按出手先后排好的命令)。别的线程已经在结算了就是 None"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("update combat_rounds set resolving = true, resolving_at = now() "
                    "where room_id = %s and not resolving and deadline is not null returning round", (room_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("delete from combat_queue where room_id = %s returning player_id, text, actions, source, notes, created_at",
                    (room_id,))
        return row["round"], sorted(cur.fetchall(), key=lambda r: r["created_at"])


def end_round(conn: Connection, room_id: str) -> None:
    """这一轮的叙事写完了：还在打就进下一轮（结算期间已经有人出手了就接着等其他人），打完了、没人排队就收掉"""
    with conn.transaction():
        cur = _cursor(conn)
        fighting = in_combat(cur, room_id)
        cur.execute("select 1 from combat_queue where room_id = %s limit 1", (room_id,))
        queued = cur.fetchone() is not None
        if not fighting and not queued:
            cur.execute("delete from combat_rounds where room_id = %s", (room_id,))
            return
        # 打完了但还有人排着队：也照样结算掉（round_due 看的是在场的人是不是都出手了）
        cur.execute("""update combat_rounds set resolving = false, round = round + 1,
                         deadline = case when %s then now() end
                       where room_id = %s""", (queued, room_id))


def round_info(conn: Connection, player: Player) -> Optional[dict]:
    """给界面看的战斗回合：第几轮、是不是在结算、谁出手了、在等谁"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("select round, resolving from combat_rounds where room_id = %s", (player.room_id,))
        row = cur.fetchone()
        if row is None and not in_combat(cur, player.room_id):
            return None
        cur.execute("select p.name from combat_queue q join players p on p.id = q.player_id where q.room_id = %s",
                    (player.room_id,))
        acted = sorted(r["name"] for r in cur.fetchall())
        members = [m["name"] for m in round_members(cur, player.room_id)]
        return {"round": row["round"] if row else 1, "resolving": bool(row and row["resolving"]),
                "acted": acted, "waiting": [n for n in members if n not in acted]}


def round_enemies(conn: Connection, room_id: str, done: dict[UUID, list[tuple[PlayerAction, ActionResult]]],
                  hits: dict[UUID, UUID], healers: set[UUID]) -> list[str]:
    """一轮里全队出完手之后，敌人统一行动一次：发现没发现的人、倒地的爬起来、每只怪挑一个人逼近或者动手。
    done 是每个人这一轮做了的动作和结果，hits 是每只怪上一下是谁打的，healers 是这一轮给人用药、急救的人"""
    with conn.transaction():
        cur = _cursor(conn)
        # 这一轮出过手的人也算（没被发现的人出手以后，他的排队已经清掉了，不算进来的话敌人就当他不存在）
        ids = list(dict.fromkeys([m["id"] for m in round_members(cur, room_id)] + list(done)))
        players = [load_player(cur, pid, lock=True) for pid in ids]
        players = [p for p in players if p.room_id == room_id and p.hp > 0]
        alive = _enemies(cur, room_id)
        if not players or not alive:
            return []
        facts = _tick_npc_effects(cur, players[0], alive)      # 被装备打中毒、流血的怪先掉血
        alive = [n for n in alive if n.alive]
        enemies = [n for n in alive if n.status is None]        # 被放倒、捆住、绊倒的这一轮不动手
        facts += _enemies_stand(cur, alive)
        freed = {f for acts in done.values() for _, r in acts for f in r.facts if "摆脱了" in f}
        cands = []
        for p in players:
            st = _stealth(p)
            hid = dodge = False
            seen = st.detected or st.alerted
            for a, r in done.get(p.id, []):             # 按先后顺序：先躲再动手就暴露，动完手再躲成了就藏住
                if a.action in ("attack", "stunt") and r.success:
                    st.detected, st.hidden, hid = True, False, False
                    seen = True
                elif a.action in ("say", "talk"):
                    st.hidden = hid = False
                elif a.action == "hide" and r.success:
                    st.hidden, st.detected, hid = True, False, True
                elif a.action == "dodge" and r.success:
                    dodge = True
            if hid and (heard := _heard(enemies, done.get(p.id, []))):
                st.hidden, st.detected, hid, seen = False, True, False, True
                facts.append(heard.replace("{who}", p.name))
            if enemies and not hid and not st.detected:
                keen = any(n.template.props.get("keen") for n in enemies)
                if keen or _roll(min(1.0, st.chance * _sharpness(enemies) * (FOG_SPOT if _fog(cur, room_id) else 1))):
                    st.detected, st.hidden = True, False
                    facts.append(f"{'、'.join(n.name for n in enemies)}发现了{p.name}")
                elif not st.hidden:
                    step = max(DETECT_STEP_MIN, DETECT_STEP - 0.01 * skill_level(p.skills.get("stealth", 0))) \
                        + DETECT_PER_FOE * (len(enemies) - 1)
                    st.chance = round(min(1.0, st.chance + step), 2)
            bonus = _evade(p, dodge)
            if _room_env(cur, room_id).get("ground") == "water" and not gear_has(cur, p, "wade"):
                bonus /= 2
            entry = {"p": p, "st": st, "dodge": bonus, "hit": 1.0}
            if st.detected and not hid:
                cands.append(entry)
            elif hid and seen and any(_distance(st, n) == 0 for n in enemies):
                cands.append(entry | {"hit": HIDDEN_HIT, "close": {n.id for n in enemies if _distance(st, n) == 0}})
            else:
                _save_stealth(cur, p, st)
        for npc in [n for n in enemies if not n.template.props.get("inert") and not _away(cur, n)]:
            if any(f.startswith(f"{npc.name}摆脱了") for f in freed):
                continue                                # 挨打那一下刚摆脱状态的，这一轮来不及还手
            # 头目：该放技能就放（放技能就是这一次出手；判罪标完照样打）
            skill, acted = _boss_turn(cur, npc, [c["p"] for c in cands if c["p"].hp > 0 and "close" not in c],
                                      {c["p"].id for c in cands if c["dodge"] > 0})
            facts += _gate_phase(cur, npc) + _memory(cur, npc) + skill
            if acted == "skip":
                continue
            # 一轮出手几次：一般一次；组队时房间怪满了、折成血的那些多打几次（dungeon._spawn_group）；
            # 深层头目主动作放了技能，副动作照打
            for k in range(npc.template.props.get("attacks", 1) + _extra_acts(cur, npc)):
                if acted and k < npc.template.props.get("base_attacks", 99):
                    continue
                # 躲起来的人只有贴身、早发现他的怪够得着（摸黑乱挥，命中减半）
                live = [c for c in cands if c["p"].hp > 0 and ("close" not in c or npc.id in c["close"])]
                if not live:
                    break
                extra = k >= npc.template.props.get("base_attacks", 99)
                if extra and not _roll(npc.template.props.get("extra_chance", 1.0)):
                    continue                            # 迅捷的：多出来的那一下这回没赶上
                c = _pick_target(cur, npc, live, hits, healers)
                facts += _enemy_act(cur, c["p"], c["st"], npc, c["dodge"], hits.get(npc.id) == c["p"].id, c["hit"],
                                    dmg=npc.template.props.get("extra_mult", 1.0) if extra else 1.0)
        for c in cands:
            _save_stealth(cur, c["p"], c["st"])
        for p in players:
            _guard_tick(cur, p.id)
            facts += _glare(cur, p, room_id) + _sand_sink(cur, load_player(cur, p.id), done.get(p.id, []))
            facts += _turn_over(cur, p.id, room_id)
        return facts + _room_turn(cur, room_id) + _smoke_fades(cur, room_id)
