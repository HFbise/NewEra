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
import random
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

from psycopg import Connection, Cursor
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from schema import (
    ActionResult, Attack, Drop, Equip, Feature, Freeform, Give, Invite, ItemInstance, Join, LeaveParty, Look,
    Move, Npc, OtherPlayer, Player, PlayerAction, Reject, Revive, Room, RoomExit, RoomView, Say, Status, Struggle,
    Stunt, Take, Talk, Use, dir_name,
)


ONLINE_WINDOW = "15 seconds"            # 超过这么久没有心跳的玩家算睡着
AFFINITY_STEP = 5                       # 对话 AI 每次最多调整的好感度
AFFINITY_RANGE = (-100, 100)
PARTY_MAX = 5                           # 一支队伍最多几个人
INVITE_WINDOW = "10 minutes"            # 组队邀请多久内有效
DOWNED_ALLOWED = {"look", "say"}        # 倒下的人只能看和说话（喊人来救）

# 创意攻击（stunt）和负面状态。AI 只选档位和难度，数字都在这里
TIERS = ["none", "light", "heavy", "lethal"]
TIER_DAMAGE = {"none": 0, "light": 3, "heavy": 8, "lethal": 15}
IMPROVISED_MAX_TIER = "light"           # 没在 world.yaml 声明成可利用地形的东西最多轻伤
DIFFICULTIES = ["easy", "normal", "hard"]
SUCCESS_CHANCE = {"easy": 0.9, "normal": 0.65, "hard": 0.35}
ESCAPE_CHANCE = {"easy": 0.7, "normal": 0.45, "hard": 0.2}
ESCAPE_BONUS = 0.15                     # 每挣脱失败一次，下次成功率加这么多
STATUS_MAX = "3 minutes"                # 负面状态最长多久自动解除，防止 AI 一直判醒不过来把人卡死
# 被束缚时做不了的动作；失去战斗能力时只能挣脱（醒来）
RESTRAINED_BLOCKED = {"move", "attack", "stunt", "take", "drop", "equip", "use", "give", "revive"}


class ActionError(Exception):
    """动作不合法。事务回滚，message 作为失败的 fact"""


# ============ 读取 ============

ITEM_SELECT = """
select i.id, i.quantity, i.room_id, i.player_id, i.npc_id, i.equipped_slot, i.props,
       row_to_json(t) as template
from item_instances i join item_templates t on t.id = i.template_id
"""

NPC_SELECT = """
select n.id, n.room_id, n.hp, n.alive, n.memory, n.status, row_to_json(t) as template
from npcs n join npc_templates t on t.id = n.template_id
"""


def _cursor(conn: Connection) -> Cursor:
    return conn.cursor(row_factory=dict_row)


def load_player(cur: Cursor, player_id: UUID, lock: bool = False) -> Player:
    cur.execute(
        "select id, name, room_id, hp, max_hp, attack, defense, flags, party_id, status from players where id = %s"
        + (" for update" if lock else ""),
        (player_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ActionError("玩家不存在")
    return Player(**row)


def load_room(cur: Cursor, room_id: str) -> Room:
    cur.execute("select id, name, description, details from rooms where id = %s", (room_id,))
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


def delete_player(conn: Connection, player_id: UUID) -> None:
    """删角色：身上的东西先放到所在房间地上（不然任务物品会永远消失），再删账号，玩家行级联删除"""
    with conn.transaction():
        cur = _cursor(conn)
        player = load_player(cur, player_id, lock=True)
        if player.party_id:
            _leave_party(cur, player)
        for item in load_items(cur, "i.player_id = %s", (player_id,), lock=True):
            _move_item(cur, item, room_id=player.room_id)
        # 开发期账号是 server.py 塞进 auth.users 的假用户；正式版走 Supabase Auth 的删除接口
        cur.execute("delete from auth.users where id = %s", (player_id,))


def _refresh_room(cur: Cursor, room_id: str) -> None:
    """刷新：NPC 复活、门自动锁回、物品重新出现。
    不用后台任务，有人在这个房间（发命令或页面轮询）时顺手检查，没人的房间不用管"""
    # NPC 死了 respawn_seconds 秒后原地复活
    cur.execute(
        """update npcs n set alive = true, hp = t.max_hp, died_at = null
           from npc_templates t
           where t.id = n.template_id and n.room_id = %s and not n.alive
             and t.props ? 'respawn_seconds'
             and n.died_at < now() - make_interval(secs => (t.props->>'respawn_seconds')::int)""",
        (room_id,),
    )
    # 负面状态到点自动解除
    for table in ("players", "npcs"):
        cur.execute(
            f"""update {table} set status = null where room_id = %s and status is not null
                and (status->>'since')::timestamptz < now() - interval '{STATUS_MAX}'""",
            (room_id,),
        )
    # 用掉的可利用地形到点恢复
    cur.execute(
        """update room_features set uses_left = max_uses, used_at = null
           where room_id = %s and used_at < now() - make_interval(secs => respawn_seconds)""",
        (room_id,),
    )
    # 打开的门 relock_seconds 秒后自动锁回去
    cur.execute(
        """update room_exits set locked = true, unlocked_at = null
           where room_id = %s and not locked and relock_seconds is not null
             and unlocked_at < now() - make_interval(secs => relock_seconds)""",
        (room_id,),
    )
    # 物品刷新点：发现东西不在了先记时间，到点再补一个。for update 防止两个人同时刷新补出两份
    cur.execute(
        """select s.id, s.template_id, s.respawn_seconds, s.empty_since, s.room_id, n.id as npc_id
           from spawns s left join npcs n on n.template_id = s.npc_template and n.room_id = %s and n.alive
           where s.room_id = %s or n.id is not null
           for update of s""",
        (room_id, room_id),
    )
    for sp in cur.fetchall():
        col, val = ("room_id", sp["room_id"]) if sp["room_id"] else ("npc_id", sp["npc_id"])
        cur.execute(f"select 1 from item_instances where template_id = %s and {col} = %s",
                    (sp["template_id"], val))
        if cur.fetchone():
            if sp["empty_since"]:
                cur.execute("update spawns set empty_since = null where id = %s", (sp["id"],))
        elif sp["empty_since"] is None:
            cur.execute("update spawns set empty_since = now() where id = %s", (sp["id"],))
        else:
            cur.execute(
                """update spawns set empty_since = null
                   where id = %s and empty_since < now() - make_interval(secs => respawn_seconds)
                   returning id""",
                (sp["id"],),
            )
            if cur.fetchone():
                cur.execute(f"insert into item_instances (template_id, {col}) values (%s, %s)",
                            (sp["template_id"], val))


def load_view(conn: Connection, player_id: UUID) -> RoomView:
    """构建给意图解析 AI 的房间上下文，并分配短编号。顺便跑一次房间刷新"""
    # 只读也包在事务里：psycopg 默认非 autocommit，事务外查询会留下一个不提交的隐式事务
    with conn.transaction():
        cur = _cursor(conn)
        _refresh_room(cur, load_player(cur, player_id).room_id)
        player = load_player(cur, player_id)          # 刷新可能解除了自己的状态，重新读
        cur.execute(
            f"""select name, coalesce(last_active_at > now() - interval '{ONLINE_WINDOW}', false) as awake,
                       hp <= 0 as downed, status
                from players where room_id = %s and id <> %s order by name""",
            (player.room_id, player.id),
        )
        others = [OtherPlayer(**r) for r in cur.fetchall()]
        party = []
        if player.party_id:
            cur.execute("select name from players where party_id = %s and id <> %s order by name",
                        (player.party_id, player.id))
            party = [r["name"] for r in cur.fetchall()]
        cur.execute(
            f"""select p.name from party_invites i join players p on p.id = i.inviter
                where i.invitee = %s and i.created_at > now() - interval '{INVITE_WINDOW}'
                order by i.created_at""",
            (player.id,),
        )
        invites = [r["name"] for r in cur.fetchall()]
        cur.execute("select id, key, name, max_tier, uses_left from room_features where room_id = %s and uses_left > 0 order by key",
                    (player.room_id,))
        features = [Feature(**r) for r in cur.fetchall()]
        view = RoomView(
            player=player,
            room=load_room(cur, player.room_id),
            exits=load_exits(cur, player.room_id),
            items=load_items(cur, "i.room_id = %s", (player.room_id,)),
            npcs=load_npcs(cur, "n.room_id = %s and n.alive", (player.room_id,)),
            inventory=load_items(cur, "i.player_id = %s", (player.id,)),
            others=others,
            party=party,
            invites=invites,
            features=features,
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
            facts.append("出口：" + "、".join(
                f"{dir_name(e.direction)}（通往{load_room(cur, e.to_room).name}" + ("，门锁着" if e.locked else "") + "）"
                for e in exits))
        items = load_items(cur, "i.room_id = %s", (player.room_id,))
        if items:
            facts.append("地上有：" + "、".join(_label(i) for i in items))
        npcs = load_npcs(cur, "n.room_id = %s and n.alive", (player.room_id,))
        if npcs:
            facts.append("这里有：" + "、".join(n.name + (f"（{n.status.describe()}）" if n.status else "")
                                            for n in npcs))
        cur.execute(
            f"""select name, hp, status, coalesce(last_active_at > now() - interval '{ONLINE_WINDOW}', false) as awake
                from players where room_id = %s and id <> %s order by name""",
            (player.room_id, player.id),
        )
        others = [r["name"] + ("（倒在地上，等人急救）" if r["hp"] <= 0
                               else f"（{Status(**r['status']).describe()}）" if r["status"]
                               else "" if r["awake"] else "（在原地睡着了，不会动也不会回应）")
                  for r in cur.fetchall()]
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
        if n.status:
            facts.append(f"{n.name}{n.status.describe()}")
        return facts
    raise ActionError("这里看不到那个东西")


def do_take(cur: Cursor, player: Player, view: RoomView, a: Take) -> list[str]:
    item = _get_item(cur, view, a.item)
    if item.room_id != player.room_id:
        raise ActionError(f"这里没有{item.name}")
    if not item.template.takeable:
        raise ActionError(f"{item.name}拿不起来")
    _move_item(cur, item, player_id=player.id)
    return [f"{player.name}从地上捡起了{_label(item)}"]


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
        cur.execute("update room_exits set locked = false, unlocked_at = now() where room_id = %s and direction = %s",
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


# ============ 战斗 ============
# 战斗公式是占位的，待定问题里还没定。
# 借环境的创意攻击（stunt）由 AI 当裁判给难度、档位、状态，这里掷骰、限幅、执行，AI 给不出具体数字

def _roll(chance: float) -> bool:
    """掷骰，测试时可以换掉"""
    return random.random() < chance


def _cap(value: str, cap: str, scale: list[str]) -> str:
    return scale[min(scale.index(value), scale.index(cap))]


def _lower(value: str, scale: list[str]) -> str:
    return scale[max(0, scale.index(value) - 1)]


def _set_status(cur: Cursor, table: str, obj_id: UUID, status: Optional[Status]) -> None:
    cur.execute(f"update {table} set status = %s where id = %s",
                (Jsonb(status.model_dump()) if status else None, obj_id))


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


def _hurt_npc(cur: Cursor, player: Player, npc: Npc, dmg: int) -> tuple[list[str], bool]:
    """扣 NPC 血，返回 (facts, 是否死了)。死了掉东西，击杀标记算整支队伍的"""
    hp = max(0, npc.hp - dmg)
    facts = [f"{npc.name} HP {hp}/{npc.template.max_hp}"]
    if hp > 0:
        cur.execute("update npcs set hp = %s where id = %s", (hp, npc.id))
        return facts, False
    cur.execute("update npcs set hp = 0, alive = false, died_at = now(), status = null where id = %s", (npc.id,))
    cur.execute("update item_instances set npc_id = null, room_id = %s where npc_id = %s",
                (player.room_id, npc.id))
    facts.append(f"{npc.name}被击败了")
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
    return facts, True


def _hurt_player(cur: Cursor, target: Player, dmg: int) -> tuple[list[str], bool]:
    """扣玩家血，返回 (facts, 是否倒下)。被打的人不自动反击，要还手得自己出手"""
    hp = max(0, target.hp - dmg)
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (hp, target.id))
    facts = [f"{target.name} HP {hp}/{target.max_hp}"]
    if hp == 0:
        facts.append(f"{target.name}倒下了")
    return facts, hp == 0


def _npc_counter(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """NPC 挨打后反击。身上有负面状态就不还手，按施加时的挣脱难度看这回合能不能恢复"""
    if npc.status:
        st = npc.status
        if _roll(ESCAPE_CHANCE[st.escape] + ESCAPE_BONUS * st.attempts):
            _set_status(cur, "npcs", npc.id, None)
            return [f"{npc.name}摆脱了“{st.label}”的状态，但这回合来不及还手"]
        st.attempts += 1
        _set_status(cur, "npcs", npc.id, st)
        return [f"{npc.name}还{st.label}，没法还手"]
    armor = _equipped(cur, player, "armor")
    back = max(1, npc.template.attack - player.defense - (armor.template.defense if armor else 0))
    hp = max(0, player.hp - back)
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (hp, player.id))
    facts = [f"{npc.name}反击，对{player.name}造成 {back} 点伤害", f"{player.name} HP {hp}/{player.max_hp}"]
    if hp == 0:
        facts.append(f"{player.name}倒下了")
    return facts


def do_attack(cur: Cursor, player: Player, view: RoomView, a: Attack) -> list[str]:
    # target 是 ref 就是打 NPC，不是 ref 就当玩家名字（PvP）
    weapon = _equipped(cur, player, "weapon")
    how = weapon.name if weapon else "拳头"
    if a.target not in view.refs:
        target = _pvp_target(cur, player, a.target)
        armor = _equipped(cur, target, "armor")
        dmg = max(1, player.attack + (weapon.template.damage if weapon else 0)
                  - target.defense - (armor.template.defense if armor else 0))
        facts, _ = _hurt_player(cur, target, dmg)
        return [f"{player.name}用{how}攻击{target.name}，造成 {dmg} 点伤害"] + facts

    npc = _room_npc(cur, view, player, a.target)
    if not npc.combatable:
        raise ActionError(f"{npc.name}不是能打的对象")
    dmg = max(1, player.attack + (weapon.template.damage if weapon else 0) - npc.template.defense)
    facts, dead = _hurt_npc(cur, player, npc, dmg)
    facts = [f"{player.name}用{how}攻击{npc.name}，造成 {dmg} 点伤害"] + facts
    return facts if dead else facts + _npc_counter(cur, player, npc)


def do_stunt(cur: Cursor, player: Player, view: RoomView, a: Stunt) -> list[str]:
    """借环境、创意动作打人。AI 给的档位先按地形上限裁剪（没声明的地形最多轻伤），打玩家再降一档"""
    cap, feature = IMPROVISED_MAX_TIER, None
    if a.feature:
        uid = view.resolve(a.feature)
        if uid:
            cur.execute("select id, name, max_tier, uses_left from room_features where id = %s and room_id = %s for update",
                        (uid, player.room_id))
            feature = cur.fetchone()
        if feature is None:
            raise ActionError("这里没有能这么用的东西")
        if feature["uses_left"] <= 0:
            raise ActionError(f"{feature['name']}已经被用过了，暂时没法再用")
        cap = feature["max_tier"]
    tier = _cap(a.tier, cap, TIERS)
    escape = a.escape if feature else "easy"      # 随手的东西弄出来的状态都好挣脱

    if a.target in view.refs:
        target = _room_npc(cur, view, player, a.target)
        if not target.combatable:
            raise ActionError(f"{target.name}不是能打的对象")
    else:
        target = _pvp_target(cur, player, a.target)
        tier, escape = _lower(tier, TIERS), _lower(escape, DIFFICULTIES)
    is_npc = isinstance(target, Npc)

    if feature:
        cur.execute("update room_features set uses_left = uses_left - 1, used_at = coalesce(used_at, now()) where id = %s",
                    (feature["id"],))
    facts = [f"{player.name}尝试：{a.description}"]
    if not _roll(SUCCESS_CHANCE[a.difficulty]):
        facts.append("没有成功")
        return facts + (_npc_counter(cur, player, target) if is_npc else [])

    facts.append("成功了")
    down = False
    if dmg := TIER_DAMAGE[tier]:
        hurt, down = _hurt_npc(cur, player, target, dmg) if is_npc else _hurt_player(cur, target, dmg)
        facts += [f"{target.name}受到 {dmg} 点伤害"] + hurt
    if a.status and not down:
        st = Status(kind=a.status, escape=escape, since=datetime.now(timezone.utc).isoformat(),
                    label=(a.status_label or "").strip()[:20]
                    or ("被打得失去了战斗能力" if a.status == "incapacitated" else "被困住了"))
        _set_status(cur, "npcs" if is_npc else "players", target.id, st)
        facts.append(f"{target.name}{st.describe()}")
        return facts                                  # 刚被放倒、捆住的 NPC 这回合不还手
    if is_npc and not down:
        facts += _npc_counter(cur, player, target)
    return facts


def do_struggle(cur: Cursor, player: Player, view: RoomView, a: Struggle) -> list[str]:
    """挣脱、醒来。AI 看玩家怎么做判这次的难度，但不能比施加时判的容易超过一档；每失败一次下次更容易"""
    st = player.status
    if st is None:
        raise ActionError(f"{player.name}没有被困住，用不着挣脱")
    diff = DIFFICULTIES[max(DIFFICULTIES.index(a.difficulty), DIFFICULTIES.index(st.escape) - 1)]
    facts = [f"{player.name}尝试：{a.description or '挣脱'}"]
    if _roll(ESCAPE_CHANCE[diff] + ESCAPE_BONUS * st.attempts):
        _set_status(cur, "players", player.id, None)
        return facts + [f"{player.name}摆脱了“{st.label}”的状态"]
    st.attempts += 1
    _set_status(cur, "players", player.id, st)
    return facts + [f"{player.name}还{st.label}，没能摆脱"]


def do_revive(cur: Cursor, player: Player, view: RoomView, a: Revive) -> list[str]:
    """急救倒下的人（救起来 1 HP），也能帮人松绑、叫醒"""
    target, _ = _room_player(cur, player, a.target, lock=True)
    if target.hp > 0 and target.status is None:
        raise ActionError(f"{target.name}没有倒下也没被困住，用不着急救")
    facts = []
    if target.hp <= 0:
        cur.execute("update players set hp = 1, updated_at = now() where id = %s", (target.id,))
        facts += [f"{player.name}给{target.name}做了急救，{target.name}醒了过来", f"{target.name} HP 1/{target.max_hp}"]
    if target.status:
        _set_status(cur, "players", target.id, None)
        facts.append(f"{player.name}帮{target.name}摆脱了“{target.status.label}”的状态")
    return facts


# ============ 组队 ============

def _party_names(cur: Cursor, party_id: UUID) -> list[str]:
    cur.execute("select name from players where party_id = %s order by name", (party_id,))
    return [r["name"] for r in cur.fetchall()]


def _leave_party(cur: Cursor, player: Player) -> None:
    """退队；队伍只剩一个人就解散"""
    cur.execute("update players set party_id = null where id = %s", (player.id,))
    cur.execute(
        """update players set party_id = null
           where party_id = %(p)s and (select count(*) from players where party_id = %(p)s) = 1""",
        {"p": player.party_id},
    )


def do_invite(cur: Cursor, player: Player, view: RoomView, a: Invite) -> list[str]:
    target, _ = _room_player(cur, player, a.target)
    if player.party_id and player.party_id == target.party_id:
        raise ActionError(f"{target.name}已经是{player.name}的队友了")
    if player.party_id and len(_party_names(cur, player.party_id)) >= PARTY_MAX:
        raise ActionError(f"队伍已经满了（最多 {PARTY_MAX} 人）")
    cur.execute(
        """insert into party_invites (inviter, invitee) values (%s, %s)
           on conflict (inviter, invitee) do update set created_at = now()""",
        (player.id, target.id),
    )
    return [f"{player.name}邀请{target.name}加入队伍"]


def do_join(cur: Cursor, player: Player, view: RoomView, a: Join) -> list[str]:
    cur.execute(
        f"""select i.inviter from party_invites i join players p on p.id = i.inviter
            where i.invitee = %s and p.name = %s and i.created_at > now() - interval '{INVITE_WINDOW}'""",
        (player.id, a.target),
    )
    row = cur.fetchone()
    if row is None:
        raise ActionError(f"{a.target}没有邀请{player.name}组队，或者邀请已经过期")
    inviter = load_player(cur, row["inviter"], lock=True)
    if player.party_id and player.party_id == inviter.party_id:
        raise ActionError(f"{player.name}已经在{inviter.name}的队伍里了")
    party_id = inviter.party_id or uuid4()
    if inviter.party_id is None:
        cur.execute("update players set party_id = %s where id = %s", (party_id, inviter.id))
    elif len(_party_names(cur, party_id)) >= PARTY_MAX:
        raise ActionError(f"{inviter.name}的队伍已经满了（最多 {PARTY_MAX} 人）")
    if player.party_id:
        _leave_party(cur, player)
    cur.execute("update players set party_id = %s where id = %s", (party_id, player.id))
    cur.execute("delete from party_invites where inviter = %s and invitee = %s", (inviter.id, player.id))
    return [f"{player.name}加入了{inviter.name}的队伍", "队伍成员：" + "、".join(_party_names(cur, party_id))]


def do_leave_party(cur: Cursor, player: Player, view: RoomView, a: LeaveParty) -> list[str]:
    if not player.party_id:
        raise ActionError(f"{player.name}没有在队伍里")
    _leave_party(cur, player)
    return [f"{player.name}离开了队伍"]


def do_talk(cur: Cursor, player: Player, view: RoomView, a: Talk) -> list[str]:
    # 这里只校验对象在场，NPC 怎么回、给不给东西由对话步骤决定（见 giveable_items / npc_give）
    npc = _room_npc(cur, view, player, a.target)
    return [f"{player.name}对{npc.name}说：“{a.message}”"]


def do_give(cur: Cursor, player: Player, view: RoomView, a: Give) -> list[str]:
    item = _inv_item(cur, view, player, a.item)
    npc = _room_npc(cur, view, player, a.target)
    _move_item(cur, item, npc_id=npc.id)
    return [f"{player.name}把{_label(item)}交给了{npc.name}"]


def do_say(cur: Cursor, player: Player, view: RoomView, a: Say) -> list[str]:
    if a.target is None:
        return [f"{player.name}说：“{a.message}”"]
    cur.execute("select 1 from players where room_id = %s and name = %s and id <> %s",
                (player.room_id, a.target, player.id))
    if not cur.fetchone():
        raise ActionError(f"{a.target}不在这里")
    return [f"{player.name}对{a.target}说：“{a.message}”"]


def do_freeform(cur: Cursor, player: Player, view: RoomView, a: Freeform) -> list[str]:
    # 不改任何状态，叙事 AI 自由发挥
    return [f"{player.name}尝试：{a.description}"]


def do_reject(cur: Cursor, player: Player, view: RoomView, a: Reject) -> list[str]:
    raise ActionError(a.reason)


HANDLERS: dict[str, Callable[..., list[str]]] = {
    "move": do_move, "look": do_look, "take": do_take, "drop": do_drop, "use": do_use,
    "equip": do_equip, "attack": do_attack, "talk": do_talk, "give": do_give, "freeform": do_freeform,
    "say": do_say, "revive": do_revive, "stunt": do_stunt, "struggle": do_struggle, "invite": do_invite, "join": do_join, "leave_party": do_leave_party,
    "reject": do_reject,
}


# ============ 入口 ============

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
                if st and (action.action in RESTRAINED_BLOCKED
                           or st.kind == "incapacitated" and action.action != "struggle"):
                    raise ActionError(f"{player.name}{st.label}，做不到")
                facts = HANDLERS[action.action](cur, player, view, action)
            return ActionResult(action=action.action, success=True, facts=facts)
        except ActionError as e:
            return ActionResult(action=action.action, success=False, facts=[str(e)])
        except pg_errors.DeadlockDetected:
            if attempt:
                raise


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


NPC_MEMORY_LIMIT = 150                  # NPC 对每个玩家的记忆摘要上限（字）


def get_npc_memory(conn: Connection, player_id: UUID, npc_id: UUID) -> str:
    """NPC 对这个玩家记得什么（按 NPC 模板记，NPC 复活、重新 seed 都不丢）"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute(
            """select r.memory from player_npc_relations r join npcs n on n.template_id = r.npc_template
               where r.player_id = %s and n.id = %s""",
            (player_id, npc_id),
        )
        row = cur.fetchone()
        return row["memory"] if row else ""


def set_npc_memory(conn: Connection, player_id: UUID, npc_id: UUID, memory: str) -> None:
    """对话后更新 NPC 的记忆摘要。只是 NPC 的印象，不是游戏状态，所以由 AI 写，这里只截断长度"""
    memory = memory.strip()[:NPC_MEMORY_LIMIT]
    with conn.transaction():
        conn.execute(
            """insert into player_npc_relations (player_id, npc_template, memory)
               select %s, template_id, %s from npcs where id = %s
               on conflict (player_id, npc_template) do update set memory = excluded.memory""",
            (player_id, memory, npc_id),
        )


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
