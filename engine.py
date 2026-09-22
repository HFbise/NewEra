"""
规则引擎：校验并执行玩家动作，写数据库，输出 facts。
- 不调用 AI，全是确定性代码
- 每个动作一个事务，玩家行先 for update 锁住，失败就回滚
- 动作里的 ref（i1、n1）用 RoomView.refs 换成真实 id，之后一律以数据库为准，
  所以同一句话里的多个动作可以共用一份 refs（拿起 i1 之后还能装备 i1）

用法:
  view = load_view(conn, player_id)
  results = execute_all(conn, view, parsed.actions)
"""
from typing import Any, Callable, Optional
from uuid import UUID

from psycopg import Connection, Cursor
from psycopg.rows import dict_row

from schema import (
    ActionResult, Attack, Drop, Equip, Freeform, Give, ItemInstance, Look, Move,
    Npc, Player, PlayerAction, Reject, Room, RoomExit, RoomView, Take, Talk, Use, dir_name,
)


ONLINE_WINDOW = "15 seconds"            # 超过这么久没有心跳的玩家算睡着
AFFINITY_STEP = 5                       # 对话 AI 每次最多调整的好感度
AFFINITY_RANGE = (-100, 100)


class ActionError(Exception):
    """动作不合法。事务回滚，message 作为失败的 fact"""


# ============ 读取 ============

ITEM_SELECT = """
select i.id, i.quantity, i.room_id, i.player_id, i.npc_id, i.equipped_slot, i.props,
       row_to_json(t) as template
from item_instances i join item_templates t on t.id = i.template_id
"""

NPC_SELECT = """
select n.id, n.room_id, n.hp, n.alive, n.memory, row_to_json(t) as template
from npcs n join npc_templates t on t.id = n.template_id
"""


def _cursor(conn: Connection) -> Cursor:
    return conn.cursor(row_factory=dict_row)


def load_player(cur: Cursor, player_id: UUID, lock: bool = False) -> Player:
    cur.execute(
        "select id, name, room_id, hp, max_hp, attack, defense, flags from players where id = %s"
        + (" for update" if lock else ""),
        (player_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ActionError("玩家不存在")
    return Player(**row)


def load_room(cur: Cursor, room_id: str) -> Room:
    cur.execute("select id, name, description from rooms where id = %s", (room_id,))
    return Room(**cur.fetchone())


def load_exits(cur: Cursor, room_id: str) -> list[RoomExit]:
    cur.execute("select * from room_exits where room_id = %s order by direction", (room_id,))
    return [RoomExit(**r) for r in cur.fetchall()]


def load_items(cur: Cursor, where: str, params: tuple, lock: bool = False) -> list[ItemInstance]:
    cur.execute(
        ITEM_SELECT + f" where {where} order by t.name, i.id" + (" for update of i" if lock else ""),
        params,
    )
    return [ItemInstance(**r) for r in cur.fetchall()]


def load_npcs(cur: Cursor, where: str, params: tuple, lock: bool = False) -> list[Npc]:
    cur.execute(
        NPC_SELECT + f" where {where} order by t.name, n.id" + (" for update of n" if lock else ""),
        params,
    )
    return [Npc(**r) for r in cur.fetchall()]


def touch(conn: Connection, player_id: UUID) -> None:
    """记录玩家活跃（页面心跳或发命令时调用）"""
    with conn.transaction():
        conn.execute("update players set last_active_at = now() where id = %s", (player_id,))


def sleep(conn: Connection, player_id: UUID) -> None:
    """主动下线：角色留在原地睡着"""
    with conn.transaction():
        conn.execute("update players set last_active_at = null where id = %s", (player_id,))


def load_view(conn: Connection, player_id: UUID) -> RoomView:
    """构建给意图解析 AI 的房间上下文，并分配短编号"""
    # 只读也包在事务里：psycopg 默认非 autocommit，事务外查询会留下一个不提交的隐式事务
    with conn.transaction():
        cur = _cursor(conn)
        player = load_player(cur, player_id)
        view = RoomView(
            player=player,
            room=load_room(cur, player.room_id),
            exits=load_exits(cur, player.room_id),
            items=load_items(cur, "i.room_id = %s", (player.room_id,)),
            npcs=load_npcs(cur, "n.room_id = %s and n.alive", (player.room_id,)),
            inventory=load_items(cur, "i.player_id = %s", (player.id,)),
        )
    view.assign_refs()
    return view


# ============ 通用校验 ============

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


def _equipped(cur: Cursor, player: Player, slot: str) -> Optional[ItemInstance]:
    items = load_items(cur, "i.player_id = %s and i.equipped_slot = %s", (player.id, slot))
    return items[0] if items else None


def _affinity(cur: Cursor, player: Player, npc: Npc) -> int:
    cur.execute("select affinity from player_npc_relations where player_id = %s and npc_template = %s",
                (player.id, npc.template.id))
    row = cur.fetchone()
    return row["affinity"] if row else 0


def _set_flag(cur: Cursor, player: Player, flag: str) -> None:
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, true) where id = %s",
                (flag, player.id))


# ============ 动作处理 ============
# 签名统一: (cur, player, view, action) -> facts

def do_move(cur: Cursor, player: Player, view: RoomView, a: Move) -> list[str]:
    ex = _find_exit(cur, player.room_id, a.direction)
    if ex is None:
        raise ActionError(f"这里没有往{dir_name(a.direction)}的路")
    if ex["locked"]:
        raise ActionError(f"往{dir_name(a.direction)}的门锁着")
    cur.execute("update players set room_id = %s, updated_at = now() where id = %s",
                (ex["to_room"], player.id))
    room = load_room(cur, ex["to_room"])
    return [f"{player.name}往{dir_name(a.direction)}走，来到了{room.name}"]


def do_look(cur: Cursor, player: Player, view: RoomView, a: Look) -> list[str]:
    if a.target is None:
        room = load_room(cur, player.room_id)
        facts = [f"{room.name}：{room.description}"]
        exits = load_exits(cur, player.room_id)
        if exits:
            facts.append("出口：" + "、".join(dir_name(e.direction) + ("（锁着）" if e.locked else "") for e in exits))
        items = load_items(cur, "i.room_id = %s", (player.room_id,))
        if items:
            facts.append("地上有：" + "、".join(_label(i) for i in items))
        npcs = load_npcs(cur, "n.room_id = %s and n.alive", (player.room_id,))
        if npcs:
            facts.append("这里有：" + "、".join(n.name for n in npcs))
        cur.execute(
            f"""select name, coalesce(last_active_at > now() - interval '{ONLINE_WINDOW}', false) as awake
                from players where room_id = %s and id <> %s and hp > 0 order by name""",
            (player.room_id, player.id),
        )
        others = [r["name"] + ("" if r["awake"] else "（睡着了）") for r in cur.fetchall()]
        if others:
            facts.append("其他玩家：" + "、".join(others))
        return facts

    ex = _find_exit(cur, player.room_id, a.target)
    if ex is not None:
        room = load_room(cur, ex["to_room"])
        return [f"往{dir_name(a.target)}通往{room.name}" + ("，门锁着" if ex["locked"] else "")]

    uid = _resolve(view, a.target)
    items = load_items(cur, "i.id = %s and (i.room_id = %s or i.player_id = %s)",
                       (uid, player.room_id, player.id))
    if items:
        return [f"{items[0].name}：{items[0].template.description}"]
    npcs = load_npcs(cur, "n.id = %s and n.room_id = %s and n.alive", (uid, player.room_id))
    if npcs:
        n = npcs[0]
        facts = [f"{n.name}：{n.template.description}"]
        if n.combatable:
            facts.append(f"{n.name} HP {n.hp}/{n.template.max_hp}")
        return facts
    raise ActionError("这里看不到那个东西")


def do_take(cur: Cursor, player: Player, view: RoomView, a: Take) -> list[str]:
    item = _get_item(cur, view, a.item)
    if item.room_id != player.room_id:
        raise ActionError(f"这里没有{item.name}")
    if not item.template.takeable:
        raise ActionError(f"{item.name}拿不起来")
    _move_item(cur, item, player_id=player.id)
    return [f"{player.name}拿起了{_label(item)}"]


def do_drop(cur: Cursor, player: Player, view: RoomView, a: Drop) -> list[str]:
    item = _inv_item(cur, view, player, a.item)
    facts = [f"{player.name}卸下了{item.name}"] if item.equipped_slot else []
    _move_item(cur, item, room_id=player.room_id)
    return facts + [f"{player.name}把{_label(item)}放在了地上"]


def do_use(cur: Cursor, player: Player, view: RoomView, a: Use) -> list[str]:
    item = _inv_item(cur, view, player, a.item)

    if a.target is not None:
        ex = _find_exit(cur, player.room_id, a.target, lock=True)
        if ex is None:
            raise ActionError(f"不知道怎么对那个东西使用{item.name}")
        if not ex["locked"]:
            raise ActionError(f"往{dir_name(a.target)}的门没有锁")
        if ex["key_item"] != item.template.id:
            raise ActionError(f"{item.name}打不开往{dir_name(a.target)}的门")
        cur.execute("update room_exits set locked = false where room_id = %s and direction = %s",
                    (player.room_id, a.target))
        return [f"{player.name}用{item.name}打开了往{dir_name(a.target)}的门"]

    if item.template.type != "consumable":
        raise ActionError(f"{item.name}不能直接使用")
    healed = min(item.template.heal, player.max_hp - player.hp)
    hp = player.hp + healed
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (hp, player.id))
    if item.quantity > 1:
        cur.execute("update item_instances set quantity = quantity - 1 where id = %s", (item.id,))
    else:
        cur.execute("delete from item_instances where id = %s", (item.id,))
    return [f"{player.name}吃掉了{item.name}", f"恢复 {healed} 点 HP，当前 HP {hp}/{player.max_hp}"]


def do_equip(cur: Cursor, player: Player, view: RoomView, a: Equip) -> list[str]:
    item = _inv_item(cur, view, player, a.item)
    slot = item.template.type
    if slot not in ("weapon", "armor"):
        raise ActionError(f"{item.name}不能装备")
    if item.equipped_slot:
        raise ActionError(f"{item.name}已经装备着了")
    facts = []
    old = _equipped(cur, player, slot)
    if old:
        cur.execute("update item_instances set equipped_slot = null where id = %s", (old.id,))
        facts.append(f"{player.name}卸下了{old.name}")
    cur.execute("update item_instances set equipped_slot = %s where id = %s", (slot, item.id))
    return facts + [f"{player.name}装备了{item.name}"]


def do_attack(cur: Cursor, player: Player, view: RoomView, a: Attack) -> list[str]:
    # 战斗公式是占位的，待定问题里还没定
    npc = _room_npc(cur, view, player, a.target)
    if not npc.combatable:
        raise ActionError(f"{npc.name}不是能打的对象")
    weapon = _equipped(cur, player, "weapon")
    armor = _equipped(cur, player, "armor")

    dmg = max(1, player.attack + (weapon.template.damage if weapon else 0) - npc.template.defense)
    npc_hp = max(0, npc.hp - dmg)
    facts = [f"{player.name}用{weapon.name if weapon else '拳头'}攻击{npc.name}，造成 {dmg} 点伤害",
             f"{npc.name} HP {npc_hp}/{npc.template.max_hp}"]

    if npc_hp == 0:
        cur.execute("update npcs set hp = 0, alive = false where id = %s", (npc.id,))
        cur.execute("update item_instances set npc_id = null, room_id = %s where npc_id = %s",
                    (player.room_id, npc.id))
        facts.append(f"{npc.name}被击败了")
        flag = npc.template.props.get("on_death", {}).get("set_flag")
        if flag:
            _set_flag(cur, player, flag)
        return facts

    cur.execute("update npcs set hp = %s where id = %s", (npc_hp, npc.id))
    back = max(1, npc.template.attack - player.defense - (armor.template.defense if armor else 0))
    hp = max(0, player.hp - back)
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (hp, player.id))
    facts += [f"{npc.name}反击，对{player.name}造成 {back} 点伤害", f"{player.name} HP {hp}/{player.max_hp}"]
    if hp == 0:
        facts.append(f"{player.name}倒下了")
    return facts


def do_talk(cur: Cursor, player: Player, view: RoomView, a: Talk) -> list[str]:
    # 这里只校验对象在场，NPC 怎么回、给不给东西由对话步骤决定（见 giveable_items / npc_give）
    npc = _room_npc(cur, view, player, a.target)
    return [f"{player.name}对{npc.name}说：“{a.message}”"]


def do_give(cur: Cursor, player: Player, view: RoomView, a: Give) -> list[str]:
    item = _inv_item(cur, view, player, a.item)
    npc = _room_npc(cur, view, player, a.target)
    _move_item(cur, item, npc_id=npc.id)
    return [f"{player.name}把{_label(item)}交给了{npc.name}"]


def do_freeform(cur: Cursor, player: Player, view: RoomView, a: Freeform) -> list[str]:
    # 不改任何状态，叙事 AI 自由发挥
    return [f"{player.name}尝试：{a.description}"]


def do_reject(cur: Cursor, player: Player, view: RoomView, a: Reject) -> list[str]:
    raise ActionError(a.reason)


HANDLERS: dict[str, Callable[..., list[str]]] = {
    "move": do_move, "look": do_look, "take": do_take, "drop": do_drop, "use": do_use,
    "equip": do_equip, "attack": do_attack, "talk": do_talk, "give": do_give, "freeform": do_freeform,
    "reject": do_reject,
}


# ============ 入口 ============

def execute(conn: Connection, view: RoomView, action: PlayerAction) -> ActionResult:
    try:
        with conn.transaction():
            cur = _cursor(conn)
            player = load_player(cur, view.player.id, lock=True)
            if player.hp <= 0 and action.action != "look":
                raise ActionError(f"{player.name}已经倒下了，动弹不得")
            facts = HANDLERS[action.action](cur, player, view, action)
        return ActionResult(action=action.action, success=True, facts=facts)
    except ActionError as e:
        return ActionResult(action=action.action, success=False, facts=[str(e)])


def execute_all(conn: Connection, view: RoomView, actions: list[PlayerAction]) -> list[ActionResult]:
    """按顺序执行，前一步失败就中断"""
    results = []
    for action in actions:
        result = execute(conn, view, action)
        results.append(result)
        if not result.success:
            break
    return results


# ============ NPC 给予（对话步骤调用） ============

def _give_rule_ok(cur: Cursor, npc: Npc, player: Player, item: ItemInstance) -> bool:
    rule: Any = npc.template.props.get("gives", {}).get(item.template.id)
    if rule == "ai":
        return True
    if not isinstance(rule, dict) or not rule:
        return False                    # 没配置就是不给
    if "requires" in rule and not player.flags.get(rule["requires"]):
        return False
    if "min_affinity" in rule and _affinity(cur, player, npc) < rule["min_affinity"]:
        return False
    return True


def giveable_items(conn: Connection, player_id: UUID, npc_id: UUID) -> list[ItemInstance]:
    """NPC 现在允许给这个玩家的物品，告诉对话 AI 它可以给哪些"""
    with conn.transaction():
        cur = _cursor(conn)
        player = load_player(cur, player_id)
        npcs = load_npcs(cur, "n.id = %s", (npc_id,))
        if not npcs:
            return []
        items = load_items(cur, "i.npc_id = %s", (npc_id,))
        return [i for i in items if _give_rule_ok(cur, npcs[0], player, i)]


def npc_give(conn: Connection, player_id: UUID, npc_id: UUID, item_id: UUID) -> ActionResult:
    """对话 AI 提议 NPC 给玩家东西时调用，规则不满足就当没说"""
    try:
        with conn.transaction():
            cur = _cursor(conn)
            player = load_player(cur, player_id, lock=True)
            npcs = load_npcs(cur, "n.id = %s", (npc_id,), lock=True)
            if not npcs or not npcs[0].alive or npcs[0].room_id != player.room_id:
                raise ActionError("对方不在这里")
            npc = npcs[0]
            items = load_items(cur, "i.id = %s and i.npc_id = %s", (item_id, npc.id), lock=True)
            if not items or not _give_rule_ok(cur, npc, player, items[0]):
                raise ActionError(f"{npc.name}不会给这个")
            _move_item(cur, items[0], player_id=player.id)
        return ActionResult(action="npc_give", success=True,
                            facts=[f"{npc.name}把{_label(items[0])}交给了{player.name}"])
    except ActionError as e:
        return ActionResult(action="npc_give", success=False, facts=[str(e)])


# ============ 好感度（对话步骤调用） ============

def get_affinity(conn: Connection, player_id: UUID, npc_id: UUID) -> int:
    with conn.transaction():
        cur = _cursor(conn)
        npcs = load_npcs(cur, "n.id = %s", (npc_id,))
        return _affinity(cur, load_player(cur, player_id), npcs[0]) if npcs else 0


def adjust_affinity(conn: Connection, player_id: UUID, npc_id: UUID, delta: int) -> ActionResult:
    """对话 AI 提议调整好感度，单次幅度和总范围都由这里限制，AI 给多大都没用"""
    delta = max(-AFFINITY_STEP, min(AFFINITY_STEP, delta))
    lo, hi = AFFINITY_RANGE
    try:
        with conn.transaction():
            cur = _cursor(conn)
            player = load_player(cur, player_id, lock=True)
            npcs = load_npcs(cur, "n.id = %s", (npc_id,))
            if not npcs or not npcs[0].alive or npcs[0].room_id != player.room_id:
                raise ActionError("对方不在这里")
            npc = npcs[0]
            cur.execute(
                """insert into player_npc_relations (player_id, npc_template, affinity)
                   values (%(p)s, %(t)s, greatest(%(lo)s, least(%(hi)s, %(d)s)))
                   on conflict (player_id, npc_template) do update
                     set affinity = greatest(%(lo)s, least(%(hi)s, player_npc_relations.affinity + %(d)s))
                   returning affinity""",
                {"p": player.id, "t": npc.template.id, "d": delta, "lo": lo, "hi": hi},
            )
            value = cur.fetchone()["affinity"]
        trend = "上升" if delta > 0 else "下降" if delta < 0 else "没有变化"
        return ActionResult(action="affinity", success=True,
                            facts=[f"{npc.name}对{player.name}的好感{trend}（当前 {value}）"])
    except ActionError as e:
        return ActionResult(action="affinity", success=False, facts=[str(e)])
