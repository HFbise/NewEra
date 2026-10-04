"""Small shared helpers: ref resolution, item lookup, dice, NPC tallies. / 通用小工具：ref 换 id、找物品、掷骰、NPC 计数"""
# 这个包里的模块共用一个命名空间：engine/__init__.py 按 MODULES 的顺序加载，每个模块都能直接用别的模块里的名字
# （跟拆分前在同一个文件里一样）。All modules in this package share one namespace; see engine/__init__.py.


def _label(item: ItemInstance) -> str:
    return f"{item.name} x{item.quantity}" if item.quantity > 1 else item.name


def _resolve(view: RoomView, ref: str) -> UUID:
    uid = view.resolve(ref)
    if uid is None:
        raise ActionError("找不到这个东西")
    return uid


def _get_item(cur: Cursor, view: RoomView, ref: str) -> ItemInstance:
    items = load_items(cur, "i.id = %s", (_resolve(view, ref),), lock=True)
    if not items:
        raise ActionError("那件东西已经不在了")
    return items[0]


def _inv_item(cur: Cursor, view: RoomView, player: Player, ref: str) -> ItemInstance:
    item = _get_item(cur, view, ref)
    if item.player_id != player.id:
        raise ActionError(f"{player.name}身上没有{item.name}")
    return item


def _room_npc(cur: Cursor, view: RoomView, player: Player, ref: str) -> Npc:
    npcs = load_npcs(cur, "n.id = %s", (_resolve(view, ref),), lock=True)
    if not npcs or not npcs[0].alive or npcs[0].room_id != player.room_id:
        raise ActionError("对方不在这里")
    return npcs[0]


def _find_exit(cur: Cursor, room_id: str, direction: str, lock: bool = False) -> Optional[dict]:
    cur.execute(
        "select to_room, locked, key_item from room_exits where room_id = %s and direction = %s"
        + (" for update" if lock else ""),
        (room_id, direction),
    )
    return cur.fetchone()


def _move_item(cur: Cursor, item: ItemInstance, *, room_id: Optional[str] = None,
               player_id: Optional[UUID] = None, npc_id: Optional[UUID] = None) -> None:
    """把物品挪到新位置（三选一），可叠加物品会并入目的地已有的同类堆"""
    col, val = next((c, v) for c, v in
                    (("room_id", room_id), ("player_id", player_id), ("npc_id", npc_id)) if v is not None)
    if item.template.stackable:
        cur.execute(
            f"select id from item_instances where template_id = %s and {col} = %s"
            " and equipped_slot is null and id <> %s for update",
            (item.template.id, val, item.id),
        )
        stack = cur.fetchone()
        if stack:
            cur.execute("update item_instances set quantity = quantity + %s where id = %s",
                        (item.quantity, stack["id"]))
            cur.execute("delete from item_instances where id = %s", (item.id,))
            return
    cur.execute(
        "update item_instances set room_id = %s, player_id = %s, npc_id = %s, equipped_slot = null"
        " where id = %s",
        (room_id, player_id, npc_id, item.id),
    )


def _worn(cur: Cursor, player: Player) -> list[ItemInstance]:
    """身上装备着的全部东西"""
    return load_items(cur, "i.player_id = %s and i.equipped_slot is not null", (player.id,))


def _weapons(cur: Cursor, player: Player) -> list[ItemInstance]:
    """手上拿着的武器（双持就是两把，副手只加一部分，见 weapon_damage）"""
    return [i for i in _worn(cur, player) if i.template.type == "weapon" and i.equipped_slot in SLOT_CHOICES["hand"]]


def _prop(item: ItemInstance, key: str) -> Any:
    """物品特性：实例上的（NPC 现做的）优先，没有就看模板"""
    return item.props.get(key, item.template.props.get(key))


# 战斗公式是占位的，待定问题里还没定。
# 借环境的创意攻击（stunt）由 AI 当裁判给难度、档位、状态，这里掷骰、限幅、执行，AI 给不出具体数字

def _roll(chance: float) -> bool:
    """掷骰，测试时可以换掉"""
    return random.random() < chance


def _check(cur: Cursor, player: Player, view: RoomView, skill: Optional[str], difficulty: int) -> tuple[bool, list[str]]:
    """按技能掷骰，返回 (成没成, facts)。skill 为空是不靠本事的判定（昏过去醒来），按 0 级算、不涨熟练。
    成功而且难度高于当前等级才算一次熟练，一条消息里同一个技能最多涨一次"""
    count = player.skills.get(skill, 0) if skill else 0
    level = skill_level(count)
    bonus = int(gear_add(cur, player, "skill", skill)) if skill else 0      # 矿工短镐运动 +1、钥匙串巧手 +2
    chance = max(0.0, skill_chance(level + bonus, difficulty) - _drunk(player) - _poisoned(player))
    ok = _roll(chance)
    name = SKILL_NAMES[skill] if skill else "判定"
    facts = [f"（{name} {level} 级对难度 {difficulty}，成功率 {round(chance * 100)}%"
             + ("，喝醉了" if player.drunk else "") + f"：{'成功' if ok else '失败'}）"]
    if ok and skill and difficulty > level and skill not in view.trained:
        view.trained.append(skill)
        facts += _gain_skill(cur, player, skill)
    return ok, facts


def _gain_skill(cur: Cursor, player: Player, skill: str) -> list[str]:
    """熟练 +1；耐性升级顺带涨 HP 上限（当前 HP 一起涨）"""
    count = player.skills.get(skill, 0)
    level = skill_level(count)
    player.skills[skill] = count + 1
    cur.execute("update players set skills = skills || jsonb_build_object(%s::text, %s::int) where id = %s",
                (skill, count + 1, player.id))
    new, have, need = skill_progress(count + 1)
    name = SKILL_NAMES[skill]
    if new == level:
        return [f"{name}熟练 +1（{have}/{need}）"]
    facts = [f"{player.name}的{name}升到了 {new} 级"]
    if skill == "endurance":
        gain = ENDURANCE_HP * (new - level)
        player.max_hp += gain
        player.hp += gain
        cur.execute("update players set max_hp = max_hp + %s, hp = hp + %s where id = %s", (gain, gain, player.id))
        facts.append(f"{player.name}的 HP 上限 +{gain}，现在 HP {player.hp}/{player.max_hp}")
    return facts


# 运动也靠跑动练：战斗里靠近、退开每满 STRIDE["fight"] 格，平时走路每满 STRIDE["walk"] 个房间，熟练 +1
# （以前只有判定成功才涨，自由动作用得少以后运动几乎不涨）。攒的步数记在 players.flags 的 _stride_*，满了扣掉接着攒
STRIDE = {"fight": 6, "walk": 25}


def _stride(cur: Cursor, player: Player, view: RoomView, kind: str, n: int) -> list[str]:
    if n <= 0:
        return []
    key = f"_stride_{kind}"
    have = int(player.flags.get(key, 0)) + n
    gained = have >= STRIDE[kind] and "athletics" not in view.trained
    if gained:
        have -= STRIDE[kind]
        view.trained.append("athletics")
    player.flags[key] = have
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::int) where id = %s", (key, have, player.id))
    return _gain_skill(cur, player, "athletics") if gained else []


def _toughen(cur: Cursor, player: Player, chance: float = 1.0) -> list[str]:
    """扛住了一下（挨了打还站着、喝了毒还活着、扛住酒劲）：按几率涨一次耐性熟练"""
    return _gain_skill(cur, player, "endurance") if player.hp > 0 and _roll(chance) else []


def _cap(value: str, cap: str, scale: list[str]) -> str:
    return scale[min(scale.index(value), scale.index(cap))]


def _lower(value: str, scale: list[str]) -> str:
    return scale[max(0, scale.index(value) - 1)]


def _on_you(name: str, label: str) -> str:
    """怪打中附带效果的说法：写成"你"的（"一截裹尸布缠住了你的手"）把"你"换成名字，别的接在名字后面"""
    return label.replace("你", name) if "你" in label else f"{name}{label}"


def load_players_in(cur: Cursor, room_id: str) -> list[Player]:
    cur.execute("select id from players where room_id = %s and hp > 0", (room_id,))
    return [load_player(cur, r["id"]) for r in cur.fetchall()]


# 敌人出一次手（enemy_turn、round_enemies 共用）：
# - 治疗的怪（props.healer，比例）：有同伴掉到 HEALER_BELOW 以下，这一下给伤得最重的那个回血，不打人
# - 远程的怪（props.ranged）：不主动靠近，离人一步以上就射（ENEMY_RANGED_HIT，有掩体的房间 −RANGED_COVER）；
#   被贴身了先往后跳开一步、这一下不射；这一轮刚被这个人贴身砍中（pinned，被缠住了）就跳不开，只能贴身硬射。
#   所以对付远程怪要"冲上去砍它"（靠近和攻击一句话里说完），或者用远程武器、躲在掩体后面
# - 近战的怪：逼近一格，够得着就扑上来
# 命中、跳开、治疗的数值：rules.ENEMY_RANGED_HIT、RANGED_BACKS、HEALER_*


def _tallies(cur: Cursor, npc: Npc) -> dict:
    cur.execute("select tally from npcs where id = %s", (npc.id,))
    row = cur.fetchone()
    return (row and row["tally"]) or {}


def _tally_merge(cur: Cursor, npc: Npc, values: dict) -> None:
    cur.execute("update npcs set tally = tally || %s where id = %s", (Jsonb(values), npc.id))


def _tally(cur: Cursor, npc: Npc, key: str) -> int:
    cur.execute("select coalesce((tally->>%s)::int, 0) as n from npcs where id = %s", (key, npc.id))
    return cur.fetchone()["n"]


def _count(cur: Cursor, npc: Npc, key: str) -> int:
    """这只怪这场的某个本事用了一次，返回用过几次了"""
    cur.execute("""update npcs set tally = tally || jsonb_build_object(%s::text, coalesce((tally->>%s)::int, 0) + 1)
                   where id = %s returning (tally->>%s)::int as n""", (key, key, npc.id, key))
    return cur.fetchone()["n"]


HOURGLASS = "_hourglass"                # players.flags：沙漏房里还剩几个动作 {"room", "left"}


def _room_props(cur: Cursor, room_id: str) -> dict:
    cur.execute("select props from rooms where id = %s", (room_id,))
    row = cur.fetchone()
    return (row and row["props"]) or {}      # 执行完加声响的动作（静默神殿）


def _rows_by_names(cur: Cursor, names: list[str]) -> list[dict]:
    cur.execute("select id from players where name = any(%s)", (names,))
    return cur.fetchall()
