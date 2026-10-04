"""Boss skills: telegraphs, combos, phases, stances, nodes, gate bosses. / 头目技能、阶段、关卡头目"""
# 这个包里的模块共用一个命名空间：engine/__init__.py 按 MODULES 的顺序加载，每个模块都能直接用别的模块里的名字
# （跟拆分前在同一个文件里一样）。All modules in this package share one namespace; see engine/__init__.py.


def _douse_npc(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: str) -> list[str]:
    """泼泉水：熔化状态的矮人王铸像淬火裂开（到它下一次出手：防御归零、挨的伤害 ×1.5，那一次出手跳过）；
    怕水的怪（炉火精）重伤档 ×1.5；泼别的只是泼湿了"""
    npc = _room_npc(cur, view, player, target)
    _consume(cur, item)
    facts = [f"{player.name}把{item.name}泼向{npc.name}"]
    p = npc.template.props
    if p.get("quench") and _stance(cur, npc)[0] == "molten":
        _tally_merge(cur, npc, {"cracked": True, "stance": p["quench"].get("then_stance", "cold"), "stance_at": _tally(cur, npc, "acts")})
        return facts + [p["quench"].get("label", f"{npc.name}淬火裂开了"),
                        f"（{npc.name}裂开了：它下一次出手之前防御归零，挨的伤害 ×{WEAK_MULT:g}，那一次出手也会跳过）"]
    if p.get("quench"):
        return facts + [f"泉水泼在冰冷的青铜上，顺着纹路淌了下去，什么事也没有（得等它烧红、熔化的时候泼）"]
    if p.get("weak") != "water":
        return facts + [f"{npc.name}只是被泼湿了，什么事也没有"]
    dmg = math.ceil(random.randint(*TIER_RANGE["heavy"]) * WEAK_MULT)
    hurt, _ = _hurt_npc(cur, player, npc, dmg)
    return facts + [f"{npc.name}尖叫着缩成一团，冒起一大股白烟，受到 {dmg} 点伤害"] + hurt


def _node_guard(cur: Cursor, npc: Npc) -> int:
    """头目身上的晶簇还有活着的：头目防御加 nodes.while_alive.def（先敲掉晶簇）"""
    cfg = npc.template.props.get("nodes")
    if not cfg:
        return 0
    cur.execute("""select 1 from npcs n join npc_templates t on t.id = n.template_id
                   where n.room_id = %s and n.alive and (t.props->>'node')::boolean limit 1""", (npc.room_id,))
    return int((cfg.get("while_alive") or {}).get("def", 0)) if cur.fetchone() else 0


def _stance(cur: Cursor, npc: Npc) -> tuple[str, dict]:
    """头目现在的状态（props.stances：冷却 / 熔化轮换），(名字, {def, atk})；没有状态轮换的是 ("", {})"""
    st = npc.template.props.get("stances")
    if not st:
        return "", {}
    name = _tallies(cur, npc).get("stance") or st.get("start", "cold")
    return name, st.get(name, {})


def _boss_resists(cur: Cursor, npc: Npc, mark: bool = True) -> bool:
    """头目同一场只吃一次控制（定身、迷倒、捆住、绊倒，本来就最多困一轮）：第一次记下来，第二次起不管用。
    好感回礼的古书、迷药在普通战斗里照样好用，只是头目战不能靠它们一轮轮地平推"""
    if npc.template.props.get("dungeon", {}).get("rank") != "boss":
        return False
    if _tally(cur, npc, "held") >= 1:
        return True
    if mark:
        _count(cur, npc, "held")
    return False


def _by(npc: Npc, label: str, you: str = "众人") -> str:
    """头目招式的说法：去掉开头的"他""她""它"接在名字后面，"你"换成挨招的人"""
    return npc.name + (label[1:] if label[:1] in "他她它" else label).replace("你", you)


def _you_of(cur: Cursor, npc: Npc, s: dict, targets: list) -> str:
    """招式说法里的"你"是谁：只打一个人的就是那个人，全场的是众人"""
    if "你" not in (s.get("label") or "") and "你" not in str(s):
        return "众人"
    if s.get("target", "all") == "all" or not targets:
        return "众人"
    hit = _boss_targets(cur, npc, s.get("target"), targets)
    return hit[0].name if len(hit) == 1 else "众人"


def _fare(cur: Cursor, npc: Npc, targets: list[Player], depth: int, theme: str, dodging: set) -> tuple[list[str], bool]:
    """渡魂者（props.fare）：每出手 every 次伸手要钱（gold_per_depth × 层数），这一下不打人；
    下一次出手前有人付了（「给渡魂者 N 金币」），他收手；没人付就是一记重击（refuse）"""
    fare = npc.template.props.get("fare")
    t = _tallies(cur, npc)
    if t.get("fare_due"):
        _tally_merge(cur, npc, {"fare_due": None, "fare_paid": None})
        if t.get("fare_paid"):
            return [f"{npc.name}把钱收进斗篷里，船桨慢慢放了下来（这一下不打了）"], True
        return [f"没人给钱。{npc.name}把船桨高高抡了起来"] + _skill_effect(cur, npc, fare["refuse"], targets, depth, theme, dodging), True
    if _count(cur, npc, "fare_acts") % fare.get("every", 4) == 0:
        gold = int(fare.get("gold_per_depth", 5)) * depth
        _tally_merge(cur, npc, {"fare_due": gold})
        return [_by(npc, fare.get("label", "伸手要钱")) + f"（船费 {gold} 金币：说「给{npc.name} {gold} 金币」）"], True
    return [], False


GATE_REMNANT, GATE_REMNANT_MAX = 0.05, 3      # 关卡头目：每失败一次下次它的血 -5%，最多三次（-15%）


SIN_SOURCES = {"consume": ("use",), "dodge": ("dodge",), "retreat": ("maneuver",), "flee": ("flee",)}


def _sin(player: Player) -> int:
    s = player.flags.get("_sin") or {}
    return int(s.get("n", 0)) if s.get("room") == player.room_id else 0


def _set_sin(cur: Cursor, player: Player, n: int) -> None:
    player.flags["_sin"] = {"room": player.room_id, "n": n}
    cur.execute("update players set flags = flags || jsonb_build_object('_sin', %s::jsonb) where id = %s",
                (Jsonb(player.flags["_sin"]), player.id))


def _add_sin(cur: Cursor, player_id: UUID, action) -> list[str]:
    """在称心的头目（props.sin）面前逃避审判：吃喝、闪避、后退、逃跑，罪 +1（最多 max）"""
    player = load_player(cur, player_id)
    judge = next((n for n in _enemies(cur, player.room_id) if n.template.props.get("sin")), None)
    if not judge:
        return []
    cfg = judge.template.props["sin"]
    kinds = {a for src in cfg.get("sources", []) for a in SIN_SOURCES.get(src, ())}
    if action.action not in kinds or (action.action == "maneuver" and getattr(action, "steps", 0) >= 0):
        return []
    n = min(int(cfg.get("max", 6)), _sin(player) + 1)
    _set_sin(cur, player, n)
    return [f"（天平上{player.name}那一端又沉了一点：罪 ×{n}，{judge.name}称心时每层重 40%）"]


def _gate_phase(cur: Cursor, npc: Npc) -> list[str]:
    """关卡头目（props.phases）：第一次出手前算失败的余威；血掉到下一阶段的 at 以下，换一套招，
    进阶段的 label、env、summon、actions、atk、on_hit 一起生效"""
    props = npc.template.props
    phases = props.get("phases")
    if not phases or npc.hp is None:
        return []
    t = _tallies(cur, npc)
    facts = []
    if not t.get("remnant_done"):
        cur.execute("select coalesce(max((flags->'gate_fails'->>%s)::int), 0) as n from players where room_id = %s",
                    (str(props["dungeon"]["depth"]), npc.room_id))
        fails = min(GATE_REMNANT_MAX, cur.fetchone()["n"])
        _tally_merge(cur, npc, {"remnant_done": 1})
        if fails:
            cut = round(npc.template.max_hp * GATE_REMNANT * fails)
            npc.hp = max(1, npc.hp - cut)
            cur.execute("update npcs set hp = %s where id = %s", (npc.hp, npc.id))
            facts.append(f"{npc.name}身上还留着上次砍出的伤（失败的余威：血少了 {round(GATE_REMNANT * fails * 100)}%，"
                         f"HP {npc.hp}/{npc.template.max_hp}）")
    k = int(t.get("gphase", 0))
    if k + 1 >= len(phases) or npc.hp / npc.template.max_hp >= phases[k + 1]["at"]:
        return facts
    ph = phases[k + 1]
    upd = {"gphase": k + 1, "pending": None, "used": [], "rnd": {}}
    if ph.get("actions"):
        upd["extra_acts"] = int(t.get("extra_acts", 0)) + ph["actions"]
    if ph.get("atk"):
        upd["phase_atk"] = int(t.get("phase_atk", 0)) + ph["atk"]
    if ph.get("on_hit"):
        upd["on_hit"] = ph["on_hit"]
    _tally_merge(cur, npc, upd)
    facts.append(_by(npc, ph.get("label", "换了一副架势")))
    env = ph.get("env") or {}
    if env:
        info = props.get("dungeon", {})
        facts += _skill_effect(cur, npc, {"do": "phase", "env": env}, [], info.get("depth", 1), info.get("theme", ""))
    if sm := ph.get("summon"):
        info = props.get("dungeon", {})
        names = dungeon.spawn_minions(cur, npc.room_id, info.get("depth", 1), sm["kind"], info.get("theme", ""), sm.get("count", 1))
        facts.append(f"{'、'.join(names)}加入了战斗（这些是跟着{npc.name}来的，它一倒下就会跑）")
    if ph.get("actions"):
        facts.append(f"（{npc.name}的动作快了，一轮多出手 {ph['actions']} 次）")
    return facts


def _phase_skills(props: dict, t: dict, acts: int) -> tuple[list[dict], dict]:
    """这一阶段的招（关卡头目按 gphase 取），every_random 换成"到第几次出手就放"（tally.rnd 记下一次）。
    返回 (招, 新的 rnd)"""
    skills = props["phases"][int(t.get("gphase", 0))]["skills"] if props.get("phases") else props.get("skills") or []
    rnd = dict(t.get("rnd") or {})
    out = []
    for i, s in enumerate(skills):
        if s.get("when") == "every_random":
            lo, hi = s["value"]
            if str(i) not in rnd:
                rnd[str(i)] = acts + random.randint(lo, hi)
            s = {**s, "when": "at_act", "value": rnd[str(i)], "every_random": [lo, hi]}
        out.append(s)
    return out, rnd


def _memory(cur: Cursor, npc: Npc) -> list[str]:
    """永不醒来的人（props.memories）：血掉到 at 以下，房间整个换成另一段记忆（名字、光亮、掩体、环境物件、灼热）"""
    mems = npc.template.props.get("memories") or []
    done = int(_tally(cur, npc, "memory"))
    ratio = npc.hp / npc.template.max_hp if npc.template.max_hp else 1
    if done >= len(mems) or ratio >= mems[done]["at"]:
        return []
    m = mems[done]
    r = m.get("room") or {}
    _tally_merge(cur, npc, {"memory": done + 1})
    env = {"light": 40 + dungeon.LIGHT_OFFSET.get(r.get("light", "dim"), 0), "cover": bool(r.get("cover")), "shift": 0,
           **({"heat": r["heat"]} if r.get("heat") else {})}
    cur.execute("""update rooms set name = %s, props = jsonb_set(props, '{env}', coalesce(props->'env', '{}'::jsonb) || %s)
                   where id = %s""", (f"第 {dungeon.parse_room(npc.room_id)[1]} 层·{r.get('name', '一段记忆')}", Jsonb(env), npc.room_id))
    cur.execute("delete from room_features where room_id = %s", (npc.room_id,))
    for i, ft in enumerate(r.get("features") or []):
        cur.execute("""insert into room_features (room_id, key, name, max_tier, max_uses, uses_left, respawn_seconds)
                       values (%s, %s, %s, %s, 1, 1, %s)""", (npc.room_id, f"m{done}{i}", ft["name"], ft["max_tier"], dungeon.FEATURE_RESPAWN))
    return [_by(npc, m.get("label", "四周的一切都变了")),
            f"（这里变成了{r.get('name', '另一段记忆')}：光亮 {env['light']}" + ("，有掩体" if env["cover"] else "")
            + ("，炉火烫人（每个动作掉血）" if r.get("heat") else "") + "）"]


def _boss_turn(cur: Cursor, npc: Npc, targets: list[Player], dodging: Optional[set] = None) -> tuple[list[str], bool]:
    """头目这一次出手前：该放技能就放（rules.due_skill），返回 (facts, 这次出手是不是已经用掉了)。
    预告过的大招这一次结算：预告以后挨了血量上限 INTERRUPT_SHARE 以上的伤害就被打断（打断靠重创，不算控制）"""
    props = npc.template.props
    skills = props.get("skills")
    if not skills or not targets or npc.hp is None:
        return [], False
    if (fly := _tally(cur, npc, "flying")) > 0:
        _tally_merge(cur, npc, {"flying": fly - 1})         # 飞鞋：盘旋的回合一轮轮过去
    if _tally(cur, npc, "grounded"):
        _tally_merge(cur, npc, {"grounded": 0})
    if muted := _tally(cur, npc, "muted"):
        _tally_merge(cur, npc, {"muted": muted - 1})        # 被禁了声：这一下只能普通地打
        return [], False
    if props.get("fare"):
        info = props.get("dungeon", {})
        fared, took = _fare(cur, npc, targets, info.get("depth", 1), info.get("theme", ""), dodging or set())
        if took:
            return fared, True
    t = _tallies(cur, npc)
    acts, used = t.get("acts", 0), set(t.get("used", []))
    upd: dict = {"acts": acts + 1}
    skills, rnd = _phase_skills(props, t, acts)
    upd["rnd"] = rnd
    if t.get("silence"):
        upd["silence"] = t["silence"] - 1
    info = props.get("dungeon", {})
    depth, theme = info.get("depth", 1), info.get("theme", "mine")
    dodging = dodging or set()
    regen_facts = []
    if t.get("cracked"):
        # 淬火裂开：这一次出手跳过，裂口合上
        _tally_merge(cur, npc, upd | {"cracked": False})
        return [f"{npc.name}身上的裂口还冒着白气，这一下动弹不得"], "skip"
    if st := props.get("stances"):
        name = t.get("stance") or st.get("start", "cold")
        if acts - t.get("stance_at", 0) >= st.get("every", 3):
            name = "molten" if name == "cold" else "cold"
            upd |= {"stance": name, "stance_at": acts}
            regen_facts.append(f"{npc.name}{st[name].get('label', '')}")
    if regen := t.get("regen"):
        # 扎根（古树守卫转阶段）：每轮回血，被火打中的那一轮不回
        if t.get("singed"):
            upd["singed"] = False
            regen_facts.append(f"{npc.name}身上被火燎过的地方冒着烟，这一轮没能长回来")
        elif npc.hp < npc.template.max_hp:
            npc.hp = min(npc.template.max_hp, npc.hp + math.ceil(npc.template.max_hp * regen))
            cur.execute("update npcs set hp = %s where id = %s", (npc.hp, npc.id))
            regen_facts.append(f"{npc.name}的根须吸着地底的水，伤口慢慢合上（HP {npc.hp}/{npc.template.max_hp}）")
    if (pend := t.get("pending")) is not None:
        upd["pending"] = None
        s = skills[pend["i"]]
        if pend["hp"] - npc.hp >= math.ceil(npc.template.max_hp * INTERRUPT_SHARE):
            upd["interrupted"] = t.get("interrupted", 0) + 1
            _tally_merge(cur, npc, upd)
            return regen_facts + [f"{npc.name}挨了这一下重的，聚起来的招一下子散了（打断了）"], True
        _tally_merge(cur, npc, upd)
        return regen_facts + _skill_effect(cur, npc, s["then"], targets, depth, theme, dodging), True
    i = due_skill(skills, npc.hp / npc.template.max_hp, acts, used, bool(t.get("phased")), _star_dark(cur, npc.room_id),
                  _light(cur, npc.room_id))
    if i is None:
        _tally_merge(cur, npc, upd)
        return regen_facts, False
    s = skills[i]
    if er := s.get("every_random"):
        upd["rnd"] = rnd | {str(i): acts + random.randint(*er)}      # 下一次隔几下再放，猜不准
    upd["casts"] = t.get("casts", []) + [s["do"]]
    if s.get("when") in ("hp_below", "fight_start"):
        upd["used"] = sorted(used | {i})
    if s["do"] == "telegraph":
        upd["pending"] = {"i": i, "hp": npc.hp}
        _tally_merge(cur, npc, upd)
        hint = STRIKE_HINT if (s.get("then") or {}).get("do") == "strike" else ""
        return regen_facts + [_by(npc, s.get('label', '在蓄一招大的'), _you_of(cur, npc, s.get("then") or {}, targets)) + hint], True
    if s["do"] == "mark":
        target = random.choice(targets)
        upd |= {"mark": str(target.id), "mark_bonus": s.get("bonus", 2)}
        _tally_merge(cur, npc, upd)
        return [f"{npc.name}{s.get('label', '盯上了一个人')}（{target.name}被盯上了：挨它的打 +{s.get('bonus', 2)}，"
                f"直到它打中一次）"], False             # 标完照样打
    if s["do"] == "silence":
        upd["silence"] = s.get("turns", 2)
        _tally_merge(cur, npc, upd)
        return [f"{npc.name}{s.get('label', '禁了声')}（它接下来出手 {s.get('turns', 2)} 次之内，谁都念不了书和卷轴）"], True
    _tally_merge(cur, npc, upd)
    return regen_facts + _skill_effect(cur, npc, s, targets, depth, theme, dodging), True


STRIKE_HINT = "（这一轮狠狠砍它一下能打断；被它盯上的人「闪避」能躲开一半）"


HARD_CONTROL = {"restrained", "prone", "stun", "wrapped"}


def _boss_targets(cur: Cursor, npc: Npc, mode: str, targets: list[Player]) -> list[Player]:
    """蓄力重击、组合技打谁：all 全场、highest_threat 这一场打它最多的人、marked 被判罪的人、lowest_hp 血量比例最低的人"""
    live = [p for p in targets if p.hp > 0]
    if not live or mode == "all":
        return live
    t = _tallies(cur, npc)
    if mode == "marked" and (m := next((p for p in live if str(p.id) == t.get("mark")), None)):
        return [m]
    if mode == "lowest_hp":
        return [min(live, key=lambda p: p.hp / max(1, p.max_hp))]
    threat = t.get("threat") or {}
    return [max(live, key=lambda p: threat.get(str(p.id), 0))]


def _skill_effect(cur: Cursor, npc: Npc, s: dict, targets: list[Player], depth: int, theme: str,
                  dodging: Optional[set] = None) -> list[str]:
    """头目技能真正起作用的那一下：叫帮手、全场上状态、全场中毒腐蚀、回血、蓄力重击、组合技、转阶段"""
    facts = [_by(npc, s["label"], _you_of(cur, npc, s, targets))] if s.get("label") else []
    do = s["do"]
    dodging = dodging or set()
    if do == "vanish":
        _tally_merge(cur, npc, {"vanished": 1, "sneak": s.get("sneak_mult", 1.8)})
        return facts
    if do == "flight":
        _tally_merge(cur, npc, {"flying": s.get("rounds", 2) + 1})
        return facts
    if do == "petrify":
        for p in _boss_targets(cur, npc, s.get("target", "all"), targets):
            if (flag := UNLESS_FLAGS.get(s.get("unless"))) and load_player(cur, p.id).flags.get(flag):
                facts.append(f"{p.name}紧紧闭着眼，没有看那道光")
                continue
            facts += _inflict(cur, load_player(cur, p.id), "stun", "被那道光照成了一尊石像", depth, npc.name, escape=1)
        return facts
    if do == "strike":
        # 蓄力重击：被瞄准的人闪避减半，写了 cover_halves 的全场大招躲在掩体后面减半
        cover = bool(_room_env(cur, npc.room_id).get("cover"))
        atk = npc.template.attack + int(_tallies(cur, npc).get("phase_atk", 0))
        hit = _boss_targets(cur, npc, s.get("target", "highest_threat"), targets)
        if below := s.get("only_below"):
            # 斩首：只砍血量低于 only_below 的人，没人伤得够重就落空
            hit = [p for p in hit if p.hp / max(1, p.max_hp) < below]
            if not hit:
                return facts + ["镰刀划过一道弧线落了空：没有人伤得够重"]
        for p in hit:
            dmg = round(hurt_player_by(atk, _defense(cur, p), depth) * s.get("mult", 2.0))
            if per := s.get("per_sin"):
                sin = _sin(p)
                dmg = round(dmg * (1 + per * sin))      # 称心：罪越重越疼
                if sin:
                    facts.append(f"天平上{p.name}那一端重重地沉了下去（罪 ×{sin}）")
            note = ""
            if p.id in dodging:
                dmg, note = dmg // 2, f"，{p.name}闪开了大半"
            elif s.get("cover_halves") and cover:
                dmg, note = dmg // 2, f"，{p.name}躲在掩体后面挡掉了一半"
            hurt, _ = _hurt_player(cur, p, max(SCALE, dmg), "npc", npc.name)
            facts += [f"{npc.name}这一击落在{p.name}身上，造成 {max(SCALE, dmg)} 点伤害{note}"] + hurt
            for e in s.get("effects", []) if p.hp > 0 else []:
                facts += _inflict(cur, p, e["kind"], e.get("label", ""), depth, npc.name, turns=e.get("turns"), base=dmg)
            if s.get("per_sin"):
                if (cv := s.get("convict")) and _sin(p) >= cv.get("sin_at_least", 4) and p.hp > 0:
                    e = cv["effect"]
                    facts += [f"{p.name}被定了罪：天平那一端再也抬不起来"] \
                        + _inflict(cur, p, e["kind"], "被定了罪", depth, npc.name, turns=e.get("turns"), heal_mult=e.get("heal_mult"))
                _set_sin(cur, p, _sin(p) // 2)          # 称完减半（向下取整）
        if s.get("target") == "marked":
            _tally_merge(cur, npc, {"mark": None})
        return facts
    if do == "combo":
        # 组合技：一招同时挂几个效果，一次最多一个硬控（硬控照样受"挣脱后免疫一轮"保护，持续伤害按比例）
        for p in _boss_targets(cur, npc, s.get("target", "all"), targets):
            if (flag := UNLESS_FLAGS.get(s.get("unless"))) and load_player(cur, p.id).flags.get(flag):
                facts.append(f"{p.name}{UNLESS_WORDS[s['unless']]}")
                continue
            hard = False
            for e in s.get("effects", []):
                k = e["kind"]
                if k in HARD_CONTROL:
                    if hard:
                        continue
                    hard = True
                if k == "silence" and (npc.template.props.get("silence_field") or e.get("personal")):
                    # 无舌的大祭司：禁声打在人身上（说不了话、念不了书卷）
                    facts += _inflict(cur, p, "silence",
                                      e.get("label", "喉咙一紧，发不出声音"), depth, npc.name, turns=e.get("turns"))
                    continue
                if k == "silence":
                    _tally_merge(cur, npc, {"silence": e.get("turns", 2)})
                    facts.append(f"（{npc.name}接下来出手 {e.get('turns', 2)} 次之内，谁都念不了书和卷轴）")
                    continue
                if k == "mark":
                    _tally_merge(cur, npc, {"mark": str(p.id), "mark_bonus": e.get("bonus", 2 * SCALE)})
                    facts.append(f"（{p.name}被盯上了：挨{npc.name}的打 +{e.get('bonus', 2 * SCALE)}，直到它打中一次）")
                    continue
                resist = gear_resist(cur, p, k)
                if resist <= 0 or (resist < 1 and not _roll(resist)):
                    facts.append(f"{p.name}扛住了{EFFECT_NAMES.get(k, STATE_NAMES.get(k, k))}")
                    continue
                facts += _inflict(cur, p, k, e.get("label", ""), depth, npc.name, e.get("escape", 2), e.get("turns"),
                                  e.get("value_mult", 1.0), expected_hit(npc.template.attack, _defense(cur, p), depth),
                                  e.get("heal_mult"))
        return facts
    if do == "phase":
        # 转阶段：头目多一动、攻击加、扎根回血；房间变暗、变积水、封门；之后 phase: true 的招才放
        upd: dict = {"phased": True}
        t = _tallies(cur, npc)
        if n := s.get("actions") or s.get("actions_add"):
            upd["extra_acts"] = t.get("extra_acts", 0) + n
            facts.append(f"（{npc.name}的动作快了，一轮多出手 {n} 次）")
        if n := s.get("atk"):
            upd["phase_atk"] = t.get("phase_atk", 0) + n
        if r := s.get("regen"):
            upd["regen"] = r
            facts.append(f"（{npc.name}扎下了根：每轮回 {round(r * 100)}% 的血，被火打中的那一轮回不了）")
        _tally_merge(cur, npc, upd)
        env = s.get("env") or {}
        if env:
            room_env = _room_env(cur, npc.room_id)
            new = {}
            if "light" in env:
                new["light"] = max(0, min(100, int(room_env.get("light", 50)) + env["light"]))
                facts.append(f"（房间{'暗' if env['light'] < 0 else '亮'}了下来，光亮 {new['light']}）")
            if env.get("ground"):
                new["ground"] = env["ground"]
                facts.append({"sand": "（脚下变成了流沙：挪一步都难，站着不动会往下陷）"}.get(env["ground"], "（脚下变成了积水，行动不便）"))
            if "star_cycle_period" in env:
                new["period"] = env["star_cycle_period"]
                facts.append(f"（魔星明灭快了一倍：每 {env['star_cycle_period']} 个回合换一次）")
            if "fog_start_distance" in env:
                new["fog_start"] = env["fog_start_distance"]
                for p in load_players_in(cur, npc.room_id):
                    st = _stealth(p)
                    for n in _enemies(cur, npc.room_id):
                        _set_distance(st, n, min(_distance(st, n), int(env["fog_start_distance"])))
                    _save_stealth(cur, p, st)
                facts.append("（雾浓得伸手不见五指，所有东西都贴到了跟前）")
            if env.get("sealed"):
                new["sealed"] = True
                facts.append("（门被封死了，打完之前谁也出不去）")
            cur.execute("update rooms set props = jsonb_set(props, '{env}', coalesce(props->'env', '{}'::jsonb) || %s) where id = %s",
                        (Jsonb(new), npc.room_id))
        if (cfg := npc.template.props.get("nodes")) and cfg.get("regrow_on_phase") and (n := dungeon.spawn_nodes(cur, npc.room_id, npc.id)):
            facts.append(f"（{npc.name}身上又长出了 {n} 簇{cfg.get('name', '晶簇')}：敲掉之前它更硬）")
        if then := s.get("then"):
            facts += _skill_effect(cur, npc, then, targets, depth, theme, dodging)
        return facts
    if do == "summon":
        cur.execute("""select count(*) as n from npcs n join npc_templates t on t.id = n.template_id
                       where n.room_id = %s and n.alive and (t.props->>'minion')::boolean""", (npc.room_id,))
        room = max(0, SUMMON_MAX - cur.fetchone()["n"])
        if room <= 0:
            return facts + ["可是这一回没人应声（场上的帮手已经够多了）"]
        names = dungeon.spawn_minions(cur, npc.room_id, depth, s["kind"], theme, min(summon_count(s, depth), room),
                                      elite=bool(s.get("elite")))
        return facts + [f"{'、'.join(names)}加入了战斗（这些是跟着{npc.name}来的，它一倒下就会跑）"]
    if do in ("status_all", "effect_all"):
        for p in targets:
            if p.hp <= 0:
                continue
            resist = gear_resist(cur, p, s["kind"])
            if resist <= 0 or (resist < 1 and not _roll(resist)):
                facts.append(f"{p.name}扛住了")
                continue
            facts += _inflict(cur, p, s["kind"], "", depth, npc.name, s.get("escape", 2), s.get("turns"),
                              s.get("value_mult", 1.0), expected_hit(npc.template.attack, _defense(cur, p), depth),
                              s.get("heal_mult"))
        return facts
    if do == "self_heal":
        if s.get("unless_status") == "burning" and _tally(cur, npc, "singed"):
            return facts + [f"{npc.name}身上还带着火，符上的金光一碰就散了（没回成血）"]
        gain = math.ceil(npc.template.max_hp * s.get("heal", 0.2))
        npc.hp = min(npc.template.max_hp, npc.hp + gain)
        cur.execute("update npcs set hp = %s where id = %s", (npc.hp, npc.id))
        return facts + [f"{npc.name} HP {npc.hp}/{npc.template.max_hp}"]
    return facts


def _silenced(cur: Cursor, room_id: str, player: Optional[Player] = None) -> Optional[str]:
    """念不了书卷：房间里有头目禁了声（或者在场就禁声的大祭司），或者这人自己中了禁声。返回谁弄的"""
    if player and (e := _effect(player, "silence")):
        return e.source or "禁声"
    return next((n.name for n in _enemies(cur, room_id)
                 if n.template.props.get("silence_field") or (n.template.props.get("skills") and _tallies(cur, n).get("silence"))),
                None)


def _extra_acts(cur: Cursor, npc: Npc) -> int:
    """转阶段多出来的副动作"""
    return int(_tallies(cur, npc).get("extra_acts", 0)) if npc.template.props.get("skills") else 0


FLIGHT_FREE = {"say", "look", "reject", "close_eyes", "pinch", "struggle", "stand", "talk"}


GROUNDERS = ("绳", "网", "钩索")                  # 拿这些东西拽他更容易


def _flight(cur: Cursor, player: Player, view: RoomView, action) -> Optional[list[str]]:
    """飞鞋（关卡头目 flight）：他在头顶盘旋时，别的动作都会被他俯冲打断（作废，挨一下 ×0.5）；
    冲着他做的花样（抓脚踝、甩绳子、撒网）或者装着钩索箭射他，是把他拽下来：运动判定，成了飞行结束，这一轮他挨打 ×1.5"""
    if action.action in FLIGHT_FREE or not dungeon.is_dungeon(player.room_id):
        return None
    flyer = next((n for n in _enemies(cur, player.room_id) if _tally(cur, n, "flying") > 0), None)
    if not flyer:
        return None
    target = getattr(action, "target", None)
    at_him = target in view.refs and view.refs[target] == flyer.id
    grapple = any(_prop(w, "loaded_ammo") == "grapple_bolt" or (w.props or {}).get("loaded_ammo") == "grapple_bolt"
                  for w in _weapons(cur, player))
    if at_him and (action.action == "stunt" or (action.action == "attack" and grapple)):
        tool = next((i for i in view.inventory if any(w in i.name for w in GROUNDERS)), None)
        depth = dungeon.parse_room(player.room_id)[1]
        ok, rolled = _check(cur, player, view, "athletics", max(1, 2 + depth // 10 - (1 if tool or grapple else 0)))
        if ok:
            _tally_merge(cur, flyer, {"flying": 0, "grounded": 1})
            how = f"用{tool.name}" if tool else "射出钩索箭" if grapple else "一把抓住他的脚踝"
            return [f"{player.name}{how}，把{flyer.name}从半空拽了下来"] + rolled + [f"（{flyer.name}摔在地上还没站稳：这一轮挨打 ×1.5）"]
        return [f"{player.name}想把{flyer.name}拽下来，没够着"] + rolled \
            + _npc_strike(cur, player, flyer, "从头顶俯冲下来", 1.0, 0.5)
    return [f"{player.name}刚一动，{flyer.name}就从头顶俯冲下来，把这一下打断了"] \
        + _npc_strike(cur, player, flyer, "借着俯冲划了一刀", 1.0, 0.5)
