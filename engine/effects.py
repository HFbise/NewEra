"""Player status effects and control (poison, bleed, blind, silence, blessings, restraints). / 玩家身上的效果和控制状态"""
# 这个包里的模块共用一个命名空间：engine/__init__.py 按 MODULES 的顺序加载，每个模块都能直接用别的模块里的名字
# （跟拆分前在同一个文件里一样）。All modules in this package share one namespace; see engine/__init__.py.


def _set_status(cur: Cursor, table: str, obj_id: UUID, status: Optional[Status]) -> None:
    cur.execute(f"update {table} set status = %s where id = %s",
                (Jsonb(status.model_dump()) if status else None, obj_id))


# 中毒：每回合（每条消息）掉血、命中和判定 -POISON_HIT；流血：每个动作掉血、打出的伤害 ×BLEED_DAMAGE；
# 看不清：光亮算 0（命中只剩 5%）；腐蚀：防御 -value、血量上限临时扣 hp（消退还回来）。
# 同一种再中一次只刷新时间。住店、扎营、被扶起来、急救都会清掉
EFFECT_NAMES = {"poison": "中毒", "bleed": "流血", "blind": "看不清", "corrode": "腐蚀", "whet": "磨利", "cheer": "浑身是劲",
                "wound": "重伤", "silence": "禁声", "bless": "祝福"}


FLOOR_EFFECTS = ("whet", "bless")       # 只管这一层的（source 记 run:depth），换了一层就散


# 住店、扎营、被扶起来清掉的只是坏效果，磨利、浑身是劲这种好的留着（那几处 update 里直接写了 jsonb 过滤）
# 清掉所有效果时把腐蚀扣的血量上限还回去（SQL 片段，用在 update players set ... 里，得在改 hp 之前算）
RESTORE_MAX_HP = "coalesce((select sum((e->>'hp')::int) from jsonb_array_elements(effects) e), 0)"


def _effect(player: Player, kind: str) -> Optional[Effect]:
    return next((e for e in player.effects if e.kind == kind), None)


def _poisoned(player: Player) -> float:
    return POISON_HIT if _effect(player, "poison") else 0.0


def _bled(player: Player, dmg: int) -> int:
    """流血时打出的伤害打折"""
    return max(SCALE, round(dmg * BLEED_DAMAGE)) if _effect(player, "bleed") and dmg > 0 else dmg


def _save_effects(cur: Cursor, player: Player) -> None:
    cur.execute("update players set effects = %s where id = %s", (Jsonb([e.model_dump() for e in player.effects]), player.id))


def effects_text(player: Player) -> str:
    """给 AI 和界面看：中毒（还剩 2 回合）、流血（还剩 3 个动作）"""
    return "、".join(f"磨利：普通攻击伤害 +{e.value}（这一层）" if e.kind == "whet"
                    else f"{EFFECT_NAMES[e.kind]}：{e.label}（还剩 {e.left} {'个动作' if e.kind == 'bleed' else '回合'}）"
                    for e in player.effects)


def _on_hit(cur: Cursor, player: Player, npc: Npc, base: Optional[int] = None) -> list[str]:
    """怪打中人以后按 props.on_hit 的几率附带效果：中毒、流血、看不清、腐蚀，或者缠住、撞倒（状态）"""
    hit = (_tallies(cur, npc).get("on_hit") if npc.template.props.get("phases") else None) or npc.template.props.get("on_hit")
    if not hit or player.hp <= 0:
        return []
    resist = gear_resist(cur, player, hit["kind"])       # 装备抗性：0 免疫，0.5 减半
    if resist <= 0 or not _roll(hit.get("chance", 0.25) * resist):
        return []
    depth = npc.template.props.get("dungeon", {}).get("depth", 1)
    if hit["kind"] == "charm":
        # 迷惑：迷迷糊糊往雾里走了一步，离所有怪都远一格
        st = _stealth(player)
        for n in _enemies(cur, player.room_id):
            _set_distance(st, n, _distance(st, n) + 1)
        _save_stealth(cur, player, st)
        return [_on_you(player.name, hit.get("label", "被迷住了，往后退了一步")) + "（离怪都远了一格）"]
    return _inflict(cur, player, hit["kind"], hit.get("label", ""), depth, npc.name, hit.get("escape", 2), hit.get("turns"),
                   hit.get("value_mult", 1.0), base, void_add=int(hit.get("void_difficulty", 0)))


def _inflict(cur: Cursor, player: Player, kind: str, label: str, depth: int, source: str, escape: int = 2,
             turns: Optional[int] = None, value_mult: float = 1.0, base: Optional[float] = None,
             heal_mult: Optional[float] = None, void_add: int = 0) -> list[str]:
    """给玩家上一个效果：怪打中时附带的（_on_hit），决斗里对手装备触发的（_pvp_hit_extras）。
    base：挂上它的那一下打出的伤害（中毒、流血按它的比例跳，rules.dot_value）"""
    if kind == "dispel":
        good = [e for e in player.effects if e.kind in ("whet", "cheer", "bless")]
        if not good:
            return []
        player.effects = [e for e in player.effects if e not in good]
        _save_effects(cur, player)
        return [f"{_on_you(player.name, label or '身上的好兆头被吸走了')}（{'、'.join(e.label or EFFECT_NAMES[e.kind] for e in good)}没了）"]
    wrapped = kind == "wrapped"
    if wrapped:
        kind, label = "restrained", label or "被裹尸布缠住了，东西都拿不起来"
    if kind in ("restrained", "prone", "stun"):
        if player.status:
            return []
        guard = player.flags.get(CTRL_GUARD) or {}
        if guard.get("kind") == ("incapacitated" if kind == "stun" else kind) and guard.get("left", 0) > 0:
            return [f"{player.name}刚{'爬起来' if kind == 'prone' else '挣脱出来'}，还提防着，没再{(label if label.startswith('被') else '被' + label) if label else '被控住'}"]
        label = label or {"stun": "被打得晕头转向", "restrained": "被缠住了", "prone": "被撞倒在地"}[kind]
        if kind == "stun" and _fx(_worn(cur, player), "resist_once", when="hurt", kind="stun") \
                and _once_per_fight(cur, player, "resist_stun"):
            return [f"{player.name}眼前一黑又清醒过来，没被{label.removeprefix('被')}（这一场用过了）"]
        # 状态栏里的说法不带"你"（"一截裹尸布缠住了你的手"这种只在打中那一句里用）
        short = label if "你" not in label else ("被裹尸布缠住了" if wrapped else
                                                 {"stun": "被打得晕头转向", "restrained": "被缠住了", "prone": "被撞倒在地"}[kind])
        st = Status(kind="incapacitated" if kind == "stun" else kind, label=short[:20], escape=escape,
                    since=datetime.now(timezone.utc).isoformat(), wrapped=wrapped)
        _set_status(cur, "players", player.id, st)
        player.status = st
        return [_on_you(player.name, label) + ("，得先挣脱（火一碰就能烧断）" if wrapped else "，得先挣脱" if kind == "restrained"
                                            else "，得先爬起来" if kind == "prone"
                                            else "，失去战斗能力")] \
            + (_noise(cur, player, "fall") + _void_fall(cur, player, void_add) if kind == "prone" else [])
    label = label or EFFECT_NAMES[kind]
    # 中毒、流血按那一下的伤害算（value_mult：这只怪的毒、血口子轻一点）；腐蚀、看不清照旧
    value = dot_value(kind, base, depth, value_mult) if kind != "wound" else round(100 * (0.5 if heal_mult is None else heal_mult))
    old = _effect(player, kind)
    if old:
        full = turns or EFFECT_TURNS[kind]
        if old.left >= full:
            return [f"{_on_you(player.name, label)}，不过{EFFECT_NAMES[kind]}已经挂满了"]
        old.left += 1                           # 重复中招只延长一回合，封顶到原本的持续时间
        _save_effects(cur, player)
        return [f"{_on_you(player.name, label)}，{EFFECT_NAMES[kind]}多挂了一回合（还剩 {old.left}）"]
    e = Effect(kind=kind, value=value, left=turns or EFFECT_TURNS[kind], label=label[:20], source=source)     # turns：这只怪自己的持续轮数（on_hit.turns）
    what = {"poison": f"接下来 {e.left} 回合每回合掉 {value} 点血，出手也不准了",
            "bleed": f"接下来 {e.left} 个动作每动一下掉 {value} 点血，使不上劲",
            "blind": "这一回合什么都看不清",
            "wound": f"接下来 {e.left} 回合受到的治疗" + ("全都不起作用" if value == 0 else f"只剩 {value}%") + "，药草、绷带解不了",
            "silence": f"接下来 {e.left} 回合说不出话，也念不了书和卷轴",
            "corrode": ""}.get(kind, "")
    if kind == "corrode":
        e.hp = min(player.max_hp - 1, max(1, round(player.max_hp * CORRODE_HP)))
        player.max_hp -= e.hp
        player.hp = min(player.hp, player.max_hp)
        cur.execute("update players set max_hp = %s, hp = %s where id = %s", (player.max_hp, player.hp, player.id))
        what = f"防御 -{value}，血量上限暂时 -{e.hp}，持续 {e.left} 回合"
    player.effects.append(e)
    _save_effects(cur, player)
    return [f"{_on_you(player.name, label)}（{EFFECT_NAMES[kind]}：{what}）"]


def _floor_tag(room_id: str) -> str:
    run, depth = dungeon.parse_room(room_id)
    return f"{run.hex}:{depth}"


def _bless(player: Player, stat: str) -> float:
    """这一层身上的祝福（许愿池、清醒梦、星图）加的比例"""
    return sum(e.value / 100 for e in player.effects if e.kind == "bless" and e.stat == stat)


def _give_bless(cur: Cursor, player: Player, stat: str, pct: int, label: str) -> None:
    player.effects = [e for e in player.effects if not (e.kind == "bless" and e.stat == stat)] + [
        Effect(kind="bless", value=pct, left=1, label=label, source=_floor_tag(player.room_id), stat=stat)]
    _save_effects(cur, player)


def _tick_effects(cur: Cursor, player: Player, unit: str) -> list[str]:
    """unit="turn"：每条消息开头结一次中毒、看不清、腐蚀；unit="action"：每个动作前结一次流血。
    时间到了就消退（腐蚀把血量上限还回去）"""
    if not player.effects or player.hp <= 0:
        return []
    facts, keep = [], []
    for e in player.effects:
        if e.kind == "cheer":
            keep.append(e)                      # 按层数算，下到新的一层才减（_torch_floor）
            continue
        if e.kind in FLOOR_EFFECTS:
            if unit == "turn" and e.kind == "bless" and e.stat == "regen" and player.hp < player.max_hp \
                    and dungeon.is_dungeon(player.room_id) and e.source == _floor_tag(player.room_id):
                # 许愿池的回血祝福：每回合回一点
                gain = min(player.max_hp - player.hp, max(1, round(player.max_hp * e.value / 100)))
                cur.execute("update players set hp = hp + %s where id = %s", (gain, player.id))
                player.hp += gain
            if unit == "turn" and dungeon.is_dungeon(player.room_id) and e.source != "{}:{}".format(
                    dungeon.parse_room(player.room_id)[0].hex, dungeon.parse_room(player.room_id)[1]):
                facts.append(f"{player.name}武器上磨出来的锋利劲过去了" if e.kind == "whet" else f"{player.name}身上的{e.label}散了")
            elif unit == "turn" and not dungeon.is_dungeon(player.room_id):
                facts.append(f"{player.name}武器上磨出来的锋利劲过去了" if e.kind == "whet" else f"{player.name}身上的{e.label}散了")
            else:
                keep.append(e)
            continue
        if (e.kind == "bleed") != (unit == "action"):
            keep.append(e)
            continue
        if e.left <= 0:
            if e.hp:
                player.max_hp += e.hp
                cur.execute("update players set max_hp = max_hp + %s where id = %s", (e.hp, player.id))
            facts.append(f"{player.name}身上的{EFFECT_NAMES[e.kind]}消退了")
            continue
        if e.kind in ("poison", "bleed") and player.hp > 0:
            hurt, _ = _hurt_player(cur, player, e.value, "npc", e.source or EFFECT_NAMES[e.kind])
            facts += [f"{player.name}{EFFECT_NAMES[e.kind]}，掉了 {e.value} 点血"] + hurt
        e.left -= 1
        keep.append(e)
    player.effects = keep
    _save_effects(cur, player)
    return facts


CTRL_GUARD = "_ctrl_guard"               # players.flags：刚挣脱、爬起来的那种控制，{"kind", "left": 还护几个敌人回合}


def _guard_control(cur: Cursor, player: Player, kind: str) -> None:
    """挣脱缠住、爬起来以后：接下来一个敌人回合里不会再被同一种控制打中（不然一狗两藤轮流控住，一直没法动）"""
    player.flags[CTRL_GUARD] = {"kind": kind, "left": 1}
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::jsonb) where id = %s",
                (CTRL_GUARD, Jsonb(player.flags[CTRL_GUARD]), player.id))


def _guard_tick(cur: Cursor, player_id: UUID) -> None:
    """一个敌人回合过去了：刚挣脱的保护用掉"""
    cur.execute("update players set flags = flags - %s where id = %s and flags ? %s", (CTRL_GUARD, player_id, CTRL_GUARD))


def do_stand(cur: Cursor, player: Player, view: RoomView, a: Stand) -> list[str]:
    """倒地后爬起来：不用掷骰，这一下就花在站起来上了"""
    st = player.status
    if st is None or st.kind != "prone":
        raise ActionError(f"{player.name}没有倒在地上" + (f"，而是{st.describe()}" if st else ""))
    _set_status(cur, "players", player.id, None)
    _guard_control(cur, player, "prone")
    return [f"{player.name}从地上爬了起来"]


def do_struggle(cur: Cursor, player: Player, view: RoomView, a: Struggle) -> list[str]:
    """挣脱（体操）、醒来（不靠技能）。AI 看玩家怎么做判这次的难度，但不能比施加时判的容易超过一级；
    每失败一次难度降一级"""
    st = player.status
    if st is None:
        raise ActionError(f"{player.name}没有被困住，用不着挣脱")
    if st.kind == "prone":
        return do_stand(cur, player, view, Stand(action="stand"))
    if st.wrapped and (fire := next((w for w in _worn(cur, player) if _burning(w) or _prop(w, "fire")), None)):
        # 裹尸布怕火：拿着点着的火把、带火的兵器，一碰就烧断
        _set_status(cur, "players", player.id, None)
        _guard_control(cur, player, st.kind)
        return [f"{player.name}把{fire.name}往身上的裹尸布一燎，干透的布呼地烧断了", f"{player.name}摆脱了“{st.label}”的状态"]
    diff = max(1, max(a.difficulty, st.escape - 1) - st.attempts)
    ok, rolled = _check(cur, player, view, "acrobatics" if st.kind == "restrained" else None, diff)
    facts = [f"{player.name}尝试：{a.description or '挣脱'}"] + rolled
    if ok:
        _set_status(cur, "players", player.id, None)
        _guard_control(cur, player, st.kind)
        return facts + [f"{player.name}摆脱了“{st.label}”的状态"]
    st.attempts += 1
    _set_status(cur, "players", player.id, st)
    return facts + [f"{player.name}还{st.label}，没能摆脱"]
