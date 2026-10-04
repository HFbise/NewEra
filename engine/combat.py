"""Attacks, stunts, damage both ways, enemy turns. / 攻击、花样、伤害、敌人的回合"""
# 这个包里的模块共用一个命名空间：engine/__init__.py 按 MODULES 的顺序加载，每个模块都能直接用别的模块里的名字
# （跟拆分前在同一个文件里一样）。All modules in this package share one namespace; see engine/__init__.py.


def _subdued(target: Any) -> bool:
    """被放倒、捆住了，任人摆布（能补刀、能灌药）；只是摔倒在地的还能挣扎，不算"""
    return target.status is not None and target.status.kind != "prone"


def _prone_bonus(target: Any, d: int) -> float:
    """打倒在地上的（被绊倒的）更容易打中；够不着的还是够不着"""
    return PRONE_HIT_BONUS if d in MELEE_HIT and target.status and target.status.kind == "prone" else 0.0


def _hurt_npc(cur: Cursor, player: Player, npc: Npc, dmg: int) -> tuple[list[str], bool]:
    """扣 NPC 血，返回 (facts, 是否死了)。死了掉东西，击杀标记算整支队伍的"""
    if npc.template.props.get("dormant") and not _tally(cur, npc, "awake"):
        _tally_merge(cur, npc, {"awake": 1})          # 挨了打的石像醒了
    if dmg > 0 and _tally(cur, npc, "shield") > 0:
        cur.execute("""update npcs set tally = tally || jsonb_build_object('shield', (tally->>'shield')::int - 1) where id = %s""", (npc.id,))
        return [f"{npc.name}身上那层嗡嗡作响的光膜挡下了这一下，碎了（HP {npc.hp}/{npc.template.max_hp}）"], False
    if npc.template.props.get("skills") and dmg > 0:
        cur.execute("""update npcs set tally = jsonb_set(tally || jsonb_build_object('threat', coalesce(tally->'threat', '{}'::jsonb)),
                         array['threat', %s], to_jsonb(coalesce((tally->'threat'->>%s)::int, 0) + %s)) where id = %s""",
                    (str(player.id), str(player.id), dmg, npc.id))
    hp = max(0, npc.hp - dmg)
    facts = [f"{npc.name} HP {hp}/{npc.template.max_hp}"]
    if hp > 0:
        cur.execute("update npcs set hp = %s where id = %s", (hp, npc.id))
        return facts, False
    return facts + _npc_gone(cur, player, npc), True


def _npc_gone(cur: Cursor, player: Player, npc: Npc, tamed: bool = False) -> list[str]:
    """NPC 没了：被打倒，或者（野兽）被安抚走了。身上的东西掉在地上，击杀标记、金币照给"""
    cur.execute("update npcs set hp = 0, alive = false, died_at = now(), status = null where id = %s", (npc.id,))
    cur.execute("update item_instances set npc_id = null, room_id = %s where npc_id = %s",
                (player.room_id, npc.id))
    facts = [f"{npc.name}平静下来，慢慢走开了" if tamed else f"{npc.name}被击败了"]
    rank = npc.template.props.get("dungeon", {}).get("rank")
    if (gate := npc.template.props.get("gate")) and not tamed:
        depth = str(npc.template.props.get("dungeon", {}).get("depth", 0))
        title = npc.template.props.get("title", "")
        cur.execute("""update players set flags = jsonb_set(jsonb_set(flags - '_sin', '{gates}',
                           coalesce(flags->'gates', '{}'::jsonb) || jsonb_build_object(%s::text, %s::text)),
                         '{gate_fails}', coalesce(flags->'gate_fails', '{}'::jsonb) - %s::text)
                           || jsonb_build_object('titles', (select coalesce(jsonb_agg(distinct x), '[]'::jsonb) from
                               jsonb_array_elements_text(coalesce(flags->'titles', '[]'::jsonb) || to_jsonb(%s::text)) x))
                       where room_id = %s and hp > 0 returning name""", (depth, gate, depth, title, npc.room_id))
        names = [r["name"] for r in cur.fetchall()]
        if names:
            facts.append(f"关卡打通了。{'、'.join(names)}得到了称号「{title}」，往下的楼梯敞开了")
    if rank in ("boss", "elite"):
        dungeon.log_fights(cur, [npc.id], "tamed" if tamed else "killed")
        # 召来、带来的小怪没了主心骨，四散逃走（它们不掉东西，留着只是白打一架）
        cur.execute("""select 1 from npcs n join npc_templates t on t.id = n.template_id
                       where n.room_id = %s and n.alive and t.props->'dungeon'->>'rank' in ('boss', 'elite')""", (npc.room_id,))
        if not cur.fetchone():
            cur.execute("""update npcs n set hp = 0, alive = false, died_at = now(), status = null from npc_templates t
                           where t.id = n.template_id and n.room_id = %s and n.alive and (t.props->>'minion')::boolean
                           returning t.name""", (npc.room_id,))
            if fled := list(dict.fromkeys(r["name"] for r in cur.fetchall())):
                facts.append(f"{'、'.join(fled)}见{npc.name}倒下了，四散逃进了黑暗里")
    if npc.template.props.get("leader") and (kind := npc.template.props.get("pack")):
        cur.execute("""update npcs n set hp = 0, alive = false, died_at = now(), status = null from npc_templates t
                       where t.id = n.template_id and n.room_id = %s and n.alive and t.props->>'pack' = %s
                       returning t.name""", (npc.room_id, kind))
        if fled := list(dict.fromkeys(r["name"] for r in cur.fetchall())):
            facts.append(f"头领一倒，{'、'.join(fled)}夹着尾巴逃进了黑暗里")
    flag = npc.template.props.get("on_death", {}).get("set_flag")
    if flag:
        # 同一房间的队友一起记上标记
        cur.execute(
            """update players set flags = flags || jsonb_build_object(%s::text, true)
               where id = %s or (party_id = %s and room_id = %s) returning name""",
            (flag, player.id, player.party_id, player.room_id),
        )
        mates = [r["name"] for r in cur.fetchall() if r["name"] != player.name]
        if mates:
            facts.append(f"这次击杀算整支队伍的，{'、'.join(mates)}也记了功")
    # 掉金币（world.yaml 的 on_death.gold: [最少, 最多]），给补最后一刀的人
    # 同房间的队友每人一份（组队时怪更肉，钱也得跟上）
    if gold := npc.template.props.get("on_death", {}).get("gold"):
        n = random.randint(*gold)
        if dungeon.is_dungeon(player.room_id):
            n = max(1, round(n * (1 + 0.5 * dark_factor(_light(cur, player.room_id)))))   # 越暗掉得越多
        n = max(1, round(n * (1 + gear_gold(cur, player))))      # 幸运古币、贪婪之戒（只算最好的一件）
        cur.execute("""update players set gold = gold + %s
                       where id = %s or (party_id = %s and room_id = %s and hp > 0) returning name""",
                    (n, player.id, player.party_id, player.room_id))
        mates = [r["name"] for r in cur.fetchall() if r["name"] != player.name]
        facts.append(f"{player.name}{'在' + npc.name + '趴过的角落里翻到了' if tamed else '从' + npc.name + '身上摸到了'} {n} 枚金币"
                     + (f"，{'、'.join(mates)}也各分到 {n} 枚" if mates else ""))
    return facts + _guards_flee(cur, npc.room_id)


def _shield_for(cur: Cursor, npc: Npc) -> Optional[Npc]:
    """挡在 npc 前面的盾卫（props.guard_allies）：同一房间、活着、能动、不是它自己"""
    return next((n for n in _enemies(cur, npc.room_id)
                 if n.id != npc.id and n.status is None and n.template.props.get("guard_allies")), None)


def _guards_flee(cur: Cursor, room_id: str) -> list[str]:
    """盾卫身后的同伴都倒下了：扔下挡板逃跑（不掉钱，身上的东西丢在地上）"""
    left = [n for n in _enemies(cur, room_id)] if room_id else []
    guards = [n for n in left if n.template.props.get("guard_allies")]
    if not guards or len(guards) < len(left):
        return []
    for g in guards:
        cur.execute("update npcs set hp = 0, alive = false, died_at = now(), status = null where id = %s", (g.id,))
        cur.execute("update item_instances set npc_id = null, room_id = %s where npc_id = %s", (room_id, g.id))
    return [f"{'、'.join(g.name for g in guards)}见身后的同伴都倒下了，扔下挡板掉头就跑，一溜烟没了影"]


def _hurt_player(cur: Cursor, target: Player, dmg: int, kind: str, by: str) -> tuple[list[str], bool]:
    """扣玩家血，返回 (facts, 是否倒下)。被打的人不自动反击，要还手得自己出手。
    kind / by 是倒下的原因（见 _downed_by）"""
    hp = max(0, target.hp - dmg)
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (hp, target.id))
    facts = [f"{target.name} HP {hp}/{target.max_hp}"]
    if hp == 0:
        facts.append(f"{target.name}倒下了")
        _downed_by(cur, target, kind, by)
    target.hp = hp
    return facts + _toughen(cur, target, ENDURE_HIT_CHANCE), hp == 0


def _downed_by(cur: Cursor, player: Player, kind: str, by: str) -> None:
    """记下为什么倒下：npc 被怪打倒 / player 被人打倒 / poison 吃了有毒的东西，by 是谁、什么东西。
    看店的 NPC 扶人时按这个说俏皮话（world.yaml 的 revive_lines）。在关卡头目面前倒下记一次失败（下次它的血少一点）"""
    cur.execute("update players set downed_by = %s where id = %s", (Jsonb({"kind": kind, "by": by}), player.id))
    cur.execute("""select t.props->'dungeon'->>'depth' as d from npcs n join npc_templates t on t.id = n.template_id
                   where n.room_id = %s and n.alive and t.props ? 'gate' limit 1""", (player.room_id,))
    if row := cur.fetchone():
        cur.execute("""update players set flags = jsonb_set(flags, '{gate_fails}', coalesce(flags->'gate_fails', '{}'::jsonb)
                         || jsonb_build_object(%s::text, least(%s, coalesce((flags->'gate_fails'->>%s)::int, 0) + 1)))
                       where id = %s""", (row["d"], GATE_REMNANT_MAX, row["d"], player.id))


def _npc_counter(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """NPC 挨打后反击。身上有负面状态就不还手，按施加时的挣脱难度看这回合能不能恢复"""
    if npc.status:
        st = npc.status
        if st.kind == "prone":
            return [f"{npc.name}还倒在地上，没法还手"]
        if _roll(escape_chance(st.escape, st.attempts)):
            _set_status(cur, "npcs", npc.id, None)
            return [f"{npc.name}摆脱了“{st.label}”的状态，但这回合来不及还手"]
        st.attempts += 1
        _set_status(cur, "npcs", npc.id, st)
        return [f"{npc.name}还{st.label}，没法还手"]
    if npc.template.hostile:
        return []                       # 敌人的还手在 enemy_turn 里按距离算
    return _npc_strike(cur, player, npc, "反击")


def _npc_strike(cur: Cursor, player: Player, npc: Npc, verb: str, chance: float = 1.0, mult: float = 1.0) -> list[str]:
    """NPC 打玩家一下（反击、主动攻击），chance 是命中率。player.hp 跟着更新，同一回合几个敌人连着打能接上"""
    if _npc_effect(npc, "blind"):
        chance *= NPC_BLIND_HIT                 # 被墨汁糊了眼的怪
    if not _roll(chance):
        return [f"{npc.name}{verb}，没打中{player.name}"]
    if _fx(_worn(cur, player), "miss_next", when="hurt") and _once_per_fight(cur, player, "miss_next"):
        return [f"{npc.name}{verb}，眼看就要打中，{player.name}头上的盔忽然一暗，整个人从它眼前消失了一瞬：这一下落了空（这一场用过了）"]
    if npc.template.props.get("ranged") and (ref := _fx(_worn(cur, player), "reflect_ranged", when="hurt")) \
            and _roll(max(e.get("chance", 0) for e in ref)):
        back = max(SCALE, npc.template.attack // 2)
        hurt, _ = _hurt_npc(cur, player, npc, back)
        return [f"{npc.name}{verb}，打在{player.name}的盾面上折了回去，自己挨了 {back} 点"] + hurt
    # 影步靴这类：几率完全躲开（几件取最高）；拿着盾（props.block）：几率完全挡下。两样一起掷，合计最多 AVOID_CAP。
    # 挡的只是这一击，中毒、流血这些持续伤害挡不住
    dodges = _fire(cur, player, "hurt", "dodge", npc, roll=False)
    dodge = max((e.get("chance", 1) for e in dodges), default=0.0)
    shield = max((i for i in _worn(cur, player) if _prop(i, "block")), key=lambda i: _prop(i, "block"), default=None)
    block = float(_prop(shield, "block")) if shield else 0.0
    r = random.random()
    if r < min(dodge, AVOID_CAP):
        return [f"{npc.name}{verb}，" + (max(dodges, key=lambda e: e.get("chance", 1)).get("label") or f"被{player.name}躲开了")]
    if r < min(dodge + block, AVOID_CAP):
        return [f"{npc.name}{verb}，被{player.name}用{shield.name}稳稳挡了下来"]
    light = _light(cur, npc.room_id)
    atk = npc.template.attack + (dark_attack(light) if dungeon.is_dungeon(npc.room_id) else 0)
    if npc.template.props.get("light_averse") and light >= LIGHT_BRIGHT:
        atk -= SCALE                            # 怕光的怪在亮处缩手缩脚
    depth = npc.template.props.get("dungeon", {}).get("depth", 0)
    props = npc.template.props
    ratio = npc.hp / npc.template.max_hp if npc.hp and npc.template.max_hp else 1.0
    if props.get("frenzy") and ratio < 0.5:
        atk += props["frenzy"]                  # 狂暴的精英：血量一半以下
    if props.get("dungeon", {}).get("rank") == "boss" and enraged(depth, ratio):
        atk += ENRAGE_ATK                       # 深层头目血少了狂暴
    if props.get("skills"):
        atk += int(_tallies(cur, npc).get("phase_atk", 0))     # 转阶段加的攻击
    atk += int(_stance(cur, npc)[1].get("atk", 0))             # 矮人王的铸像：冷却时手软、熔化时手重
    atk += int(_tallies(cur, npc).get("rung", 0))               # 敲钟人摇过铃
    if mirror := props.get("mirror_attack"):
        mine, _ = gear_totals(player.attack, player.defense, _worn(cur, player))
        atk = round(mine * mirror)                                  # 镜中人：学着你的样子打回来
    marked = props.get("skills") and _tallies(cur, npc).get("mark") == str(player.id)
    if marked:
        atk += int(_tallies(cur, npc).get("mark_bonus", 2))     # 典狱长判了罪的人
    guard = sum(int(e.get("value", 0)) for e in _fire(cur, player, "hurt", "guard", npc))       # 恶犬项圈
    ambush = props.get("ambush") if props.get("ambush") and not _tally(cur, npc, "ambushed") else 1
    if sneak := _tallies(cur, npc).get("sneak"):
        _tally_merge(cur, npc, {"sneak": None})
        ambush *= sneak                                     # 从看不见的地方出手
    if _tallies(cur, npc).get("hide_punish") == str(player.id):
        _tally_merge(cur, npc, {"hide_punish": None})
        ambush *= 1.5
    if ambush != 1:
        _tally_merge(cur, npc, {"ambushed": 1})         # 雾鳗：每场第一口从雾里扑出来
    dmg = scale_damage(hurt_player_by(atk, _defense(cur, player), depth, guard=guard), props.get("dmg_mult", 1) * mult * ambush)
    saved = []
    if dmg >= player.hp and (mark := _carries(cur, player, "guard_once")) and _once_per_floor(cur, player, "cheat_death"):
        return [f"{npc.name}{verb}，这一下本该要了{player.name}的命",
                f"{player.name}身上的{mark.name}亮了一下，硬生生挡下了这一击（这一层用过了）"]
    if dmg >= player.hp and (cd := _fire(cur, player, "hurt", "cheat_death", npc)) \
            and (_once_per_fight(cur, player, "cheat_death") if all(e.get("once") == "per_fight" for e in cd)
                 else _cheat_death_ready(cur, player)):
        dmg, saved = max(0, player.hp - SCALE), [x.replace("你", player.name) for x in _labels(cd)] or [f"{player.name}硬撑着没倒下"]
    player.hp = max(0, player.hp - dmg)
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (player.hp, player.id))
    if saved and any(e.get("cleanse") for e in cd) and (bad := [e for e in player.effects if e.kind not in ("whet", "cheer", "bless")]):
        # 黄金天平：免死的同时清掉身上所有减益（腐蚀扣的血量上限还回去）
        player.effects = [e for e in player.effects if e not in bad]
        _save_effects(cur, player)
        cur.execute("update players set max_hp = max_hp + %s, status = null where id = %s", (sum(e.hp for e in bad), player.id))
        player.max_hp += sum(e.hp for e in bad)
        saved.append(f"{player.name}身上的{'、'.join(EFFECT_NAMES[e.kind] for e in bad)}一下子都散了")
    hit = (f"{npc.name}{verb}，这一下本该要了{player.name}的命" if saved
           else f"{npc.name}{verb}，对{player.name}造成 {dmg} 点伤害")
    facts = [hit, f"{player.name} HP {player.hp}/{player.max_hp}"] + saved
    if props.get("dungeon", {}).get("rank") in ("boss", "elite"):
        cur.execute("""update npcs set tally = tally || jsonb_build_object(
                         'dealt', coalesce((tally->>'dealt')::int, 0) + %s, 'downs', coalesce((tally->>'downs')::int, 0) + %s,
                         'foes', (select coalesce(jsonb_agg(distinct x), '[]'::jsonb)
                                  from jsonb_array_elements_text(coalesce(tally->'foes', '[]'::jsonb) || to_jsonb(%s::text)) x))
                       where id = %s""", (dmg, int(player.hp == 0), player.name, npc.id))
    if marked:
        _tally_merge(cur, npc, {"mark": None})
        facts.append(f"{npc.name}这一下落在了被判罪的人身上，判罪了结")
    if (steal := props.get("lifesteal")) and npc.hp and npc.hp < npc.template.max_hp and (gain := whole(dmg * steal)):
        npc.hp = min(npc.template.max_hp, npc.hp + gain)        # 嗜血的精英：打中回造成伤害的一半
        cur.execute("update npcs set hp = %s where id = %s", (npc.hp, npc.id))
        facts.append(f"{npc.name}舔了舔沾血的爪子，回了 {gain} 点血（HP {npc.hp}/{npc.template.max_hp}）")
    if player.hp == 0:
        facts.append(f"{player.name}倒下了")
        _downed_by(cur, player, "npc", npc.name)
    # 荆棘软甲：扑上来的挨一下（不减防御）
    if reflect := sum(int(e.get("value", 0)) for e in _fire(cur, player, "hurt", "reflect", npc)):
        hurt, _ = _hurt_npc(cur, player, npc, reflect)
        facts += [f"{npc.name}被扎了一下，受到 {reflect} 点伤害"] + hurt
    return facts + _toughen(cur, player, ENDURE_HIT_CHANCE) + _on_hit(cur, player, npc, dmg)


def _enemies(cur: Cursor, room_id: str) -> list[Npc]:
    """在场、活着的敌人"""
    return [n for n in load_npcs(cur, f"n.room_id = %s and n.alive and {AWAKE_SQL}", (room_id,), lock=True)
            if n.template.hostile and n.combatable]


def do_maneuver(cur: Cursor, player: Player, view: RoomView, a: Maneuver) -> list[str]:
    """在同一区域里走近、退开某个 NPC 或决斗对手"""
    steps = max(-MAX_STEP, min(MAX_STEP, a.steps))
    if sand := _sand(cur, player):
        steps = max(-sand.get("move_max", 1), min(sand.get("move_max", 1), steps))
        stuck = sand.get("stuck_chance", 0.3) - sand.get("athletics_step", 0.05) * skill_level(player.skills.get("athletics", 0))
        if steps and _roll(max(0.0, stuck)):
            return [f"{player.name}想挪一步，脚却陷在流沙里拔不出来，原地没动"]
    if a.target not in view.refs:
        other, _ = _room_player(cur, player, a.target)
        duel = _active_duel(cur, player.id, other.id)
        if duel is None:
            raise ActionError(f"{player.name}没有在跟{other.name}决斗，用不着拉开或拉近距离")
        d = max(0, min(MAX_DISTANCE, duel["distance"] - steps))
        cur.execute("update duels set distance = %s where challenger = %s", (d, duel["challenger"]))
        how = f"朝{other.name}靠近了 {steps} 格" if steps > 0 else f"从{other.name}身边退开了 {-steps} 格" if steps else f"在{other.name}附近挪了挪"
        return [f"{player.name}{how}，现在离{other.name} {distance_word(d)}"]             + _stride(cur, player, view, "fight", abs(duel["distance"] - d))
    npc = _room_npc(cur, view, player, a.target)
    st = _stealth(player)
    before = _distance(st, npc)
    d = _set_distance(st, npc, before - steps)
    _save_stealth(cur, player, st)
    how = f"朝{npc.name}靠近了 {steps} 格" if steps > 0 else f"从{npc.name}身边退开了 {-steps} 格" if steps else f"在{npc.name}附近挪了挪"
    return [f"{player.name}{how}，现在离{npc.name} {distance_word(d)}"]         + (_stride(cur, player, view, "fight", abs(before - d)) if npc.template.hostile else [])


# 驯兽：野兽（怪标了 animal）可以安抚，避开这一仗。难度 1 + 层数/6，精英 +1，头目安抚不了。
# 成了这一群同种的野兽平静下来走开，金币、身上的东西照给；不成它们被激怒，马上扑上来先打一下。
# 身上带着肉骨头（麦琪后厨卖的）会先扔一根，难度 -BAIT_EASE
BAIT_EASE = 1


def do_tame(cur: Cursor, player: Player, view: RoomView, a: Tame) -> list[str]:
    npc = _room_npc(cur, view, player, a.target)
    rank = npc.template.props.get("dungeon", {}).get("rank", "normal")
    if not (npc.template.hostile and npc.template.props.get("animal")):
        raise ActionError(f"{npc.name}不是野兽，安抚不了")
    if rank == "boss":
        raise ActionError(f"{npc.name}是这一层的头目，安抚不了")
    pack = [n for n in _enemies(cur, player.room_id) if n.template.id == npc.template.id]
    depth = npc.template.props.get("dungeon", {}).get("depth", 1)
    difficulty = 1 + depth // 6 + (1 if rank == "elite" else 0)
    # 身上有肉骨头（props.bait）就先扔一根过去：难度 -BAIT_EASE，成不成骨头都没了
    bone = next((i for i in view.inventory if _prop(i, "bait")), None)
    thrown = []
    if bone:
        _consume(cur, bone)
        difficulty = max(0, difficulty - BAIT_EASE)
        thrown = [f"{player.name}先把一根{bone.name}扔到{npc.name}跟前，它低头嗅了嗅"]
    ok, facts = _check(cur, player, view, "animal", difficulty)
    facts = thrown + [f"{player.name}{a.description or '放低身子、慢慢靠近，压着嗓子安抚'}{npc.name}"] + facts
    if ok:
        for n in pack:
            facts += _npc_gone(cur, player, n, tamed=True)
        return facts
    # 没安抚住：被发现、贴到跟前，先挨一下
    st = _stealth(player)
    st.detected, st.hidden = True, False
    facts.append(f"{npc.name}被激怒了，龇着牙扑了上来")
    for n in pack:
        _set_distance(st, n, 0)
        if player.hp > 0:
            facts += _npc_strike(cur, player, n, "抢先扑上来", MELEE_HIT[0])
    _save_stealth(cur, player, st)
    return facts


def _beast_hint(cur: Cursor, room_id: str) -> list[str]:
    """进门看到野兽：告诉玩家可以安抚避战（不然没人知道驯兽能这么用）"""
    beasts = list(dict.fromkeys(n.name for n in _enemies(cur, room_id)
                                if n.template.props.get("animal") and n.template.props.get("dungeon", {}).get("rank") != "boss"))
    if not beasts:
        return []
    pack = any(n.template.props.get("pack") for n in _enemies(cur, room_id))
    return [f"{'、'.join(beasts)}是野兽：可以试着安抚它，避开这一仗（说「安抚{beasts[0]}」，看驯兽）；"
            "安抚成了照样有收获，失败会被它抢先扑上来；身上带着肉骨头会先扔一根，容易些"] \
        + (["它们的鼻子一直追着你身上的肉味；领头的那只一倒，剩下的就会夹着尾巴跑"] if pack else [])


HIDE_SEEN, HIDE_CLOSE = 3, 4              # 战斗中躲藏的难度下限：已被发现 / 有怪贴身


HIDDEN_HIT = 0.5                        # 躲起来那一轮，贴身又早发现了他的怪摸黑乱挥，命中减半


EYES_CLOSED = "_eyes_closed"             # players.flags：这一轮闭着眼（敌人回合结束清掉）


def do_close_eyes(cur: Cursor, player: Player, view: RoomView, a: CloseEyes) -> list[str]:
    """闭眼、背过身：这一轮炫光、晃眼的招打不着，自己也看不清"""
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, true) where id = %s", (EYES_CLOSED, player.id))
    player.flags[EYES_CLOSED] = True
    if not _effect(player, "blind"):
        player.effects.append(Effect(kind="blind", value=1, left=1, label="闭着眼", source="自己"))
        _save_effects(cur, player)
    return [f"{player.name}闭上眼睛背过身去：这一轮不怕晃眼的光，可自己也什么都看不见"]


PINCHED = "_pinched"                    # players.flags：这一轮掐了自己（敌人回合结束清掉）


UNLESS_FLAGS = {"eyes_closed": EYES_CLOSED, "pinch": PINCHED}     # 招式的 unless：做了这个动作的人躲得过


UNLESS_WORDS = {"eyes_closed": "闭着眼，没被晃到", "pinch": "掐着自己保持清醒，没睡过去"}


def do_pinch(cur: Cursor, player: Player, view: RoomView, a: Pinch) -> list[str]:
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, true) where id = %s", (PINCHED, player.id))
    player.flags[PINCHED] = True
    return [f"{player.name}狠狠掐了自己一把，疼得一激灵，这一轮怎么也睡不过去"]


def enemy_turn(conn: Connection, player_id: UUID, actions: list[PlayerAction],
               results: list[ActionResult]) -> tuple[list[str], bool]:
    """敌人的一轮（玩家上一轮以来做的 actions）：没发现就掷骰，发现了就动手一次；被绊倒的这一轮用来爬起来。
    返回 (facts, 有没有打断玩家的动静)"""
    done = list(zip(actions, results))
    with conn.transaction():
        cur = _cursor(conn)
        player = load_player(cur, player_id, lock=True)
        st = _stealth(player)
        if any(a.action == "move" and r.success for a, r in done) or all(a.action == "reject" for a, _ in done)                 or player.hp <= 0:
            return [], False            # 刚进门这一下不算；没做成的空话不算；倒下的不管
        alive = _enemies(cur, player.room_id)
        ticked = _tick_npc_effects(cur, player, alive)          # 被装备打中毒、流血的怪先掉血
        alive = [n for n in alive if n.alive]
        if not alive:
            st = Stealth(room=player.room_id, chance=DETECT_START, start=_start_distance(cur, player.room_id))   # 敌人都死了，重新算
        # 被放倒、被捆住的看不见也打不了人，正是偷袭的时候
        enemies = [n for n in alive if n.status is None]
        hid = dodge = False
        seen = st.detected or st.alerted            # 躲之前就被发现过（躲藏已经先存了，看戒备）：贴身的怪这一轮照样摸黑乱挥
        for a, r in done:                           # 按先后顺序：先躲再动手就暴露，动完手再躲成了就藏住
            if a.action in ("attack", "stunt") and r.success:
                st.detected, st.hidden, hid = bool(alive), False, False     # 动了手就暴露了
                seen = True
            elif a.action in ("say", "talk"):
                st.hidden = hid = False                                     # 出声就藏不住了
            elif a.action == "hide" and r.success:
                st.hidden, st.detected, hid = True, False, True
            elif a.action == "dodge" and r.success:
                dodge = True
        if hid and (heard := _heard(alive, done)):
            st.hidden, st.detected, hid, seen = False, True, False, True
            ticked.append(heard.replace("{who}", player.name))
        stood = ticked + _enemies_stand(cur, alive)          # 倒地的这一轮爬起来，不算打断
        if hid and enemies and seen:
            # 躲起来了：别的怪跟丢了他，贴身的那几只摸黑乱挥（命中减半）
            close = [n for n in enemies if _distance(st, n) == 0]
            facts = [f"{'、'.join(n.name for n in close)}就在跟前，朝着{player.name}刚才的位置乱挥"] if close else []
            for npc in close:
                for _ in range(npc.template.props.get("attacks", 1)):
                    facts += _enemy_act(cur, player, st, npc, 0.0, False, HIDDEN_HIT)
                if player.hp <= 0:
                    break
            _save_stealth(cur, player, st)
            return stood + facts, bool(facts)
        if not enemies or hid:
            _save_stealth(cur, player, st)
            return stood, False
        facts = []
        if not st.detected:
            keen = any(n.template.props.get("keen") for n in enemies)     # 狼、恶犬、石像鬼、头目：藏不住
            if keen or _roll(min(1.0, st.chance * _sharpness(enemies) * (FOG_SPOT if _fog(cur, player.room_id) else 1))):
                st.detected, st.hidden = True, False
                facts.append(f"{'、'.join(n.name for n in enemies)}发现了{player.name}")
            elif not st.hidden:
                # 隐匿越高，被发现的几率涨得越慢
                step = max(DETECT_STEP_MIN, DETECT_STEP - 0.01 * skill_level(player.skills.get("stealth", 0))) \
                    + DETECT_PER_FOE * (len(enemies) - 1)
                st.chance = round(min(1.0, st.chance + step), 2)
        # 闪避：察觉越高躲得越好
        dodge_bonus = _evade(player, dodge)
        if _room_env(cur, player.room_id).get("ground") == "water" and not gear_has(cur, player, "wade"):
            dodge_bonus /= 2                    # 积水泥泞，躲不利索（沼泽高筒靴不怕）
        if st.detected:
            pinned = any(a.action in ("attack", "stunt") and r.success for a, r in done)    # 贴身砍中了：远程的怪跳不开
            for npc in [n for n in enemies if not n.template.props.get("inert") and not _away(cur, n)]:
                # 挨打那一下刚摆脱状态的，这回合来不及还手
                if any(f.startswith(f"{npc.name}摆脱了") for r in results for f in r.facts):
                    continue
                skill, acted = _boss_turn(cur, npc, [player], {player.id} if dodge else set())
                facts += _gate_phase(cur, npc) + _memory(cur, npc) + skill
                if acted == "skip":
                    continue
                props = npc.template.props
                for k in range(props.get("attacks", 1) + _extra_acts(cur, npc)):
                    extra = k >= props.get("base_attacks", 99)
                    if acted and not extra:
                        continue                    # 主动作放了技能，副动作照打
                    if extra and not _roll(props.get("extra_chance", 1.0)):
                        continue                    # 迅捷的：多出来的那一下这回没赶上
                    facts += _enemy_act(cur, player, st, npc, dodge_bonus, pinned,
                                        dmg=props.get("extra_mult", 1.0) if extra else 1.0)
                if player.hp <= 0:
                    break
        _save_stealth(cur, player, st)
        _guard_tick(cur, player.id)
        facts += _glare(cur, player, player.room_id) + _sand_sink(cur, player, done)
        facts += _turn_over(cur, player.id, player.room_id) + _room_turn(cur, player.room_id)
        return stood + facts + _smoke_fades(cur, player.room_id), bool(facts)


def _holds_fire(cur: Cursor, player: Player) -> bool:
    cur.execute("""select 1 from item_instances i join item_templates t on t.id = i.template_id
                   where i.player_id = %s and i.equipped_slot is not null and t.props ? 'burning' limit 1""", (player.id,))
    return cur.fetchone() is not None


def _pick_target(cur: Cursor, npc: Npc, cands: list[dict], hits: dict, healers: set) -> dict:
    """怪这一下打谁（cands 是发现了、没藏住的人）：
    头目先打正在给人用药、急救的；野兽扑血最少的；怕光的躲开拿火把的；
    别的先打上一轮打它的人（仇恨），没人打它就随便挑一个"""
    props = npc.template.props
    if props.get("dungeon", {}).get("rank") == "boss" and (h := [c for c in cands if c["p"].id in healers]):
        return random.choice(h)
    if props.get("animal"):
        return min(cands, key=lambda c: (c["p"].hp / max(1, c["p"].max_hp), random.random()))
    if props.get("light_averse") and (dark := [c for c in cands if not _holds_fire(cur, c["p"])]):
        cands = dark
    elif props.get("ranged") and (lit := [c for c in cands if _holds_fire(cur, c["p"])]):
        cands = lit                             # 远程的怪先射拿火把的人：黑暗里最显眼
    if (c := next((c for c in cands if c["p"].id == hits.get(npc.id)), None)) is not None:
        return c
    return random.choice(cands)


def _enemy_act(cur: Cursor, player: Player, st: Stealth, npc: Npc, dodge: float, pinned: bool,
               hit: float = 1.0, dmg: float = 1.0) -> list[str]:
    """hit：命中倍数（躲起来那一轮贴身的怪摸黑乱挥是 HIDDEN_HIT）；dmg：伤害倍数（深层头目第二下）"""
    props = npc.template.props
    if (pull := props.get("pull")) and _count(cur, npc, "pull_acts") % pull.get("every", 3) == 0:
        facts = [f"{npc.name}手一挥，一股看不见的力把{player.name}往外推了出去"]
        if _room_env(cur, player.room_id).get("edge"):
            return facts + _void_fall(cur, player)
        st = _stealth(player)
        for n in _enemies(cur, player.room_id):
            _set_distance(st, n, _distance(st, n) + 1)
        _save_stealth(cur, player, st)
        return facts + [f"（{player.name}被推开了一格）"]
    if (share := props.get("revive_once")) and not _tally(cur, npc, "revived"):
        cur.execute("""select n.id, t.name, t.max_hp from npcs n join npc_templates t on t.id = n.template_id
                       where n.room_id = %s and not n.alive and t.hostile and n.died_at > now() - interval '10 minutes'
                       and not coalesce((t.props->>'minion')::boolean, false) order by n.died_at desc limit 1""", (npc.room_id,))
        if dead := cur.fetchone():
            hp = max(1, round(dead["max_hp"] * share))
            cur.execute("update npcs set alive = true, hp = %s, died_at = null, status = null where id = %s", (hp, dead["id"]))
            _tally_merge(cur, npc, {"revived": 1})
            return [f"{npc.name}甩出一根发光的丝线，把倒下的{dead['name']}一针一针缝了起来：它又站了起来（HP {hp}）"]
    if (bell := props.get("bell")) and _count(cur, npc, "bell_acts") % bell.get("every", 3) == 0:
        # 敲钟人：摇一次铃，这一场全场的怪攻击都加一截，声响跟着涨
        for n in _enemies(cur, npc.room_id):
            _tally_merge(cur, n, {"rung": _tally(cur, n, "rung") + int(bell.get("atk", SCALE))})
        return [f"{npc.name}松开了手，铜铃叮的一声响彻大殿：所有的怪都红了眼（攻击 +{bell.get('atk', SCALE)}）"] \
            + _noise(cur, player, int(bell.get("noise", 3)), heal=False)
    if (sh := props.get("shield_allies")) and _count(cur, npc, "shield_acts") % sh.get("every", 3) == 0:
        # 共鸣者：每出手几次给一个没盾的同伴套一层能挡一下的光膜
        allies = [n for n in _enemies(cur, npc.room_id) if n.id != npc.id and not n.template.props.get("inert")
                  and _tally(cur, n, "shield") <= 0]
        if allies:
            ally = random.choice(allies)
            _tally_merge(cur, ally, {"shield": sh.get("absorb", 1)})
            return [f"{npc.name}嗡的一声，{ally.name}身上罩上了一层光膜（能挡下一次攻击）"]
    if (share := props.get("healer")) and _roll(HEALER_CHANCE) and _tally(cur, npc, "heals") < HEALER_MAX:
        hurt = [n for n in _enemies(cur, npc.room_id)
                if n.id != npc.id and n.hp is not None and n.hp < n.template.max_hp * HEALER_BELOW]
        if hurt:
            n = min(hurt, key=lambda n: n.hp / n.template.max_hp)
            depth = props.get("dungeon", {}).get("depth", 1)
            gain = max(1, math.ceil(dungeon.monster_stats(depth, {})[0] * share))
            hp = min(n.template.max_hp, n.hp + gain)
            cur.execute("update npcs set hp = %s where id = %s", (hp, n.id))
            used = _count(cur, npc, "heals")
            beads = {HEALER_MAX - 1: "，手里的念珠只剩最后一颗了", HEALER_MAX: "，最后一颗念珠碎了，她再也治不了谁"}.get(used, "")
            return [f"{npc.name}朝{n.name}低声念诵，它身上的伤口合上了一些（{n.name} HP {hp}/{n.template.max_hp}）{beads}"]
    d = _distance(st, npc)
    if props.get("ranged"):
        if d == 0 and not pinned:
            if _tally(cur, npc, "backs") < RANGED_BACKS:
                _count(cur, npc, "backs")
                _set_distance(st, npc, 1)
                return [f"{npc.name}往后一跳，拉开了和{player.name}的距离（{distance_word(1)}）"]
            pinned_note = [f"{npc.name}想往后跳，背后却撞上了石壁，退无可退"]
        else:
            pinned_note = []
        room = npc.room_id
        chance = ENEMY_RANGED_HIT.get(d, ENEMY_RANGED_HIT[max(ENEMY_RANGED_HIT)]) - dodge - _cover(cur, room, True)
        if not _holds_fire(cur, player):        # 拿火把的人在暗处最显眼；别人在暗处不好瞄
            chance = light_hit(_light(cur, room), chance)
        chance *= _shot_smoke(cur, room, True)
        if d >= 2 and _fog(cur, room):
            chance *= FOG_FAR                   # 浓雾里隔着几步开外射不准
        return pinned_note + _npc_strike(cur, player, npc, (props.get("verb") or "放了一箭").replace("你", player.name), max(0.0, chance) * hit, dmg)
    facts = []
    if d > 0:
        d = _set_distance(st, npc, d - ENEMY_STEP)
        facts.append(f"{npc.name}逼近过来，离{player.name} {distance_word(d)}")
    if d in MELEE_HIT:
        facts += _npc_strike(cur, player, npc, (props.get("verb") or "扑上来攻击").replace("你", player.name), max(0.0, MELEE_HIT[d] - dodge) * hit, dmg)
    return facts


def _enemies_stand(cur: Cursor, alive: list[Npc]) -> list[str]:
    """被绊倒的敌人轮到行动时只能爬起来，这一轮不扑上来。头目被定住、迷倒、捆住也最多耽误这一轮（不用判定的迷药、
    古书对头目只管一轮），下一轮就挣开了"""
    facts = []
    for n in alive:
        if n.status and n.status.kind == "prone":
            _set_status(cur, "npcs", n.id, None)
            facts.append(f"{n.name}从地上爬了起来")
        elif n.status and n.template.props.get("dungeon", {}).get("rank") == "boss":
            _set_status(cur, "npcs", n.id, None)
            facts.append(f"{n.name}低吼一声，硬生生挣开了“{n.status.label}”（头目只能被困住一轮）")
    return facts


def do_attack(cur: Cursor, player: Player, view: RoomView, a: Attack) -> list[str]:
    # target 是 ref 就是打 NPC，不是 ref 就当玩家名字（PvP）
    # 双持：主手全额，副手加一半。手上有装好的远程武器、对方没贴身（或者是麦琪的弩这种远近都准的）就射
    weapons = _weapons(cur, player)
    want = _inv_item(cur, view, player, a.item) if a.item else None
    if a.target not in view.refs:
        target = _pvp_target(cur, player, a.target)
        duel = _active_duel(cur, player.id, target.id)
        if duel is None:
            raise ActionError(f"{player.name}和{target.name}没有在决斗，不能伤害对方（先申请决斗，对方接受了才行）")
        d = duel["distance"]
        melee, shooter = _choose_weapons(weapons, d, want)
        how, power = _how(player, melee, shooter)
        # 对手上一回合摆好了闪避：这一下更难打中，用掉就没了
        dodged = target.id in duel["dodging"]
        if dodged:
            cur.execute("update duels set dodging = array_remove(dodging, %s) where challenger = %s",
                        (target.id, duel["challenger"]))
        # 装备特效在决斗里也生效；只对怪有用的（vs 亡灵、野兽）对不上人，_fire 自己会跳过
        swung = [e for e in _fire(cur, player, "attack") if e["do"] in ("bonus", "self_damage")]
        ammo = _ammo_fx(cur, shooter, None)
        spent = _spend(cur, player, shooter)
        if not _roll(max(0.0, (_hit_base(shooter, d) - _cover(cur, player.room_id, shooter)) * _shot_smoke(cur, player.room_id, shooter)
                         * _blind(player) - (_dodge_bonus(target) if dodged else 0) - _drunk(player)
                         - _poisoned(player) + _prone_bonus(target, d))):
            return [f"{player.name}{how}{target.name}，" + (f"隔着 {distance_word(d)}够不着" if d not in MELEE_HIT and not shooter
                                                            else f"隔着 {distance_word(d)}，没打中")] \
                + _pvp_self_damage(cur, player, swung) + spent
        fired = _weapon_fit(_fire(cur, player, "hit"), shooter) + ammo
        bonus = whole(sum(e.get("value", 0) for e in swung + fired if e["do"] == "bonus"))
        pierce = whole(sum(e.get("value", 0) for e in fired if e["do"] == "pierce"))
        whet = _effect(player, "whet")
        power = power + (whet.value if whet else 0) + bonus + _aura(cur, player, "attack")
        dmg = hurt_player_by(_bled(player, power), max(0, _defense(cur, target) - pierce))
        facts, down = _hurt_player(cur, target, dmg, "player", player.name)
        return ([f"{player.name}{how}{target.name}，造成 {dmg} 点伤害"]
                + _labels([e for e in fired if e["do"] in ("bonus", "pierce")]) + facts
                + _pvp_hit_extras(cur, player, target, dmg, down, fired) + _pvp_self_damage(cur, player, swung)
                + spent + _duel_over(cur, down))

    npc = _room_npc(cur, view, player, a.target)
    if _away(cur, npc):
        raise ActionError(f"{npc.name}随着星光一起不见了，等暗下来它才会出现")
    if not npc.combatable:
        raise ActionError(f"{npc.name}不是能打的对象")
    d = _distance(_stealth(player), npc) if npc.template.hostile else 0
    melee, shooter = _choose_weapons(weapons, d, want)
    how, power = _how(player, melee, shooter)
    ammo = _ammo_fx(cur, shooter, npc)
    loaded = shooter.props.get("loaded_ammo") if shooter else None      # 射出去就清了，先记下来（火油箭打怕火的）
    spent = _spend(cur, player, shooter)
    if npc.template.hostile:
        # 按距离算命中（近战贴身准，远程离远了准）；敌人以外的 NPC 就在跟前说话，不算距离
        light = _light(cur, player.room_id, player=player)
        if gear_has(cur, player, "darkvision") and not _effect(player, "blind"):
            light = max(light, LIGHT_FULL)      # 石像鬼之眼：暗处命中不打折
        fogged = FOG_FAR if shooter and d >= 2 and _fog(cur, player.room_id, player) else 1.0
        if (ev := npc.template.props.get("dark_evasion")) and _star_dark(cur, player.room_id):
            fogged *= 1 - ev                    # 星光暗时星座兽几乎看不见
        chance = fogged * light_hit(light, _hit_base(shooter, d) - _cover(cur, player.room_id, shooter)) \
            * _shot_smoke(cur, player.room_id, shooter) * _blind(player)
        if not _roll(max(0.0, chance - _drunk(player) - _poisoned(player) + _prone_bonus(npc, d))):
            swung = _fire(cur, player, "attack", npc=npc)
            dim = "，太暗了看不太清" if (d in MELEE_HIT or shooter) and light < LIGHT_FULL else ""     # 让玩家知道是黑的缘故
            return [f"{player.name}{how}{npc.name}，" + (f"隔着 {distance_word(d)}够不着" if d not in MELEE_HIT and not shooter
                                                       else f"隔着 {distance_word(d)}，没打中{dim}")] \
                + _attack_extras(cur, player, npc, [e for e in swung if e["do"] in ("splash", "self_damage")]) + spent
    # 狗头人盾卫：近战砍向它身后的同伴，一半几率被它举着挡板挡下，这一下落在它身上
    guarded = []
    if not shooter and npc.template.hostile and (g := _shield_for(cur, npc)) and _roll(g.template.props["guard_allies"]):
        guarded = [f"{g.name}猛地举起挡板，替{npc.name}挡下了这一下"]
        npc = g
    # 这场仗的第一次出手（伏击者短刀、猎手之戒）
    st = _stealth(player)
    first = not st.struck
    if first:
        st.struck = True
        _save_stealth(cur, player, st)
    swung = _fire(cur, player, "attack", npc=npc)
    fired = _weapon_fit(_fire(cur, player, "hit", npc=npc) + (_fire(cur, player, "fight", npc=npc) if first else []), shooter) + ammo
    bonus = whole(sum(e.get("value", 0) for e in swung + fired if e["do"] == "bonus"))
    pierce = whole(sum(e.get("value", 0) for e in fired if e["do"] == "pierce"))
    corrode = _npc_effect(npc, "corrode")
    armor = max(0, npc.template.defense + int(_stance(cur, npc)[1].get("def", 0)) + _node_guard(cur, npc)
                - pierce - (corrode.value if corrode else 0))
    cracked = bool(npc.template.props.get("quench") and _tallies(cur, npc).get("cracked"))
    if cracked:
        armor = 0                               # 淬火裂开：防御归零
    whet = _effect(player, "whet")
    dmg = _bled(player, hurt_npc_by(power + (whet.value if whet else 0) + bonus + _aura(cur, player, "attack"), armor))
    weak = _weak_spot(cur, player, npc, melee, shooter, loaded, fired)
    if weak or cracked:
        dmg = math.ceil(dmg * WEAK_MULT)
        if npc.template.props.get("weak") == "fire" and npc.template.props.get("skills"):
            _tally_merge(cur, npc, {"singed": True})     # 扎根的古树守卫：被火燎过这一轮长不回来
    # 会心一击（锋墨石）：身上几件只取几率最高的一件掷一次，不叠加
    crits = _fire(cur, player, "hit", "crit", npc, roll=False)
    crit = max(crits, key=lambda e: e.get("chance", 0), default=None)
    critted = bool(crit) and _roll(crit.get("chance", 0))
    if critted:
        dmg = math.ceil(dmg * crit.get("mult", 2))
    if npc.template.props.get("swarm"):
        dmg = max(SCALE, dmg // 2)              # 虫群：一下只拍死一小片
    if npc.template.props.get("grounded_weak") and _tally(cur, npc, "grounded"):
        dmg = round(dmg * npc.template.props["grounded_weak"])      # 刚被拽下来，还没站稳
    if _tally(cur, npc, "vanished"):
        # 隐身：这一下落空；群伤照样打得到，察觉过了能听声辨位
        _tally_merge(cur, npc, {"vanished": 0})
        ok, rolled = _check(cur, player, view, "perception", 2 + npc.template.props.get("dungeon", {}).get("depth", 0) // 10)
        if not ok and not any(e["do"] == "splash" for e in fired):
            return [f"{player.name}{how}{npc.name}刚才站的地方，却扑了个空：他早就不在那儿了"] + rolled
        dmg_note0 = rolled + [f"{player.name}听出了他的位置，这一下结结实实打中了"]
    else:
        dmg_note0 = []
    if npc.template.props.get("lure") and not _tally(cur, npc, "lured"):
        _tally_merge(cur, npc, {"lured": 1})
        ok, rolled = _check(cur, player, view, "perception", 2 + npc.template.props.get("dungeon", {}).get("depth", 0) // 6)
        if not ok:
            return [f"{player.name}{how}{npc.name}，却只打中了它挂在前面的那盏灯，灯晃了一下又亮了"] + rolled
        rolled.append(f"{player.name}看穿了那盏灯只是个饵，一下打在了灯后面的身子上")
        dmg_note = rolled
    else:
        dmg_note = []
    if shooter and (reflect := npc.template.props.get("reflect_ranged")) and _roll(reflect):
        # 棱镜元素：远程的这一下被晶面折了回来，打在自己身上（一半）
        hurt, _ = _hurt_player(cur, player, max(SCALE, dmg // 2), "npc", npc.name)
        return [f"{player.name}{how}{npc.name}，被它身上的晶面折了回来，自己挨了 {max(SCALE, dmg // 2)} 点"] + hurt
    if (same := [e for e in _fx(_worn(cur, player), "bonus_pct", when="hit") if (e.get("if") or {}).get("same_target")]) \
            and player.flags.get("_last_target") == str(npc.id):
        dmg = round(dmg * (1 + max(e.get("value", 0) for e in same)))       # 连着砍同一个：越砍越顺手
    cur.execute("update players set flags = flags || jsonb_build_object('_last_target', %s::text) where id = %s", (str(npc.id), player.id))
    player.flags["_last_target"] = str(npc.id)
    facts, dead = _hurt_npc(cur, player, npc, dmg)
    facts[:0] = dmg_note0 + dmg_note
    if not shooter and not dead and (thorns := npc.template.props.get("thorns"))             and _roll(npc.template.props.get("thorns_chance", 1.0)):
        hurt, _ = _hurt_player(cur, player, thorns, "npc", npc.name)          # 荆棘的精英：近战砍它被扎回来
        facts += [f"{npc.name}身上的硬刺扎了回来，{player.name}受到 {thorns} 点伤害"] + hurt
    if weak:
        facts = [f"正打在{npc.name}的弱点上（{weak}），伤害 ×{WEAK_MULT:g}"] + facts
    if critted:
        facts = [f"会心一击！这一下伤害 ×{crit.get('mult', 2)}"] + facts
    facts += _assassinated(cur, player, view, npc, dead, not st.detected and not st.alerted)
    facts = (guarded + [f"{player.name}{how}{npc.name}，造成 {dmg} 点伤害"]
             + _labels([e for e in fired if e["do"] in ("bonus", "pierce")]) + facts
             + _hit_extras(cur, player, npc, dmg, dead, fired)
             + _attack_extras(cur, player, npc, [e for e in swung if e["do"] in ("splash", "self_damage")]) + spent)
    return facts if dead else facts + _npc_counter(cur, player, npc)


def do_stunt(cur: Cursor, player: Player, view: RoomView, a: Stunt) -> list[str]:
    """借环境、创意动作打人。AI 给的档位先按地形上限裁剪（没声明的地形最多轻伤），打玩家再降一档"""
    cap, feature = IMPROVISED_MAX_TIER, None
    if a.feature:
        uid = view.resolve(a.feature)
        if uid:
            cur.execute("select id, key, name, max_tier, uses_left from room_features where id = %s and room_id = %s for update",
                        (uid, player.room_id))
            feature = cur.fetchone()
        if feature is None:
            raise ActionError("这里没有能这么用的东西")
        if feature["uses_left"] <= 0:
            raise ActionError(f"{feature['name']}已经被用过了，暂时没法再用")
        cap = feature["max_tier"]
    # 背包里的东西也算真东西：拿什么捆人合不合理由 AI 判（绳子、腰带、布条），挣脱难度照 AI 判的，伤害仍按随手的算
    item = _inv_item(cur, view, player, a.item) if a.item else None
    # 捆人得有真东西：环境描述里的东西拿不走，只用它们捆不了人
    if a.status == "restrained" and not feature and not item:
        raise ActionError(f"{player.name}手边没有能用来捆人的东西")
    # 捆上去的东西留在对方身上，泼出去、烧掉的也没了；钥匙、任务物品这类不能拿来当材料
    # 武器、护甲是拿来用的，不是材料（拿剑架住、压住人，剑还在手上）
    material = (item if item and item.template.type not in ("weapon", "armor")
                and (a.consume or a.status == "restrained" and not feature) else None)
    if material and _precious(cur, material):
        raise ActionError(f"{material.name}太要紧了，不能拿来这么用")
    # 拿武器做花样比空手能打得狠：上限放到重伤，打中了再加一半武器伤害。武器按面板算：手上拿着的都算
    # （双持副手按比例），AI 没写用哪件、只要手上有近战武器、这一下没借地形也没用别的东西，也算拿武器做的
    held = [w for w in _weapons(cur, player) if not _prop(w, "ranged") and not _prop(w, "loads")]
    weapon = held or ([item] if item and item.template.type == "weapon" else [])         if (item and item.template.type == "weapon") or (not item and not feature) else []
    if weapon and not feature:
        cap = WEAPON_MAX_TIER
    tier = _cap(a.tier, cap, TIERS)
    escape = a.escape if feature or item else 1       # 随手的东西弄出来的状态都好挣脱

    # 把人推、踹、扔进某个出口：门得开着（同一句里先开门就行）
    exit_ = None
    if a.push:
        exit_ = _find_exit(cur, player.room_id, a.push)
        if exit_ is None:
            raise ActionError(f"这里没有往{dir_name(a.push)}的路")
        if exit_["locked"]:
            raise ActionError(f"往{dir_name(a.push)}的门锁着，推不过去")

    if a.target in view.refs:
        target = _room_npc(cur, view, player, a.target)
        if _away(cur, target):
            raise ActionError(f"{target.name}随着星光一起不见了，等暗下来它才会出现")
        if not target.combatable:
            raise ActionError(f"{target.name}不是能打的对象")
        if exit_:
            raise ActionError(f"{target.name}不肯挪窝，推不走")      # NPC 暂时不能挪房间
    elif exit_:
        target, harmless = _push_target(cur, player, a.target)
        tier, escape = _lower(tier, TIERS), max(1, escape - 1)
        if harmless:                                              # 推队友、拖倒下的人：只挪位置不伤人
            tier, a.status = "none", None
    else:
        target = _pvp_target(cur, player, a.target)
        tier, escape = _lower(tier, TIERS), max(1, escape - 1)
    is_npc = isinstance(target, Npc)
    # 玩家之间没开决斗：整人可以（捆住、迷眼、推出门），但不掉血
    duel = None if is_npc else _active_duel(cur, player.id, target.id)
    harmless_prank = not is_npc and duel is None
    if harmless_prank:
        tier = "none"

    if feature:
        cur.execute("update room_features set uses_left = uses_left - 1, used_at = coalesce(used_at, now()) where id = %s",
                    (feature["id"],))
    env = _room_env(cur, player.room_id)
    doused = _feature_break(cur, player.room_id, feature["name"]) if feature and feature["uses_left"] <= 1 else []
    if feature and (n := _feature_cfg(cur, player.room_id, feature["name"]).get("clears_fog")):
        cur.execute("update rooms set props = jsonb_set(props, '{env,fog_off}', to_jsonb(%s::int)) where id = %s", (n, player.room_id))
        doused.append(f"{feature['name']}一声长鸣，四周的雾被震得散开了（{n} 个回合里看得清）")
    if feature:
        doused += _noise(cur, player, _feature_cfg(cur, player.room_id, feature["name"]).get("noise")
                         or ("break" if feature["max_tier"] in ("heavy", "lethal") else 0))
    if a.tier in ("heavy", "lethal"):
        doused += _noise(cur, player, "heavy_hit")
    if feature and feature["key"] in env.get("lamps", []):
        # 火盆、烛台被拿去砸人：灯灭了，房间暗下来
        dimmer = max(0, _base_light(env) - dungeon.LAMP_LIGHT)
        cur.execute("""update rooms set props = jsonb_set(props, '{env,light}', to_jsonb(%s::int)) where id = %s""",
                    (dimmer, player.room_id))
        doused += [f"{feature['name']}的火光灭了，这里暗了下来（光亮 {dimmer}）"]
    # 拿有毒的酒菜泼、砸别人：东西用掉，砸中了按它的毒性算（伤害、放倒），不按 AI 给的档位
    poison = item if item and item.template.type == "consumable" and (item.harm or item.knockout) else None
    if poison:
        _consume(cur, poison)
    facts = [f"{player.name}尝试：{a.description}"] + doused
    # 泼出去、扔出去、点着的东西不管成没成都没了；捆人的东西成了才留在对方身上
    if material and material is not poison and a.status != "restrained":
        _consume(cur, material)
        facts.append(f"{material.name}用掉了")
    skill, diff, finisher = a.skill, a.difficulty, False
    helpless = _subdued(target) or (not is_npc and target.hp <= 0)
    # 一击毙命、直接打晕（扭断脖子、一拳打晕）：不借地形、不用药，对方又有防备，这种事不可能成。
    # 对方已经倒下、被放倒、被捆住就照常补刀；敌人还没发现你是偷袭，按隐匿判，难度至少 4，得手就照判的档位来
    # 泼出去、撒出去的东西（酒迷眼、石灰）跟下毒一样是实打实的手段，不算徒手一招制敌
    if not feature and not poison and not material and (a.tier == "lethal" or a.status == "incapacitated"):
        # 扭脖子、打晕都是贴身的事，敌人离着几格就得先摸过去
        if is_npc and target.template.hostile and (d := _distance(_stealth(player), target)) > 0:
            raise ActionError(f"离{target.name}还有 {distance_word(d)}，够不着，得先靠近")
        if duel and duel["distance"] > 0:
            raise ActionError(f"离{target.name}还有 {distance_word(duel['distance'])}，够不着，得先靠近")
        if helpless:
            # 对无力反抗的补刀不受徒手最多轻伤的限制（打玩家照旧降一档）
            tier = a.tier if is_npc else _lower(a.tier, TIERS)
            finisher = is_npc
        else:
            sneak = is_npc and target.template.hostile and not _stealth(player).detected and not _stealth(player).alerted
            downed = target.status is not None and target.status.kind == "prone"
            if downed and not sneak:
                # 刚被绊倒在地：趁它爬起来之前下狠手，有机会但不容易
                diff = max(diff, PRONE_DECISIVE_DIFFICULTY)
                facts.append(f"{target.name}倒在地上，正好趁机下狠手")
                tier, finisher = a.tier if is_npc else _lower(a.tier, TIERS), is_npc
            elif not sneak and a.status != "incapacitated":
                tier = _cap(tier, "heavy", TIERS)   # 对有防备的"一剑刺穿"只是描写，按重伤以下算，不是一击毙命
            elif not sneak:
                facts.append(f"{target.name}有防备，想这样一下制住{'它' if is_npc else '他'}根本不可能")
                return facts + (_npc_counter(cur, player, target) if is_npc else [])
            else:
                depth = target.template.props.get("dungeon", {}).get("depth", 0)
                skill, diff = "stealth", max(diff, assassinate_difficulty(depth)) + (KEEN_EXTRA if target.template.props.get("keen") else 0)
                facts.append(f"{target.name}还没发现{player.name}，可以出其不意地偷袭")
                tier, finisher = a.tier, True       # 偷袭得手不受徒手最多轻伤的限制
    if harmless_prank:
        tier = "none"
    # 伤得越重难度下限越高（轻伤 2、重伤 3、致命 4）；对无力反抗的补刀、下毒不算
    if not poison and not helpless:
        diff = max(diff, TIER_MIN_DIFFICULTY[tier])
    if _light(cur, player.room_id, player=player) < LIGHT_DARK or _effect(player, "blind"):
        diff = min(10, diff + 1)                # 漆黑里看不清，做什么都更难
    ok, rolled = _check(cur, player, view, skill, diff)
    facts += rolled
    if not ok:
        facts.append("没有成功")
        return facts + (_npc_counter(cur, player, target) if is_npc else [])

    facts.append("成功了")
    down = stunned = False
    # 偷袭、补刀判成致命就是一击毙命（扭断脖子），其余按档位的区间随机
    lethal_blow = finisher and tier == "lethal"
    double = False
    if lethal_blow and is_npc and target.template.props.get("dungeon", {}).get("rank") in ("elite", "boss"):
        lethal_blow, tier, double = False, "heavy", True
        facts.append(f"{target.name}身板太硬，这一下没能要了它的命，但伤得不轻")
    # 只图伤害的花样（不上状态、不推不撞、没借地形、没用掉东西）打怪：按普攻的算法（面板攻击减防御）再加档位的伤害，
    # 比普攻重一点；带效果的照旧是档位 + 一半武器伤害（好处在效果上）
    pure = (is_npc and tier != "none" and not a.status and not a.push and not a.knockback and not feature and not material
            and not poison and not harmless_prank)
    if pure:
        corrode = _npc_effect(target, "corrode")
        armor = max(0, target.template.defense + int(_stance(cur, target)[1].get("def", 0)) + _node_guard(cur, target)
                    - (corrode.value if corrode else 0))
        if target.template.props.get("quench") and _tallies(cur, target).get("cracked"):
            armor = 0
        swing = hurt_npc_by(_how(player, weapon, None)[1] + random.randint(*TIER_RANGE[tier]), armor)
    if dmg := (0 if harmless_prank else poison.harm if poison else target.hp if lethal_blow
               else _bled(player, swing) if pure
               else _bled(player, random.randint(*TIER_RANGE[tier])
                          + (int(weapon_damage(weapon) * WEAPON_STUNT_SHARE) if weapon and tier != "none" else 0))):
        if double:
            dmg *= 2                            # 对精英、头目的致命偷袭：重伤档的伤害翻倍
        if is_npc and skill == "stealth" and finisher:
            _count(cur, target, "sneaks")       # fight_log：这一场是偷袭打的，校准不拿它算
        if is_npc:
            hurt, down = _hurt_npc(cur, player, target, dmg)
            hurt += _assassinated(cur, player, view, target, down, not _stealth(player).detected and not _stealth(player).alerted)
        else:
            hurt, down = _hurt_player(cur, target, dmg, *(("poison", poison.name) if poison else ("player", player.name)))
        facts += [f"{target.name}受到 {dmg} 点伤害"] + hurt + ([] if is_npc else _duel_over(cur, down))
    immune = is_npc and a.status and a.status in (target.template.props.get("immune") or []) and not down
    resisted = not immune and is_npc and ((poison and poison.knockout) or a.status) and not down and _boss_resists(cur, target)
    if immune:
        facts.append(f"{target.name}不吃这一套（免疫{STATE_NAMES.get(a.status, a.status)}）")
    elif resisted:
        facts.append(f"{target.name}这一仗已经被困住过一回，有了防备，没被放倒")
    elif poison and poison.knockout and not down:
        _knock_out(cur, "npcs" if is_npc else "players", target.id, poison.knockout)
        facts.append(f"{target.name}{poison.knockout}，失去战斗能力")
        stunned = True
    elif a.status and not down:
        st = Status(kind=a.status, escape=escape, since=datetime.now(timezone.utc).isoformat(),
                    label=(a.status_label or "").strip()[:20]
                    or ("被打得失去了战斗能力" if a.status == "incapacitated" else "被困住了"))
        _set_status(cur, "npcs" if is_npc else "players", target.id, st)
        facts.append(f"{target.name}{st.describe()}")
        if material and a.status == "restrained":
            _consume(cur, material)
            facts.append(f"{material.name}用来捆住了{target.name}，留在{'它' if is_npc else '他'}身上")
        stunned = True                               # 刚被放倒、捆住的 NPC 这回合不还手
    if is_npc and not down and a.knockback > 0 and target.template.hostile:
        pst = _stealth(player)
        d = _set_distance(pst, target, _distance(pst, target) + min(MAX_STEP, a.knockback))
        _save_stealth(cur, player, pst)
        facts.append(f"{target.name}被弄开了，离{player.name} {distance_word(d)}")
    elif duel and not down and a.knockback > 0:
        d = min(MAX_DISTANCE, duel["distance"] + min(MAX_STEP, a.knockback))
        cur.execute("update duels set distance = %s where challenger = %s", (d, duel["challenger"]))
        facts.append(f"{target.name}被弄开了，离{player.name} {distance_word(d)}")
    if exit_:
        room = load_room(cur, exit_["to_room"])
        cur.execute("update players set room_id = %s, following = null, updated_at = now() where id = %s",
                    (exit_["to_room"], target.id))
        facts.append(f"{target.name}被弄到了{room.name}")
        # 那边的人（包括被推的人自己）看到他进来；player_id 记推人的，被推的人自己才收得到这条
        cur.execute("insert into events (room_id, player_id, kind, observer) values (%s, %s, 'pushed', %s)",
                    (exit_["to_room"], player.id, f"{target.name}被{player.name}从{load_room(cur, player.room_id).name}弄了进来。"))
        facts += _end_stale_duels(cur)                # 被推走的是决斗对手，决斗就结束了
    elif is_npc and not down and not stunned:
        facts += _npc_counter(cur, player, target)
    return facts


def _push_target(cur: Cursor, player: Player, name: str) -> tuple[Player, bool]:
    """推人能推的玩家，返回 (玩家, 是否只挪位置不伤人)。
    队友和倒下的人也能推（把队友踹进门、把倒下的人拖走），但不伤人；睡着的人不能动"""
    target, awake = _room_player(cur, player, name, lock=True)
    if not awake:
        raise ActionError(f"{target.name}睡着了，不能趁人睡着下手")
    mate = bool(player.party_id and player.party_id == target.party_id)
    return target, mate or target.hp <= 0
