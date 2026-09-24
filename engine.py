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
import math
import random
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import Connection, Cursor
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import dungeon
from commands import REST_TALK_RE
from schema import (
    ActionResult, Attack, Drop, Equip, Feature, Follow, Freeform, Give, Invite, ItemInstance, Join, LeaveParty, Look,
    Move, Npc, OtherPlayer, Player, PlayerAction, Reject, Revive, Room, RoomExit, RoomView, Say, Status, Struggle,
    Stunt, Take, Talk, Unfollow, Unequip, Use, dir_name, Dispenser, SLOT_CHOICES, SLOT_NAMES, Dodge, Hide, Maneuver, Search, Stealth,
    AcceptDuel, Challenge, DeclineDuel, Duel, Flee, SKILL_NAMES, Upgrade, Respawn, Stand, Rest, Pay, Camp, Teleport, Sell,
    Effect,
)


START_ROOM = "square"                   # 新角色出生、后台传送回去的地方
ONLINE_WINDOW = "90 seconds"            # 超过这么久没有心跳的玩家算睡着。后台标签页浏览器会降低定时器频率，给宽一点
AFFINITY_STEP = 5                       # 对话 AI 每次最多调整的好感度
AFFINITY_RANGE = (-100, 100)
PARTY_MAX = 5                           # 一支队伍最多几个人
INVITE_WINDOW = "10 minutes"            # 组队邀请多久内有效
DOWNED_ALLOWED = {"look", "say", "respawn"}   # 倒下的人只能看、说话（喊人来救），或者选择被抬回酒馆
RESPAWN_ROOM = "tavern"                 # 倒下的人选择复活时默认被抬去的地方

# 创意攻击（stunt）和负面状态。AI 只选档位和难度，数字都在这里
TIERS = ["none", "light", "heavy", "lethal"]
# 伤害在区间里随机：自由动作是控场用的，新手拿它打伤害不如老老实实砍一刀
TIER_RANGE = {"none": (0, 0), "light": (1, 3), "heavy": (3, 6), "lethal": (5, 9)}
TIER_MIN_DIFFICULTY = {"none": 1, "light": 2, "heavy": 3, "lethal": 4}   # 伤得越重难度越高（对无力反抗的补刀不算）
IMPROVISED_MAX_TIER = "light"           # 空手、随手的东西（没在 world.yaml 声明成可利用地形）最多轻伤
WEAPON_MAX_TIER = "heavy"               # 拿武器的花样（挑、劈、刺喉）最多重伤
WEAPON_STUNT_SHARE = 0.5                # 拿武器的花样打中了，档位伤害之外再加武器伤害的一半（向下取整）
STATUS_MAX = "3 minutes"                # 负面状态最长多久自动解除，防止 AI 一直判醒不过来把人卡死
# 被束缚时做不了的动作；失去战斗能力时只能挣脱（醒来）
RESTRAINED_BLOCKED = {"move", "attack", "stunt", "take", "drop", "equip", "unequip", "use", "give", "revive", "hide", "search",
                      "maneuver", "dodge", "flee"}
# 倒地时做不了的（得先站起来）；吃喝、说话、看、换装备都行
PRONE_BLOCKED = {"move", "attack", "stunt", "maneuver", "dodge", "flee", "hide", "search", "take", "revive", "respawn"}
PRONE_HIT_BONUS = 0.20                  # 打倒在地上的目标，近战命中率加这么多

# 敌人发现玩家：进门时几率 DETECT_START，之后玩家每发一条消息掷一次骰，没被发现就涨 DETECT_STEP（躲着不涨）。
# 被发现了，在场能动的敌人每轮都打他一下（玩家每做 ENEMY_EVERY 个动作敌人行动一次，见 execute_all）
DETECT_START = 0.05
DETECT_STEP = 0.10
DETECT_STEP_MIN = 0.02                  # 隐匿每级让每次上涨少 1%，最少涨这么多

# 同一区域里的距离（格）：刚发现时隔 START_DISTANCE 格。近战（普通攻击，玩家打敌人、敌人打玩家）按距离算命中，
# 够不着的格数命中率是 0。扔东西、推石头这类 stunt 不看距离。
# 玩家挪几格、把敌人踹开几格由 AI 判（每次最多 MAX_STEP）；敌人被发现后每条消息自己逼近 ENEMY_STEP 格再出手
START_DISTANCE = 2
MAX_DISTANCE = 4
MAX_STEP = 2
ENEMY_STEP = 1
MELEE_HIT = {0: 0.95, 1: 0.45, 2: 0.05}
DODGE_BONUS = 0.15                      # 闪避动作让敌人这一下的命中率降低这么多，察觉每级再多降 DODGE_PER_LEVEL
DODGE_PER_LEVEL = 0.05
DISTANCE_WORDS = {0: "贴身", 1: "一步之遥", 2: "几步开外"}
# 玩家每做这么多个动作，敌人行动一次（一句话结尾不够数也算一次）。敌人有动静（发现、逼近、出手）就打断后面的，
# 免得一条超长的消息一口气打出一串伤害；被绊倒的敌人轮到时只是爬起来，不打断
ENEMY_EVERY = 2

# 徒手一击毙命、直接打晕：只有敌人没发现你时能偷袭，按隐匿判，难度至少这么高；对方有防备就不可能
ASSASSINATE_DIFFICULTY = 4
PRONE_DECISIVE_DIFFICULTY = 3           # 对刚被绊倒在地的下狠手（打晕、断手、一击毙命）难度至少这么高

# 技能判定：难度减技能等级的差值 → 成功率，差值不超过 0 是 SKILL_SURE，比表里最大的还大就必定失败
SKILL_SURE = 0.95
SKILL_GAP_CHANCE = {1: 0.9, 2: 0.6, 3: 0.3}
# 从 n 级升到 n+1 级要攒 SKILL_STEP * (n+1) 次熟练；只有难度高于当前等级的成功才算熟练
SKILL_STEP = 3
# 耐性：每升一级 HP 上限 +ENDURANCE_HP。除了硬扛的判定，挨打（活下来的）、扛住没喝醉有 ENDURE_HIT_CHANCE 的几率涨熟练，
# 吃了有毒的东西活下来一定涨；耐性每级让喝醉的几率乘 DRUNK_RESIST
ENDURANCE_HP = 3
ENDURE_HIT_CHANCE = 0.25
DRUNK_RESIST = 0.85
FLEE_DIFFICULTY = {0: 3, 1: 2, 2: 1}    # 决斗逃跑（运动）按距离的难度，更远不用判定
REVIVE_DIFFICULTY = 1                   # 急救倒下的人（医药）

# 喝酒（名字带"酒"字，或者 props.alcohol；毒酒也算，毒照样生效）有几率喝醉：一段时间里说话含糊
# （系统往原话里插"嗝""……"），技能判定和普通攻击命中率都降低。几率随连着喝的杯数指数上升：
# 第 n 杯是 DRUNK_CHANCE × 2^(n-1)，DRINK_WINDOW 里没再喝就重新数。醉着再喝重新计时；吃解酒药（props.sober）马上清醒
DRUNK_CHANCE = 0.2
DRINK_WINDOW = "10 minutes"
DRUNK_TIME = "5 minutes"
DRUNK_PENALTY = 0.2

# 搜索找东西（world.yaml 房间 forage）的默认几率和冷却秒数：默认一搜就有、不冷却，房间里可以单独配
FORAGE_CHANCE = 1.0
FORAGE_COOLDOWN = 0

# 决斗（PvP）：申请多久内有效；接受后双方隔几格开打；发起者逃跑按当时的距离掷骰，逃掉了才能离开
DUEL_WINDOW = "5 minutes"
DUEL_DISTANCE = 3
DUEL_RULES = ("决斗规则：对方接受后才开打，只有决斗中才会对彼此造成伤害；双方起始相隔 3 格；"
              "任何一方离开这里决斗就结束，但发起者必须先逃跑成功才能离开，被挑战的一方随时可以走开")


class ActionError(Exception):
    """动作不合法。事务回滚，message 作为失败的 fact"""


# ============ 读取 ============

ITEM_SELECT = """
select i.id, i.quantity, i.room_id, i.player_id, i.npc_id, i.equipped_slot, i.props,
       row_to_json(t) as template
from item_instances i join item_templates t on t.id = i.template_id
"""

NPC_SELECT = """
select n.id, n.room_id, n.hp, n.alive, n.memory, n.status, n.effects, row_to_json(t) as template
from npcs n join npc_templates t on t.id = n.template_id
"""


def _cursor(conn: Connection) -> Cursor:
    return conn.cursor(row_factory=dict_row)


def load_player(cur: Cursor, player_id: UUID, lock: bool = False) -> Player:
    cur.execute(
        "select id, name, room_id, hp, max_hp, attack, defense, flags, party_id, status, following, gold, stealth, skills, effects,"
        " coalesce(drunk_until > now(), false) as drunk"
        " from players where id = %s"
        + (" for update" if lock else ""),
        (player_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ActionError("玩家不存在")
    return Player(**row)


def load_room(cur: Cursor, room_id: str) -> Room:
    cur.execute("select id, name, description, details, props from rooms where id = %s", (room_id,))
    return Room(**cur.fetchone())


def forage_labels(cur: Cursor, room: Room) -> list[str]:
    """搜索能找到的东西，写明要搜、几率多少，不然没人会去搜：["药草（搜索，60%）"]"""
    forage = room.props.get("forage", [])
    if not forage:
        return []
    cur.execute("select id, name from item_templates where id = any(%s)", ([f["item"] for f in forage],))
    names = {r["id"]: r["name"] for r in cur.fetchall()}
    labels = []
    for f in forage:
        chance = f.get("chance", FORAGE_CHANCE)
        labels.append(f"{names[f['item']]}（搜索）" if chance >= 1 else f"{names[f['item']]}（搜索，{round(chance * 100)}%）")
    return labels


def load_dispensers(cur: Cursor, room: Room, player_id: UUID) -> list[Dispenser]:
    """房间里的取用处（武器桶、摆着护符的桌子），id 按房间和 key 算，每次都一样。
    available 按这个玩家算：身上已经有了（或有 unless 里的东西）就拿不了，只显示桶、桌子本身"""
    cfg = room.props.get("dispensers", {})
    if not cfg:
        return []
    cur.execute("select id, name from item_templates where id = any(%s)", ([d["item"] for d in cfg.values()],))
    names = {r["id"]: r["name"] for r in cur.fetchall()}
    cur.execute("select distinct template_id from item_instances where player_id = %s", (player_id,))
    owned = {r["template_id"] for r in cur.fetchall()}
    cur.execute("select key from dispenser_log where player_id = %s and room_id = %s", (player_id, room.id))
    taken = {r["key"] for r in cur.fetchall()}
    return [Dispenser(id=uuid5(NAMESPACE_URL, f"newera:dispenser:{room.id}:{key}"), room=room.id, key=key,
                      container=d["name"], description=d.get("description", ""), item=d["item"],
                      item_name=names[d["item"]], unless=d.get("unless", []), where=d.get("where", "里"),
                      once=d.get("once", False), repeat=d.get("repeat", False), skill=d.get("skill"),
                      difficulty=d.get("difficulty", 0), fail=d.get("fail", ""), fail_damage=d.get("fail_damage", 0),
                      available=(d.get("repeat") or not owned & {d["item"], *d.get("unless", [])})
                      and not (d.get("once") and key in taken))
            for key, d in cfg.items()]


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
    """刷新：NPC 复活、门自动锁回、物品重新出现、看店的 NPC 扶起倒下的人。
    不用后台任务，有人在这个房间（发命令或页面轮询）时顺手检查，没人的房间不用管"""
    _respawn_npcs(cur, room_id)
    _keeper_revive(cur, room_id)
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


# 看店 NPC 扶起人之后，服务器用它让 AI 按人设、好感、倒下的原因现写一句话，稍后作为房间动态出现。
# 引擎不调 AI，没设（纯规则模式）就用 world.yaml 的 revive_lines 当场说一句
# 参数：(房间 id, NPC 模板 id, 玩家 id, 玩家名字, 倒下的原因 {kind, by}, 保底台词)
REVIVE_HOOK: Optional[Callable[[str, str, UUID, str, dict, str], None]] = None


def _keeper_revive(cur: Cursor, room_id: str) -> list[str]:
    """看店的 NPC（配了 revive_lines、不敌对、醒着）把自己店里倒下的人扶起来，回满血。
    房间里的人会看到一条动态；返回 facts 给当回合用"""
    cur.execute(
        """select t.id, t.name, t.props->'revive_lines' as lines from npcs n join npc_templates t on t.id = n.template_id
           where n.room_id = %s and n.alive and n.status is null and not t.hostile and t.props ? 'revive_lines'
           order by t.name limit 1""",
        (room_id,),
    )
    keeper = cur.fetchone()
    if keeper is None:
        return []
    cur.execute(f"""update players set hp = max_hp + {RESTORE_MAX_HP}, max_hp = max_hp + {RESTORE_MAX_HP},
                        effects = '[]', updated_at = now() where room_id = %s and hp <= 0
                    returning id, name, max_hp, downed_by""", (room_id,))
    facts = []
    for r in cur.fetchall():
        # 按倒下的原因说句话：有 REVIVE_HOOK 就让 AI 现写（稍后出现），world.yaml 的 revive_lines 只当保底
        why = r["downed_by"] or {}
        lines = keeper["lines"] or {}
        line = lines.get(why.get("kind")) or lines.get("other")
        quip = line.format(name=r["name"], by=why.get("by", "")) if line else ""
        fact = f"{keeper['name']}把倒在地上的{r['name']}扶了起来，照料了一番，{r['name']}缓过劲来，HP {r['max_hp']}/{r['max_hp']}"
        if REVIVE_HOOK:
            REVIVE_HOOK(room_id, keeper["id"], r["id"], r["name"], why, quip)
        elif quip:
            fact = f"{fact}。{quip}"
        # 不记在谁名下，房间里所有人（包括被扶起来的本人）都看得到
        cur.execute("insert into events (room_id, kind, observer) values (%s, 'keeper_revive', %s)", (room_id, fact + "。"))
        facts.append(fact)
    return facts


def _respawn_npcs(cur: Cursor, room_id: str, found: bool = False) -> list[str]:
    """NPC 死了 respawn_seconds 秒后，没有醒着的玩家在场就悄悄原地复活；
    有人搜寻（found=True）就不用等，直接找出来。返回复活的名字"""
    cur.execute(
        f"""update npcs n set alive = true, hp = t.max_hp, died_at = null, status = null
            from npc_templates t
            where t.id = n.template_id and n.room_id = %s and not n.alive
              and t.props ? 'respawn_seconds'
              and (%s or n.died_at < now() - make_interval(secs => (t.props->>'respawn_seconds')::int)
                   and not exists (select 1 from players p where p.room_id = n.room_id
                                   and p.last_active_at > now() - interval '{ONLINE_WINDOW}'))
            returning t.name""",
        (room_id, found),
    )
    return [r["name"] for r in cur.fetchall()]


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
        following = None
        if player.following:
            cur.execute("select name from players where id = %s", (player.following,))
            following = cur.fetchone()["name"]
        room = load_room(cur, player.room_id)
        if env := env_text(cur, room):
            room.details = (room.details + "\n" + env).strip()
        _end_stale_duels(cur)
        duel = _active_duel(cur, player.id)
        cur.execute(
            """select p.name from duels d join players p on p.id = d.challenger
               where d.target = %s and not d.accepted order by d.created_at""", (player.id,))
        challenges = [r["name"] for r in cur.fetchall()]
        cur.execute("select p.name from duels d join players p on p.id = d.target where d.challenger = %s and not d.accepted",
                    (player.id,))
        challenging = (cur.fetchone() or {}).get("name")
        view = RoomView(
            player=player,
            room=room,
            exits=load_exits(cur, player.room_id),
            items=load_items(cur, "i.room_id = %s", (player.room_id,)),
            npcs=load_npcs(cur, "n.room_id = %s and n.alive", (player.room_id,)),
            inventory=load_items(cur, "i.player_id = %s", (player.id,)),
            others=others,
            party=party,
            invites=invites,
            features=features,
            dispensers=load_dispensers(cur, room, player.id),
            forage=forage_labels(cur, room),
            following=following,
            duel=duel and Duel(opponent=duel["opponent"], challenger=duel["challenger"] == player.id,
                               distance=duel["distance"]),
            challenges=challenges,
            challenging=challenging,
        )
    view.assign_refs()
    return view


# ============ 决斗 ============

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
            ok, rolled = _check(cur, player, view, "athletics", diff)
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


def _worn(cur: Cursor, player: Player) -> list[ItemInstance]:
    """身上装备着的全部东西"""
    return load_items(cur, "i.player_id = %s and i.equipped_slot is not null", (player.id,))


def _weapons(cur: Cursor, player: Player) -> list[ItemInstance]:
    """手上拿着的武器（双持就是两把，副手只加一部分，见 weapon_damage）"""
    return [i for i in _worn(cur, player) if i.template.type == "weapon" and i.equipped_slot in SLOT_CHOICES["hand"]]


# ============ 装备的特殊效果（物品 props.effects，写法见 items_dungeon.yaml 开头）============
# 叠加规则：固定数值加起来（血量上限、技能、逃跑难度、伤害加成、减伤、破甲）；
# 百分比和几率只取最好的一件（金币加成、闪避、吸血、抗性、光亮）
def _fx(items: list[ItemInstance], do: str, when: str = "passive", kind: Optional[str] = None) -> list[dict]:
    return [e for i in items for e in (_prop(i, "effects") or [])
            if e.get("when") == when and e.get("do") == do and (kind is None or e.get("kind") == kind)]


def gear_add(cur: Cursor, player: Player, do: str, kind: Optional[str] = None) -> float:
    return sum(e.get("value", 0) for e in _fx(_worn(cur, player), do, kind=kind))


def gear_has(cur: Cursor, player: Player, do: str) -> bool:
    return bool(_fx(_worn(cur, player), do))


def gear_resist(cur: Cursor, player: Player, kind: str) -> float:
    """状态几率乘多少：0 是免疫，0.5 减半；几件取最好的"""
    return min([1.0] + [e.get("value", 1) for e in _fx(_worn(cur, player), "resist", kind=kind)])


def gear_gold(cur: Cursor, player: Player) -> float:
    return max([0.0] + [e.get("value", 0) for e in _fx(_worn(cur, player), "gold")])


def _ally_down(cur: Cursor, player: Player) -> bool:
    if not player.party_id:
        return False
    cur.execute("select 1 from players where party_id = %s and room_id = %s and hp <= 0 and id <> %s limit 1",
                (player.party_id, player.room_id, player.id))
    return cur.fetchone() is not None


def _fire(cur: Cursor, player: Player, when: str, do: Optional[str] = None, npc: Optional[Npc] = None) -> list[dict]:
    """这一刻（when）身上装备触发了哪些效果：对得上 vs 标签、满足 if 条件、掷中 chance 的"""
    out = []
    for e in (e for i in _worn(cur, player) for e in (_prop(i, "effects") or [])
              if e.get("when") == when and (do is None or e.get("do") == do)):
        if (vs := e.get("vs")) and not (npc and any(npc.template.props.get(t) for t in vs)):
            continue
        cond = e.get("if") or {}
        if "hp_below" in cond and not player.hp < player.max_hp * cond["hp_below"]:
            continue
        if "target_hp_below" in cond and not (npc and npc.combatable and npc.hp < npc.template.max_hp * cond["target_hp_below"]):
            continue
        if cond.get("ally_down") and not _ally_down(cur, player):
            continue
        if e.get("chance", 1) < 1 and not _roll(e["chance"]):
            continue
        out.append(e)
    return out


def _labels(effects: list[dict]) -> list[str]:
    return [e["label"] for e in effects if e.get("label")]


def _aura(cur: Cursor, player: Player, kind: str) -> int:
    """军团徽记这类光环：同房间的队友（和自己）身上最强的一件"""
    cur.execute("""select i.id from item_instances i join players p on p.id = i.player_id
                   where i.equipped_slot is not null and p.room_id = %s
                     and (p.id = %s or (p.party_id is not null and p.party_id = %s))""",
                (player.room_id, player.id, player.party_id))
    ids = [r["id"] for r in cur.fetchall()]
    items = load_items(cur, "i.id = any(%s)", (ids,)) if ids else []
    return max([0] + [int(e.get("value", 0)) for e in _fx(items, "aura", kind=kind)])


# 装备打到怪身上的状态：怪也能中毒、流血（每轮敌人行动掉血）、看不清（命中 ×NPC_BLIND_HIT）、腐蚀（防御 -NPC_CORRODE_DEF）；
# 定身 stun、缠住、撞倒用怪原来的状态
NPC_EFFECT_TURNS = {"poison": 3, "bleed": 3, "blind": 2, "corrode": 3}
NPC_BLIND_HIT, NPC_CORRODE_DEF = 0.25, 2


def _npc_effect(npc: Npc, kind: str) -> Optional[Effect]:
    return next((e for e in npc.effects if e.kind == kind), None)


def _save_npc_effects(cur: Cursor, npc: Npc) -> None:
    cur.execute("update npcs set effects = %s where id = %s", (Jsonb([e.model_dump() for e in npc.effects]), npc.id))


def _npc_affect(cur: Cursor, player: Player, npc: Npc, kind: str, label: str) -> list[str]:
    """给怪上一个状态（装备触发的）"""
    if not npc.alive or npc.hp is None or npc.hp <= 0:
        return []
    if kind in ("stun", "restrained", "prone"):
        if npc.status:
            return []
        st = Status(kind="incapacitated" if kind == "stun" else kind, label=(label or "动弹不得")[:20], escape=2,
                    since=datetime.now(timezone.utc).isoformat())
        _set_status(cur, "npcs", npc.id, st)
        npc.status = st
        return [f"{npc.name}{label}" if label else f"{npc.name}{st.describe()}"]
    depth = npc.template.props.get("dungeon", {}).get("depth", 1)
    value = NPC_CORRODE_DEF if kind == "corrode" else 1 + depth // 5
    npc.effects = [e for e in npc.effects if e.kind != kind] + [
        Effect(kind=kind, value=value, left=NPC_EFFECT_TURNS[kind], label=(label or EFFECT_NAMES[kind])[:20], source=player.name)]
    _save_npc_effects(cur, npc)
    return [f"{npc.name}{label or ''}（{EFFECT_NAMES[kind]}）"]


def _tick_npc_effects(cur: Cursor, player: Player, npcs: list[Npc]) -> list[str]:
    """敌人每轮行动前：身上的中毒、流血掉血，时间到了消退"""
    facts = []
    for npc in npcs:
        if not npc.effects:
            continue
        keep = []
        for e in npc.effects:
            if e.left <= 0:
                facts.append(f"{npc.name}身上的{EFFECT_NAMES[e.kind]}消退了")
                continue
            if e.kind in ("poison", "bleed") and npc.hp > 0:
                hurt, dead = _hurt_npc(cur, player, npc, e.value)
                npc.hp = max(0, npc.hp - e.value)
                facts += [f"{npc.name}{EFFECT_NAMES[e.kind]}，掉了 {e.value} 点血"] + hurt
                if dead:
                    npc.alive = False
                    break
            e.left -= 1
            keep.append(e)
        if npc.alive:
            npc.effects = keep
            _save_npc_effects(cur, npc)
    return facts


def _attack_extras(cur: Cursor, player: Player, npc: Npc, fired: list[dict]) -> list[str]:
    """普通攻击出手时（打没打中都算）触发的：甩开铁链扫到别的敌人、握柄的刃咬手"""
    facts = []
    for e in fired:
        if e["do"] == "splash":
            for other in [n for n in _enemies(cur, player.room_id) if n.id != npc.id]:
                hurt, _ = _hurt_npc(cur, player, other, int(e.get("value", 1)))
                facts += [f"{other.name}被扫到，受到 {e.get('value', 1)} 点伤害"] + hurt
        elif e["do"] == "self_damage":
            hurt, _ = _hurt_player(cur, player, int(e.get("value", 1)), "other", "手里的凶器")
            facts += [f"{player.name}自己掉了 {e.get('value', 1)} 点血"] + hurt
    return _labels(fired) + facts


def _hit_extras(cur: Cursor, player: Player, npc: Npc, dmg: int, dead: bool, fired: list[dict]) -> list[str]:
    """普通攻击打中以后触发的：上状态、全体上状态、连锁、吸血；杀死了就是杀敌效果"""
    facts = []
    if dead:
        for e in _fire(cur, player, "kill", npc=npc):
            if e["do"] == "heal":
                facts += _labels([e]) + _heal_player(cur, player, int(e.get("value", 1)))
        return facts
    for e in fired:
        if e["do"] == "status":
            facts += _npc_affect(cur, player, npc, e["kind"], e.get("label", ""))
        elif e["do"] == "status_all":
            facts += _labels([e])
            for other in _enemies(cur, player.room_id):
                facts += _npc_affect(cur, player, other, e["kind"], "")
        elif e["do"] == "chain":
            others = [n for n in _enemies(cur, player.room_id) if n.id != npc.id]
            if others:
                other = random.choice(others)
                hurt, _ = _hurt_npc(cur, player, other, int(e.get("value", 1)))
                facts += _labels([e]) + [f"{other.name}受到 {e.get('value', 1)} 点伤害"] + hurt
    if leech := max([0] + [e.get("value", 0) for e in fired if e["do"] == "leech"]):      # 吸血取最好的一件
        facts += _heal_player(cur, player, math.ceil(dmg * leech))
    return facts


def _heal_player(cur: Cursor, player: Player, amount: int) -> list[str]:
    hp = min(player.max_hp, player.hp + amount)
    if hp == player.hp:
        return []
    cur.execute("update players set hp = %s where id = %s", (hp, player.id))
    gained, player.hp = hp - player.hp, hp
    return [f"{player.name}回了 {gained} 点血，当前 HP {hp}/{player.max_hp}"]


def _sync_gear_hp(cur: Cursor, player_id: UUID) -> list[str]:
    """装备带的血量上限（活力之戒 +3、贪婪之戒 -3）：跟 players.gear_hp 比，差多少就改多少，当前血量不超过上限"""
    player = load_player(cur, player_id, lock=True)
    bonus = round(gear_add(cur, player, "max_hp"))
    cur.execute("select gear_hp from players where id = %s", (player_id,))
    had = cur.fetchone()["gear_hp"]
    if bonus == had:
        return []
    cur.execute("""update players set max_hp = greatest(1, max_hp + %s), gear_hp = %s,
                   hp = least(hp, greatest(1, max_hp + %s)) where id = %s returning max_hp""",
                (bonus - had, bonus, bonus - had, player_id))
    return [f"{player.name}的血量上限{'+' if bonus > had else ''}{bonus - had}（身上的装备），现在上限 {cur.fetchone()['max_hp']}"]


# ============ 伤害怎么被防御挡掉 ============
# 按挨打的一方分：挨打的是玩家（怪打人、NPC 打人、决斗）按比例减伤，每一点防御都有用、越往上越少；
# 挨打的是怪和 NPC 还是减法（怪的防御是我们定的，不会像玩家那样被装备叠上去，重甲怪就该硬，破甲才有价值）。
# 中毒流血每回合掉的血、陷阱、事件失败、吃了有毒的东西都是固定值，不看防御（这正是对付高防玩家的手段）。
# 玩家挨打的结算顺序（以后加效果照这个顺序插）：
#   1 攻击方的破甲先扣掉防御  2 伤害 = 攻击 × K ÷ (K + 防御)  3 小数按几率进位（2.5 就一半 2 一半 3）
#   4 防守方的减伤（guard，比如项圈 -1）  5 最少 1 点
DEF_K = 4


def def_k(depth: int = 0) -> int:
    """比例减伤的常数：越大防御越不顶用。先写死，以后当深层难度的旋钮（比如 4 + 层数/5）"""
    return DEF_K


def hurt_player_by(atk: int, defense: int, depth: int = 0, pierce: int = 0, guard: int = 0) -> int:
    """玩家挨一下打实际掉多少血（顺序见上面）"""
    k = def_k(depth)
    x = atk * k / (k + max(0, defense - pierce))
    dmg = int(x) + (random.random() < x - int(x))
    return max(1, dmg - guard)


def _defense(cur: Cursor, player: Player) -> int:
    """基础防御加上所有装备的防御（护甲、护符、以后的盾）"""
    corrode = _effect(player, "corrode")
    return max(0, player.defense + sum(i.defense for i in _worn(cur, player)) + _aura(cur, player, "defense")
               - (corrode.value if corrode else 0))


OFFHAND_SHARE = 0.25                    # 双持时副手（左手）武器只加这么多伤害，向下取整；只拿一把的不管在哪只手都算主手


def weapon_damage(weapons: list[ItemInstance]) -> int:
    """手上武器加的伤害：主手（右手，右手空着就是那唯一一把）全额，副手按 OFFHAND_SHARE"""
    main = next((w for w in weapons if w.equipped_slot == "right_hand"), weapons[0] if weapons else None)
    return sum(w.damage if w is main else int(w.damage * OFFHAND_SHARE) for w in weapons)


def _wielding(weapons: list[ItemInstance]) -> str:
    return "和".join(i.name for i in weapons) or "拳头"


def _affinity(cur: Cursor, player: Player, npc: Npc) -> int:
    cur.execute("select affinity from player_npc_relations where player_id = %s and npc_template = %s",
                (player.id, npc.template.id))
    row = cur.fetchone()
    return row["affinity"] if row else 0


# ============ 动作处理 ============
# 签名统一: (cur, player, view, action) -> facts

def do_move(cur: Cursor, player: Player, view: RoomView, a: Move) -> list[str]:
    ex = _find_exit(cur, player.room_id, a.direction, lock=True)
    if ex is None:
        raise ActionError(f"这里没有往{dir_name(a.direction)}的路")
    facts = []
    # 决斗中：发起者得先逃跑成功才能走；被挑战的一方走开，决斗就结束
    if duel := _active_duel(cur, player.id):
        if duel["challenger"] == player.id:
            raise ActionError(f"{player.name}发起的决斗还没结束，得先逃跑成功才能离开")
        cur.execute("delete from duels where challenger = %s", (duel["challenger"],))
        facts.append(f"{player.name}走开了，和{duel['opponent']}的决斗结束了")
    # 被敌人发现、正在交手：得先逃跑成功（甩开了就不算被发现）才能离开
    if _stealth(player).detected and (foes := [n.name for n in _enemies(cur, player.room_id) if n.status is None]):
        raise ActionError(f"{'、'.join(foes)}正缠着{player.name}，得先逃跑成功才能离开")
    if ex["locked"]:
        # 身上带着对应的钥匙就顺手打开，不用玩家专门说"用钥匙开门"
        keys = load_items(cur, "i.player_id = %s and i.template_id = %s", (player.id, ex["key_item"])) \
            if ex["key_item"] else []
        if not keys:
            raise ActionError(f"往{dir_name(a.direction)}的门锁着")
        cur.execute("update room_exits set locked = false, unlocked_at = now() where room_id = %s and direction = %s",
                    (player.room_id, a.direction))
        facts.append(f"{player.name}用{keys[0].name}打开了往{dir_name(a.direction)}的门")
    # 地窖的漆黑入口、地牢楼梯间往下：去哪一层由地牢决定（第一次到的那层当场生成）
    to, arrived = ex["to_room"], []
    if to == dungeon.GATE:
        to, arrived = dungeon.through_gate(cur, player, player.room_id, ONLINE_WINDOW)
    dungeon.touch(cur, player.room_id)
    cur.execute("""update rooms set props = props #- '{env,scroll}'
                   where id = %s and props->'env'->'scroll'->>'by' = %s""", (player.room_id, str(player.id)))
    # 进门前没人在的区域先把该回来的敌人刷出来（走进去才碰上），进门时还没被发现
    _respawn_npcs(cur, to)
    # 自己走就不再跟着别人
    cur.execute("update players set room_id = %s, following = null, stealth = %s, updated_at = now() where id = %s",
                (to, Jsonb(Stealth(room=to, chance=DETECT_START).model_dump()), player.id))
    room = load_room(cur, to)
    facts.append(f"{player.name}往{dir_name(a.direction)}走，来到了{room.name}")
    if heal := sum(int(e.get("value", 0)) for e in _fire(cur, player, "enter", "heal")):
        facts += _heal_player(cur, player, heal)
    facts += arrived + (_torch_floor(cur, [player.name], to) if arrived else [])
    # 同房间跟着他的人一起走：睡着、倒下、带着负面状态的跟不上
    cur.execute(
        f"""update players set room_id = %s, updated_at = now()
            where following = %s and room_id = %s and hp > 0 and status is null
              and last_active_at > now() - interval '{ONLINE_WINDOW}'
              and id not in (select challenger from duels where accepted)
            returning name""",
        (to, player.id, player.room_id),
    )
    names = [r["name"] for r in cur.fetchall()]
    # 地牢里两个房间之间走动：可能碰上陷阱、零钱、跟进来的怪、怪声
    if not arrived and dungeon.is_dungeon(player.room_id) and dungeon.is_dungeon(to) and _roll(dungeon.ROAD_EVENT_CHANCE):
        facts += _road_event(cur, player, view, to)
    if names:
        facts.append(f"{'、'.join(names)}跟着{player.name}一起来到了{room.name}")
        if arrived:
            facts += dungeon.arrived(cur, names, dungeon.parse_room(to)[1]) + _torch_floor(cur, names, to)
    return facts


# 走路随机事件的累计几率：陷阱、零钱、路边小木匣，剩下是怪声（循声去找就会碰上游荡的怪）
ROAD_TRAP, ROAD_COINS, ROAD_CHEST = 0.45, 0.65, 0.8
ROAD_CHEST_ITEMS = ["herb", "bread", "torch", "rope"]     # 路边小木匣里的东西：普通补给，不给好东西


def _road_event(cur: Cursor, player: Player, view: RoomView, to: str) -> list[str]:
    """地牢里走路时碰上的事。陷阱：察觉发现就绕过去，没发现挨一下（活下来耐性一定涨）。
    怪声：在房间上记一笔，有人循声去找（搜索）就引出一只游荡的怪"""
    f = dungeon.floor_info(cur, to)
    depth, r = f["depth"], random.random()
    if r < ROAD_TRAP:
        trap = random.choice(dungeon.data()["traps"])
        ok, rolled = _check(cur, player, view, "perception", 2 + depth // 3)
        if ok:
            return [f"路上{trap}"] + rolled + [f"{player.name}及时察觉，躲了过去"]
        dmg = 2 + depth // 3
        hurt, down = _hurt_player(cur, player, dmg, "other", "陷阱")
        return [f"路上{trap}"] + rolled + [f"{player.name}没能躲开，受到 {dmg} 点伤害"] + hurt             + ([] if down else _toughen(cur, player))
    if r < ROAD_COINS:
        coins = max(1, round(random.randint(1, 3) * 1.2 ** depth))
        cur.execute("update players set gold = gold + %s where id = %s", (coins, player.id))
        return [f"{player.name}在路边的碎石里踢到了 {coins} 枚古币，顺手捡了起来"]
    if r < ROAD_CHEST:
        item = dungeon.road_box_item(f["theme"], depth) or random.choice(ROAD_CHEST_ITEMS)
        _give_player_new(cur, player, item)
        cur.execute("select name from item_templates where id = %s", (item,))
        return [f"{player.name}在路边的碎石下翻出一只烂木匣，里面有一件{cur.fetchone()['name']}，顺手收下了"]
    cur.execute("""update rooms set props = props || '{"noise": true}' where id = %s""", (to,))
    return [random.choice(dungeon.data()["themes"][f["theme"]]["eerie"]),
            "声音像是就在这附近，要是循声去找（搜索），也许能找到它的来源"]


def do_teleport(cur: Cursor, player: Player, view: RoomView, a: Teleport) -> list[str]:
    """传送石边上：回到地窖。地窖里：传送到到过的传送石那一层。同房间跟着他的人一起走"""
    here = player.room_id
    if _active_duel(cur, player.id):
        raise ActionError(f"{player.name}正在决斗，走不了")
    if dungeon.is_dungeon(here) and view.room.props.get("stone"):
        to, facts = dungeon.ENTRANCE, [f"{player.name}把手按在传送石上，纹路里的银光一下子亮起来裹住全身，"
                                       f"再睁眼已经站在了{load_room(cur, dungeon.ENTRANCE).name}的漆黑入口前"]
        dungeon.touch(cur, here)
    elif here == dungeon.ENTRANCE:
        cur.execute("select waypoints from players where id = %s", (player.id,))
        points = cur.fetchone()["waypoints"]
        if a.floor is None or a.floor not in points:
            raise ActionError((f"{player.name}还没到过第 {a.floor} 层的传送石。" if a.floor else "")
                              + dungeon.waypoints_text(points))
        to, facts = dungeon.teleport_to(cur, player, a.floor, ONLINE_WINDOW)
        facts = [f"{player.name}站在漆黑的入口前默念第 {a.floor} 层，一阵银光闪过"] + facts
    else:
        raise ActionError("只有在地牢的传送石边上、或者地窖的漆黑入口前才能传送")
    cur.execute("update players set room_id = %s, following = null, stealth = null, updated_at = now() where id = %s",
                (to, player.id))
    cur.execute(
        f"""update players set room_id = %s, updated_at = now()
            where following = %s and room_id = %s and hp > 0 and status is null
              and last_active_at > now() - interval '{ONLINE_WINDOW}' returning name""", (to, player.id, here))
    names = [r["name"] for r in cur.fetchall()]
    if names:
        facts.append(f"{'、'.join(names)}跟着{player.name}一起传了过去")
        if dungeon.is_dungeon(to):
            facts += dungeon.arrived(cur, names, dungeon.parse_room(to)[1])
    return facts + _torch_floor(cur, [player.name] + names, to)


# 扎营：回 max_hp × CAMP_BASE，带帐篷再加 CAMP_TENT（用掉一顶），生存判定成功再加 CAMP_SURVIVAL，空房里整体 ×CAMP_EMPTY
CAMP_BASE, CAMP_TENT, CAMP_SURVIVAL, CAMP_EMPTY = 0.3, 0.3, 0.15, 1.5


def do_camp(cur: Cursor, player: Player, view: RoomView, a: Camp) -> list[str]:
    """地牢里清完怪的房间扎营，每层每人一次"""
    if not dungeon.is_dungeon(player.room_id):
        raise ActionError("只有在远古地牢里才用得着扎营，村里可以去酒馆住店")
    if foes := [n.name for n in _enemies(cur, player.room_id)]:
        raise ActionError(f"{'、'.join(foes)}还在这里，没法扎营")
    run, depth = dungeon.parse_room(player.room_id)
    cur.execute("""update dungeon_floors set camped = array_append(camped, %s)
                   where run_id = %s and depth = %s and not (%s = any(camped)) returning 1""",
                (player.id, run, depth, player.id))
    if not cur.fetchone():
        raise ActionError(f"{player.name}在这一层已经扎过营了，再歇也缓不过来，得往下走")
    tent = next((i for i in view.inventory if _prop(i, "camp")), None)
    facts = [f"{player.name}在{view.room.name}扎营休息" + (f"，搭起了{tent.name}" if tent else "")]
    share = CAMP_BASE + (CAMP_TENT if tent else 0)
    if tent:
        _consume(cur, tent)
    ok, rolled = _check(cur, player, view, "survival", 1 + depth // 3)
    facts += rolled
    if ok:
        share += CAMP_SURVIVAL
        facts.append("营地收拾得很妥当，睡得更安稳")
    if view.room.props.get("dungeon", {}).get("kind") == "empty":
        share *= CAMP_EMPTY
        facts.append("这里安静空旷，是个扎营的好地方")
    if player.effects:
        # 好好歇一觉，身上的毒、伤口、腐蚀都缓过来了
        player.max_hp += sum(e.hp for e in player.effects)
        facts.append(f"{player.name}身上的{'、'.join(EFFECT_NAMES[e.kind] for e in player.effects)}都缓过来了")
        player.effects = []
        cur.execute("update players set max_hp = %s, effects = '[]' where id = %s", (player.max_hp, player.id))
    hp = min(player.max_hp, player.hp + round(player.max_hp * share))
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (hp, player.id))
    return facts + [f"{player.name}恢复了 {hp - player.hp} 点 HP，当前 HP {hp}/{player.max_hp}"]


def do_follow(cur: Cursor, player: Player, view: RoomView, a: Follow) -> list[str]:
    target, _ = _room_player(cur, player, a.target)
    if target.following == player.id:
        raise ActionError(f"{target.name}正跟着{player.name}，不能互相跟着")
    cur.execute("update players set following = %s where id = %s", (target.id, player.id))
    return [f"{player.name}决定跟着{target.name}，以后{target.name}走到哪就跟到哪（这一回合两人都没有移动）"]


def do_unfollow(cur: Cursor, player: Player, view: RoomView, a: Unfollow) -> list[str]:
    if player.following is None:
        raise ActionError(f"{player.name}没有在跟着谁")
    cur.execute("update players set following = null where id = %s returning (select name from players where id = %s)",
                (player.id, player.following))
    return [f"{player.name}不再跟着{cur.fetchone()['name']}了"]


def do_look(cur: Cursor, player: Player, view: RoomView, a: Look) -> list[str]:
    if a.target is None:
        room = load_room(cur, player.room_id)
        facts = [f"{room.name}：{room.description}"] + ([env] if (env := env_text(cur, room)) else [])
        exits = load_exits(cur, player.room_id)
        if exits:
            facts.append("出口：" + "、".join(
                f"{dir_name(e.direction)}（通往{load_room(cur, e.to_room).name}" + ("，门锁着" if e.locked else "") + "）"
                for e in exits))
        items = load_items(cur, "i.room_id = %s", (player.room_id,))
        ground = [_label(i) for i in items] + view.forage
        if ground:
            facts.append("地上有：" + "、".join(ground))
        npcs = load_npcs(cur, "n.room_id = %s and n.alive", (player.room_id,))
        here = [n.name + (f"（{n.status.describe()}）" if n.status else "") for n in npcs]             + [f"{d.container}（{d.where}面有{d.item_name}）" if d.available else d.container
               for d in view.dispensers]
        if here:
            facts.append("这里有：" + "、".join(here))
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

    # 看同房间的玩家：HP、倒下、负面状态、手里拿着什么（背包里别的东西看不到）
    if a.target not in view.refs:
        try:
            other, awake = _room_player(cur, player, a.target)
        except ActionError:
            # 不是人：环境里写着的东西（石碑、壁画、井）照环境细节看，地窖石碑把排行榜念出来
            room = view.room
            seen = a.target.strip("的")
            if not seen or seen not in room.description + room.details:
                raise
            return [f"{player.name}仔细看了看{seen}"]
        worn = _worn(cur, other)
        facts = [f"{other.name}：HP {other.hp}/{other.max_hp}"
                 + ("，倒在地上" if other.hp <= 0 else "") + ("" if awake else "，睡着了")]
        if other.status:
            facts.append(f"{other.name}{other.status.describe()}")
        if worn:
            facts.append(f"{other.name}身上装备着" + "、".join(f"{i.name}（{SLOT_NAMES[i.equipped_slot]}）" for i in worn))
        return facts

    uid = _resolve(view, a.target)
    if d := next((d for d in view.dispensers if d.id == uid), None):
        return [f"{d.container}：{d.description}".rstrip("：")]             + ([f"{d.container}{d.where}有{d.item_name}"] if d.available else [])
    items = load_items(cur, "i.id = %s and (i.room_id = %s or i.player_id = %s)",
                       (uid, player.room_id, player.id))
    if items:
        return [f"{items[0].name}：{items[0].description}"]
    npcs = load_npcs(cur, "n.id = %s and n.room_id = %s and n.alive", (uid, player.room_id))
    if npcs:
        n = npcs[0]
        facts = [f"{n.name}：{n.template.description}"] + _stele(cur, n, player)
        if n.combatable:
            facts.append(f"{n.name} HP {n.hp}/{n.template.max_hp}")
        if n.status:
            facts.append(f"{n.name}{n.status.describe()}")
        return facts
    raise ActionError("这里看不到那个东西")


def _give_player_new(cur: Cursor, player: Player, template_id: str) -> None:
    """凭空给玩家一件新东西（武器桶、搜索找到的），可叠加的并进已有的那堆"""
    cur.execute(
        """update item_instances i set quantity = quantity + 1 from item_templates t
           where t.id = i.template_id and t.stackable and i.template_id = %s and i.player_id = %s
             and i.equipped_slot is null
           returning i.id""",
        (template_id, player.id),
    )
    if not cur.fetchone():
        cur.execute("insert into item_instances (template_id, player_id) values (%s, %s)", (template_id, player.id))


def _take_from(cur: Cursor, player: Player, view: RoomView, d: Dispenser) -> list[str]:
    """从武器桶这类地方拿一件：身上已经有同类的（锈剑、磨亮的剑）就不能拿；once 的拿过一次就再也不能拿。
    要技能判定的（挖矿、挑酒）先判，失败了这次拿不到（可能挨一下），下次还能再试"""
    cur.execute(
        """select t.name from item_instances i join item_templates t on t.id = i.template_id
           where i.player_id = %s and i.template_id = any(%s) limit 1""",
        (player.id, [d.item] + d.unless),
    )
    if (row := cur.fetchone()) and not d.repeat:
        raise ActionError(f"{player.name}身上已经有{row['name']}了，{d.container}{d.where}的留给别人")
    if d.once:
        cur.execute("select 1 from dispenser_log where player_id = %s and room_id = %s and key = %s",
                    (player.id, d.room, d.key))
        if cur.fetchone():
            raise ActionError(f"{d.container}{d.where}已经没有{player.name}能拿的东西了")
    facts = []
    if d.skill:
        ok, facts = _check(cur, player, view, d.skill, d.difficulty)
        if not ok:
            facts = [f"{player.name}想从{d.container}{d.where}取{d.item_name}"] + facts + [d.fail or "没能成功"]
            if d.fail_damage:
                hurt, _ = _hurt_player(cur, player, d.fail_damage, "other", d.container)
                facts += [f"{player.name}受到 {d.fail_damage} 点伤害"] + hurt
            return facts
    if d.once:
        # 拿过一次就没了：并发时靠主键挡住第二次
        cur.execute("""insert into dispenser_log (player_id, room_id, key) values (%s, %s, %s)
                       on conflict do nothing returning 1""", (player.id, d.room, d.key))
        if not cur.fetchone():
            raise ActionError(f"{d.container}{d.where}已经没有{player.name}能拿的东西了")
    _give_player_new(cur, player, d.item)
    return facts + [f"{player.name}从{d.container}{d.where}拿了一件{d.item_name}"]


def do_take(cur: Cursor, player: Player, view: RoomView, a: Take) -> list[str]:
    uid = _resolve(view, a.item)
    if d := next((d for d in view.dispensers if d.id == uid), None):
        return _take_from(cur, player, view, d)
    item = _get_item(cur, view, a.item)
    if item.player_id == player.id:
        # "从背包里拿出钥匙开门"会解析成先 take，东西本来就在身上，算成功，后面的动作照常执行
        return [f"{item.name}就在{player.name}身上"]
    if item.room_id != player.room_id:
        raise ActionError(f"这里没有{item.name}")
    if not item.template.takeable:
        raise ActionError(f"{item.name}拿不起来")
    if coins := item.props.get("gold"):
        cur.execute("delete from item_instances where id = %s", (item.id,))
        cur.execute("select id, name from players where party_id = %s and room_id = %s and id <> %s order by name",
                    (player.party_id, player.room_id, player.id))
        mates = cur.fetchall() if player.party_id else []
        share = coins // (len(mates) + 1)
        for m in mates:
            cur.execute("update players set gold = gold + %s where id = %s", (share, m["id"]))
        mine = coins - share * len(mates)
        cur.execute("update players set gold = gold + %s where id = %s", (mine, player.id))
        return [f"{player.name}捡起{item.name}，倒出了 {coins} 枚金币"
                + (f"，跟{'、'.join(m['name'] for m in mates)}平分，每人 {share} 枚" if mates else "")]
    _move_item(cur, item, player_id=player.id)
    return [f"{player.name}从地上捡起了{_label(item)}"]


def do_drop(cur: Cursor, player: Player, view: RoomView, a: Drop) -> list[str]:
    item = _inv_item(cur, view, player, a.item)
    if _burning(item):
        return _douse(cur, player, item)
    facts = [f"{player.name}卸下了{item.name}"] if item.equipped_slot else []
    _move_item(cur, item, room_id=player.room_id)
    return facts + [f"{player.name}把{_label(item)}放在了地上"]


def do_use(cur: Cursor, player: Player, view: RoomView, a: Use) -> list[str]:
    item = _inv_item(cur, view, player, a.item)
    if _prop(item, "stun"):
        return _stun(cur, player, view, item, a.target)
    for key, fn in (("refuel", _refuel), ("whet", _whet), ("room_light", _room_light), ("holy", _holy)):
        if _prop(item, key):
            return fn(cur, player, view, item, a.target)

    ex = _find_exit(cur, player.room_id, a.target, lock=True) if a.target is not None else None
    if a.target is not None and ex is None and item.template.type == "consumable":
        return _feed(cur, player, item, a.target)
    if a.target is not None:
        if ex is None:
            raise ActionError(f"不知道怎么对那个东西使用{item.name}")
        if not ex["locked"]:
            raise ActionError(f"往{dir_name(a.target)}的门没有锁")
        if ex["key_item"] != item.template.id:
            raise ActionError(f"{item.name}打不开往{dir_name(a.target)}的门")
        cur.execute("update room_exits set locked = false, unlocked_at = now() where room_id = %s and direction = %s",
                    (player.room_id, a.target))
        return [f"{player.name}用{item.name}打开了往{dir_name(a.target)}的门"]

    if _prop(item, "recall"):
        return _recall(cur, player, item)
    if item.template.type != "consumable":
        raise ActionError(f"{item.name}不能直接使用")
    _consume(cur, item)
    return [f"{player.name}{_eat_verb(item)}掉了{item.name}"] + _eat_effect(cur, player, item, player)


STUN_ESCAPE = 3                         # 古书残卷念出来的定身：敌人挣脱的难度（每回合 30%、60%、90% 醒过来）


def _use_up(cur: Cursor, item: ItemInstance) -> None:
    """用掉一次：能用好几次的（props.uses，借阅簿 3 次）记在实例上，用完才没"""
    total = _prop(item, "uses")
    left = item.props.get("uses_left", total or 1) - 1
    if left > 0:
        cur.execute("update item_instances set props = props || jsonb_build_object('uses_left', %s::int) where id = %s",
                    (left, item.id))
    else:
        _consume(cur, item)


def _refuel(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """灯油：倒在弱光的火把上，让它重新烧旺"""
    torch = next((i for i in _worn(cur, player) if i.template.id == "torch_dim"), None)
    if torch is None:
        raise ActionError(f"手上没有快灭的火把，{item.name}用不上")
    _consume(cur, item)
    cur.execute("update item_instances set template_id = %s, props = '{}' where id = %s", (_prop(item, "refuel"), torch.id))
    return [f"{player.name}把{item.name}倒在火把上，火苗呼地一下又旺了起来"]


def _whet(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """磨刀石：这一层普通攻击伤害 +value（换了层就没了）"""
    if not dungeon.is_dungeon(player.room_id):
        raise ActionError(f"{item.name}的锋利劲撑不过一层地牢，下了地牢再磨吧")
    run, depth = dungeon.parse_room(player.room_id)
    _consume(cur, item)
    player.effects = [e for e in player.effects if e.kind != "whet"] + [
        Effect(kind="whet", value=int(_prop(item, "whet")), left=1, label="刃口磨得雪亮", source=f"{run.hex}:{depth}")]
    _save_effects(cur, player)
    return [f"{player.name}用{item.name}把武器磨得雪亮（这一层普通攻击伤害 +{_prop(item, 'whet')}）"]


def _room_light(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """光明卷轴：这个房间光亮 +N，走出房间就散了"""
    if not view.room.props.get("env"):
        raise ActionError("这里够亮了，用不着")
    _consume(cur, item)
    cur.execute("""update rooms set props = jsonb_set(props, '{env,scroll}', %s) where id = %s""",
                (Jsonb({"value": int(_prop(item, "room_light")), "by": str(player.id)}), player.room_id))
    return [f"{player.name}展开{item.name}，上面的字一个接一个亮起来，把整个房间照得雪亮"]


def _holy(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """圣水：泼向亡灵或怕光的怪，按档位重创它；泼别的没用（水也没了）"""
    if not target or target not in view.refs:
        raise ActionError(f"{item.name}要泼向一个敌人")
    npc = _room_npc(cur, view, player, target)
    _consume(cur, item)
    facts = [f"{player.name}把{item.name}泼向{npc.name}"]
    if not (npc.template.props.get("undead") or npc.template.props.get("light_averse")):
        return facts + [f"{npc.name}只是被泼湿了，什么事也没有"]
    dmg = random.randint(*TIER_RANGE[_prop(item, "holy")])
    hurt, dead = _hurt_npc(cur, player, npc, dmg)
    return facts + [f"圣水在{npc.name}身上嘶嘶地冒起白烟，受到 {dmg} 点伤害"] + hurt


def _stun(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """念能定身的东西（古书残卷：props.stun 是状态说明）：不用判定，房间里所有敌人一起失去战斗能力，东西用掉"""
    foes = _enemies(cur, player.room_id)
    if not foes:
        raise ActionError(f"这里没有敌人，{item.name}念了也没用")
    _use_up(cur, item)
    label = str(_prop(item, "stun"))[:20]
    for npc in foes:
        _set_status(cur, "npcs", npc.id, Status(kind="incapacitated", label=label, escape=STUN_ESCAPE,
                                                since=datetime.now(timezone.utc).isoformat()))
    names = "、".join(n.name for n in foes)
    return [f"{player.name}翻开{item.name}，念出上面的古老文字，字句在空气里回荡，书页化成灰烬散开",
            f"{names}{label}，失去了战斗能力"]


RECALL_TO = "square"                    # 回城水晶把人传回这里


def _recall(cur: Cursor, player: Player, item: ItemInstance) -> list[str]:
    """在地牢里捏碎回城水晶：传回村口广场。被怪缠着也能走，这就是它值钱的地方"""
    if not dungeon.is_dungeon(player.room_id):
        raise ActionError(f"{item.name}只有在远古地牢里才有用")
    if _active_duel(cur, player.id):
        raise ActionError(f"{player.name}正在决斗，走不了")
    _consume(cur, item)
    dungeon.touch(cur, player.room_id)
    cur.execute("update players set room_id = %s, following = null, stealth = null, updated_at = now() where id = %s",
                (RECALL_TO, player.id))
    return [f"{player.name}捏碎了{item.name}，一阵淡蓝色的光裹住全身，再睁眼已经回到了{load_room(cur, RECALL_TO).name}"]


def _eat_verb(item: ItemInstance) -> str:
    """酒、水、汤是喝的"""
    return "喝" if item.template.id == "made_drink" or _is_alcohol(item) or any(c in item.name for c in "水汤茶奶") else "吃"


# 吃喝回血按血量上限的百分比算，耐性练高了药也跟着管用：heal 每 1 点回 HEAL_PCT%（血量 20 时正好 1 点），
# 酒回得少一点，每点 HEAL_PCT_ALCOHOL%。毒（harm）还是按点数掉血
HEAL_PCT = 5
HEAL_PCT_ALCOHOL = 3


def _is_alcohol(item: ItemInstance) -> bool:
    return bool(_prop(item, "alcohol")) or "酒" in item.name


def heal_amount(points: int, max_hp: int, alcohol: bool = False) -> int:
    """heal 点数 → 这个人实际回多少血"""
    return max(1, round(max_hp * points * (HEAL_PCT_ALCOHOL if alcohol else HEAL_PCT) / 100)) if points > 0 else 0


def _eat_effect(cur: Cursor, eater: Player, item: ItemInstance, user: Player) -> list[str]:
    """吃喝下去的效果：有毒的掉血，蒙汗药这类把人放倒，正常的回血（倒下的人吃了回血药也能站起来）。
    回血按吃的人血量上限的百分比（heal_amount）；药草（模板 props.herbal）按用药的人（自己吃是自己，喂别人是喂的人）
    的自然等级多回"""
    facts = []
    points = item.heal + (skill_level(user.skills.get("nature", 0)) if item.heal and item.template.props.get("herbal") else 0)
    heal = heal_amount(points, eater.max_hp, _is_alcohol(item))
    harm = item.harm
    if harm and (check := _prop(item, "harm_check")):
        # 沼泽菇这类：认得出来（自然判定）就是好吃的，认不出来吃到有毒的
        if _roll(skill_chance(skill_level(user.skills.get(check, 0)), 2)):
            harm = 0
            facts.append(f"{user.name}认出这是能吃的那种")
        else:
            facts.append(f"{user.name}没认出来，吃到了有毒的那种")
            if not _effect(eater, "poison"):
                eater.effects.append(Effect(kind="poison", value=1, left=EFFECT_TURNS["poison"], label="吃坏了肚子",
                                            source=item.name))
                _save_effects(cur, eater)
    hp = max(0, min(eater.max_hp, eater.hp + heal - harm))
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (hp, eater.id))
    if (cure := _prop(item, "cure")) and (e := _effect(eater, cure)):
        # 绷带止血、解毒苔解毒
        eater.effects.remove(e)
        _save_effects(cur, eater)
        facts.append(f"{eater.name}身上的{EFFECT_NAMES[cure]}好了")
    if harm:
        facts.append(f"{item.name}有毒，{eater.name}掉了 {harm} 点 HP，当前 HP {hp}/{eater.max_hp}")
    elif item.heal:
        facts.append(f"{eater.name}恢复 {hp - eater.hp} 点 HP，当前 HP {hp}/{eater.max_hp}")
        if eater.hp <= 0 < hp:
            facts.append(f"{eater.name}醒了过来")
    if hp == 0:
        facts.append(f"{eater.name}倒下了")
        _downed_by(cur, eater, "poison", item.name)
    elif harm:
        eater.hp = hp
        facts += _toughen(cur, eater)                   # 毒扛过去了，耐性一定涨
    if hp > 0 and item.knockout:
        _knock_out(cur, "players", eater.id, item.knockout)
        facts.append(f"{eater.name}{item.knockout}，失去战斗能力")
    if _prop(item, "sober"):
        cur.execute("update players set drunk_until = null where id = %s", (eater.id,))
        facts.append(f"{eater.name}酒醒了，脑子清楚了")
    elif _prop(item, "alcohol") or "酒" in item.name:
        # 连着喝第几杯：上一杯在 DRINK_WINDOW 里就接着数，否则从 1 开始
        # 烈酒（props.strength）一杯顶几杯
        cur.execute(f"""update players set drinks = case when last_drink_at > now() - interval '{DRINK_WINDOW}'
                                                         then drinks else 0 end + %s, last_drink_at = now()
                        where id = %s returning drinks""", (_prop(item, "strength") or 1, eater.id))
        n = cur.fetchone()["drinks"]
        resist = DRUNK_RESIST ** skill_level(eater.skills.get("endurance", 0))
        if hp > 0 and _roll(min(1.0, DRUNK_CHANCE * 2 ** (n - 1) * resist)):
            cur.execute(f"update players set drunk_until = now() + interval '{DRUNK_TIME}' where id = %s", (eater.id,))
            facts.append(f"{eater.name}喝醉了（连着喝了 {n} 杯）：接下来一阵子说话含糊，做什么都不太利索")
        elif hp > 0 and n >= 2:
            eater.hp = hp
            facts += _toughen(cur, eater, ENDURE_HIT_CHANCE)   # 连着喝还没醉，扛住了酒劲
    return facts


def _subdued(target: Any) -> bool:
    """被放倒、捆住了，任人摆布（能补刀、能灌药）；只是摔倒在地的还能挣扎，不算"""
    return target.status is not None and target.status.kind != "prone"


def _prone_bonus(target: Any, d: int) -> float:
    """打倒在地上的（被绊倒的）更容易打中；够不着的还是够不着"""
    return PRONE_HIT_BONUS if d in MELEE_HIT and target.status and target.status.kind == "prone" else 0.0


def _drunk(player: Player) -> float:
    return DRUNK_PENALTY if player.drunk else 0.0


def _prop(item: ItemInstance, key: str) -> Any:
    """物品特性：实例上的（NPC 现做的）优先，没有就看模板"""
    return item.props.get(key, item.template.props.get(key))


def slur(text: str) -> str:
    """醉话：在原话里随机插“嗝”和“……”"""
    out = []
    for ch in text:
        out.append(ch)
        if ch in "，。！？、,.!? " and random.random() < 0.6:
            out.append(random.choice(["……", "……嗝，", "嗝……"]))
        elif ch not in "，。！？、,.!? " and random.random() < 0.08:
            out.append(random.choice(["……", "嗝"]))
    return "".join(out) + random.choice(["……嗝", "……", ""])


def _feed(cur: Cursor, player: Player, item: ItemInstance, name: str) -> list[str]:
    """喂同房间的玩家吃喝：倒下的、被放倒捆住的、队友都能喂。
    有毒、下了药的东西，清醒的外人不会乖乖吃下去（想害人得泼、得骗他自己吃）"""
    target, awake = _room_player(cur, player, name, lock=True)
    if not awake:
        raise ActionError(f"{target.name}睡着了，喂不进去")
    helpless = target.hp <= 0 or _subdued(target)
    mate = bool(player.party_id and player.party_id == target.party_id)
    if (item.harm or item.knockout) and not helpless and not mate:
        # 强行塞嘴里也一样：清醒的人会挣扎吐掉，得先把他放倒、捆住
        raise ActionError(f"{target.name}清醒着，不肯吃{item.name}，硬塞也会被吐掉，得先把他制住")
    # 喂毒掉血也是伤害，得在决斗里；只把人药倒不掉血的算整人
    if item.harm and not _active_duel(cur, player.id, target.id):
        raise ActionError(f"{item.name}有毒，{player.name}和{target.name}没有在决斗，不能拿它害人")
    _consume(cur, item)
    return [f"{player.name}喂{target.name}{_eat_verb(item)}了{item.name}"] + _eat_effect(cur, target, item, player)


def _consume(cur: Cursor, item: ItemInstance) -> None:
    """用掉一个（叠着的减一，最后一个删掉）"""
    if item.quantity > 1:
        cur.execute("update item_instances set quantity = quantity - 1 where id = %s", (item.id,))
    else:
        cur.execute("delete from item_instances where id = %s", (item.id,))


def _precious(cur: Cursor, item: ItemInstance) -> bool:
    """钥匙、任务要交的东西、只能拿一次的东西（护符）：不能被当成材料用掉"""
    if item.template.type == "key":
        return True
    cur.execute("""select exists (select 1 from quests where needs_item = %(t)s)
                       or exists (select 1 from rooms, jsonb_each(coalesce(props->'dispensers', '{}')) d
                                  where d.value->>'item' = %(t)s and coalesce((d.value->>'once')::boolean, false)
                                    and rooms.id not like 'dg-%%') as p""",
                {"t": item.template.id})
    return cur.fetchone()["p"]


def _knock_out(cur: Cursor, table: str, target_id: UUID, label: str) -> None:
    """药倒：失去战斗能力，挣脱（醒来）难度普通"""
    _set_status(cur, table, target_id, Status(kind="incapacitated", label=label[:20], escape="normal",
                                              since=datetime.now(timezone.utc).isoformat()))


def do_equip(cur: Cursor, player: Player, view: RoomView, a: Equip) -> list[str]:
    """装上：武器左右手都行（双持），戒指两个位都行。玩家说了哪只手就放哪只，没说就放空着的，都满了换掉第一个"""
    item = _inv_item(cur, view, player, a.item)
    kind = item.template.slot
    if not kind:
        raise ActionError(f"{item.name}不能装备")
    choices = SLOT_CHOICES.get(kind, [kind])
    worn = {i.equipped_slot: i for i in _worn(cur, player)}
    if a.slot in choices:
        slot = a.slot
    elif item.equipped_slot:
        raise ActionError(f"{item.name}已经装备在{SLOT_NAMES[item.equipped_slot]}了")
    else:
        # 两只手都占着时，火把默认换掉左手（副手），别把主手的武器换下来
        # 盾默认拿左手（副手），武器默认右手
        prefer = ["left_hand", "right_hand"] if kind == "hand" and item.template.type == "armor" else choices
        slot = next((c for c in prefer if c not in worn), "left_hand" if _prop(item, "lights") else prefer[0])
    if item.equipped_slot == slot:
        raise ActionError(f"{item.name}已经装备在{SLOT_NAMES[slot]}了")
    facts = []
    if kind == "hand":
        # 双手武器（props.two_handed）拿在右手，左手得空出来；拿着双手武器时再往手上拿别的，先把它放下
        if _prop(item, "two_handed"):
            slot = "right_hand"
            other = worn.get("left_hand")
            if other and other.id != item.id:
                if _burning(other):
                    raise ActionError(f"{item.name}要两只手拿，左手的{other.name}点着，收起来就灭了；先把火把熄掉再换")
                cur.execute("update item_instances set equipped_slot = null where id = %s", (other.id,))
                facts.append(f"{player.name}放下了{other.name}，腾出两只手")
                worn.pop("left_hand")
        elif (big := next((i for i in worn.values() if _prop(i, "two_handed") and i.id != item.id), None)):
            cur.execute("update item_instances set equipped_slot = null where id = %s", (big.id,))
            facts.append(f"{player.name}放下了要两只手拿的{big.name}")
            worn = {s: i for s, i in worn.items() if i.id != big.id}
    old = worn.get(slot)
    if old and _burning(old) and not item.equipped_slot:
        raise ActionError(f"{SLOT_NAMES[slot]}拿着点着的{old.name}，收起来就灭了；要换就先把火把熄掉（卸下火把），或者说换到另一只手")
    prev = item.equipped_slot
    if prev:
        # 已经装着的换个位置（右手的斧子换到左手）：先腾出来，免得撞上"每格一件"的唯一索引
        cur.execute("update item_instances set equipped_slot = null where id = %s", (item.id,))
    if old and prev:
        # 两只手互换：原来那格的东西挪到空出来的这格
        cur.execute("update item_instances set equipped_slot = %s where id = %s", (prev, old.id))
        facts.append(f"{player.name}把{old.name}换到了{SLOT_NAMES[prev]}")
    elif old:
        cur.execute("update item_instances set equipped_slot = null where id = %s", (old.id,))
        facts.append(f"{player.name}卸下了{old.name}")
    cur.execute("update item_instances set equipped_slot = %s where id = %s", (slot, item.id))
    if lit := _prop(item, "lights"):
        cur.execute("update item_instances set template_id = %s, props = '{}' where id = %s", (lit, item.id))
        return facts + [f"{player.name}把{item.name}拿在{SLOT_NAMES[slot]}点着了，火光一下子亮起来"]
    return facts + [f"{player.name}把{item.name}装备在{SLOT_NAMES[slot]}"]


def do_unequip(cur: Cursor, player: Player, view: RoomView, a: Unequip) -> list[str]:
    item = _inv_item(cur, view, player, a.item)
    if not item.equipped_slot:
        # "卸下火把"：背包里没点的火把名字一模一样，要卸的其实是手上点着的那支
        item = next((i for i in _worn(cur, player) if i.name.startswith(item.name)), item)
    if not item.equipped_slot:
        raise ActionError(f"{item.name}没有装备着")
    if _burning(item):
        return _douse(cur, player, item)
    cur.execute("update item_instances set equipped_slot = null where id = %s", (item.id,))
    return [f"{player.name}卸下了{SLOT_NAMES[item.equipped_slot]}的{item.name}"]


# ============ 战斗 ============
# 战斗公式是占位的，待定问题里还没定。
# 借环境的创意攻击（stunt）由 AI 当裁判给难度、档位、状态，这里掷骰、限幅、执行，AI 给不出具体数字

def _roll(chance: float) -> bool:
    """掷骰，测试时可以换掉"""
    return random.random() < chance


# ============ 技能判定 ============

def _skill_total(level: int) -> int:
    """升到 level 级一共要攒几次熟练：3、9、18、30……"""
    return SKILL_STEP * level * (level + 1) // 2


def skill_level(count: int) -> int:
    level = 0
    while count >= _skill_total(level + 1):
        level += 1
    return level


def skill_progress(count: int) -> tuple[int, int, int]:
    """(等级, 这一级攒了几次, 升下一级要几次)"""
    level = skill_level(count)
    return level, count - _skill_total(level), SKILL_STEP * (level + 1)


def skill_chance(level: int, difficulty: int) -> float:
    gap = difficulty - level
    return SKILL_SURE if gap <= 0 else SKILL_GAP_CHANCE.get(gap, 0.0)


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


def _toughen(cur: Cursor, player: Player, chance: float = 1.0) -> list[str]:
    """扛住了一下（挨了打还站着、喝了毒还活着、扛住酒劲）：按几率涨一次耐性熟练"""
    return _gain_skill(cur, player, "endurance") if player.hp > 0 and _roll(chance) else []


def _escape_chance(escape: int, attempts: int) -> float:
    """挣脱、醒来不靠技能等级的那种（NPC 的、昏过去的）：施加时的难度，每失败一次降一级"""
    return skill_chance(0, max(1, escape - attempts))


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
    # 掉金币（world.yaml 的 on_death.gold: [最少, 最多]），给补最后一刀的人
    # 同房间的队友每人一份（组队时怪更肉，钱也得跟上）
    if gold := npc.template.props.get("on_death", {}).get("gold"):
        n = random.randint(*gold)
        if dungeon.is_dungeon(player.room_id):
            n = max(1, round(n * (1 + 0.5 * _dark_factor(_light(cur, player.room_id)))))   # 越暗掉得越多
        n = max(1, round(n * (1 + gear_gold(cur, player))))      # 幸运古币、贪婪之戒（只算最好的一件）
        cur.execute("""update players set gold = gold + %s
                       where id = %s or (party_id = %s and room_id = %s and hp > 0) returning name""",
                    (n, player.id, player.party_id, player.room_id))
        mates = [r["name"] for r in cur.fetchall() if r["name"] != player.name]
        facts.append(f"{player.name}从{npc.name}身上摸到了 {n} 枚金币"
                     + (f"，{'、'.join(mates)}也各分到 {n} 枚" if mates else ""))
    return facts, True


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
    看店的 NPC 扶人时按这个说俏皮话（world.yaml 的 revive_lines）"""
    cur.execute("update players set downed_by = %s where id = %s", (Jsonb({"kind": kind, "by": by}), player.id))


def _npc_counter(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """NPC 挨打后反击。身上有负面状态就不还手，按施加时的挣脱难度看这回合能不能恢复"""
    if npc.status:
        st = npc.status
        if st.kind == "prone":
            return [f"{npc.name}还倒在地上，没法还手"]
        if _roll(_escape_chance(st.escape, st.attempts)):
            _set_status(cur, "npcs", npc.id, None)
            return [f"{npc.name}摆脱了“{st.label}”的状态，但这回合来不及还手"]
        st.attempts += 1
        _set_status(cur, "npcs", npc.id, st)
        return [f"{npc.name}还{st.label}，没法还手"]
    if npc.template.hostile:
        return []                       # 敌人的还手在 enemy_turn 里按距离算
    return _npc_strike(cur, player, npc, "反击")


def _npc_strike(cur: Cursor, player: Player, npc: Npc, verb: str, chance: float = 1.0) -> list[str]:
    """NPC 打玩家一下（反击、主动攻击），chance 是命中率。player.hp 跟着更新，同一回合几个敌人连着打能接上"""
    if _npc_effect(npc, "blind"):
        chance *= NPC_BLIND_HIT                 # 被墨汁糊了眼的怪
    if not _roll(chance):
        return [f"{npc.name}{verb}，没打中{player.name}"]
    # 影步靴这类：几率完全躲开（几件取最高）
    dodges = _fire(cur, player, "hurt", "dodge", npc)
    if dodges:
        return [f"{npc.name}{verb}，" + (dodges[0].get("label") or f"被{player.name}躲开了")]
    light = _light(cur, npc.room_id)
    atk = npc.template.attack + (math.floor(_dark_factor(light) + 0.5) if dungeon.is_dungeon(npc.room_id) else 0)
    if npc.template.props.get("light_averse") and light >= LIGHT_BRIGHT:
        atk -= 1                                # 怕光的怪在亮处缩手缩脚
    depth = npc.template.props.get("dungeon", {}).get("depth", 0)
    guard = sum(int(e.get("value", 0)) for e in _fire(cur, player, "hurt", "guard", npc))       # 恶犬项圈
    dmg = hurt_player_by(atk, _defense(cur, player), depth, guard=guard)
    saved = []
    if dmg >= player.hp and (cd := _fire(cur, player, "hurt", "cheat_death", npc)) and _cheat_death_ready(cur, player):
        dmg, saved = player.hp - 1, _labels(cd) or [f"{player.name}硬撑着没倒下"]
    player.hp = max(0, player.hp - dmg)
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (player.hp, player.id))
    hit = (f"{npc.name}{verb}，这一下本该要了{player.name}的命" if saved
           else f"{npc.name}{verb}，对{player.name}造成 {dmg} 点伤害")
    facts = [hit, f"{player.name} HP {player.hp}/{player.max_hp}"] + saved
    if player.hp == 0:
        facts.append(f"{player.name}倒下了")
        _downed_by(cur, player, "npc", npc.name)
    # 荆棘软甲：扑上来的挨一下（不减防御）
    if reflect := sum(int(e.get("value", 0)) for e in _fire(cur, player, "hurt", "reflect", npc)):
        hurt, _ = _hurt_npc(cur, player, npc, reflect)
        facts += [f"{npc.name}被扎了一下，受到 {reflect} 点伤害"] + hurt
    return facts + _toughen(cur, player, ENDURE_HIT_CHANCE) + _on_hit(cur, player, npc)


def _cheat_death_ready(cur: Cursor, player: Player) -> bool:
    """不熄的羽毛：每层地牢一次（记在 players.flags 的 cheat_death 里）"""
    here = ":".join(map(str, dungeon.parse_room(player.room_id))) if dungeon.is_dungeon(player.room_id) else "surface"
    if player.flags.get("cheat_death") == here:
        return False
    cur.execute("""update players set flags = flags || jsonb_build_object('cheat_death', %s::text) where id = %s""",
                (here, player.id))
    player.flags["cheat_death"] = here
    return True


def _stealth(player: Player) -> Stealth:
    """玩家在当前区域的隐蔽情况；记录的是别的区域（刚被人带进来、刚登录）就当刚进门"""
    st = player.stealth
    return st if st and st.room == player.room_id else Stealth(room=player.room_id, chance=DETECT_START)


def distance_word(d: int) -> str:
    return f"{d} 格（{DISTANCE_WORDS.get(d, '离得很远')}）"


def _distance(st: Stealth, npc: Npc) -> int:
    return st.distance.get(str(npc.id), START_DISTANCE)


def _set_distance(st: Stealth, npc: Npc, d: int) -> int:
    d = max(0, min(MAX_DISTANCE, d))
    st.distance[str(npc.id)] = d
    return d


# ============ 负面效果（怪打中时附带，players.effects）============
# 中毒：每回合（每条消息）掉血、命中和判定 -POISON_HIT；流血：每个动作掉血、打出的伤害 ×BLEED_DAMAGE；
# 看不清：光亮算 0（命中只剩 5%）；腐蚀：防御 -value、血量上限临时扣 hp（消退还回来）。
# 同一种再中一次只刷新时间。住店、扎营、被扶起来、急救都会清掉
EFFECT_NAMES = {"poison": "中毒", "bleed": "流血", "blind": "看不清", "corrode": "腐蚀", "whet": "磨利"}
EFFECT_TURNS = {"poison": 3, "bleed": 4, "blind": 1, "corrode": 3}
POISON_HIT, BLEED_DAMAGE, CORRODE_HP = 0.15, 0.75, 0.15
# 清掉所有效果时把腐蚀扣的血量上限还回去（SQL 片段，用在 update players set ... 里，得在改 hp 之前算）
RESTORE_MAX_HP = "coalesce((select sum((e->>'hp')::int) from jsonb_array_elements(effects) e), 0)"


def _effect(player: Player, kind: str) -> Optional[Effect]:
    return next((e for e in player.effects if e.kind == kind), None)


def _poisoned(player: Player) -> float:
    return POISON_HIT if _effect(player, "poison") else 0.0


def _bled(player: Player, dmg: int) -> int:
    """流血时打出的伤害打折"""
    return max(1, round(dmg * BLEED_DAMAGE)) if _effect(player, "bleed") and dmg > 0 else dmg


def _save_effects(cur: Cursor, player: Player) -> None:
    cur.execute("update players set effects = %s where id = %s", (Jsonb([e.model_dump() for e in player.effects]), player.id))


def effects_text(player: Player) -> str:
    """给 AI 和界面看：中毒（还剩 2 回合）、流血（还剩 3 个动作）"""
    return "、".join(f"磨利：普通攻击伤害 +{e.value}（这一层）" if e.kind == "whet"
                    else f"{EFFECT_NAMES[e.kind]}：{e.label}（还剩 {e.left} {'个动作' if e.kind == 'bleed' else '回合'}）"
                    for e in player.effects)


def _on_hit(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """怪打中人以后按 props.on_hit 的几率附带效果：中毒、流血、看不清、腐蚀，或者缠住、撞倒（状态）"""
    hit = npc.template.props.get("on_hit")
    if not hit or player.hp <= 0:
        return []
    resist = gear_resist(cur, player, hit["kind"])       # 装备抗性：0 免疫，0.5 减半
    if resist <= 0 or not _roll(hit.get("chance", 0.25) * resist):
        return []
    depth = npc.template.props.get("dungeon", {}).get("depth", 1)
    kind, label = hit["kind"], hit.get("label", "")
    if kind in ("restrained", "prone"):
        if player.status:
            return []
        st = Status(kind=kind, label=label[:20], escape=hit.get("escape", 2), since=datetime.now(timezone.utc).isoformat())
        _set_status(cur, "players", player.id, st)
        player.status = st
        return [f"{player.name}{label}" + ("，得先挣脱" if kind == "restrained" else "，得先爬起来")]
    value = 1 + depth // 6 if kind == "corrode" else 1 + depth // 5
    old = _effect(player, kind)
    if old:
        old.left = EFFECT_TURNS[kind]
        _save_effects(cur, player)
        return [f"{player.name}又{label}，{EFFECT_NAMES[kind]}的时间重新算"]
    e = Effect(kind=kind, value=value, left=EFFECT_TURNS[kind], label=label[:20], source=npc.name)
    what = {"poison": f"接下来 {e.left} 回合每回合掉 {value} 点血，出手也不准了",
            "bleed": f"接下来 {e.left} 个动作每动一下掉 {value} 点血，使不上劲",
            "blind": "这一回合什么都看不清",
            "corrode": ""}[kind]
    if kind == "corrode":
        e.hp = min(player.max_hp - 1, max(1, round(player.max_hp * CORRODE_HP)))
        player.max_hp -= e.hp
        player.hp = min(player.hp, player.max_hp)
        cur.execute("update players set max_hp = %s, hp = %s where id = %s", (player.max_hp, player.hp, player.id))
        what = f"防御 -{value}，血量上限暂时 -{e.hp}，持续 {e.left} 回合"
    player.effects.append(e)
    _save_effects(cur, player)
    return [f"{player.name}{label}（{EFFECT_NAMES[kind]}：{what}）"]


def _tick_effects(cur: Cursor, player: Player, unit: str) -> list[str]:
    """unit="turn"：每条消息开头结一次中毒、看不清、腐蚀；unit="action"：每个动作前结一次流血。
    时间到了就消退（腐蚀把血量上限还回去）"""
    if not player.effects or player.hp <= 0:
        return []
    facts, keep = [], []
    for e in player.effects:
        if e.kind == "whet":
            if unit == "turn" and dungeon.is_dungeon(player.room_id) and e.source != "{}:{}".format(
                    dungeon.parse_room(player.room_id)[0].hex, dungeon.parse_room(player.room_id)[1]):
                facts.append(f"{player.name}武器上磨出来的锋利劲过去了")
            elif unit == "turn" and not dungeon.is_dungeon(player.room_id):
                facts.append(f"{player.name}武器上磨出来的锋利劲过去了")
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


# ============ 环境（地牢房间 props.env，见 dungeon.yaml）============
# 光亮 0~100（村里没设环境的算 100）：低于 LIGHT_FULL 普通攻击命中按比例打折，0 时只剩 LIGHT_MIN_HIT；
# 低于 LIGHT_DARK 做花样难度 +1、躲藏容易 1 级。有人带火把 +TORCH_LIGHT；看不清（blind）的人光亮算 0。
# 越暗怪越凶、钱越多（_dark_factor）；怕光的怪在 LIGHT_BRIGHT 以上攻击 -1。
# 积水：闪避效果减半、逃跑难度 +1。掩体：躲藏容易 1 级
LIGHT_FULL, LIGHT_MIN_HIT, LIGHT_DARK, LIGHT_BRIGHT = 50, 0.05, 20, 70
TORCH_LIGHT = 35                        # 说明用；实际按 world.yaml 火把（点燃）/（弱光）的 props.light
LIGHT_OLD = {"bright": 70, "dim": 40, "dark": 15}      # 旧存档里的文字写法


def _room_env(cur: Cursor, room_id: str) -> dict:
    cur.execute("select props->'env' as env from rooms where id = %s", (room_id,))
    row = cur.fetchone()
    return (row and row["env"]) or {}


def _base_light(env: dict) -> int:
    level = env.get("light", 100) if env else 100
    return LIGHT_OLD.get(level, 40) if isinstance(level, str) else int(level)


def _light(cur: Cursor, room_id: str, env: Optional[dict] = None, player: Optional[Player] = None) -> int:
    """房间现在的光亮 0~100：房间本身（含点着的灯）+ 有人带火把；看不清的人（player 有 blind）算 0"""
    if player is not None and _effect(player, "blind"):
        return 0
    env = _room_env(cur, room_id) if env is None else env
    level = _base_light(env)
    if env and level < 100:
        # 点着的火把（props.burning）取最亮的一支，永久光源（矿工灯、提灯）也只取最亮的一件，两样叠加
        cur.execute("""select max((t.props->>'light')::int) filter (where t.props ? 'burning') as torch,
                              max((t.props->>'light')::int) filter (where not t.props ? 'burning') as lamp
                       from item_instances i join item_templates t on t.id = i.template_id
                       join players p on p.id = i.player_id
                       where p.room_id = %s and t.props ? 'light' and i.equipped_slot is not null""", (room_id,))
        row = cur.fetchone()
        level += (row["torch"] or 0) + (row["lamp"] or 0)
    if env and (scroll := env.get("scroll")):
        cur.execute("select 1 from players where id = %s and room_id = %s", (scroll["by"], room_id))
        if cur.fetchone():
            level += scroll["value"]
    return max(0, min(100, level))


def _light_hit(light: int, chance: float) -> float:
    """命中按光亮打折：LIGHT_FULL 以上不打折，往下按比例降，最低 LIGHT_MIN_HIT（够不着的还是 0）"""
    return 0.0 if chance <= 0 else max(LIGHT_MIN_HIT, chance * min(1.0, light / LIGHT_FULL))


def _dark_factor(light: int) -> float:
    """-1（光亮 100）到 +1（光亮 0）：越暗越大。怪的攻击、掉的钱跟着它走"""
    return (LIGHT_FULL - light) / LIGHT_FULL


def env_text(cur: Cursor, room: Room) -> str:
    """给 AI 看的环境说明，没设环境的房间是空的"""
    env = room.props.get("env")
    return dungeon.env_text(env, _light(cur, room.id, env)) if env else ""


def _save_stealth(cur: Cursor, player: Player, st: Stealth) -> None:
    cur.execute("update players set stealth = %s where id = %s", (Jsonb(st.model_dump()), player.id))


def _enemies(cur: Cursor, room_id: str) -> list[Npc]:
    """在场、活着的敌人"""
    return [n for n in load_npcs(cur, "n.room_id = %s and n.alive", (room_id,), lock=True)
            if n.template.hostile and n.combatable]


def do_maneuver(cur: Cursor, player: Player, view: RoomView, a: Maneuver) -> list[str]:
    """在同一区域里走近、退开某个 NPC 或决斗对手"""
    steps = max(-MAX_STEP, min(MAX_STEP, a.steps))
    if a.target not in view.refs:
        other, _ = _room_player(cur, player, a.target)
        duel = _active_duel(cur, player.id, other.id)
        if duel is None:
            raise ActionError(f"{player.name}没有在跟{other.name}决斗，用不着拉开或拉近距离")
        d = max(0, min(MAX_DISTANCE, duel["distance"] - steps))
        cur.execute("update duels set distance = %s where challenger = %s", (d, duel["challenger"]))
        how = f"朝{other.name}靠近了 {steps} 格" if steps > 0 else f"从{other.name}身边退开了 {-steps} 格" if steps else f"在{other.name}附近挪了挪"
        return [f"{player.name}{how}，现在离{other.name} {distance_word(d)}"]
    npc = _room_npc(cur, view, player, a.target)
    st = _stealth(player)
    d = _set_distance(st, npc, _distance(st, npc) - steps)
    _save_stealth(cur, player, st)
    how = f"朝{npc.name}靠近了 {steps} 格" if steps > 0 else f"从{npc.name}身边退开了 {-steps} 格" if steps else f"在{npc.name}附近挪了挪"
    return [f"{player.name}{how}，现在离{npc.name} {distance_word(d)}"]


def do_dodge(cur: Cursor, player: Player, view: RoomView, a: Dodge) -> list[str]:
    """闪避：打敌人时在 enemy_turn 里算；决斗中记下来，对手下一次攻击他时生效"""
    cur.execute("""update duels set dodging = array_append(array_remove(dodging, %(p)s), %(p)s)
                   where accepted and (challenger = %(p)s or target = %(p)s)""", {"p": player.id})
    return [f"{player.name}{a.description or '摆好架势，准备闪避'}"]


def _dodge_bonus(player: Player) -> float:
    """闪避让对方这一下命中率降多少：察觉越高降得越多"""
    return DODGE_BONUS + DODGE_PER_LEVEL * skill_level(player.skills.get("perception", 0))


def do_hide(cur: Cursor, player: Player, view: RoomView, a: Hide) -> list[str]:
    """躲起来（隐匿）：成功了几率不再上涨；已经被发现的，躲成功就甩掉了（难度高一级）"""
    st = _stealth(player)
    env = _room_env(cur, player.room_id)
    easier = bool(env.get("cover")) + (_light(cur, player.room_id, env) < LIGHT_DARK)
    ok, rolled = _check(cur, player, view, "stealth", max(1, min(10, a.difficulty + st.detected - easier)))
    facts = [f"{player.name}尝试：{a.description or '躲起来'}"] + rolled
    if not ok:
        return facts + [f"{player.name}没能藏好"]
    st.hidden, st.detected = True, False
    _save_stealth(cur, player, st)
    return facts + [f"{player.name}藏好了，暂时没人能发现"]


def do_search(cur: Cursor, player: Player, view: RoomView, a: Search) -> list[str]:
    """四处搜寻：死了的敌人不用等复活时间，直接找出来；房间 forage 里的东西（草药）找到放进背包，
    有冷却的每个人各算各的，不先到先得"""
    facts = [f"{player.name}尝试：{a.description or '四处搜寻'}"]
    # 地牢里走路时听见的怪声：循声找过去，引出一只游荡的怪，它直接扑上来（已经发现了人）
    if view.room.props.get("noise"):
        cur.execute("update rooms set props = props - 'noise' where id = %s", (player.room_id,))
        name = dungeon.spawn_wanderer(cur, player.room_id)
        st = _stealth(player)
        st.detected, st.hidden = True, False
        _save_stealth(cur, player, st)
        return facts + [f"{player.name}循着声音找过去，一只{name}从暗处扑了出来"]
    if found := _respawn_npcs(cur, player.room_id, found=True):
        facts.append(f"{player.name}发现了{'、'.join(found)}")
    elif here := [n.name for n in view.npcs if n.template.hostile]:
        facts.append(f"{'、'.join(here)}就在这里")
    for f in view.room.props.get("forage", []):
        cur.execute("select name from item_templates where id = %s", (f["item"],))
        name = cur.fetchone()["name"]
        cur.execute(
            """select 1 from forage_log where player_id = %s and room_id = %s and template_id = %s
               and found_at > now() - make_interval(secs => %s)""",
            (player.id, player.room_id, f["item"], f.get("cooldown", FORAGE_COOLDOWN)),
        )
        if cur.fetchone():
            facts.append(f"附近能找的{name}刚被{player.name}采过，一时找不到新的")
        elif (chance := f.get("chance", FORAGE_CHANCE)) >= 1 or _roll(chance):
            _give_player_new(cur, player, f["item"])
            cur.execute(
                """insert into forage_log (player_id, room_id, template_id) values (%s, %s, %s)
                   on conflict (player_id, room_id, template_id) do update set found_at = now()""",
                (player.id, player.room_id, f["item"]),
            )
            facts.append(f"{player.name}找到了{name}")
        else:
            facts.append(f"{player.name}没找到{name}")
    # 地牢空房里藏着的古币：搜的时候过调查判定，每人一次机会
    if stash := view.room.props.get("stash"):
        cur.execute("""insert into dispenser_log (player_id, room_id, key) values (%s, %s, 'stash')
                       on conflict do nothing returning 1""", (player.id, player.room_id))
        if cur.fetchone():
            ok, rolled = _check(cur, player, view, "investigation", stash["difficulty"])
            facts += rolled
            if ok:
                cur.execute("update players set gold = gold + %s where id = %s", (stash["gold"], player.id))
                facts.append(f"{player.name}在不起眼的角落里发现了藏着的 {stash['gold']} 枚古币")
            else:
                facts.append("这里就算藏着什么，也没被找到")
    return facts if len(facts) > 1 else facts + ["什么也没找到"]


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
            st = Stealth(room=player.room_id, chance=DETECT_START)   # 敌人都死了，下一只（搜出来、刷回来的）重新算
        # 被放倒、被捆住的看不见也打不了人，正是偷袭的时候
        enemies = [n for n in alive if n.status is None]
        hid = dodge = False
        for a, r in done:                           # 按先后顺序：先躲再动手就暴露，动完手再躲成了就藏住
            if a.action in ("attack", "stunt") and r.success:
                st.detected, st.hidden, hid = bool(alive), False, False     # 动了手就暴露了
            elif a.action in ("say", "talk"):
                st.hidden = hid = False                                     # 出声就藏不住了
            elif a.action == "hide" and r.success:
                st.hidden, st.detected, hid = True, False, True
            elif a.action == "dodge" and r.success:
                dodge = True
        stood = ticked + _enemies_stand(cur, alive)          # 倒地的这一轮爬起来，不算打断
        if not enemies or hid:
            _save_stealth(cur, player, st)
            return stood, False
        facts = []
        if not st.detected:
            if _roll(st.chance):
                st.detected, st.hidden = True, False
                facts.append(f"{'、'.join(n.name for n in enemies)}发现了{player.name}")
            elif not st.hidden:
                # 隐匿越高，被发现的几率涨得越慢
                step = max(DETECT_STEP_MIN, DETECT_STEP - 0.01 * skill_level(player.skills.get("stealth", 0)))
                st.chance = round(min(1.0, st.chance + step), 2)
        # 闪避：察觉越高躲得越好
        dodge_bonus = _dodge_bonus(player) if dodge else 0.0
        if _room_env(cur, player.room_id).get("ground") == "water" and not gear_has(cur, player, "wade"):
            dodge_bonus /= 2                    # 积水泥泞，躲不利索（沼泽高筒靴不怕）
        if st.detected:
            for npc in enemies:
                # 挨打那一下刚摆脱状态的，这回合来不及还手
                if any(f.startswith(f"{npc.name}摆脱了") for r in results for f in r.facts):
                    continue
                d = _distance(st, npc)
                if d > 0:
                    d = _set_distance(st, npc, d - ENEMY_STEP)
                    facts.append(f"{npc.name}逼近过来，离{player.name} {distance_word(d)}")
                if d not in MELEE_HIT:
                    continue
                facts += _npc_strike(cur, player, npc, "扑上来攻击", max(0.0, MELEE_HIT[d] - dodge_bonus))
                if player.hp <= 0:
                    break
        _save_stealth(cur, player, st)
        return stood + facts, bool(facts)


def _enemies_stand(cur: Cursor, alive: list[Npc]) -> list[str]:
    """被绊倒的敌人轮到行动时只能爬起来，这一轮不扑上来"""
    facts = []
    for n in alive:
        if n.status and n.status.kind == "prone":
            _set_status(cur, "npcs", n.id, None)
            facts.append(f"{n.name}从地上爬了起来")
    return facts


def do_attack(cur: Cursor, player: Player, view: RoomView, a: Attack) -> list[str]:
    # target 是 ref 就是打 NPC，不是 ref 就当玩家名字（PvP）
    # 双持：主手全额，副手加四分之一
    weapons = _weapons(cur, player)
    how = _wielding(weapons)
    power = player.attack + weapon_damage(weapons)
    if a.target not in view.refs:
        target = _pvp_target(cur, player, a.target)
        duel = _active_duel(cur, player.id, target.id)
        if duel is None:
            raise ActionError(f"{player.name}和{target.name}没有在决斗，不能伤害对方（先申请决斗，对方接受了才行）")
        d = duel["distance"]
        # 对手上一回合摆好了闪避：这一下更难打中，用掉就没了
        dodged = target.id in duel["dodging"]
        if dodged:
            cur.execute("update duels set dodging = array_remove(dodging, %s) where challenger = %s",
                        (target.id, duel["challenger"]))
        if not _roll(max(0.0, MELEE_HIT.get(d, 0) - (_dodge_bonus(target) if dodged else 0) - _drunk(player)
                         - _poisoned(player) + _prone_bonus(target, d))):
            return [f"{player.name}用{how}攻击{target.name}，" + (f"隔着 {distance_word(d)}够不着" if d not in MELEE_HIT
                                                                else f"隔着 {distance_word(d)}，没打中")]
        dmg = hurt_player_by(_bled(player, power), _defense(cur, target))
        facts, down = _hurt_player(cur, target, dmg, "player", player.name)
        return [f"{player.name}用{how}攻击{target.name}，造成 {dmg} 点伤害"] + facts + _duel_over(cur, down)

    npc = _room_npc(cur, view, player, a.target)
    if not npc.combatable:
        raise ActionError(f"{npc.name}不是能打的对象")
    if npc.template.hostile:
        # 近战按距离算命中；敌人以外的 NPC 就在跟前说话，不算距离
        d = _distance(_stealth(player), npc)
        light = _light(cur, player.room_id, player=player)
        if gear_has(cur, player, "darkvision") and not _effect(player, "blind"):
            light = max(light, LIGHT_FULL)      # 石像鬼之眼：暗处命中不打折
        chance = _light_hit(light, MELEE_HIT.get(d, 0))
        if not _roll(max(0.0, chance - _drunk(player) - _poisoned(player) + _prone_bonus(npc, d))):
            swung = _fire(cur, player, "attack", npc=npc)
            return [f"{player.name}用{how}攻击{npc.name}，" + (f"隔着 {distance_word(d)}够不着" if d not in MELEE_HIT
                                                             else f"隔着 {distance_word(d)}，没打中")] \
                + _attack_extras(cur, player, npc, [e for e in swung if e["do"] in ("splash", "self_damage")])
    # 这场仗的第一次出手（伏击者短刀）
    st = _stealth(player)
    first = not st.struck
    if first:
        st.struck = True
        _save_stealth(cur, player, st)
    swung = _fire(cur, player, "attack", npc=npc)
    fired = _fire(cur, player, "hit", npc=npc) + (_fire(cur, player, "fight", npc=npc) if first else [])
    bonus = sum(int(e.get("value", 0)) for e in swung + fired if e["do"] == "bonus")
    pierce = sum(int(e.get("value", 0)) for e in fired if e["do"] == "pierce")
    corrode = _npc_effect(npc, "corrode")
    armor = max(0, npc.template.defense - pierce - (corrode.value if corrode else 0))
    whet = _effect(player, "whet")
    dmg = _bled(player, max(1, power + (whet.value if whet else 0) + bonus + _aura(cur, player, "attack") - armor))
    facts, dead = _hurt_npc(cur, player, npc, dmg)
    facts = ([f"{player.name}用{how}攻击{npc.name}，造成 {dmg} 点伤害"]
             + _labels([e for e in fired if e["do"] in ("bonus", "pierce")]) + facts
             + _hit_extras(cur, player, npc, dmg, dead, fired)
             + _attack_extras(cur, player, npc, [e for e in swung if e["do"] in ("splash", "self_damage")]))
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
    # 拿武器做花样比空手能打得狠：上限放到重伤，打中了再加一半武器伤害
    weapon = item if item and item.template.type == "weapon" else None
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
    doused = []
    if feature and feature["key"] in env.get("lamps", []):
        # 火盆、烛台被拿去砸人：灯灭了，房间暗下来
        dimmer = max(0, _base_light(env) - dungeon.LAMP_LIGHT)
        cur.execute("""update rooms set props = jsonb_set(props, '{env,light}', to_jsonb(%s::int)) where id = %s""",
                    (dimmer, player.room_id))
        doused = [f"{feature['name']}的火光灭了，这里暗了下来（光亮 {dimmer}）"]
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
            sneak = is_npc and target.template.hostile and not _stealth(player).detected
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
                skill, diff = "stealth", max(diff, ASSASSINATE_DIFFICULTY)
                facts.append(f"{target.name}还没发现{player.name}，可以出其不意地偷袭")
                tier, finisher = a.tier, True       # 偷袭得手不受徒手最多轻伤的限制
    if harmless_prank:
        tier = "none"
    # 伤得越重难度下限越高（轻伤 2、重伤 3、致命 4）；对无力反抗的补刀、下毒不算
    if not poison and not helpless:
        diff = max(diff, TIER_MIN_DIFFICULTY[tier])
    if _light(cur, player.room_id, player=player) < LIGHT_DARK:
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
    if dmg := (0 if harmless_prank else poison.harm if poison else target.hp if lethal_blow
               else _bled(player, random.randint(*TIER_RANGE[tier])
                          + (int(weapon.damage * WEAPON_STUNT_SHARE) if weapon and tier != "none" else 0))):
        if is_npc:
            hurt, down = _hurt_npc(cur, player, target, dmg)
        else:
            hurt, down = _hurt_player(cur, target, dmg, *(("poison", poison.name) if poison else ("player", player.name)))
        facts += [f"{target.name}受到 {dmg} 点伤害"] + hurt + ([] if is_npc else _duel_over(cur, down))
    if poison and poison.knockout and not down:
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


def do_rest(cur: Cursor, player: Player, view: RoomView, a: Rest) -> list[str]:
    """住店：这里有开店的 NPC（world.yaml 的 props.inn）就付钱睡一觉，回满血、醒酒、状态清掉"""
    host = next((n for n in view.npcs if n.template.props.get("inn") and n.status is None), None)
    if host is None:
        raise ActionError("这里没有能住的地方")
    price = host.template.props["inn"].get("price", 0)
    patron = _pay(cur, player, price, host)
    cur.execute(f"""update players set hp = max_hp + {RESTORE_MAX_HP}, max_hp = max_hp + {RESTORE_MAX_HP}, effects = '[]',
                   status = null, drunk_until = null, drinks = 0, updated_at = now()
                   where id = %s""", (player.id,))
    return [f"{player.name}付了 {price} 金币，在{host.name}这儿要了间房，美美睡了一觉",
            f"{player.name}精神饱满，HP {player.max_hp}/{player.max_hp}"
            + ("，酒也醒了" if player.drunk else "")] + patron


def do_stand(cur: Cursor, player: Player, view: RoomView, a: Stand) -> list[str]:
    """倒地后爬起来：不用掷骰，这一下就花在站起来上了"""
    st = player.status
    if st is None or st.kind != "prone":
        raise ActionError(f"{player.name}没有倒在地上" + (f"，而是{st.describe()}" if st else ""))
    _set_status(cur, "players", player.id, None)
    return [f"{player.name}从地上爬了起来"]


def do_struggle(cur: Cursor, player: Player, view: RoomView, a: Struggle) -> list[str]:
    """挣脱（体操）、醒来（不靠技能）。AI 看玩家怎么做判这次的难度，但不能比施加时判的容易超过一级；
    每失败一次难度降一级"""
    st = player.status
    if st is None:
        raise ActionError(f"{player.name}没有被困住，用不着挣脱")
    if st.kind == "prone":
        return do_stand(cur, player, view, Stand(action="stand"))
    diff = max(1, max(a.difficulty, st.escape - 1) - st.attempts)
    ok, rolled = _check(cur, player, view, "acrobatics" if st.kind == "restrained" else None, diff)
    facts = [f"{player.name}尝试：{a.description or '挣脱'}"] + rolled
    if ok:
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
        # 医药判定：救醒回 1 点血，医药每级多回 1 点
        ok, facts = _check(cur, player, view, "medicine", REVIVE_DIFFICULTY)
        if not ok:
            return facts + [f"{player.name}给{target.name}做了急救，但没能救醒"]
        hp = min(target.max_hp, 1 + skill_level(player.skills.get("medicine", 0)))
        cur.execute(f"""update players set hp = %s, max_hp = max_hp + {RESTORE_MAX_HP}, effects = '[]', updated_at = now()
                        where id = %s""", (hp, target.id))
        facts += [f"{player.name}给{target.name}做了急救，{target.name}醒了过来", f"{target.name} HP {hp}/{target.max_hp}"]
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
    # 武器桶这类物件跟 NPC 列在一起，但不会说话，叙事按它的描述写
    if d := next((d for d in view.dispensers if d.id == view.resolve(a.target)), None):
        return [f"{player.name}对{d.container}说：“{a.message}”", f"{d.container}只是个物件，不会回应"]
    npc = _room_npc(cur, view, player, a.target)
    # AI 解析偶尔把"对麦琪 来杯酒"整句当成说的话：只去掉"对麦琪"这种指令开头；"麦琪大人""麦琪，救我"是喊人，留着。
    # 外面多包的一层引号（玩家自己打了引号）也去掉
    message = re.sub(rf"^(对|跟|和|向){re.escape(npc.name)}(说|讲|问)?[\s，,：:]*", "", a.message).strip() or a.message
    message = message.strip("\"“”'‘’「」").strip() or message
    facts = [f"{player.name}对{npc.name}说：“{message}”"] + _stele(cur, npc, player)
    offers = _offers(cur, player.id, npc)
    # 刚说要白给她钱、她问了"真要给我？"，这句回"是""给你"就给
    if (tip := offers.get("tip")) and TIP_CONFIRM_RE.search(message) and not re.search(r"不|算了|别", message[:4]):
        if tip["price"] > player.gold:
            return facts + [f"{player.name}身上只有 {player.gold} 金币，给不出 {tip['price']}"]
        return facts + _tip(cur, player, npc, tip["price"], tip)
    # 问住店（"住店多少钱"）：记一笔住店的报价，接着给钱就是付房钱
    if (inn := npc.template.props.get("inn")) and REST_TALK_RE.search(message):
        _put_offer(cur, player.id, npc, "inn", inn.get("price", 0))
    return facts


TIP_CONFIRM_RE = re.compile(r"^(是|对|嗯|确认|真的|真给|给你|收下|拿着|收着|当然|没错|给|要)")


def do_pay(cur: Cursor, player: Player, view: RoomView, a: Pay) -> list[str]:
    if a.amount > player.gold:
        raise ActionError(f"{player.name}身上只有 {player.gold} 金币，不够 {a.amount}")
    if a.target not in view.refs:
        other, _ = _room_player(cur, player, a.target, lock=True)
        cur.execute("update players set gold = gold - %s where id = %s", (a.amount, player.id))
        cur.execute("update players set gold = gold + %s where id = %s", (a.amount, other.id))
        return [f"{player.name}给了{other.name} {a.amount} 金币"]
    npc = _room_npc(cur, view, player, a.target)
    offers = _offers(cur, player.id, npc)
    deals = {k: v for k, v in offers.items() if k != "tip"}
    if deals:
        # 刚谈过买卖：付的就是最近那笔的钱
        key, offer = max(deals.items(), key=lambda kv: kv[1].get("at", 0))
        price = offer["price"]
        if a.amount < price:
            raise ActionError(f"{npc.name}要的是 {price} 金币，{player.name}只给了 {a.amount}")
        facts = _close_deal(cur, player, view, npc, key)
        if extra := a.amount - price:
            player = load_player(cur, player.id, lock=True)
            facts += [f"多给的 {extra} 金币{npc.name}当小费收下了"] + _pay(cur, player, extra, npc)
        return facts
    return _tip(cur, player, npc, a.amount, offers.get("tip"))


def _close_deal(cur: Cursor, player: Player, view: RoomView, npc: Npc, key: str) -> list[str]:
    """付钱成交一笔报过价的买卖：住店、升级、墙上的货、现做的东西"""
    if key == "inn":
        cur.execute("update player_npc_relations set offers = offers - 'inn' where player_id = %s and npc_template = %s",
                    (player.id, npc.template.id))
        return do_rest(cur, player, view, Rest(action="rest"))
    if key.startswith("upgrade:"):
        ref = next((r for r, uid in view.refs.items() if str(uid) == key[8:]), None)
        if ref is None:
            raise ActionError("要升级的武器不在身上了")
        return do_upgrade(cur, player, view, Upgrade(action="upgrade", item=ref, target=next(
            r for r, uid in view.refs.items() if uid == npc.id)))
    r = (npc_buy_made if key.startswith("made:") else npc_sell)(cur.connection, player.id, npc, key)
    if not r.success:
        raise ActionError(r.facts[0])
    return r.facts


def _tip(cur: Cursor, player: Player, npc: Npc, amount: int, pending: Optional[dict]) -> list[str]:
    """没在做买卖就给钱：第一次 NPC 只问一句，同样的数目再给一次（或者回一句"是""给你"）才收"""
    if pending is None or pending["price"] != amount:
        _put_offer(cur, player.id, npc, "tip", amount)
        return [f"{player.name}想白给{npc.name} {amount} 金币（不是买东西），{npc.name}还没收",
                f"{npc.name}要先问一句是不是真要给她，{player.name}确认了才收"]
    cur.execute("update player_npc_relations set offers = offers - 'tip' where player_id = %s and npc_template = %s",
                (player.id, npc.template.id))
    return ([f"{player.name}把 {amount} 金币塞给了{npc.name}，{npc.name}收下了（白给的心意，不是买东西，她不用拿东西给他）"]
            + _pay(cur, player, amount, npc))


def _stele(cur: Cursor, npc: Npc, player: Player) -> list[str]:
    """地窖石碑（world.yaml props.leaderboard）：看它、跟它说话，碑面上都会显出到过地牢最深处的人，
    还有这个人能直接传送到哪几层"""
    if not npc.template.props.get("leaderboard"):
        return []
    cur.execute("select waypoints from players where id = %s", (player.id,))
    points = cur.fetchone()["waypoints"]
    return [dungeon.leaderboard(cur), f"碑面上还浮现出给{player.name}的字：{dungeon.waypoints_text(points)}"]


# 收购：做生意的 NPC（props.buys.likes 是她用得上的种类）什么都收，按参考价（base_price）的一定比例给钱：
# 用得上的 BUY_LIKED，别的 BUY_OTHER，好感每 10 点再多 BUY_AFFINITY（最多 ±BUY_AFFINITY_MAX）。钥匙、任务要交的东西不收
BUY_LIKED, BUY_OTHER = 0.6, 0.3
BUY_AFFINITY, BUY_AFFINITY_MAX = 0.02, 0.1


def item_kinds(item: ItemInstance) -> set[str]:
    """东西属于哪几类（收购时看她用不用得上）：weapon / armor / food / drink / herb / 礼物种类（ore wine book） / misc"""
    kinds = {item.template.type if item.template.type in ("weapon", "armor", "misc") else ""}
    if item.template.type == "consumable":
        kinds.add("drink" if _is_alcohol(item) or any(c in item.name for c in "水汤茶奶") else "food")
    if _prop(item, "herbal"):
        kinds.add("herb")
    if gift := _prop(item, "gift"):
        kinds.add(gift)
    return kinds - {""}


def item_stats(item: ItemInstance) -> dict:
    return {"damage": item.damage, "defense": item.defense, "heal": item.heal, "harm": item.harm,
            "knockout": item.knockout, "price": _prop(item, "price")}


def buy_price(npc: Npc, item: ItemInstance, affinity: int) -> int:
    """这个 NPC 收这件东西给多少钱"""
    liked = item_kinds(item) & set(npc.template.props.get("buys", {}).get("likes", []))
    rate = (BUY_LIKED if liked else BUY_OTHER) + max(-BUY_AFFINITY_MAX, min(BUY_AFFINITY_MAX, affinity // 10 * BUY_AFFINITY))
    return max(1, round(base_price(item_stats(item)) * rate)) * item.quantity


def buy_quotes(conn: Connection, player_id: UUID, npc: Npc, items: list[ItemInstance]) -> list[tuple[str, int]]:
    """给 AI 看的：她收玩家身上这些东西各给多少（玩家问收不收、值多少时照这个说）"""
    if not npc.template.props.get("buys"):
        return []
    with conn.transaction():
        cur = _cursor(conn)
        affinity = _affinity(cur, load_player(cur, player_id), npc)
        return [(i.name, buy_price(npc, i, affinity)) for i in items if not _precious(cur, i)]


def do_sell(cur: Cursor, player: Player, view: RoomView, a: Sell) -> list[str]:
    npc = _room_npc(cur, view, player, a.target)
    if not npc.template.props.get("buys"):
        raise ActionError(f"{npc.name}不收东西")
    item = _inv_item(cur, view, player, a.item)
    if _burning(item):
        raise ActionError(f"点着的{item.name}{npc.name}可不收")
    if _precious(cur, item):
        raise ActionError(f"{item.name}太要紧了，{npc.name}不收")
    price = buy_price(npc, item, _affinity(cur, player, npc))
    cur.execute("delete from item_instances where id = %s", (item.id,))
    cur.execute("update players set gold = gold + %s where id = %s", (price, player.id))
    return [f"{player.name}把{_label(item)}卖给了{npc.name}，得到 {price} 金币"]


LIKED_GIFT = 10                      # 送心爱的礼物加的好感（不受花钱加好感的上限）
GIFT_FACT = "心爱的礼物"                # 台词那边认这几个字，演出特别的反应


def do_give(cur: Cursor, player: Player, view: RoomView, a: Give) -> list[str]:
    item = _inv_item(cur, view, player, a.item)
    if _burning(item):
        raise ActionError(f"点着的{item.name}只能拿在自己手上，递不出去")
    if a.target not in view.refs:
        # 给同房间的玩家：target 是名字。睡着、倒下的也能收（东西放进他背包）
        other, _ = _room_player(cur, player, a.target, lock=True)
        _move_item(cur, item, player_id=other.id)          # 装备着的会自动卸下
        return [f"{player.name}把{_label(item)}交给了{other.name}"]
    npc = _room_npc(cur, view, player, a.target)
    # 正好是这个 NPC 的委托要的东西（锈剑交给莉娜）：先留在身上，接下来的 quest_turn 收走并发奖励，
    # 不然东西进了 NPC 背包，委托就再也触发不了
    cur.execute(
        """select 1 from quests q left join player_quests pq on pq.quest_id = q.id and pq.player_id = %s
           where q.giver = %s and q.needs_item = %s and pq.status is distinct from 'rewarded'""",
        (player.id, npc.template.id, item.template.id),
    )
    if cur.fetchone():
        return [f"{player.name}把{item.name}递给了{npc.name}"]
    # 送她心爱的礼物（world.yaml props.gift_likes 里的种类）：好感固定 +LIKED_GIFT，东西她收下（从世界里拿走）
    if (gift := _prop(item, "gift")) and gift in npc.template.props.get("gift_likes", []):
        cur.execute("delete from item_instances where id = %s", (item.id,))
        new = min(100, _affinity(cur, player, npc) + LIKED_GIFT)
        cur.execute("""insert into player_npc_relations (player_id, npc_template, affinity) values (%s, %s, %s)
                       on conflict (player_id, npc_template) do update set affinity = excluded.affinity""",
                    (player.id, npc.template.id, new))
        return [f"{player.name}把{_label(item)}送给了{npc.name}，这正是她最想要的东西（{GIFT_FACT}）",
                f"{npc.name}对{player.name}的好感上升（当前 {new}，收到心爱的礼物）"]
    _move_item(cur, item, npc_id=npc.id)
    return [f"{player.name}把{_label(item)}交给了{npc.name}"]


# 铁匠升级武器：每级伤害 +1，名字后面标 +N。升到第 N 级有 N × UPGRADE_BREAK_STEP 的几率失败（最多 UPGRADE_BREAK_MAX），
# 失败不会碎，而是退一级（+3 升 +4 失败变 +2，+0 失败还是 +0），钱照收。
# 等级不封顶。费用是升级后和升级前建议价的差（至少 UPGRADE_MIN_COST），跟着伤害指数涨
UPGRADE_BREAK_STEP = 0.10
UPGRADE_BREAK_MAX = 0.90
UPGRADE_MIN_COST = 5


def upgrade_terms(item: ItemInstance) -> tuple[int, int, float]:
    """(升到几级, 费用, 失败的几率)"""
    level = item.props.get("plus", 0) + 1
    cost = max(UPGRADE_MIN_COST, base_price({"damage": item.damage + 1}) - base_price({"damage": item.damage}))
    return level, cost, min(UPGRADE_BREAK_MAX, UPGRADE_BREAK_STEP * level)


def do_upgrade(cur: Cursor, player: Player, view: RoomView, a: Upgrade) -> list[str]:
    npc = _room_npc(cur, view, player, a.target)
    if not npc.template.props.get("upgrades"):
        raise ActionError(f"{npc.name}不会升级武器")
    item = _inv_item(cur, view, player, a.item)
    if item.template.type != "weapon":
        raise ActionError(f"{item.name}不是武器，{npc.name}没法升级")
    level, cost, risk = upgrade_terms(item)
    key = f"upgrade:{item.id}"
    offer = _offers(cur, player.id, npc).get(key)
    terms = (f"升到 +{level}（伤害 {item.damage} → {item.damage + 1}）要 {cost} 金币，有 {round(risk * 100)}% 的可能失败"
             + ("，失败会退一级" if level > 1 else "，失败了钱白花"))
    if offer is None or offer["price"] != cost:
        # 第一次只开价，说清风险，玩家再说一次才动手
        _put_offer(cur, player.id, npc, key, cost)
        return [f"{npc.name}看了看{player.name}的{item.name}：{terms}", f"{player.name}再说一次要升级，{npc.name}就动手"]
    patron = _pay(cur, player, cost, npc)
    cur.execute("update player_npc_relations set offers = offers - %s where player_id = %s and npc_template = %s",
                (key, player.id, npc.template.id))
    facts = [f"{player.name}付了 {cost} 金币，{npc.name}把{item.name}放进炉火里重新锻打（{terms}）"] + patron
    base = re.sub(r" \+\d+$", "", item.name)
    if _roll(risk):
        down = max(0, level - 2)            # 现在是 level-1，失败退一级
        if down == level - 1:
            return facts + [f"淬火的时候火候没掌握好，{item.name}没升上去，好在也没伤着"]
        dmg = item.damage - 1
        name = base + (f" +{down}" if down else "")
        cur.execute("update item_instances set props = props || %s where id = %s",
                    (Jsonb({"plus": down, "damage": dmg, "name": name}), item.id))
        return facts + [f"淬火的时候刃口崩了一块，{item.name}退回了{name}，伤害 {dmg}"]
    name = base + f" +{level}"
    cur.execute("update item_instances set props = props || %s where id = %s",
                (Jsonb({"plus": level, "damage": item.damage + 1, "name": name}), item.id))
    return facts + [f"升级成功：{item.name}变成了{name}，伤害 {item.damage + 1}"]


def do_respawn(cur: Cursor, player: Player, view: RoomView, a: Respawn) -> list[str]:
    """倒下的人被抬回有看店 NPC 的地方（默认酒馆），NPC 当场扶起来回满血，按倒下的原因说句话"""
    if player.hp > 0:
        raise ActionError(f"{player.name}没有倒下，用不着复活")
    cur.execute(
        """select distinct r.id, r.name from rooms r join npcs n on n.room_id = r.id join npc_templates t on t.id = n.template_id
           where n.alive and not t.hostile and t.props ? 'revive_lines'""")
    havens = cur.fetchall()
    named = next((r for r in havens if a.target and (a.target in r["name"] or r["name"] in a.target)), None)
    dest = named or next((r for r in havens if r["id"] == RESPAWN_ROOM), None)
    if dest is None:
        raise ActionError(f"没有能把{player.name}抬过去照看的地方" + (f"（{a.target}）" if a.target else ""))
    if dest["id"] == player.room_id:
        raise ActionError(f"{player.name}已经在{dest['name']}了，等人来扶")
    cur.execute("update players set room_id = %s, following = null, stealth = null, updated_at = now() where id = %s",
                (dest["id"], player.id))
    facts = [f"有人把{player.name}带回了{dest['name']}"]
    # 那边的人先看到被抬进来，再看到扶起来
    cur.execute("insert into events (room_id, kind, observer) values (%s, 'carried_in', %s)",
                (dest["id"], f"有人把倒下的{player.name}抬了进来。"))
    revived = _keeper_revive(cur, dest["id"])
    return facts + (revived or [f"{dest['name']}里没人照看，{player.name}还躺着"])


def do_say(cur: Cursor, player: Player, view: RoomView, a: Say) -> list[str]:
    if a.target is None:
        return [f"{player.name}说：“{a.message}”"]
    cur.execute("select 1 from players where room_id = %s and name = %s and id <> %s",
                (player.room_id, a.target, player.id))
    if not cur.fetchone():
        raise ActionError(f"{a.target}不在这里")
    return [f"{player.name}对{a.target}说：“{a.message}”"]


def do_freeform(cur: Cursor, player: Player, view: RoomView, a: Freeform) -> list[str]:
    # 不改任何状态，叙事 AI 自由发挥；需要本事的（辨认草药、翻墙、查线索）判一次技能，叙事照成败写
    facts = [f"{player.name}尝试：{a.description}"]
    if a.skill and a.difficulty:
        ok, rolled = _check(cur, player, view, a.skill, a.difficulty)
        facts += rolled + ["成功了" if ok else "没有成功"]
    return facts


def do_reject(cur: Cursor, player: Player, view: RoomView, a: Reject) -> list[str]:
    raise ActionError(a.reason)


HANDLERS: dict[str, Callable[..., list[str]]] = {
    "move": do_move, "look": do_look, "take": do_take, "drop": do_drop, "use": do_use,
    "equip": do_equip, "unequip": do_unequip, "attack": do_attack, "talk": do_talk, "give": do_give, "freeform": do_freeform,
    "say": do_say, "revive": do_revive, "stunt": do_stunt, "struggle": do_struggle,
    "follow": do_follow, "unfollow": do_unfollow, "invite": do_invite, "join": do_join, "leave_party": do_leave_party,
    "maneuver": do_maneuver, "dodge": do_dodge, "hide": do_hide, "search": do_search, "reject": do_reject,
    "upgrade": do_upgrade, "pay": do_pay, "sell": do_sell, "respawn": do_respawn, "stand": do_stand, "rest": do_rest, "camp": do_camp, "teleport": do_teleport, "challenge": do_challenge, "accept_duel": do_accept_duel, "decline_duel": do_decline_duel, "flee": do_flee,
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
                if st and (st.kind == "restrained" and action.action in RESTRAINED_BLOCKED
                           or st.kind == "prone" and action.action in PRONE_BLOCKED
                           or st.kind == "incapacitated" and action.action != "struggle"):
                    raise ActionError(f"{player.name}{st.label}，" + ("得先站起来" if st.kind == "prone" else "做不到"))
                facts = HANDLERS[action.action](cur, player, view, action)
            return ActionResult(action=action.action, success=True, facts=facts)
        except ActionError as e:
            return ActionResult(action=action.action, success=False, facts=[str(e)])
        except pg_errors.DeadlockDetected:
            if attempt:
                raise


def execute_all(conn: Connection, view: RoomView, actions: list[PlayerAction]) -> list[ActionResult]:
    """按顺序执行，前一步失败就中断。玩家每做 ENEMY_EVERY 个动作，同区域的敌人行动一次（发现、逼近、攻击、爬起来），
    一句话结尾不够数也行动一次；facts 接在那一轮最后一个动作后面，results 和执行了的 actions 一一对应。
    敌人有动静就打断剩下的"""
    results, since = [], 0                  # since：上一轮敌人行动时玩家做到第几个动作
    pending = _tick(conn, view.player.id, "turn")
    for i, action in enumerate(actions, 1):
        pending += _tick(conn, view.player.id, "action")
        # 喝醉了说话含糊：改的是原话本身，叙事、旁人、NPC 听到的都是醉话
        if view.player.drunk and action.action in ("say", "talk"):
            action.message = slur(action.message)
        result = execute(conn, view, action)
        result.facts[:0], pending = pending, []
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
        if i - since == ENEMY_EVERY:
            enemy, acted = enemy_turn(conn, view.player.id, actions[since:i], results[since:i])
            since = i
            result.facts += enemy
            if acted and i < len(actions):
                result.facts.append(f"{view.player.name}被打断了，后面的动作没来得及做")
                return results
    if since < len(results):
        results[-1].facts += enemy_turn(conn, view.player.id, actions[since:len(results)], results[since:])[0]
    return results


def _tick(conn: Connection, player_id: UUID, unit: str) -> list[str]:
    with conn.transaction():
        cur = _cursor(conn)
        player = load_player(cur, player_id, lock=True)
        return _tick_effects(cur, player, unit)


# 火把：背包里的火把（props.lights）拿到手上就换成点燃的样子；点燃的（props.burning）带到地牢下一层换成弱光的，
# 弱光的再下一层就烧完了。点燃的只能拿在手上：卸下、扔掉要再说一次确认，确认了就熄灭没了；不能给人、不能卖
DOUSE_CONFIRM = 120                     # 秒：说了一次要收火把，这么久内再说一次就真的熄灭


def _burning(item: ItemInstance) -> bool:
    return "burning" in item.template.props


def _torch_floor(cur: Cursor, names: list[str], room_id: str) -> list[str]:
    """这些人拿着点着的火把下到了地牢新的一层（走下来、传送来）：火光变小，或者烧完"""
    if not dungeon.is_dungeon(room_id) or not names:
        return []
    cur.execute("""select i.id, t.name as item, t.props->>'burning' as next, p.name as who
                   from item_instances i join item_templates t on t.id = i.template_id join players p on p.id = i.player_id
                   where p.name = any(%s) and t.props ? 'burning' and i.equipped_slot is not null""", (names,))
    facts = []
    for r in cur.fetchall():
        if r["next"]:
            cur.execute("update item_instances set template_id = %s, props = '{}' where id = %s", (r["next"], r["id"]))
            facts.append(f"{r['who']}手上的火把火光变小了，照不了那么亮了，再下一层就会烧完")
        else:
            cur.execute("delete from item_instances where id = %s", (r["id"],))
            facts.append(f"{r['who']}手上的火把烧到了头，熄灭了")
    return facts


def _douse(cur: Cursor, player: Player, item: ItemInstance) -> list[str]:
    """想把点着的火把收起来、扔掉：第一次只提醒，DOUSE_CONFIRM 秒内再说一次才熄灭（东西没了）"""
    asked = item.props.get("douse_asked", 0)
    cur.execute("select extract(epoch from now())::float as t")
    now = cur.fetchone()["t"]
    if now - asked > DOUSE_CONFIRM:
        cur.execute("update item_instances set props = props || jsonb_build_object('douse_asked', %s::float) where id = %s",
                    (now, item.id))
        return [f"{item.name}收起来就灭了，灭了就没法再点，只能扔掉",
                f"{player.name}还没动手：真要熄掉就再说一次"]
    cur.execute("delete from item_instances where id = %s", (item.id,))
    return [f"{player.name}把{item.name}按在地上掐灭了，烧过的火把没法再用了"]


def _keeper_eject(conn: Connection, view: RoomView) -> list[str]:
    """房间里有能轰人的 NPC（麦琪、莉娜：配了 eject_to，醒着）就把动手打人的玩家轰出去，记进 NPC 对他的记忆。
    NPC 被放倒、捆住了就管不了"""
    keeper = next((n for n in view.npcs if can_eject(n) and not n.template.hostile), None)
    if keeper is None:
        return []
    with conn.transaction():
        cur = _cursor(conn)
        fresh = load_npcs(cur, "n.id = %s and n.alive and n.status is null", (keeper.id,))
    if not fresh or not (kicked := npc_eject(conn, view.player.id, fresh[0])):
        return []
    add_npc_log(conn, view.player.id, fresh[0], f"{view.player.name}在你这里对别的客人动手，你把他轰了出去")
    with conn.transaction():
        revived = _keeper_revive(_cursor(conn), view.room.id)      # 被打倒的客人当场扶起来
    return [f"{keeper.name}看见{view.player.name}动手打人"] + kicked.facts + revived


# ============ NPC 给予（对话步骤调用） ============

def _give_rule_ok(cur: Cursor, npc: Npc, player: Player, item: ItemInstance) -> bool:
    # 玩家身上已经有同样的东西就不再给（钥匙这类任务物品，不然每次聊天都塞一把）
    cur.execute("select 1 from item_instances where player_id = %s and template_id = %s", (player.id, item.template.id))
    if cur.fetchone():
        return False
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


# 在 NPC 那儿花钱加好感：每次成交 +1，每满 10 金币再 +1，一次最多 PATRON_MAX；
# 光靠花钱最多到 PATRON_CAP，再往上（白送东西的交情）得靠聊天、帮忙、做委托
PATRON_MAX = 5
PATRON_CAP = 30


def _pay(cur: Cursor, player: Player, price: int, npc: Optional[Npc] = None) -> list[str]:
    """交易扣钱：价钱是 AI 定的，这里只管够不够、扣掉。钱不够整笔交易回滚。
    给了 npc 就是在他那儿消费，好感跟着涨，返回好感变化的 fact"""
    price = max(0, price)
    if price > player.gold:
        raise ActionError(f"{player.name}的钱不够，要 {price} 金币，身上只有 {player.gold} 金币")
    if not price:
        return []
    cur.execute("update players set gold = gold - %s where id = %s", (price, player.id))
    if npc is None:
        return []
    now = _affinity(cur, player, npc)
    new = max(now, min(PATRON_CAP, now + min(PATRON_MAX, 1 + price // 10)))
    if new == now:
        return []                       # 已经是熟客了，再花钱也不涨
    cur.execute(
        """insert into player_npc_relations (player_id, npc_template, affinity) values (%s, %s, %s)
           on conflict (player_id, npc_template) do update set affinity = excluded.affinity""",
        (player.id, npc.template.id, new))
    return [f"{npc.name}对{player.name}的好感上升（当前 {new}，照顾生意）"]


def _deal(npc: Npc, player: Player, name: str, price: int) -> str:
    return (f"{npc.name}把{name}卖给了{player.name}，收了 {price} 金币" if price > 0
            else f"{npc.name}把{name}交给了{player.name}")


def npc_give(conn: Connection, player_id: UUID, npc_id: UUID, item_id: UUID, price: int = 0) -> ActionResult:
    """对话 AI 提议 NPC 给玩家东西时调用，规则不满足就当没说。price 是 AI 定的价钱，0 就是白给"""
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
            patron = _pay(cur, player, price, npc)
            _move_item(cur, items[0], player_id=player.id)
        return ActionResult(action="npc_give", success=True, facts=[_deal(npc, player, _label(items[0]), price)] + patron)
    except ActionError as e:
        return ActionResult(action="npc_give", success=False, facts=[str(e)])


def sellable(conn: Connection, npc: Npc) -> list[dict]:
    """NPC 能卖的货（world.yaml 的 sells），给交易 AI 看：物品 id、名字、说明、伤害防御、按效果算的原价"""
    ids = npc.template.props.get("sells", [])
    if not ids:
        return []
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("select id, name, description, type, damage, defense, heal, props->'price' as price"
                    " from item_templates where id = any(%s)", (ids,))
        return [r | {"base_price": base_price(r)} for r in cur.fetchall()]


OFFER_WINDOW = "10 minutes"             # NPC 报的价多久内有效


def get_offers(conn: Connection, player_id: UUID, npc: Npc) -> dict[str, dict]:
    """NPC 给这个玩家报过、还没过期的价 {key: {price, spec}}。
    key 是卖货的物品 id，或者 "made:名字"（现造的东西，spec 是报价时定好的规格）"""
    with conn.transaction():
        return _offers(_cursor(conn), player_id, npc)


def _offers(cur: Cursor, player_id: UUID, npc: Npc) -> dict[str, dict]:
    cur.execute(
        f"""select o.key, o.value from player_npc_relations r, jsonb_each(r.offers) o
            where r.player_id = %s and r.npc_template = %s
              and to_timestamp((o.value->>'at')::float) > now() - interval '{OFFER_WINDOW}'""",
        (player_id, npc.template.id),
    )
    return {r["key"]: r["value"] for r in cur.fetchall()}


def _put_offer(cur: Cursor, player_id: UUID, npc: Npc, key: str, price: int, spec: Optional[dict] = None) -> None:
    """记一条报价；已有的就改价钱（砍价让步），现造东西的规格不变"""
    value = {"price": max(0, price)} | ({"spec": spec} if spec else {})
    cur.execute(
        """insert into player_npc_relations (player_id, npc_template, offers)
           values (%(p)s, %(t)s, jsonb_build_object(%(k)s::text, %(v)s::jsonb || jsonb_build_object('at', extract(epoch from now()))))
           on conflict (player_id, npc_template) do update set offers = player_npc_relations.offers
             || jsonb_build_object(%(k)s::text, coalesce(player_npc_relations.offers->%(k)s, '{}'::jsonb) || %(v)s::jsonb
                                   || jsonb_build_object('at', extract(epoch from now())))""",
        {"p": player_id, "t": npc.template.id, "k": key, "v": Jsonb(value)},
    )


def set_offer(conn: Connection, player_id: UUID, npc: Npc, key: str, price: int) -> None:
    """叙事里 NPC 报了价、砍价让了步就记下来：卖货清单里的物品 id，或者已经报过价的现造东西"""
    offers = get_offers(conn, player_id, npc)
    if key not in npc.template.props.get("sells", []) and key not in offers:
        return
    with conn.transaction():
        cur = _cursor(conn)
        if key.startswith("made:"):
            stats = offers[key].get("spec", {})
        else:
            cur.execute("select damage, defense, heal, props->'price' as price from item_templates where id = %s", (key,))
            stats = cur.fetchone()
        _put_offer(cur, player_id, npc, key, clamp_price(stats, price))


def npc_hand(conn: Connection, player_id: UUID, npc: Npc, key: str, price: int, affinity: int) -> ActionResult:
    """叙事里 NPC 把货递给了玩家（交易那步没给）：照做。key 是卖货的物品 id 或 "made:名字"（做过的货）。
    叙事写了收钱就按那个价（限在建议价一半到两倍）；没写收钱的，交情够就白送，不够按建议价收；钱不够就没给成"""
    try:
        with conn.transaction():
            cur = _cursor(conn)
            player = load_player(cur, player_id, lock=True)
            if key.startswith("made:"):
                cur.execute("select spec from npc_goods where npc_template = %s and name = %s", (npc.template.id, key[5:]))
                row = cur.fetchone()
                if row is None:
                    raise ActionError(f"{npc.name}没做过{key[5:]}")
                stats, name = row["spec"], f"{row['spec']['name']}（{effect_text(row['spec'])}）"
            else:
                if key not in npc.template.props.get("sells", []):
                    raise ActionError(f"{npc.name}不卖这个")
                cur.execute("select name, damage, defense, heal, props->'price' as price from item_templates where id = %s",
                            (key,))
                stats = cur.fetchone()
                name = stats["name"]
            price = 0 if price <= 0 and can_gift(affinity) else clamp_price(stats, price if price > 0 else base_price(stats))
            patron = _pay(cur, player, price, npc)
            if key.startswith("made:"):
                _make(cur, player_id, stats)
            else:
                cur.execute("insert into item_instances (template_id, player_id) values (%s, %s)", (key, player_id))
        return ActionResult(action="npc_sell", success=True, facts=[_deal(npc, player, name, price)] + patron)
    except ActionError as e:
        return ActionResult(action="npc_sell", success=False, facts=[str(e)])


SELL_MAX_COUNT = 10                     # 墙上的货一次最多买几件


def npc_sell(conn: Connection, player_id: UUID, npc: Npc, template_id: str, price: Optional[int] = None,
             count: int = 1) -> ActionResult:
    """NPC 卖一件货给玩家（货不限量，每卖一件新造一件）。报过价就按报的价收；玩家直接下单（没报过价）就按 AI 这回合
    给的价，限在建议价的一半到两倍（没给就按建议价）"""
    try:
        if template_id not in npc.template.props.get("sells", []):
            raise ActionError(f"{npc.name}不卖这个")
        offer = get_offers(conn, player_id, npc).get(template_id)
        with conn.transaction():
            cur = _cursor(conn)
            if offer is None:
                cur.execute("select damage, defense, heal, props->'price' as price from item_templates where id = %s",
                            (template_id,))
                stats = cur.fetchone()
                offer = {"price": clamp_price(stats, price or base_price(stats))}
            player = load_player(cur, player_id, lock=True)
            count = max(1, min(SELL_MAX_COUNT, count))
            patron = _pay(cur, player, offer["price"] * count, npc)
            # 成交了这个报价就作废，再买要重新谈
            cur.execute("update player_npc_relations set offers = offers - %s where player_id = %s and npc_template = %s",
                        (template_id, player_id, npc.template.id))
            for _ in range(count):
                cur.execute("insert into item_instances (template_id, player_id) values (%s, %s)", (template_id, player_id))
            cur.execute("select name from item_templates where id = %s", (template_id,))
            name = cur.fetchone()["name"] + (f" ×{count}" if count > 1 else "")
        return ActionResult(action="npc_sell", success=True, facts=[_deal(npc, player, name, offer["price"] * count)] + patron)
    except ActionError as e:
        return ActionResult(action="npc_sell", success=False, facts=[str(e)])


# ============ 任务（对话步骤调用） ============
# 任务定义在 world.yaml 的 quests，进度按玩家记在 player_quests：没记录 = 没接，offered = NPC 提过，
# rewarded = 了结。做没做完看玩家 flags 里有没有 done_flag。跟发布任务的 NPC 说话时推进

def quest_turn(conn: Connection, player_id: UUID, npc_id: UUID) -> tuple[list[ActionResult], list[tuple[str, dict]]]:
    """跟 NPC 说话时处理这个 NPC 发布的任务：做完了就自动发奖励，没接过的记成已提起。
    返回 (奖励的执行结果, 给叙事的任务情况 [(new|active|done|closed, 任务)])"""
    results, context = [], []
    with conn.transaction():
        cur = _cursor(conn)
        player = load_player(cur, player_id, lock=True)
        npcs = load_npcs(cur, "n.id = %s", (npc_id,))
        if not npcs:
            return results, context
        npc = npcs[0]
        cur.execute(
            """select q.*, pq.status from quests q
               left join player_quests pq on pq.quest_id = q.id and pq.player_id = %s
               where q.giver = %s order by q.id""",
            (player_id, npc.template.id),
        )
        for q in cur.fetchall():
            # 两种完成条件：身上有标记（打死哥布林），或者带着要的东西来找发布者（锈剑）
            brought = None
            if q["needs_item"]:
                brought = next(iter(load_items(cur, "i.player_id = %s and i.template_id = %s",
                                               (player_id, q["needs_item"]), lock=True)), None)
            done = bool(q["done_flag"] and player.flags.get(q["done_flag"])) or brought is not None
            if q["status"] == "rewarded":
                context.append(("closed", q))
            elif done:
                # 做完了：奖励直接给（身上已经有就不重复给），不用玩家开口要；要带的东西收走
                facts = []
                if brought:
                    if brought.quantity > 1:
                        cur.execute("update item_instances set quantity = quantity - 1 where id = %s", (brought.id,))
                    else:
                        cur.execute("delete from item_instances where id = %s", (brought.id,))
                    facts.append(f"{npc.name}收走了{player.name}的{brought.name}")
                if q["reward_item"]:
                    cur.execute("select 1 from item_instances where player_id = %s and template_id = %s",
                                (player_id, q["reward_item"]))
                    if not cur.fetchone():
                        cur.execute("insert into item_instances (template_id, player_id) values (%s, %s)",
                                    (q["reward_item"], player_id))
                        cur.execute("select name from item_templates where id = %s", (q["reward_item"],))
                        facts.append(f"{npc.name}把{cur.fetchone()['name']}交给了{player.name}（任务奖励）")
                results.append(ActionResult(action="quest", success=True,
                                            facts=[f"{player.name}完成了{npc.name}的委托：{q['goal']}"] + facts))
                _set_quest(cur, player_id, q["id"], "rewarded")
                context.append(("done", q))
            elif q["status"] == "offered":
                context.append(("active", q))
            elif q["hidden"]:
                continue                        # 隐藏委托：NPC 不提，玩家自己碰上（带着东西来）才触发
            else:
                # 这次对话 NPC 提起委托：写成 fact，叙事一定会写到，玩家界面上也能看到这条
                _set_quest(cur, player_id, q["id"], "offered")
                results.append(ActionResult(action="quest", success=True,
                                            facts=[f"{npc.name}有件事想托{player.name}：{q['goal']}"]))
                context.append(("new", q))
    return results, context


def _set_quest(cur: Cursor, player_id: UUID, quest_id: str, status: str) -> None:
    cur.execute(
        """insert into player_quests (player_id, quest_id, status) values (%s, %s, %s)
           on conflict (player_id, quest_id) do update set status = excluded.status, updated_at = now()""",
        (player_id, quest_id, status),
    )


# ============ NPC 现造东西、轰人（对话步骤调用） ============

MADE_TEMPLATES = {"food": "made_food", "drink": "made_drink", "misc": "made_misc", "weapon": "made_weapon"}
CREATE_COOLDOWN = "3 minutes"           # 同一个 NPC 白送现造东西给同一个玩家的间隔
# 建议价按效果指数增长：好东西贵得快。NPC 报价在建议价的 OFFER_BAND 倍之间浮动；没有数值的东西（绳子、字条）
# 没有建议价，价钱全由 AI 定，限在 FREE_PRICE 之间
GEAR_PRICE = (2, 1.6)                   # 武器护甲：2 × 1.6^(伤害+防御)，伤害 3 约 8，伤害 5 约 21，伤害 6 约 34
POTION_PRICE = (0.8, 1.4)               # 吃喝：0.8 × 1.4^(回血+毒)，回 3 约 2，回 6 约 6
KNOCKOUT_PRICE = 5
OFFER_BAND = (0.5, 2.0)
FREE_PRICE = (1, 200)


def base_price(stats: dict) -> int:
    if stats.get("price"):
        return stats["price"]               # 模板写死了建议价（解酒药这类没数值的）
    gear = stats.get("damage", 0) + stats.get("defense", 0)
    potion = stats.get("heal", 0) + stats.get("harm", 0)
    price = ((GEAR_PRICE[0] * GEAR_PRICE[1] ** gear if gear else 0)
             + (POTION_PRICE[0] * POTION_PRICE[1] ** potion if potion else 0)
             + (KNOCKOUT_PRICE if stats.get("knockout") else 0))
    return max(1, round(price))


def has_stats(stats: dict) -> bool:
    return any(stats.get(k) for k in ("damage", "defense", "heal", "harm", "knockout", "price"))


def clamp_price(stats: dict, price: int) -> int:
    """AI 报的价限在建议价的一半到两倍；没数值的东西只限一个大范围"""
    if not has_stats(stats):
        return max(FREE_PRICE[0], min(FREE_PRICE[1], price))
    base = base_price(stats)
    return max(math.ceil(base * OFFER_BAND[0]), min(math.floor(base * OFFER_BAND[1]), price))


# 白送的门槛：NPC 现做的东西、AI 决定给不给的东西（地图），好感够高才白送，不然模型第一次见面就把剑白送了。
# 任务奖励、按条件给的（打完哥布林给钥匙）不受这个限制
GIFT_AFFINITY = 50


def can_gift(affinity: int) -> bool:
    return affinity >= GIFT_AFFINITY


def effect_text(stats: dict) -> str:
    """给人看的效果说明：伤害 4 / 回 3 点血 / 有毒，掉 3 点血 / 能把人药倒"""
    parts = [f"伤害 {stats['damage']}" if stats.get("damage") else "",
             f"防御 {stats['defense']}" if stats.get("defense") else "",
             f"回 {stats['heal'] * (HEAL_PCT_ALCOHOL if stats.get('alcohol') else HEAL_PCT)}% 体力" if stats.get("heal") else "",
             f"有毒，掉 {stats['harm']} 点血" if stats.get("harm") else "",
             "能把人药倒" if stats.get("knockout") else ""]
    return "，".join(p for p in parts if p) or "没什么效果"


def create_limits(npc: Npc) -> dict[str, dict]:
    """NPC 能现造的种类和上限（world.yaml 的 creates）。老写法 [food, drink] 当成没有数值上限"""
    cfg = npc.template.props.get("creates", {})
    if isinstance(cfg, list):
        cfg = {k: {} for k in cfg}
    return {k: (v or {}) for k, v in cfg.items() if k in MADE_TEMPLATES}


def made_spec(npc: Npc, kind: str, name: str, description: str, heal: int = 0, harm: int = 0,
              damage: int = 0, knockout: Optional[str] = None, alcohol: bool = False) -> dict:
    """AI 提议的现造东西，按 NPC 的上限裁剪成规格。种类不允许就抛 ActionError"""
    caps = create_limits(npc).get(kind)
    if caps is None:
        raise ActionError(f"{npc.name}做不了这种东西")
    spec = {"kind": kind, "name": name.strip()[:12] or "小玩意", "description": description.strip()[:120]}
    clamp = lambda v, key: max(0, min(int(v or 0), caps.get(key, 0)))
    if kind in ("food", "drink"):
        spec["harm"] = clamp(harm, "harm")
        if knockout and caps.get("knockout"):
            spec["knockout"] = knockout.strip()[:20]
        # 有毒、下了药的就不回血
        spec["heal"] = 0 if spec["harm"] or spec.get("knockout") else clamp(heal, "heal")
        if alcohol and kind == "drink":
            spec["alcohol"] = True              # 酒：喝了可能醉
    elif kind == "weapon":
        spec["damage"] = max(1, clamp(damage, "damage"))
    return spec


def creatable_kinds(conn: Connection, player_id: UUID, npc: Npc) -> dict[str, dict]:
    """NPC 能给这个玩家现造的种类和上限。白送有冷却，但买卖（先报价）不受冷却限制，所以这里总是返回"""
    return create_limits(npc)


def _gift_cooldown(cur: Cursor, player_id: UUID, npc: Npc) -> None:
    cur.execute(
        f"""insert into player_npc_relations (player_id, npc_template, last_created_at) values (%s, %s, now())
            on conflict (player_id, npc_template) do update set last_created_at = now()
            where player_npc_relations.last_created_at is null
               or player_npc_relations.last_created_at < now() - interval '{CREATE_COOLDOWN}'
            returning 1""",
        (player_id, npc.template.id),
    )
    if not cur.fetchone():
        raise ActionError(f"{npc.name}刚白送过东西，过一会儿再说")


def _make(cur: Cursor, player_id: UUID, spec: dict) -> None:
    cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                (MADE_TEMPLATES[spec["kind"]], player_id, Jsonb({k: v for k, v in spec.items() if k != "kind"})))


def npc_gift(conn: Connection, player_id: UUID, npc: Npc, spec: dict) -> ActionResult:
    """NPC 高兴了现造一件白送（请客），不用报价；同一个玩家 3 分钟一次"""
    try:
        with conn.transaction():
            cur = _cursor(conn)
            player = load_player(cur, player_id, lock=True)
            _gift_cooldown(cur, player_id, npc)
            _make(cur, player_id, spec)
            _remember_goods(cur, npc, spec)
        return ActionResult(action="npc_create", success=True,
                            facts=[f"{npc.name}把{spec['name']}（{effect_text(spec)}）送给了{player.name}"])
    except ActionError as e:
        return ActionResult(action="npc_create", success=False, facts=[str(e)])


def quote_made(conn: Connection, player_id: UUID, npc: Npc, spec: dict, price: Optional[int] = None) -> ActionResult:
    """要收钱的现造东西先报价：AI 开的价按建议价限幅（没给就按建议价），连同规格记进报价，玩家同意了再照这个规格做"""
    price = clamp_price(spec, price or base_price(spec))
    with conn.transaction():
        cur = _cursor(conn)
        player = load_player(cur, player_id)
        _put_offer(cur, player_id, npc, "made:" + spec["name"], price, spec)
        _remember_goods(cur, npc, spec, price)
    return ActionResult(action="quote", success=True,
                        facts=[f"{npc.name}给{player.name}开价：{spec['name']}（{effect_text(spec)}），{price} 金币"])


def npc_buy_made(conn: Connection, player_id: UUID, npc: Npc, key: str) -> ActionResult:
    """玩家同意了现造东西的报价：按报价收钱，照报价时的规格做"""
    try:
        offer = get_offers(conn, player_id, npc).get(key)
        if not offer or not offer.get("spec"):
            raise ActionError(f"{npc.name}还没给这件东西报价")
        spec = offer["spec"]
        with conn.transaction():
            cur = _cursor(conn)
            player = load_player(cur, player_id, lock=True)
            patron = _pay(cur, player, offer["price"], npc)
            _make(cur, player_id, spec)
            _remember_goods(cur, npc, spec, offer["price"])
            cur.execute("update player_npc_relations set offers = offers - %s where player_id = %s and npc_template = %s",
                        (key, player_id, npc.template.id))
        return ActionResult(action="npc_create", success=True,
                            facts=[_deal(npc, player, f"{spec['name']}（{effect_text(spec)}）", offer["price"])] + patron)
    except ActionError as e:
        return ActionResult(action="npc_create", success=False, facts=[str(e)])


# NPC 做过的东西记下来（按名字），下次有人要同一样就照原来的规格做，不会这回有下回没有
GOODS_SHOWN = 20                        # 给 AI 看最近做过的几样


def _remember_goods(cur: Cursor, npc: Npc, spec: dict, price: Optional[int] = None) -> None:
    cur.execute(
        """insert into npc_goods (npc_template, name, spec, price) values (%s, %s, %s, %s)
           on conflict (npc_template, name) do update set spec = excluded.spec,
               price = coalesce(excluded.price, npc_goods.price), updated_at = now()""",
        (npc.template.id, spec["name"], Jsonb(spec), price))


def known_goods(conn: Connection, npc: Npc) -> list[dict]:
    """NPC 做过的东西 [{name, spec, price}]，最近的在前"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("select name, spec, price from npc_goods where npc_template = %s order by updated_at desc limit %s",
                    (npc.template.id, GOODS_SHOWN))
        return cur.fetchall()


def can_eject(npc: Npc) -> bool:
    """world.yaml 里配了 eject_to 的 NPC 能把闹事的玩家轰出去"""
    return bool(npc.template.props.get("eject_to"))


def npc_eject(conn: Connection, player_id: UUID, npc: Npc) -> Optional[ActionResult]:
    """把玩家从 NPC 所在房间的 eject_to 出口轰出去（叙事 AI 判定玩家闹事时调用）"""
    direction = npc.template.props.get("eject_to")
    with conn.transaction():
        cur = _cursor(conn)
        player = load_player(cur, player_id, lock=True)
        ex = _find_exit(cur, npc.room_id, direction) if direction else None
        if ex is None or player.room_id != npc.room_id:
            return None
        cur.execute("update players set room_id = %s, following = null, updated_at = now() where id = %s",
                    (ex["to_room"], player_id))
        room = load_room(cur, ex["to_room"])
        cur.execute("insert into events (room_id, player_id, kind, observer) values (%s, %s, 'ejected', %s)",
                    (ex["to_room"], player_id, f"{player.name}被{npc.name}从{load_room(cur, npc.room_id).name}轰了出来。"))
    return ActionResult(action="npc_eject", success=True, facts=[f"{npc.name}把{player.name}轰出了门，{player.name}来到了{room.name}"])


# ============ 好感度（对话步骤调用） ============

def get_affinity(conn: Connection, player_id: UUID, npc_id: UUID) -> int:
    with conn.transaction():
        cur = _cursor(conn)
        npcs = load_npcs(cur, "n.id = %s", (npc_id,))
        return _affinity(cur, load_player(cur, player_id), npcs[0]) if npcs else 0


NPC_MEMORY_LIMIT = 300                  # NPC 对每个玩家的长期记忆总结上限（字）
NPC_LOG_RECENT = 10                     # 给 AI 看的最近几条完整来往记录（全部记录都留在 npc_memory_log）
NPC_LOG_LIMIT = 400                     # 每条记录上限（字）
NPC_LOG_RELATED = 5                     # 再从更早的记录里按玩家这句话翻出几条相关的
NPC_SUMMARY_WINDOW = 40                 # 后台整理摘要时看最近几条完整记录
# 翻旧账时不算数的字：常见虚词
MEMORY_STOP = set("我你他她它的了吗呢吧啊呀是在有和就都也还要去来这那个一不没么什怎给把被说对")


def ago(when: datetime) -> str:
    """给 AI 看的相对时间："刚才""20 分钟前""昨天"。具体钟点它用不上（不知道现在几点，而且是 UTC）"""
    minutes = (datetime.now(timezone.utc) - when).total_seconds() / 60
    if minutes < 3:
        return "刚才"
    if minutes < 60:
        return f"{int(minutes)} 分钟前"
    if minutes < 24 * 60:
        return f"{int(minutes // 60)} 小时前"
    days = int(minutes // (24 * 60))
    return "昨天" if days == 1 else f"{days} 天前" if days < 14 else f"{days // 7} 周前"


NPC_REPLY_SHOWN = 12                    # 给叙事看的记录里，NPC 自己以前的回话只留开头这么多字，免得它整句照抄


def clip_npc_said(entry: str, name: str) -> str:
    """房间动态里 NPC 对别人说的话（"莉娜：“……”"）去掉原话：留着开头模型也会照抄给下一个人"""
    return re.sub(rf"{re.escape(name)}：“[^”]*”", f"{name}回了几句（原话略）", entry)


def _clip_replies(entry: str) -> str:
    """NPC 自己说过的话只留个开头（"你回：“哟，杂鱼，这就……”"），玩家说的话完整留着。完整记录在库里不动"""
    return re.sub(r"你回：“([^”]{%d})[^”]+”" % NPC_REPLY_SHOWN, r"你回：“\1……”", entry)


def _bigrams(text: str, skip: set[str]) -> set[str]:
    """中文按两个字一组切（没有分词器，够用来找相关的旧记录）；含虚词的组不要"""
    chars = [c for c in text if "\u4e00" <= c <= "\u9fff" or c.isalnum()]
    return {a + b for a, b in zip(chars, chars[1:]) if a not in MEMORY_STOP and b not in MEMORY_STOP} - skip


def get_npc_memory(conn: Connection, player_id: UUID, npc_id: UUID, query: str = "") -> str:
    """NPC 对这个玩家记得什么：长期记忆总结 + 最近几条完整来往 + 按玩家这句话（query）从更早的记录里翻出的相关几条。
    按 NPC 模板记，NPC 复活、重新 seed 都不丢"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute(
            """select r.memory from player_npc_relations r join npcs n on n.template_id = r.npc_template
               where r.player_id = %s and n.id = %s""",
            (player_id, npc_id),
        )
        row = cur.fetchone()
        cur.execute(
            """select * from (
                 select l.id, l.entry, l.created_at from npc_memory_log l join npcs n on n.template_id = l.npc_template
                 where l.player_id = %s and n.id = %s order by l.id desc limit %s) t order by id""",
            (player_id, npc_id, NPC_LOG_RECENT),
        )
        recent = cur.fetchall()
        log = [f"[{ago(r['created_at'])}] {_clip_replies(r['entry'])}" for r in recent]
        # 翻旧账：更早的记录里跟这句话重合最多的几条（两人的名字每条都有，不算）
        related = []
        if query and recent:
            cur.execute("select p.name as player, t.name as npc from players p, npcs n join npc_templates t on t.id = n.template_id"
                        " where p.id = %s and n.id = %s", (player_id, npc_id))
            names = cur.fetchone()
            skip = (_bigrams(names["player"], set()) | _bigrams(names["npc"], set())) if names else set()
            words = _bigrams(query, skip)
            cur.execute(
                """select l.entry, l.created_at from npc_memory_log l join npcs n on n.template_id = l.npc_template
                   where l.player_id = %s and n.id = %s and l.id < %s order by l.id""",
                (player_id, npc_id, recent[0]["id"]))
            scored = [(len(words & _bigrams(r["entry"], skip)), r) for r in cur.fetchall()] if words else []
            related = [f"[{ago(r['created_at'])}] {_clip_replies(r['entry'])}"
                       for n, r in sorted(scored, key=lambda x: -x[0])[:NPC_LOG_RELATED] if n >= 2]
    summary = row["memory"] if row and row["memory"] else ""
    if not log:
        return summary
    return ((summary or "（还没有总结）")
            + ("\n更早的、跟他这次说的有关的来往：\n" + "\n".join(related) if related else "")
            + "\n最近的来往（从早到晚，原话记录）：\n" + "\n".join(log))


def memory_material(conn: Connection, player_id: UUID, npc_template: str) -> tuple[str, list[str]]:
    """后台整理摘要用：(旧摘要, 最近 NPC_SUMMARY_WINDOW 条完整记录，从早到晚)"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("select memory from player_npc_relations where player_id = %s and npc_template = %s",
                    (player_id, npc_template))
        row = cur.fetchone()
        cur.execute(
            """select * from (select id, entry, created_at from npc_memory_log where player_id = %s and npc_template = %s
                              order by id desc limit %s) t order by id""",
            (player_id, npc_template, NPC_SUMMARY_WINDOW))
        return (row["memory"] if row else ""), [f"[{ago(r['created_at'])}] {r['entry']}" for r in cur.fetchall()]


def add_npc_log(conn: Connection, player_id: UUID, npc: Npc, entry: str) -> None:
    """记一条完整的来往：他说了什么、NPC 回了什么、结果如何。全部留着，不删"""
    with conn.transaction():
        conn.execute("insert into npc_memory_log (player_id, npc_template, entry) values (%s, %s, %s)",
                     (player_id, npc.template.id, entry.strip()[:NPC_LOG_LIMIT]))


def set_npc_memory(conn: Connection, player_id: UUID, npc_template: str, memory: str) -> None:
    """更新 NPC 对这个玩家的记忆摘要（后台整理好的）。只是 NPC 的印象，不是游戏状态，这里只截断长度"""
    memory = memory.strip()[:NPC_MEMORY_LIMIT]
    with conn.transaction():
        conn.execute(
            """insert into player_npc_relations (player_id, npc_template, memory) values (%s, %s, %s)
               on conflict (player_id, npc_template) do update set memory = excluded.memory""",
            (player_id, npc_template, memory),
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
