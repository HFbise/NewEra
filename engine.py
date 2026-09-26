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
from collections import defaultdict
from types import SimpleNamespace
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import Connection, Cursor
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import dungeon
from rules import *                 # noqa: F403  纯规则（命中、减伤、价钱、升级、怪物数值）见 rules.py
from commands import REST_TALK_RE
from schema import (
    ActionResult, Attack, Drop, Equip, Feature, Follow, Freeform, Give, ItemInstance, LeaveParty, Kick, Look,
    Move, Npc, OtherPlayer, Player, PlayerAction, Reject, Revive, Room, RoomExit, RoomView, Say, Status, Struggle,
    Stunt, Take, Talk, Unfollow, Unequip, Use, dir_name, Dispenser, SLOT_CHOICES, SLOT_NAMES, Dodge, Hide, Maneuver, Search, Stealth, Tame,
    Uncurse, Reload, Refill, Transfer, Reroll, Rename, Write, Socket, Unsocket, Refine, Donate, TakeDonated, Dismantle, CloseEyes, Pinch,
    AcceptDuel, Challenge, DeclineDuel, Duel, Flee, SKILL_NAMES, Upgrade, Respawn, Stand, Rest, Pay, Camp, Teleport, Sell,
    Effect,
)


START_ROOM = "square"                   # 新角色出生、后台传送回去的地方
ONLINE_WINDOW = "90 seconds"            # 超过这么久没有心跳的玩家算睡着。后台标签页浏览器会降低定时器频率，给宽一点
AFFINITY_STEP = 5                       # 对话 AI 每次最多调整的好感度
AFFINITY_RANGE = (-100, 100)
PARTY_MAX = 5                           # 一支队伍最多几个人
DOWNED_ALLOWED = {"look", "say", "respawn"}   # 倒下的人只能看、说话（喊人来救），或者选择被抬回酒馆
RESPAWN_ROOM = "tavern"                 # 倒下的人选择复活时默认被抬去的地方

# 创意攻击（stunt）和负面状态。AI 只选档位和难度，数字都在这里
TIERS = ["none", "light", "heavy", "lethal"]
# 伤害在区间里随机：自由动作是控场用的，新手拿它打伤害不如老老实实砍一刀
TIER_RANGE = {"none": (0, 0), "light": (10, 30), "heavy": (30, 60), "lethal": (50, 90)}      # 战斗数值 ×10（rules.SCALE）
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
DETECT_PER_FOE = 0.02                   # 一屋子怪比一只难溜过去：每多一只，每条消息被发现的几率多涨这么多
DETECT_STEP = 0.10
DETECT_STEP_MIN = 0.02                  # 隐匿每级让每次上涨少 1%，最少涨这么多

# 同一区域里的距离（格）：刚发现时隔 START_DISTANCE 格。近战（普通攻击，玩家打敌人、敌人打玩家）按距离算命中，
# 够不着的格数命中率是 0。扔东西、推石头这类 stunt 不看距离。
# 玩家挪几格、把敌人踹开几格由 AI 判（每次最多 MAX_STEP）；敌人被发现后每条消息自己逼近 ENEMY_STEP 格再出手
START_DISTANCE = 2
MAX_DISTANCE = 4
MAX_STEP = 2
ENEMY_STEP = 1
DODGE_BONUS = 0.15                      # 闪避动作让敌人这一下的命中率降低这么多，察觉每级再多降 DODGE_PER_LEVEL
DODGE_PER_LEVEL = 0.05
DISTANCE_WORDS = {0: "贴身", 1: "一步之遥", 2: "几步开外"}
# 玩家每做这么多个动作，敌人行动一次（一句话结尾不够数也算一次）。敌人有动静（发现、逼近、出手）就打断后面的，
# 免得一条超长的消息一口气打出一串伤害；被绊倒的敌人轮到时只是爬起来，不打断

# 徒手一击毙命、直接打晕：只有敌人没发现你时能偷袭，按隐匿判，难度至少这么高；对方有防备就不可能
ASSASSINATE_DIFFICULTY = 4


def assassinate_difficulty(depth: int) -> int:
    """偷袭（暗杀）的难度下限：4 起，每 6 层 +1（连续涨），深层的怪不再跟第 1 层一样好秒"""
    return ASSASSINATE_DIFFICULTY + round(steps(depth, 6))
KEEN_EXTRA = 2                          # 对警觉的怪偷袭暗杀难度 +2（它们一见面就发现人，见 _keen_spotted）
PRONE_DECISIVE_DIFFICULTY = 3           # 对刚被绊倒在地的下狠手（打晕、断手、一击毙命）难度至少这么高

# 技能判定、升级要攒几次熟练：见 rules.SKILL_*
# 耐性：每升一级 HP 上限 +ENDURANCE_HP。除了硬扛的判定，挨打（活下来的）、扛住没喝醉有 ENDURE_HIT_CHANCE 的几率涨熟练，
# 吃了有毒的东西活下来一定涨（ENDURANCE_HP、ENDURE_HIT_CHANCE 在 rules）；耐性每级让喝醉的几率乘 DRUNK_RESIST
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
              "按回合打，两个人都出完手才一起结算；"
              "装备的特效在决斗里也生效（毒、流血、吸血、破甲、加伤害这些），只对怪有用的（专打亡灵、野兽的）不算；"
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


SERVICE_NAMES = {"free_upgrade": "锻造纹（照着敲，能把手上的兵器再打磨一番）", "mirror_duel": "自己的倒影",
                 "treasure_pool": "一件宝物", "timed": "一袋古币（沙子流完之前拿了走人）",
                 "gem": "一颗宝石", "wish": "一个愿望（投钱许愿，要安静）", "cleanse": "忏悔（清掉身上的晦气）",
                 "reveal_next_floor": "碑文（下一层的路）", "buff": "一个祝福", "shortcut": "出去的路",
                 "memory": "一段往事", "clear_fog": "灯室（点亮灯塔能散雾）", "player_note": "一张纸条"}


def load_dispensers(cur: Cursor, room: Room, player_id: UUID) -> list[Dispenser]:
    """房间里的取用处（武器桶、摆着护符的桌子），id 按房间和 key 算，每次都一样。
    available 按这个玩家算：身上已经有了（或有 unless 里的东西）就拿不了，只显示桶、桌子本身"""
    cfg = room.props.get("dispensers", {})
    if not cfg:
        return []
    cur.execute("select id, name from item_templates where id = any(%s)", ([d["item"] for d in cfg.values() if d.get("item")],))
    names = {r["id"]: r["name"] for r in cur.fetchall()}
    cur.execute("select distinct template_id from item_instances where player_id = %s", (player_id,))
    owned = {r["template_id"] for r in cur.fetchall()}
    cur.execute("select key from dispenser_log where player_id = %s and room_id = %s", (player_id, room.id))
    taken = {r["key"] for r in cur.fetchall()}
    cur.execute("""select d.id, d.container, coalesce(d.props->>'name', t.name) as name from donations d
                   join item_templates t on t.id = d.template_id where d.room_id = %s order by d.created_at""", (room.id,))
    donated = defaultdict(list)
    for r in cur.fetchall():
        donated[r["container"]].append({"id": str(r["id"]), "name": r["name"]})
    return [Dispenser(id=uuid5(NAMESPACE_URL, f"newera:dispenser:{room.id}:{key}"), room=room.id, key=key,
                      container=d["name"], description=d.get("description", ""), item=d.get("item", ""),
                      item_name=names.get(d.get("item"), "") or SERVICE_NAMES.get(d.get("service") or d.get("item"), ""),
                      unless=d.get("unless", []), where=d.get("where", "里"),
                      service=d.get("service"), bonus_gem=d.get("bonus_gem", 0.0),
                      once=d.get("once", False), repeat=d.get("repeat", False), skill=d.get("skill"),
                      difficulty=d.get("difficulty", 0), fail=d.get("fail", ""), fail_damage=d.get("fail_damage", 0),
                      donate=d.get("donate", []), donated=donated.get(key, []), extra=d,
                      available=(d.get("repeat") or not owned & {d.get("item"), *d.get("unless", [])})
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


def drop_sleepers(conn: Connection) -> None:
    """睡着（下线、挂机）的人自动离队：不再跟着谁，跟着他的人也不跟了，队伍只剩一个人就散。
    没有后台任务，谁拉状态就顺手清一次（人少，一条 update 很便宜）"""
    with conn.transaction():
        gone = [r[0] for r in conn.execute(f"""
            update players set party_id = null, following = null
            where (party_id is not null or following is not null)
              and (last_active_at is null or last_active_at < now() - interval '{ONLINE_WINDOW}')
            returning id""").fetchall()]
        if gone:
            conn.execute("update players set following = null where following = any(%s)", (gone,))
        conn.execute("""update players p set party_id = null where party_id is not null
                        and (select count(*) from players q where q.party_id = p.party_id) = 1""")


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
                        effects = coalesce((select jsonb_agg(e) from jsonb_array_elements(effects) e where e->>'kind' in ('whet', 'cheer')), '[]'::jsonb), updated_at = now() where room_id = %s and hp <= 0
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
def _effects(item: ItemInstance) -> list[dict]:
    """这件装备的效果：自己的，加上镶在上面的宝石的（props.gem_fx）"""
    return (_prop(item, "effects") or []) + (item.props.get("gem_fx") or [])


def _gem_total(items: list[ItemInstance], do: str) -> float:
    """宝石加的数值合计（防御、血量上限），全身有上限（loot.yaml gems.body_caps），免得件件都镶又叠爆"""
    total = sum(e.get("value", 0) for i in items for e in i.props.get("gem_fx") or []
                if e.get("when") == "passive" and e.get("do") == do)
    return min(total, dungeon.gem_rules()["body_caps"].get(do, total))


def _fx(items: list[ItemInstance], do: str, when: str = "passive", kind: Optional[str] = None) -> list[dict]:
    return [e for i in items for e in _effects(i)
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


def _fire(cur: Cursor, player: Player, when: str, do: Optional[str] = None, npc: Optional[Npc] = None,
          roll: bool = True) -> list[dict]:
    """这一刻（when）身上装备触发了哪些效果：对得上 vs 标签、满足 if 条件、掷中 chance 的（roll=False 不掷，只看条件）"""
    out = []
    for e in (e for i in _worn(cur, player) for e in _effects(i)
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
        if roll and e.get("chance", 1) < 1 and not _roll(e["chance"]):
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


def _npc_effect(npc: Npc, kind: str) -> Optional[Effect]:
    return next((e for e in npc.effects if e.kind == kind), None)


def _save_npc_effects(cur: Cursor, npc: Npc) -> None:
    cur.execute("update npcs set effects = %s where id = %s", (Jsonb([e.model_dump() for e in npc.effects]), npc.id))


def _npc_affect(cur: Cursor, player: Player, npc: Npc, kind: str, label: str) -> list[str]:
    """给怪上一个状态（装备触发的）"""
    if not npc.alive or npc.hp is None or npc.hp <= 0:
        return []
    if kind in (npc.template.props.get("immune") or []):
        return [f"{npc.name}不吃这一套（免疫{EFFECT_NAMES.get(kind, STATE_NAMES.get(kind, kind))}）"]
    if kind == "wrapped":
        kind = "restrained"
    if kind == "silence":
        _tally_merge(cur, npc, {"muted": 2})
        return [f"{npc.name}{label or '被捂住了嘴'}（接下来两次出手放不了招）"]
    if kind in ("stun", "restrained", "prone"):
        if npc.status or _boss_resists(cur, npc):
            return []
        st = Status(kind="incapacitated" if kind == "stun" else kind, label=(label or "动弹不得")[:20], escape=2,
                    since=datetime.now(timezone.utc).isoformat())
        _set_status(cur, "npcs", npc.id, st)
        npc.status = st
        return [f"{npc.name}{label}" if label else f"{npc.name}{st.describe()}"]
    depth = npc.template.props.get("dungeon", {}).get("depth", 1)
    value = NPC_CORRODE_DEF if kind == "corrode" else round((1 + steps(depth, 5)) * SCALE)
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
                v = math.ceil(e.value * WEAK_MULT) if npc.template.props.get("weak") == e.kind else e.value     # 典狱长怕毒
                hurt, dead = _hurt_npc(cur, player, npc, v)
                npc.hp = max(0, npc.hp - v)
                facts += [f"{npc.name}{EFFECT_NAMES[e.kind]}，掉了 {v} 点血"] + hurt
                if dead:
                    npc.alive = False
                    break
            e.left -= 1
            keep.append(e)
        if npc.alive:
            npc.effects = keep
            _save_npc_effects(cur, npc)
    return facts


def _assassinated(cur: Cursor, player: Player, view: RoomView, npc: Npc, dead: bool, unseen: bool) -> list[str]:
    """敌人还没发现你就被解决了（暗杀）：隐匿熟练 +1，一条消息最多一次"""
    if not (dead and unseen and npc.template.hostile) or "stealth" in view.trained:
        return []
    view.trained.append("stealth")
    return [f"{npc.name}到死都没发现{player.name}"] + _gain_skill(cur, player, "stealth")


def _pvp_hit_extras(cur: Cursor, player: Player, target: Player, dmg: int, down: bool, fired: list[dict]) -> list[str]:
    """决斗里打中对手以后装备触发的：上状态（对手的装备抗性照算）、吸血；打倒了就是杀敌效果。
    打群体、连锁这种对怪的效果决斗里没有"""
    facts = []
    if down:
        for e in _fire(cur, player, "kill"):
            if e["do"] == "heal":
                facts += _labels([e]) + _heal_player(cur, player, int(e.get("value", 1)))
        return facts
    depth = dungeon.parse_room(player.room_id)[1] if dungeon.is_dungeon(player.room_id) else 1
    for e in fired:
        if e["do"] == "status" and (resist := gear_resist(cur, target, e["kind"])) > 0 and (resist >= 1 or _roll(resist)):
            facts += _inflict(cur, target, e["kind"], e.get("label", ""), depth, player.name)
    if leech := max([0] + [e.get("value", 0) for e in fired if e["do"] == "leech"]):
        facts += _heal_player(cur, player, math.ceil(dmg * leech))
    return facts


def _pvp_self_damage(cur: Cursor, player: Player, swung: list[dict]) -> list[str]:
    """决斗里挥出去就触发的：握柄的刃咬手（打怪时的横扫到别的敌人，决斗里没有）"""
    facts = []
    for e in swung:
        if e["do"] == "self_damage":
            hurt, _ = _hurt_player(cur, player, int(e.get("value", 1)), "other", "手里的凶器")
            facts += _labels([e]) + [f"{player.name}自己掉了 {e.get('value', 1)} 点血"] + hurt
    return facts


def _attack_extras(cur: Cursor, player: Player, npc: Npc, fired: list[dict]) -> list[str]:
    """普通攻击出手时（打没打中都算）触发的：甩开铁链扫到别的敌人、握柄的刃咬手"""
    facts = []
    for e in fired:
        if e["do"] == "splash":
            for other in [n for n in _enemies(cur, player.room_id) if n.id != npc.id]:
                v = int(e.get("value", 1)) * (2 if other.template.props.get("swarm") else 1)     # 虫群怕横扫
                hurt, _ = _hurt_npc(cur, player, other, v)
                facts += [f"{other.name}被扫到，受到 {v} 点伤害"] + hurt
            facts += _noise(cur, player, "aoe")
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
        elif e["do"] == "knockback":
            st = _stealth(player)
            d = _set_distance(st, npc, _distance(st, npc) + 1)
            _save_stealth(cur, player, st)
            facts += [f"{npc.name}被打得往后退了一步（离{player.name} {distance_word(d)}）"]
        elif e["do"] == "chain":
            others = [n for n in _enemies(cur, player.room_id) if n.id != npc.id]
            if others:
                other = random.choice(others)
                hurt, _ = _hurt_npc(cur, player, other, int(e.get("value", 1)))
                facts += _labels([e]) + [f"{other.name}受到 {e.get('value', 1)} 点伤害"] + hurt
    if leech := max([0] + [e.get("value", 0) for e in fired if e["do"] == "leech"]):      # 吸血取最好的一件
        facts += _heal_player(cur, player, math.ceil(dmg * leech))
    return facts


def _heal_scale(player: Player, amount: int) -> int:
    """重伤（wound）：受到的治疗 × value%（药草、绷带解不了）"""
    w = next((e for e in player.effects if e.kind == "wound"), None)
    return round(amount * w.value / 100) if w else amount


def _heal_player(cur: Cursor, player: Player, amount: int) -> list[str]:
    amount = _heal_scale(player, amount)
    hp = min(player.max_hp, player.hp + amount)
    if hp == player.hp:
        return []
    cur.execute("update players set hp = %s where id = %s", (hp, player.id))
    gained, player.hp = hp - player.hp, hp
    return [f"{player.name}回了 {gained} 点血，当前 HP {hp}/{player.max_hp}"]


def _sync_gear_hp(cur: Cursor, player_id: UUID) -> list[str]:
    """装备带的血量上限（活力之戒 +3、贪婪之戒 -3）：跟 players.gear_hp 比，差多少就改多少，当前血量不超过上限"""
    player = load_player(cur, player_id, lock=True)
    worn = _worn(cur, player)
    bonus = round(sum(e.get("value", 0) for e in _fx(worn, "max_hp") if not e.get("gem")) + _gem_total(worn, "max_hp"))
    cur.execute("select gear_hp from players where id = %s", (player_id,))
    had = cur.fetchone()["gear_hp"]
    if bonus == had:
        return []
    cur.execute("""update players set max_hp = greatest(1, max_hp + %s), gear_hp = %s,
                   hp = least(hp, greatest(1, max_hp + %s)) where id = %s returning max_hp""",
                (bonus - had, bonus, bonus - had, player_id))
    return [f"{player.name}的血量上限{'+' if bonus > had else ''}{bonus - had}（身上的装备），现在上限 {cur.fetchone()['max_hp']}"]


# ============ 伤害怎么被防御挡掉：见 rules.hurt_player_by（玩家挨打，按比例）、rules.hurt_npc_by（怪挨打，减法） ============

def _defense(cur: Cursor, player: Player) -> int:
    """基础防御加上所有装备的防御（护甲、护符、以后的盾）"""
    corrode = _effect(player, "corrode")
    worn = _worn(cur, player)
    return max(0, player.defense + sum(i.defense for i in worn) + _gem_total(worn, "defense") + _aura(cur, player, "defense")
               - (corrode.value if corrode else 0))




def gear_totals(attack: int, defense: int, equipped: list[ItemInstance]) -> tuple[int, int]:
    """算上装备的攻、防（侧栏、后台看的）：双持时副手武器按 OFFHAND_SHARE，所有装备的防御相加"""
    weapons = [i for i in equipped if i.equipped_slot in ("left_hand", "right_hand") and i.template.type == "weapon"
               and not _prop(i, "ranged") and not _prop(i, "loads")]            # 远程武器射的时候单算
    worn = [i for i in equipped if i.equipped_slot]
    return attack + weapon_damage(weapons), defense + sum(i.defense for i in worn) + _gem_total(worn, "defense")


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
    # 楼梯间的守卫（精英、头目）活着就下不去：得打倒它，或者把它定住、捆住趁机溜下去
    if a.direction == "down" and dungeon.is_dungeon(player.room_id) and (guards := [
            n.name for n in _enemies(cur, player.room_id)
            if n.template.props.get("dungeon", {}).get("rank") in ("elite", "boss")
            and (n.status is None or n.status.kind == "prone")]):
        raise ActionError(f"{'、'.join(guards)}守在楼梯口，不打倒它（或者把它定住、捆住）下不去")
    if a.direction == "down" and (gate := next((n for n in _enemies(cur, player.room_id) if n.template.props.get("gate")), None)) \
            and _deepest(cur, player) <= dungeon.parse_room(player.room_id)[1] \
            and str(dungeon.parse_room(player.room_id)[1]) not in (player.flags.get("gates") or {}):
        raise ActionError(f"{gate.name}守着这一关：不打倒它，谁也下不去")
    if _room_env(cur, player.room_id).get("sealed") and _enemies(cur, player.room_id):
        raise ActionError("门被封死了，打完之前谁也出不去")
    # 被敌人发现、正在交手：得先逃跑成功（甩开了就不算被发现）才能离开
    if _stealth(player).detected and (foes := [n.name for n in _enemies(cur, player.room_id) if n.status is None]):
        raise ActionError(f"{'、'.join(foes)}正缠着{player.name}，得先逃跑成功才能离开")
    if ex["locked"]:
        # 身上带着对应的钥匙就顺手打开，不用玩家专门说"用钥匙开门"
        keys = load_items(cur, "i.player_id = %s and i.template_id = %s", (player.id, ex["key_item"])) \
            if ex["key_item"] else []
        if not keys:
            raise ActionError(f"往{dir_name(a.direction)}的门锁着" + ("（要典狱长的大钥匙）" if ex["key_item"] == "warden_key" else ""))
        facts += _unlock(cur, player, keys[0], a.direction)
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
    spotted = _keen_spotted(cur, to)
    cur.execute("update players set room_id = %s, following = null, stealth = %s, updated_at = now() where id = %s",
                (to, Jsonb(Stealth(room=to, chance=DETECT_START, detected=bool(spotted), alerted=bool(spotted),
                                   start=_start_distance(cur, to)).model_dump()), player.id))
    room = load_room(cur, to)
    if room.props.get("gate_stone"):
        d = dungeon.parse_room(to)[1]
        cur.execute("""update players set waypoints = array_append(waypoints, %s)
                       where id = %s and not (%s = any(waypoints)) returning 1""", (d, player.id, d))
        if cur.fetchone():
            facts.append(f"前厅里的传送石亮了起来：以后在地窖里说「传送到第 {d} 层」就能直接到这里，倒下了也能马上回来重来")
    if (fog := _fog(cur, to)) and (foes := _enemies(cur, to)):
        # 浓雾：进门过一次察觉，过了就先听见怪在哪
        ok, _ = _check(cur, player, view, "perception", int(fog.get("perception_reveal", 2)) + dungeon.parse_room(to)[1] // 6)
        facts.append(f"雾里传来{'、'.join(n.name for n in foes)}的动静，{player.name}听出了它们在哪" if ok
                     else f"浓雾把四周裹得严严实实，{player.name}什么都看不清，只觉得雾里有东西")
    dungeon.mark_seen(cur, to)
    facts.append(f"{player.name}往{dir_name(a.direction)}走，来到了{room.name}")
    stride = _stride(cur, player, view, "walk", 1)          # 走路也练运动（攒满了这条消息末尾报熟练）
    if heal := sum(int(e.get("value", 0)) for e in _fire(cur, player, "enter", "heal")):
        facts += _heal_player(cur, player, heal)
    facts += arrived + (_torch_floor(cur, [player.name], to) if arrived else [])
    if arrived and _room_env(cur, to).get("heat"):
        facts.append(HEAT_HINT)
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
    if (not arrived and dungeon.is_dungeon(player.room_id) and dungeon.is_dungeon(to)
            and dungeon.CELL_INDEX not in (dungeon._cell_of(player.room_id), dungeon._cell_of(to))      # 进出牢房只是一扇门
            and _roll(dungeon.ROAD_EVENT_CHANCE)):
        facts += _road_event(cur, player, view, to)
    if names:
        facts.append(f"{'、'.join(names)}跟着{player.name}一起来到了{room.name}")
        if spotted:
            cur.execute("update players set stealth = %s where name = any(%s)",
                        (Jsonb(Stealth(room=to, chance=DETECT_START, detected=True, alerted=True).model_dump()), names))
        if arrived:
            facts += dungeon.arrived(cur, names, dungeon.parse_room(to)[1]) + _torch_floor(cur, names, to)
    if spotted:
        facts.append(f"{'、'.join(spotted)}一下子就察觉到了来人")
    for npc in load_npcs(cur, "n.room_id = %s and n.alive", (to,)):
        facts += _upgrade_nudge(cur, player, npc)       # 走进铁匠铺：莉娜看不下去没升过的武器
        facts += _bow_nudge(cur, player, npc)           # 拿着弓进酒馆：麦琪提一句找人挡在前面
        facts += _armor_nudge(cur, player, npc) + _gem_nudge(cur, player, npc) + _potion_nudge(cur, player, npc)
        facts += _news(cur, player, npc)                # 上线后第一次进酒馆：麦琪八卦最近的更新
    if room.props.get("rest"):
        facts.append(REST_TEXT)
    return facts + _beast_hint(cur, to) + stride


def _keen_spotted(cur: Cursor, room_id: str) -> list[str]:
    """房间里警觉的怪（狼、恶犬、石像鬼、所有头目）：一进门就发现人，没有偷偷摸过去这回事"""
    return [n.name for n in _enemies(cur, room_id) if n.template.props.get("keen") and n.status is None]


# 走路随机事件的累计几率：陷阱、零钱、路边小木匣，剩下是怪声（循声去找就会碰上游荡的怪）
ROAD_TRAP, ROAD_COINS, ROAD_CHEST = 0.45, 0.65, 0.8
ROAD_CHEST_ITEMS = ["herb", "bread", "torch", "rope"]     # 路边小木匣里的东西：普通补给，不给好东西


TRAP_EASE = "_trap_ease"                 # players.flags：这一层踩中过几次陷阱 {"floor": "run:层", "n": 次数}
TRAP_LEARN = 0.25                       # 踩中陷阱涨察觉熟练的几率


def _road_event(cur: Cursor, player: Player, view: RoomView, to: str) -> list[str]:
    """地牢里走路时碰上的事。陷阱：察觉发现就绕过去，没发现挨一下（活下来耐性一定涨）。
    怪声：在房间上记一笔，有人循声去找（搜索）就引出一只游荡的怪"""
    f = dungeon.floor_info(cur, to)
    depth, r = f["depth"], random.random()
    if r < ROAD_TRAP:
        trap = random.choice(dungeon.data()["themes"].get(f["theme"], {}).get("traps") or dungeon.data()["traps"])
        sense = _carries(cur, player, "trap_sense")          # 麦琪的陷阱图：难度降几级
        here = ":".join(map(str, dungeon.parse_room(to)))
        ease = player.flags.get(TRAP_EASE, {})
        ease = ease.get("n", 0) if ease.get("floor") == here else 0
        diff = 2 + round(steps(depth, 6)) - (_prop(sense, "trap_sense") if sense else 0) - ease
        ok, rolled = _check(cur, player, view, "perception", max(1, diff))
        # 同一层每踩中一次，下一个陷阱难度 -1（吃过亏就留神了），躲开一次就恢复
        cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::jsonb) where id = %s",
                    (TRAP_EASE, Jsonb({"floor": here, "n": 0 if ok else ease + 1}), player.id))
        dmg = 0 if ok else round((2 + steps(depth, 3)) * SCALE)
        cur.execute("insert into trap_log (player_id, depth, difficulty, avoided, dmg) values (%s, %s, %s, %s, %s)",
                    (player.id, depth, max(1, diff), ok, dmg))
        if ok:
            return [f"路上{trap}"] + rolled + [f"{player.name}{'想起陷阱图上画过这种地方，' if sense else ''}及时察觉，躲了过去"]
        hurt, down = _hurt_player(cur, player, dmg, "other", "陷阱")
        # 踩中了也长记性：几率涨一点察觉熟练（不然察觉 0 级的人永远判不成，永远练不上去）
        learn = _gain_skill(cur, player, "perception") if not down and "perception" not in view.trained and _roll(TRAP_LEARN) else []
        if learn:
            view.trained.append("perception")
        return [f"路上{trap}"] + rolled + [f"{player.name}没能躲开，受到 {dmg} 点伤害"] + hurt \
            + ([] if down else _toughen(cur, player)) + learn
    if r < ROAD_COINS:
        coins = max(1, round(random.randint(1, 3) * gold_scale(depth)))
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
        cur.execute("select deepest_floor from players where id = %s", (player.id,))
        if a.floor and dungeon.gate_depth(a.floor) and cur.fetchone()["deepest_floor"] >= a.floor:
            points = list(points) + [a.floor]           # 关卡层的前厅：到过这么深的人都能回去挑战
        if a.floor is None or a.floor not in points:
            raise ActionError((f"{player.name}还没到过第 {a.floor} 层的传送石。" if a.floor else "")
                              + dungeon.waypoints_text(points))
        to, facts = dungeon.teleport_to(cur, player, a.floor, ONLINE_WINDOW)
        facts = [f"{player.name}站在漆黑的入口前默念第 {a.floor} 层，一阵银光闪过"] + facts
    else:
        raise ActionError("只有在地牢的传送石边上、或者地窖里才能传送")
    cur.execute("update players set room_id = %s, following = null, stealth = null, updated_at = now() where id = %s",
                (to, player.id))
    dungeon.mark_seen(cur, to)
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


# 扎营：回多少见 rules.CAMP_*


REST_TEXT = "门后传来沉重的呼吸声。这里是头目门前最后能喘口气的地方：在这儿能多扎一次营，不算这一层的扎营次数"


def do_camp(cur: Cursor, player: Player, view: RoomView, a: Camp) -> list[str]:
    """地牢里清完怪的房间扎营，每层每人一次"""
    if not dungeon.is_dungeon(player.room_id):
        raise ActionError("只有在远古地牢里才用得着扎营，村里可以去酒馆住店")
    if foes := [n.name for n in _enemies(cur, player.room_id)]:
        raise ActionError(f"{'、'.join(foes)}还在这里，没法扎营")
    run, depth = dungeon.parse_room(player.room_id)
    rest = False
    if view.room.props.get("rest"):
        # 头目门前的休息点：每人一次，记在楼梯间的房间上，不占这一层的扎营
        cur.execute("""update rooms r set props = jsonb_set(r.props, '{rested}', coalesce(r.props->'rested', '[]'::jsonb) || to_jsonb(%s::text))
                       from dungeon_floors f where f.run_id = %s and f.depth = %s and r.id = f.stairs_room
                         and not coalesce(r.props->'rested', '[]'::jsonb) ? %s returning 1""",
                    (str(player.id), run, depth, str(player.id)))
        rest = cur.fetchone() is not None
    if not rest:
        cur.execute("""update dungeon_floors set camped = array_append(camped, %s)
                       where run_id = %s and depth = %s and not (%s = any(camped)) returning 1""",
                    (player.id, run, depth, player.id))
        if not cur.fetchone():
            raise ActionError(f"{player.name}在这一层已经扎过营了，再歇也缓不过来，得往下走")
    tent = next((i for i in view.inventory if _prop(i, "camp")), None)
    facts = [f"{player.name}在{view.room.name}扎营休息" + (f"，搭起了{tent.name}" if tent else "")
             + ("（头目门前的休息点，不算这一层的扎营）" if rest else "")]
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
        cur.execute("update players set max_hp = %s, effects = coalesce((select jsonb_agg(e) from jsonb_array_elements(effects) e where e->>'kind' in ('whet', 'cheer')), '[]'::jsonb) where id = %s", (player.max_hp, player.id))
    hp = min(player.max_hp, player.hp + round(player.max_hp * share))
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (hp, player.id))
    return facts + [f"{player.name}恢复了 {hp - player.hp} 点 HP，当前 HP {hp}/{player.max_hp}"]


# 组队就是跟随：跟着谁就加入谁的队伍（他没队伍就一起新建一个），不跟了就退队。
# 地牢里、战斗中不能退队也不能停止跟随，免得把队友丢在半路；地牢按队伍分副本，不是一起进来的人碰不到面
def _party_locked(cur: Cursor, player: Player) -> Optional[str]:
    """现在不能离开队伍的原因，能离开是 None"""
    if dungeon.is_dungeon(player.room_id):
        return "在地牢里"
    cur.execute("""select 1 from npcs n join npc_templates t on t.id = n.template_id
                   where n.room_id = %s and n.alive and t.hostile limit 1""", (player.room_id,))
    if cur.fetchone():
        return "正在战斗"
    cur.execute("select 1 from duels where accepted and %s in (challenger, target) limit 1", (player.id,))
    if cur.fetchone():
        return "正在决斗"
    return None


def do_follow(cur: Cursor, player: Player, view: RoomView, a: Follow) -> list[str]:
    target, _ = _room_player(cur, player, a.target, lock=True)
    if target.following == player.id:
        raise ActionError(f"{target.name}正跟着{player.name}，不能互相跟着")
    facts = [f"{player.name}跟上了{target.name}，以后{target.name}走到哪就跟到哪（这一回合两人都没有移动）"]
    if not (player.party_id and player.party_id == target.party_id):
        if player.party_id and (why := _party_locked(cur, player)):
            raise ActionError(f"{player.name}{why}，不能丢下现在的队伍去跟{target.name}")
        party_id = target.party_id or uuid4()
        if target.party_id and len(_party_names(cur, party_id)) >= PARTY_MAX:
            raise ActionError(f"{target.name}的队伍已经满了（最多 {PARTY_MAX} 人）")
        if player.party_id:
            _leave_party(cur, player)
        if target.party_id is None:
            cur.execute("update players set party_id = %s where id = %s", (party_id, target.id))
        cur.execute("update players set party_id = %s where id = %s", (party_id, player.id))
        facts.append(f"{player.name}加入了{target.name}的队伍，队伍成员：" + "、".join(_party_names(cur, party_id)))
    cur.execute("update players set following = %s where id = %s", (target.id, player.id))
    return facts


def do_unfollow(cur: Cursor, player: Player, view: RoomView, a: Unfollow) -> list[str]:
    """不跟了就是退队"""
    if player.following is None and not player.party_id:
        raise ActionError(f"{player.name}没有在跟着谁，也不在队伍里")
    return _quit_party(cur, player)


def do_leave_party(cur: Cursor, player: Player, view: RoomView, a: LeaveParty) -> list[str]:
    if not player.party_id:
        raise ActionError(f"{player.name}没有在队伍里")
    return _quit_party(cur, player)


def do_kick(cur: Cursor, player: Player, view: RoomView, a: Kick) -> list[str]:
    """把跟着自己的人请出队伍（组队没有队长，谁跟着你你就能请他走）：他不再跟着你、离开队伍。
    他不在身边、下线了也行；地牢里、战斗中不行，跟自己退队一样"""
    cur.execute("select * from players where name = %s for update", (a.target.strip(),))
    row = cur.fetchone()
    if not row or row["id"] == player.id:
        raise ActionError(f"没有叫{a.target}的人" if not row else "不能把自己请出队伍，要走就说「离队」")
    target = load_player(cur, row["id"], lock=True)
    if target.following != player.id:
        raise ActionError(f"{target.name}没有在跟着{player.name}，只能请走跟着自己的人")
    if why := _party_locked(cur, player) or _party_locked(cur, target):
        raise ActionError(f"{why}，不能把{target.name}请出队伍（回到地面、打完这一仗再说）")
    cur.execute("update players set following = null where id = %s", (target.id,))
    if target.party_id:
        _leave_party(cur, target)
    return [f"{player.name}请{target.name}离开了队伍，{target.name}不再跟着{player.name}了"]


def _quit_party(cur: Cursor, player: Player) -> list[str]:
    if why := _party_locked(cur, player):
        raise ActionError(f"{player.name}{why}，不能丢下队伍自己走（回到地面、打完这一仗再说）")
    facts = []
    if player.following:
        cur.execute("select name from players where id = %s", (player.following,))
        facts.append(f"{player.name}不再跟着{cur.fetchone()['name']}了")
    cur.execute("update players set following = null where id = %s", (player.id,))
    if player.party_id:
        _leave_party(cur, player)
        facts.append(f"{player.name}离开了队伍")
    return facts


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
        return [f"{d.container}：{d.description}".rstrip("：")] \
            + ([f"{d.container}{d.where}有{d.item_name}"] if d.available else []) \
            + ([f"{d.container}里还有别人留下的：{'、'.join(x['name'] for x in d.donated)}（说「从{d.container}里拿某某」，每人每天一件）"]
               if d.donated else []) \
            + ([f"用不上的武器、护具可以放进{d.container}留给新人（说「把某某放进{d.container}」；强化清零，镶的宝石退回自己背包）"]
               if d.donate else [])
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


WISHES = [("atk_pct", 15, "许愿池的祝福（攻击 +15%）"), ("dodge", 10, "许愿池的祝福（对方命中 -10%）"),
          ("regen", 3, "许愿池的祝福（每回合回 3% 血）")]


# 回忆之门里看到的往事（草稿，按 NPC 设定还会再改）
MEMORY_FRAGMENTS = {
    "innkeeper": ("挂着锡杯的门", "很多年前的野猪酒馆，一个扎着小辫的女孩垫着木箱才够得着酒桶。一个满脸伤疤的老冒险者把空锡杯推到她面前，"
                  "说以后这杯归她倒。女孩踮着脚倒满，洒了一半，老头哈哈大笑，一口喝干。那只锡杯后来一直挂在吧台后面"),
    "smith": ("挂着锻锤的门", "一间炉火很旺的铁匠铺，一个小女孩蹲在炉边，用捡来的碎铁敲出一把歪歪扭扭的小刀。一双大手把刀拿过去看了很久，"
              "没说话，第二天她的小板凳旁边多了一把刚好趁手的小锤"),
    "shopkeeper": ("挂着旧书的门", "一个下雨的傍晚，一个瘦小的女孩缩在旧书店最里面的书架之间，读到天都黑了也没发觉。店主没有赶她走，"
                   "只是悄悄在她身边放了一盏灯，又把门上的牌子翻成了“打烊”"),
}


def _bottle(cur: Cursor, player: Player, d: Dispenser) -> list[str]:
    """漂流瓶：随机一张别人写过的纸条（没有就是空的），另外有几率捡到 bonus_item"""
    cur.execute("""select props from item_instances where props ? 'note' and coalesce(player_id::text, '') <> %s
                   order by random() limit 1""", (str(player.id),))
    row = cur.fetchone()
    cur.execute("select id from item_templates where props ? 'writable' limit 1")
    tpl = cur.fetchone()["id"]
    props = {k: v for k, v in (row["props"] if row else {}).items() if k in ("note", "writable", "name")}
    cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)", (tpl, player.id, Jsonb(props)))
    facts = [f"{player.name}拔开瓶塞，倒出一张" + (f"写了字的纸条：“{props['note']}”" if props.get("note") else "空白的纸条")]
    if (b := d.extra.get("bonus_item")) and _roll(b.get("chance", 0)):
        _give_player_new(cur, player, b["item"])
        cur.execute("select name from item_templates where id = %s", (b["item"],))
        facts.append(f"瓶身上挂着的{cur.fetchone()['name']}也一起到了手里")
    return facts


def _memory_door(cur: Cursor, player: Player, d: Dispenser) -> list[str]:
    """回忆之门：推开一扇门，看见那个人很久以前的一段往事；第一次看到某个人的，好感 +3"""
    seen = set(player.flags.get("_memories") or [])
    keys = [k for k in d.extra.get("npcs", list(MEMORY_FRAGMENTS)) if k in MEMORY_FRAGMENTS]
    key = random.choice([k for k in keys if k not in seen] or keys)
    door, text = MEMORY_FRAGMENTS[key]
    cur.execute("select id, name from npc_templates where id = %s", (key,))
    t = cur.fetchone()
    facts = [f"{player.name}推开了{door}", text]
    if key not in seen and t:
        cur.execute("""insert into player_npc_relations (player_id, npc_template, affinity) values (%s, %s, %s)
                       on conflict (player_id, npc_template) do update set affinity = least(100, player_npc_relations.affinity + %s)""",
                    (player.id, key, 3, 3))
        cur.execute("update players set flags = jsonb_set(flags, '{_memories}', %s) where id = %s", (Jsonb(sorted(seen | {key})), player.id))
        facts.append(f"（{player.name}好像更懂{t['name']}了一点：好感 +3）")
    return facts


def _wish(cur: Cursor, player: Player, d: Dispenser) -> list[str]:
    """许愿池：付 cost_gold × 层数的钱，这一层随机一个小祝福；整层声响已经到 fail_if_noise_at_least 就不灵（钱照付）"""
    depth = dungeon.parse_room(player.room_id)[1]
    cost = int(d.extra.get("cost_gold", 10)) * depth
    if player.gold < cost:
        raise ActionError(f"投一次要 {cost} 金币，{player.name}身上不够")
    cur.execute("update players set gold = gold - %s where id = %s", (cost, player.id))
    noise = int(dungeon.floor_state(cur, player.room_id).get("noise", 0))
    if noise >= int(d.extra.get("fail_if_noise_at_least", 99)):
        return [f"{player.name}往池里投了 {cost} 金币，钱币沉下去时却溅起了一圈水花：这一层太吵了，愿望不灵（声响 {noise}）"]
    stat, pct, label = random.choice(WISHES)
    _give_bless(cur, player, stat, pct, label)
    return [f"{player.name}往池里轻轻投了 {cost} 金币，钱币无声地沉了下去", f"{player.name}得到了{label}，出了这一层就散"]


def _cleanse(cur: Cursor, player: Player) -> list[str]:
    """忏悔室：清掉身上所有减益（腐蚀扣的血量上限还回去），回血量上限的 15%"""
    bad = [e for e in player.effects if e.kind not in ("whet", "cheer", "bless")]
    back = sum(e.hp for e in bad)
    player.effects = [e for e in player.effects if e not in bad]
    _save_effects(cur, player)
    gain = round((player.max_hp + back) * 0.15)
    cur.execute("update players set max_hp = max_hp + %s, hp = least(max_hp + %s, hp + %s), status = null where id = %s",
                (back, back, gain, player.id))
    return [f"{player.name}在帘子后面轻声说完了自己做过的事，木板后面的人叹了口气",
            f"{player.name}身上的晦气散了" + (f"（{'、'.join(EFFECT_NAMES[e.kind] for e in bad)}都消退了）" if bad else "")
            + f"，回了 {gain} 点血"]


def _read_stele(cur: Cursor, player: Player, d: Dispenser) -> list[str]:
    """禁言碑：下一层进去时整层地图都知道；代价是接下来一阵子禁声（说不了话、念不了书卷）"""
    depth = dungeon.parse_room(player.room_id)[1]
    cur.execute("update players set flags = flags || jsonb_build_object('_reveal_next', %s::int) where id = %s", (depth + 1, player.id))
    ss = d.extra.get("self_status") or {}
    player.effects = [e for e in player.effects if e.kind != "silence"] + [
        Effect(kind="silence", value=1, left=int(ss.get("actions", 10)), label="读完碑文说不出话", source="禁言碑")]
    _save_effects(cur, player)
    return [f"{player.name}一行行读完了碑文，下一层的路都记在了心里", f"读完以后{player.name}喉咙发紧，很长一段时间都说不出话来（禁声）"]


def _mirror_duel(cur: Cursor, player: Player, d: Dispenser) -> list[str]:
    """镜厅：镜子里走出一个照着他打出来的倒影（血六成，攻防跟他一样），打死了掉宝石和晶粉"""
    f = dungeon.floor_info(cur, player.room_id)
    atk, df = gear_totals(player.attack, player.defense, load_items(cur, "i.player_id = %s", (player.id,)))
    cfg = next((e for e in dungeon.data()["events"].values() if e.get("take", {}).get("service") == "mirror_duel"), {})
    name = dungeon.spawn_mirror(cur, player.room_id, player.id, player.name, max(SCALE, round(player.max_hp * 0.6)), atk, df,
                                f["depth"], f["theme"], cfg.get("take", {}).get("reward", ["gem"]))
    st = _stealth(player)
    st.detected, st.hidden = True, False
    _save_stealth(cur, player, st)
    return [f"{player.name}对着镜子看了一眼，镜子里的人却没有跟着眨眼，一步跨了出来：是{name}"]


def _free_upgrade(cur: Cursor, player: Player, d: Dispenser) -> list[str]:
    """未熄的锻炉（service: free_upgrade）：照着锻造纹敲，手上的主武器免费升一级；满级了、手上没武器就给一份碎铁"""
    weapon = next((w for w in _weapons(cur, player) if w.equipped_slot == "right_hand"), None) \
        or next(iter(_weapons(cur, player)), None)
    if weapon is None or weapon.props.get("plus", 0) >= upgrade_cap(player, weapon):
        _give_player_new(cur, player, SCRAP)
        return [f"{player.name}照着锻造纹敲了一阵，" + ("手上的兵器已经锻到头了" if weapon else "手上没拿兵器")
                + "，只敲下来一份好铁（碎铁）"]
    stat, level = "damage", weapon.props.get("plus", 0) + 1
    name = re.sub(r" \+\d+$", "", weapon.name) + f" +{level}"
    new = weapon.damage + _step(weapon, stat)
    cur.execute("update item_instances set props = (props - 'upgrade_fails') || %s where id = %s",
                (Jsonb({"plus": level, stat: new, "name": name}), weapon.id))
    return [f"{player.name}照着铁砧上的锻造纹，把{weapon.name}放进炉火里敲打了一番",
            f"{weapon.name}变成了{name}，伤害 {stat_text(new)}（没花钱）"]


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
        # 跟挣脱一样：每失败一次难度降一级，多试几次总能成（最多挨几下）
        cur.execute("select fails from dispenser_log where player_id = %s and room_id = %s and key = %s",
                    (player.id, d.room, f"{d.key}#fails"))
        fails = (cur.fetchone() or {}).get("fails", 0)
        ok, facts = _check(cur, player, view, d.skill, max(1, d.difficulty - fails))
        if not ok:
            cur.execute("""insert into dispenser_log (player_id, room_id, key, fails) values (%s, %s, %s, 1)
                           on conflict (player_id, room_id, key) do update set fails = dispenser_log.fails + 1""",
                        (player.id, d.room, f"{d.key}#fails"))
            if d.fail == "void_fall":
                return [f"{player.name}助跑一下跳向{d.container}"] + facts + ["差了一点点，脚尖擦着石台边滑了下去"] \
                    + _void_fall(cur, player, force=True)
            facts = [f"{player.name}想从{d.container}{d.where}取{d.item_name}"] + facts + [d.fail or "没能成功"]
            if d.fail_damage:
                hurt, _ = _hurt_player(cur, player, d.fail_damage, "other", d.container)
                facts += [f"{player.name}受到 {d.fail_damage} 点伤害"] + hurt
            return facts + ["摸到了点门道，下次会顺手一些"]
    if d.once:
        # 拿过一次就没了：并发时靠主键挡住第二次
        cur.execute("""insert into dispenser_log (player_id, room_id, key) values (%s, %s, %s)
                       on conflict do nothing returning 1""", (player.id, d.room, d.key))
        if not cur.fetchone():
            raise ActionError(f"{d.container}{d.where}已经没有{player.name}能拿的东西了")
    if d.service == "free_upgrade":
        return facts + _free_upgrade(cur, player, d)
    if d.service == "mirror_duel":
        return facts + _mirror_duel(cur, player, d)
    if d.service == "wish":
        return facts + _wish(cur, player, d)
    if d.service == "clear_fog":
        dungeon.set_floor_state(cur, player.room_id, {"fog_clear": True})
        return facts + [f"{player.name}点亮了灯塔，一束光扫过海面，这一层的雾散开了（远程、火把照常，怪不再一进门就在跟前）"]
    if d.service == "player_note":
        return facts + _bottle(cur, player, d)
    if d.service == "buff":
        b = d.extra.get("buff") or {}
        for stat in ("atk_pct", "hit_pct", "dodge"):
            if b.get(stat):
                pct = round(b[stat] * 100)
                _give_bless(cur, player, stat, pct, {"atk_pct": f"清醒梦（攻击 +{pct}%）", "hit_pct": f"读懂的星图（命中 +{pct}%）",
                                                      "dodge": f"祝福（对方命中 -{pct}%）"}[stat])
        if b.get("reveal") == "star_cycle":
            _give_bless(cur, player, "star_chart", 0, "读懂的星图（知道星光什么时候变）")
        return facts + [f"{player.name}{d.extra.get('done') or '心里一下子亮堂了'}：{'、'.join(e.label for e in player.effects if e.kind == 'bless')}，出了这一层就散"]
    if d.service == "shortcut":
        f = dungeon.floor_info(cur, player.room_id)
        cur.execute("select stairs_room from dungeon_floors where run_id = %s and depth = %s", dungeon.parse_room(player.room_id))
        stairs = cur.fetchone()["stairs_room"]
        cur.execute("update players set room_id = %s, stealth = null where id = %s", (stairs, player.id))
        dungeon.mark_seen(cur, stairs)
        return facts + [f"{player.name}找出了楼梯的规律，最后一遍走到底，推开门就到了第 {f['depth']} 层的楼梯间"]
    if d.service == "memory":
        return facts + _memory_door(cur, player, d)
    if d.service == "cleanse":
        return facts + _cleanse(cur, player)
    if d.service == "reveal_next_floor":
        return facts + _read_stele(cur, player, d)
    if d.item == "treasure_pool":
        f = dungeon.floor_info(cur, player.room_id)
        item = dungeon._pick(dungeon.loot_data()["treasure"].get(f["theme"]), f["depth"]) or "herb"
        dungeon._put_item(cur, item, f["depth"], player=player.id, rarity="uncommon")
        cur.execute("select name from item_templates where id = %s", (item,))
        return facts + [f"{player.name}从{d.container}{d.where}拿到了{cur.fetchone()['name']}"]
    if d.service == "timed":
        # 沙漏房：拿了钱就得在沙子流完之前走出去
        f = dungeon.floor_info(cur, player.room_id)
        coins = dungeon.treasure_gold(f["depth"], 0.3, f["party_size"] or 1)
        cur.execute("update players set gold = gold + %s where id = %s", (coins, player.id))
        return facts + [f"{player.name}一把抓起石台上的钱袋（{coins} 金币）：沙子流完之前得走出这间屋子"]
    if d.item == "gem":
        f = dungeon.floor_info(cur, player.room_id)
        gem = dungeon.put_gem(cur, dungeon.pick_gem(f["theme"]), f["depth"], player=player.id,
                              min_tier=int(d.extra.get("gem_min_tier", 1)))
        return facts + [f"{player.name}从{d.container}{d.where}撬下来一颗{gem}"]
    _give_player_new(cur, player, d.item)
    facts = facts + [f"{player.name}从{d.container}{d.where}拿了一件{d.item_name}"]
    if d.bonus_gem and dungeon.is_dungeon(player.room_id) and _roll(d.bonus_gem):
        f = dungeon.floor_info(cur, player.room_id)
        gem = dungeon.put_gem(cur, dungeon.pick_gem(f["theme"]), f["depth"], player=player.id)
        facts.append(f"敲下来的碎块里还滚出一颗{gem}")
    if d.item == UPGRADE_ORE and dungeon.is_dungeon(player.room_id) and _roll(dungeon.gem_rules()["drops"]["mining"]):
        depth = dungeon.parse_room(player.room_id)[1]
        gem = dungeon.put_gem(cur, dungeon.pick_gem("mine", 0.5), depth, player=player.id)
        facts.append(f"敲下来的碎石里还滚出一颗{gem}")
    return facts


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
    _no_curse(item, "扔")
    if _burning(item):
        return _douse(cur, player, item)
    facts = [f"{player.name}卸下了{item.name}"] if item.equipped_slot else []
    _move_item(cur, item, room_id=player.room_id)
    return facts + [f"{player.name}把{_label(item)}放在了地上"]


def do_use(cur: Cursor, player: Player, view: RoomView, a: Use) -> list[str]:
    item = _inv_item(cur, view, player, a.item)
    if _prop(item, "water") and a.target and a.target in view.refs:
        return _douse_npc(cur, player, view, item, a.target)
    if _prop(item, "stun"):
        return _stun(cur, player, view, item, a.target)
    for key, fn in (("refuel", _refuel), ("whet", _whet), ("room_light", _room_light), ("holy", _holy), ("drug", _drug),
                    ("smoke", _smoke)):
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
        return _unlock(cur, player, item, a.target)

    if _prop(item, "reveal_floor"):
        return _read_map(cur, player, item)
    if _prop(item, "flask"):
        return _flask(cur, player, item)
    if _prop(item, "antidote"):
        return _antidote(cur, player, item)
    if _prop(item, "refill_by"):
        raise ActionError(f"{item.name}已经空了，回酒馆找麦琪说「续杯」")
    if _prop(item, "recall"):
        return _recall(cur, player, item)
    if item.template.type != "consumable":
        raise ActionError(f"{item.name}不能直接使用")
    _consume(cur, item)
    return [_use_self(player, item)] + _eat_effect(cur, player, item, player)


def _unlock(cur: Cursor, player: Player, key: ItemInstance, direction: str) -> list[str]:
    """用钥匙开门。只开一次的钥匙（典狱长的大钥匙，props.opens）转开以后就卡在锁里拔不出来了"""
    cur.execute("update room_exits set locked = false, unlocked_at = now() where room_id = %s and direction = %s",
                (player.room_id, direction))
    facts = [f"{player.name}用{key.name}打开了往{dir_name(direction)}的门"]
    if _prop(key, "opens"):
        _consume(cur, key)
        facts.append(f"{key.name}转到底就卡死在锁里，拔不出来了")
    return facts




def _empty(cur: Cursor, item: ItemInstance) -> str:
    """麦琪的酒壶、药用完了：换成空的样子，返回新名字"""
    return _swap_template(cur, item, _prop(item, "empty"))


def _flask(cur: Cursor, player: Player, item: ItemInstance) -> list[str]:
    """麦琪的私酿：一口回满血，浑身是劲（这一层攻击 +CHEER_ATTACK%）。喝完就空了，找她续杯"""
    if player.hp >= player.max_hp:
        raise ActionError(f"{player.name}没受伤，舍不得喝（这一壶喝完就得回酒馆续了）")
    cur.execute("update players set hp = max_hp where id = %s", (player.id,))
    facts = [f"{player.name}拧开{item.name}灌了一大口，一股辛辣的暖流冲遍全身，壶底的纸条晃了晃",
             f"{player.name}恢复到满血，HP {player.max_hp}/{player.max_hp}"]
    player.effects = [e for e in player.effects if e.kind != "cheer"] + [
        Effect(kind="cheer", value=CHEER_ATTACK, left=CHEER_FLOORS, label="私酿的酒劲", source=item.name)]
    _save_effects(cur, player)
    facts.append(f"{player.name}浑身是劲，攻击 +{CHEER_ATTACK}%（这一层）")
    return facts + [f"{_empty(cur, item)}，回酒馆找麦琪续杯"]


def _antidote(cur: Cursor, player: Player, item: ItemInstance) -> list[str]:
    """麦琪的解毒酒壶：解毒、回一点血，喝完就空了"""
    poison = _effect(player, "poison")
    if not poison and player.hp >= player.max_hp:
        raise ActionError(f"{player.name}没中毒也没受伤，用不着喝")
    facts = [f"{player.name}灌了一口{item.name}，又苦又冲，呛得直咳嗽"]
    if poison:
        player.effects.remove(poison)
        _save_effects(cur, player)
        facts.append(f"{player.name}身上的中毒好了")
    heal = _heal_scale(player, heal_amount(int(_prop(item, "antidote")), player.max_hp))
    hp = min(player.max_hp, player.hp + heal)
    cur.execute("update players set hp = %s where id = %s", (hp, player.id))
    return facts + [f"{player.name}恢复 {hp - player.hp} 点 HP，当前 HP {hp}/{player.max_hp}", f"{_empty(cur, item)}，回酒馆找麦琪续杯"]


def _drug(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """麦琪的特制迷药：泼向一个敌人，它醉倒动弹不得（不用判定），瓶子空了"""
    foes = _enemies(cur, player.room_id)
    if target and target in view.refs:
        npc = _room_npc(cur, view, player, target)
    elif len(foes) == 1:
        npc = foes[0]
    else:
        raise ActionError("这里没有敌人" if not foes else f"要泼向谁？（{'、'.join(n.name for n in foes)}）")
    if not npc.template.hostile:
        raise ActionError(f"不能拿{item.name}对付{npc.name}")
    if _boss_resists(cur, npc):
        raise ActionError(f"{npc.name}这一仗已经被放倒过一回，有了防备，{item.name}泼过去也不管用（留着吧）")
    label = str(_prop(item, "drug"))[:20]
    _set_status(cur, "npcs", npc.id, Status(kind="incapacitated", label=label, escape=2,
                                            since=datetime.now(timezone.utc).isoformat()))
    return [f"{player.name}拔开{item.name}的塞子，朝{npc.name}泼了过去，一股甜腻的酒气散开",
            f"{npc.name}{label}", f"{_empty(cur, item)}，回酒馆找麦琪续上"]


def do_refill(cur: Cursor, player: Player, view: RoomView, a: Refill) -> list[str]:
    """找麦琪续杯：她给的酒壶、药空了的都灌满，不收钱"""
    npc = _room_npc(cur, view, player, a.target)
    empties = [i for i in view.inventory if _prop(i, "refill_by") == npc.template.id]
    if not empties:
        mine = [i for i in view.inventory if _prop(i, "empty") and any(
            t.get("item") == i.template.id for t in (npc.template.props.get("return_gifts") or {}).values())]
        raise ActionError(f"{player.name}身上{'的' + '、'.join(i.name for i in mine) + '都还满着' if mine else f'没有{npc.name}能续的东西'}")
    # 续杯是她的心意：交情掉到送这件东西那一档以下，她就不给续了
    tiers = {g.get("item"): int(t) for t, g in (npc.template.props.get("return_gifts") or {}).items()}
    aff = _affinity(cur, player, npc)
    if cold := [i for i in empties if aff < tiers.get(_prop(i, "refill_to"), 0)]:
        raise ActionError(f"{npc.name}瞥了一眼{'、'.join(i.name for i in cold)}，没接：想续？先把交情补回来再说")
    names = [_swap_template(cur, i, _prop(i, "refill_to")) for i in empties]
    return [f"{npc.name}接过{'、'.join(i.name for i in empties)}，哼了一声，背过身去一样一样灌满了又塞回{player.name}手里",
            f"{'、'.join(names)}又满了"]


def _read_map(cur: Cursor, player: Player, item: ItemInstance) -> list[str]:
    """地图残片：显出这一层所有房间，小地图上能看到楼梯间、宝箱房在哪（全队共用）"""
    if not dungeon.is_dungeon(player.room_id):
        raise ActionError(f"{item.name}得在地牢里展开，墨迹才会动")
    if not dungeon.reveal(cur, player.room_id):
        raise ActionError("这一层的地图已经全显出来了，用不着再展开一张")
    _consume(cur, item)
    return [f"{player.name}展开{item.name}，兽皮上的墨迹自己游动起来，画出了这一层所有的房间：侧栏的小地图上能看到楼梯间和宝箱房在哪了"]


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
    if who := _silenced(cur, player.room_id, player):
        raise ActionError(f"{who}禁了声，{item.name}上的字都暗着，念不出来")
    _consume(cur, item)
    noise = _noise(cur, player, "read")
    cur.execute("""update rooms set props = jsonb_set(props, '{env,scroll}', %s) where id = %s""",
                (Jsonb({"value": int(_prop(item, "room_light")), "by": str(player.id)}), player.room_id))
    return [f"{player.name}展开{item.name}，上面的字一个接一个亮起来，把整个房间照得雪亮"] + noise


def _feature_cfg(cur: Cursor, room_id: str, name: str) -> dict:
    """这个环境物件在主题里的配置（on_break、noise、clears_fog）"""
    if not dungeon.is_dungeon(room_id):
        return {}
    theme = dungeon.floor_info(cur, room_id)["theme"]
    return next((f for f in dungeon.data()["themes"][theme].get("features", []) if f["name"] == name), {})


# ============ 声响（静默神殿 theme.noise）============
# 整层一个声响值（dungeon_floors.state.noise），出声的动作往上加：说话 1、念书卷 2、砸碎重物 2、重伤以上的一下 1、
# 群伤 2、被撞倒 1、逃跑 1，铜钟这种物件自己定。房间也记一份（rooms.env.noise），到了石像的 wake_noise 它就醒。
# 满了（max，装备 noise_max_add 往上加）引来游荡的怪直接扑进这个房间（21 层起一群），声响回落到 reset_to。
# 大祭司在场时每个出声的动作给他回血量上限的 noise_heal
def _noise(cur: Cursor, player: Player, what, heal: bool = True) -> list[str]:
    room = player.room_id
    if not what or not dungeon.is_dungeon(room):
        return []
    cfg = dungeon.data()["themes"][dungeon.floor_info(cur, room)["theme"]].get("noise")
    if not cfg:
        return []
    amount = int(cfg.get("add", {}).get(what, 0)) if isinstance(what, str) else int(what)
    if mults := [e.get("value", 1) for e in _fx(_worn(cur, player), "noise_mult")]:
        amount = round(amount * min(mults))         # 软底靴这类：声响 ×value（取最好的一件）
    if amount <= 0:
        return []
    top = int(cfg.get("max", 10) + gear_add(cur, player, "noise_max_add"))
    noise = int(dungeon.floor_state(cur, room).get("noise", 0)) + amount
    env = _room_env(cur, room)
    here = int(env.get("noise", 0)) + amount
    cur.execute("update rooms set props = jsonb_set(props, '{env,noise}', to_jsonb(%s::int)) where id = %s", (here, room))
    facts = [f"（声响 {min(noise, top)}/{top}）"]
    for n in load_npcs(cur, "n.room_id = %s and n.alive and (t.props->>'dormant')::boolean", (room,)):
        if not _tally(cur, n, "awake") and here >= int(n.template.props.get("wake_noise", 3)):
            _tally_merge(cur, n, {"awake": 1})
            facts.append(f"{n.name}眼皮上的石屑簌簌往下掉：它被吵醒了")
    if heal:
        for n in _enemies(cur, room):
            if (pct := n.template.props.get("noise_heal")) and n.hp < n.template.max_hp:
                gain = min(n.template.max_hp - n.hp, max(1, round(n.template.max_hp * pct)))
                cur.execute("update npcs set hp = hp + %s where id = %s", (gain, n.id))
                facts.append(f"{n.name}的银面具微微一亮，那一声让他回了 {gain} 点血")
    if noise >= top:
        full = cfg.get("full", {})
        noise = int(full.get("reset_to", 5))
        depth = dungeon.parse_room(room)[1]
        name = dungeon.spawn_wanderer(cur, room, 2 if depth >= 21 else 1)
        st = _stealth(player)
        st.detected, st.hidden = True, False
        _save_stealth(cur, player, st)
        facts.append(f"声音在整座神殿里回荡开去，远处传来急促的脚步：{name}循着声音直扑了进来")
    dungeon.set_floor_state(cur, room, {"noise": noise})
    return facts


def _feature_break(cur: Cursor, room_id: str, name: str) -> list[str]:
    """环境物件用掉以后（on_break）：打碎晶簇，房间暗下来"""
    ft = _feature_cfg(cur, room_id, name)
    if not (brk := ft.get("on_break")) or "light" not in brk:
        return []
    env = _room_env(cur, room_id)
    new = max(0, min(100, int(env.get("light", 50)) + brk["light"]))
    cur.execute("update rooms set props = jsonb_set(props, '{env,light}', to_jsonb(%s::int)) where id = %s", (new, room_id))
    return [f"{name}碎了一地，房间{'暗' if brk['light'] < 0 else '亮'}了一截（光亮 {new}）"]


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
    if npc.template.props.get("weak") == "holy":
        dmg = math.ceil(dmg * WEAK_MULT)            # 守陵人怕圣物
    hurt, dead = _hurt_npc(cur, player, npc, dmg)
    return facts + [f"圣水在{npc.name}身上嘶嘶地冒起白烟，受到 {dmg} 点伤害"] + hurt


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


def _stun(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """念能定身的东西（古书残卷：props.stun 是状态说明）：不用判定，房间里所有敌人一起失去战斗能力，东西用掉"""
    foes = _enemies(cur, player.room_id)
    if not foes:
        raise ActionError(f"这里没有敌人，{item.name}念了也没用")
    if who := _silenced(cur, player.room_id, player):
        raise ActionError(f"{who}禁了声，{item.name}上的字都暗着，念不出来")
    # 这一仗已经被控过的头目不吃这一套；房间里只剩它的话书先不念（不白白用掉这一层的次数）
    immune = [n for n in foes if _boss_resists(cur, n, mark=False)]
    if immune and len(immune) == len(foes):
        raise ActionError(f"{'、'.join(n.name for n in immune)}这一仗已经被困住过一回，有了防备，念了也定不住")
    foes = [n for n in foes if n not in immune]
    if _prop(item, "per_floor"):
        if not _once_per_floor(cur, player, "tome"):
            raise ActionError(f"{item.name}这一层已经念过了，书页上的字暗着，要到下一层才会再亮起来")
        label = str(_prop(item, "stun"))[:20]
        for npc in foes:
            _boss_resists(cur, npc)
            _set_status(cur, "npcs", npc.id, Status(kind="incapacitated", label=label, escape=STUN_ESCAPE,
                                                    since=datetime.now(timezone.utc).isoformat()))
        return [f"{player.name}翻开{item.name}，念出上面的古老文字，书页上的字一行行亮起来又暗下去（这一层用过了）",
                f"{'、'.join(n.name for n in foes)}{label}，失去了战斗能力"]
    _use_up(cur, item)
    label = str(_prop(item, "stun"))[:20]
    for npc in foes:
        _boss_resists(cur, npc)
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


# 消耗品的种类（模板 props.kind）：决定怎么用（吃、喝、敷）、算不算药。没写的按老办法猜：酒、水、汤是喝的，别的是吃的
USE_KINDS = {"food": "食物", "drink": "饮品", "potion": "药剂", "salve": "外用药"}
MEDICINE_KINDS = ("potion", "salve")
MEDIC_BONUS = 0.1                       # 给别人用药：用药的人医药每级多回 10%
MEDIC_CURE_LEVEL = 3                    # 医药到 3 级，给别人用药顺带解毒、止血


def _use_kind(item: ItemInstance) -> str:
    if kind := _prop(item, "kind"):
        return kind
    return "drink" if item.template.id == "made_drink" or _is_alcohol(item) or any(c in item.name for c in "水汤茶奶") else "food"


def _use_self(player: Player, item: ItemInstance) -> str:
    return {"food": f"{player.name}吃掉了{item.name}", "drink": f"{player.name}喝掉了{item.name}",
            "potion": f"{player.name}喝下了{item.name}", "salve": f"{player.name}给自己用上了{item.name}"}[_use_kind(item)]


def _use_other(player: Player, target: Player, item: ItemInstance) -> str:
    return {"food": f"{player.name}喂{target.name}吃了{item.name}", "drink": f"{player.name}喂{target.name}喝了{item.name}",
            "potion": f"{player.name}给{target.name}灌下了{item.name}",
            "salve": f"{player.name}给{target.name}用上了{item.name}"}[_use_kind(item)]


# 吃喝回血按血量上限的百分比算：rules.heal_amount
def _is_alcohol(item: ItemInstance) -> bool:
    return bool(_prop(item, "alcohol")) or "酒" in item.name


def _eat_effect(cur: Cursor, eater: Player, item: ItemInstance, user: Player) -> list[str]:
    """吃喝下去的效果：有毒的掉血，蒙汗药这类把人放倒，正常的回血（倒下的人吃了回血药也能站起来）。
    回血按吃的人血量上限的百分比（heal_amount）；药草（模板 props.herbal）按用药的人（自己吃是自己，喂别人是喂的人）
    的自然等级多回。给别人用药（药剂、外用药）：用药的人医药每级多回 MEDIC_BONUS，用得上（回了血、治好了状态）练医药"""
    facts = []
    points = item.heal + (skill_level(user.skills.get("nature", 0)) if item.heal and item.template.props.get("herbal") else 0)
    medic = user.id != eater.id and _use_kind(item) in MEDICINE_KINDS
    medic_level = skill_level(user.skills.get("medicine", 0)) if medic else 0
    heal = heal_amount(points, eater.max_hp, _is_alcohol(item))
    if medic and heal:
        heal = round(heal * (1 + MEDIC_BONUS * medic_level))
    was_hp, was_effects = eater.hp, len(eater.effects)
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
    hp = max(0, min(eater.max_hp, eater.hp + _heal_scale(eater, heal) - harm))
    cur.execute("update players set hp = %s, updated_at = now() where id = %s", (hp, eater.id))
    cures = [_prop(item, "cure")] + (["poison", "bleed"] if medic and medic_level >= MEDIC_CURE_LEVEL else [])
    for cure in dict.fromkeys(c for c in cures if c):
        if e := _effect(eater, cure):
            # 绷带止血、解毒苔解毒；医药练到家的人给别人用什么药都顺带解毒止血
            eater.effects.remove(e)
            _save_effects(cur, eater)
            facts.append(f"{eater.name}身上的{EFFECT_NAMES[cure]}好了")
    practiced = medic and (hp > was_hp or len(eater.effects) < was_effects)
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
    if (q := _prop(item, "quench")) and hp > 0:
        cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::int) where id = %s",
                    (QUENCH, int(q), eater.id))
        facts.append(f"凉水下肚，{eater.name}接下来 {int(q)} 个动作不怕灼热")
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
    if practiced:
        facts += _gain_skill(cur, user, "medicine")
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
    return [_use_other(player, target, item)] + _eat_effect(cur, target, item, player)


def _consume_n(cur: Cursor, item: ItemInstance, n: int) -> None:
    """一次用掉 n 个（叠着的减 n，不够或刚好就删掉）"""
    if item.quantity > n:
        cur.execute("update item_instances set quantity = quantity - %s where id = %s", (n, item.id))
    else:
        cur.execute("delete from item_instances where id = %s", (item.id,))


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
                _no_curse(other, "放")
                if _burning(other):
                    raise ActionError(f"{item.name}要两只手拿，左手的{other.name}点着，收起来就灭了；先把火把熄掉再换")
                cur.execute("update item_instances set equipped_slot = null where id = %s", (other.id,))
                facts.append(f"{player.name}放下了{other.name}，腾出两只手")
                worn.pop("left_hand")
        elif (big := next((i for i in worn.values() if _prop(i, "two_handed") and i.id != item.id), None)):
            _no_curse(big, "放")
            cur.execute("update item_instances set equipped_slot = null where id = %s", (big.id,))
            facts.append(f"{player.name}放下了要两只手拿的{big.name}")
            worn = {s: i for s, i in worn.items() if i.id != big.id}
    old = worn.get(slot)
    if old and not item.equipped_slot:
        _no_curse(old, "换")
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
    if _prop(item, "cursed"):
        facts.append(f"{item.name}一上身就像长在了{player.name}身上，怎么也甩不掉：它被诅咒了（找杂货铺的诺艾尔解咒）")
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
    _no_curse(item)
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


def _smoke_fades(cur: Cursor, room_id: str) -> list[str]:
    """敌人动了一次：烟散一点，散完了说一声"""
    env = _room_env(cur, room_id)
    if not env.get("smoke"):
        return []
    left = env["smoke"] - 1
    cur.execute("update rooms set props = jsonb_set(props, '{env,smoke}', to_jsonb(%s::int)) where id = %s", (left, room_id))
    return [] if left > 0 else ["呛人的灰烟慢慢散开了，又能看清远处了"]


def _smoke(cur: Cursor, player: Player, view: RoomView, item: ItemInstance, target: Optional[str]) -> list[str]:
    """烟雾弹：摔碎了一大团灰烟，这一轮和下一轮远程命中减半，敌我都算"""
    _consume(cur, item)
    cur.execute("""update rooms set props = jsonb_set(props, '{env}', coalesce(props->'env', '{}'::jsonb) || jsonb_build_object('smoke', %s::int))
                   where id = %s""", (SMOKE_ROUNDS, player.room_id))
    return [f"{player.name}把{item.name}往地上一摔，一大团呛人的灰烟腾起来，谁也看不清谁（远程命中减半，撑 {SMOKE_ROUNDS} 轮）"]


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


# 送了会讲往事的礼物（props.lore）：谁讲、讲什么
LORE = {"upon_mountain": ("shopkeeper", "“在其山岳之上者”"), "maggie_past": ("innkeeper", "她自己年轻时")}
LORE_MARK = "LORE::"                    # 这一段还没写过：server 生成、存下来再换成正文（server._fill_lore）


def _once_per_fight(cur: Cursor, player: Player, key: str) -> bool:
    """这一场（这个房间里的这一仗）还没用过 key 就记下并返回 True"""
    f = player.flags.get("_fight") or {}
    used = f.get("used", []) if f.get("room") == player.room_id else []
    if key in used:
        return False
    f = {"room": player.room_id, "used": used + [key]}
    player.flags["_fight"] = f
    cur.execute("update players set flags = flags || jsonb_build_object('_fight', %s::jsonb) where id = %s", (Jsonb(f), player.id))
    return True


def _cheat_death_ready(cur: Cursor, player: Player) -> bool:
    """不熄的羽毛：每层地牢一次（记在 players.flags 的 cheat_death 里）"""
    return _once_per_floor(cur, player, "cheat_death")


def _stealth(player: Player) -> Stealth:
    """玩家在当前区域的隐蔽情况；记录的是别的区域（刚被人带进来、刚登录）就当刚进门"""
    st = player.stealth
    return st if st and st.room == player.room_id else Stealth(room=player.room_id, chance=DETECT_START)


def distance_word(d: int) -> str:
    return f"{d} 格（{DISTANCE_WORDS.get(d, '离得很远')}）"


def _distance(st: Stealth, npc: Npc) -> int:
    # 长在头目身上的（晶簇，props.host）：离头目多远就离它多远
    return st.distance.get(npc.template.props.get("host") or str(npc.id), st.start)


FOG_SPOT = 0.75                         # 浓雾里怪发现人的几率倍数（躲藏容易一级，双方都看不清）
FOG_FAR = 0.5                           # 浓雾里远程打"几步开外"的命中倍数（theme.fog.ranged_far_mult）


def _star_dark(cur: Cursor, room_id: str) -> bool:
    """星界秘境的星光这会儿是暗的"""
    env = _room_env(cur, room_id)
    return bool(dungeon.is_dungeon(room_id) and int(env.get("shift", 0)) < 0
                and dungeon.data()["themes"][dungeon.floor_info(cur, room_id)["theme"]].get("star_cycle"))


def _away(cur: Cursor, npc: Npc) -> bool:
    """只在星光暗时出现的怪（虚空之眼 only_dark）：亮的时候不在场上"""
    return bool(npc.template.props.get("only_dark")) and not _star_dark(cur, npc.room_id)


def _void_fall(cur: Cursor, player: Player, add: int = 0, force: bool = False) -> list[str]:
    """虚空边缘（env.edge，theme.void）：被撞倒、推到边上时过一次运动，失败就坠入虚空：掉血量上限的 fall_damage_pct，
    被传回旁边一间不在边上的房间（这场仗等于重新进来）。星辰之靴这类不会掉"""
    env = _room_env(cur, player.room_id)
    if player.hp <= 0 or not env.get("edge") or not dungeon.is_dungeon(player.room_id) or gear_has(cur, player, "void_immune"):
        return []
    run, depth = dungeon.parse_room(player.room_id)
    void = dungeon.data()["themes"][dungeon.floor_info(cur, player.room_id)["theme"]].get("void") or {}
    facts = []
    if not force:
        ok, facts = _check(cur, player, SimpleNamespace(trained=[]), void.get("check", "athletics"),
                           int(void.get("base_difficulty", 2)) + depth // 10 + add)
        if ok:
            return facts + [f"{player.name}在石台边上晃了两下，稳住了"]
    dmg = max(SCALE, round(player.max_hp * void.get("fall_damage_pct", 0.15)))
    cur.execute("""select e.to_room from room_exits e join rooms r on r.id = e.to_room
                   where e.room_id = %s and e.direction in ('north', 'south', 'east', 'west')
                   order by coalesce((r.props->'env'->>'edge')::boolean, false), random() limit 1""", (player.room_id,))
    row = cur.fetchone()
    hurt, _ = _hurt_player(cur, player, dmg, "other", "虚空")
    facts += [f"{player.name}脚下一空，坠进了星海里，掉了 {dmg} 点血"] + hurt
    if row and player.hp > 0:
        cur.execute("update players set room_id = %s, status = null, stealth = %s where id = %s",
                    (row["to_room"], Jsonb(Stealth(room=row["to_room"], chance=DETECT_START).model_dump()), player.id))
        dungeon.mark_seen(cur, row["to_room"])
        cur.execute("select name from rooms where id = %s", (row["to_room"],))
        facts.append(f"一阵失重过后，{player.name}摔在了{cur.fetchone()['name']}（要回去得重新走过去）")
    return facts


def _fog(cur: Cursor, room_id: str, player: Optional[Player] = None) -> Optional[dict]:
    """这个房间有没有浓雾（迷雾葬海 theme.fog）：灯塔点亮了（整层 fog_clear）、雾笛吹散了（env.fog_off）、
    带着不怕雾的东西的人不算"""
    if not dungeon.is_dungeon(room_id):
        return None
    cfg = dungeon.data()["themes"][dungeon.floor_info(cur, room_id)["theme"]].get("fog")
    if not cfg or dungeon.floor_state(cur, room_id).get("fog_clear") or int(_room_env(cur, room_id).get("fog_off", 0)) > 0:
        return None
    if player and gear_has(cur, player, "fog_immune"):
        return None
    return cfg


def _start_distance(cur: Cursor, room_id: str) -> int:
    """进门时怪离你几格：平时 START_DISTANCE，浓雾里 fog.start_distance，渡魂者吹熄了灯就贴身"""
    env = _room_env(cur, room_id)
    if "fog_start" in env:
        return int(env["fog_start"])
    fog = _fog(cur, room_id)
    return int(fog.get("start_distance", START_DISTANCE)) if fog else START_DISTANCE


def _set_distance(st: Stealth, npc: Npc, d: int) -> int:
    d = max(0, min(MAX_DISTANCE, d))
    st.distance[npc.template.props.get("host") or str(npc.id)] = d
    return d


# ============ 负面效果（怪打中时附带，players.effects）============
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


def _on_you(name: str, label: str) -> str:
    """怪打中附带效果的说法：写成"你"的（"一截裹尸布缠住了你的手"）把"你"换成名字，别的接在名字后面"""
    return label.replace("你", name) if "你" in label else f"{name}{label}"


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


# ============ 环境（地牢房间 props.env，见 dungeon.yaml）============
# 光亮 0~100（村里没设环境的算 100）：低于 LIGHT_FULL 普通攻击命中按比例打折，0 时只剩 LIGHT_MIN_HIT；
# 低于 LIGHT_DARK 做花样难度 +1、躲藏容易 1 级。有人带火把 +TORCH_LIGHT；看不清（blind）的人光亮算 0。
# 越暗怪越凶、钱越多（_dark_factor）；怕光的怪在 LIGHT_BRIGHT 以上攻击 -1。
# 积水：闪避效果减半、逃跑难度 +1。掩体：躲藏容易 1 级
TORCH_LIGHT = 35                        # 说明用；实际按 world.yaml 火把（点燃）/（弱光）的 props.light
LIGHT_OLD = {"bright": 70, "dim": 40, "dark": 15}      # 旧存档里的文字写法


def _room_env(cur: Cursor, room_id: str) -> dict:
    cur.execute("select props->'env' as env from rooms where id = %s", (room_id,))
    row = cur.fetchone()
    return (row and row["env"]) or {}


def _base_light(env: dict) -> int:
    level = env.get("light", 100) if env else 100
    level = LIGHT_OLD.get(level, 40) if isinstance(level, str) else int(level)
    return max(0, min(100, level + int(env.get("shift", 0)))) if env else level       # 梦境的浮动、星光的明暗


def _light(cur: Cursor, room_id: str, env: Optional[dict] = None, player: Optional[Player] = None) -> int:
    """房间现在的光亮 0~100：房间本身（含点着的灯）+ 有人带火把。看不清（blind）不在这里算，是命中减半（BLIND_HIT）"""
    env = _room_env(cur, room_id) if env is None else env
    level = _base_light(env)
    if env and level < 100:
        # 点着的火把（props.burning）取最亮的一支，永久光源（矿工灯、提灯）也只取最亮的一件，两样叠加
        cur.execute("""select max((t.props->>'light')::int) filter (where t.props ? 'burning') as torch,
                              greatest(max((t.props->>'light')::int) filter (where not t.props ? 'burning'),
                                       max((i.props->>'gem_light')::int)) as lamp
                       from item_instances i join item_templates t on t.id = i.template_id
                       join players p on p.id = i.player_id
                       where p.room_id = %s and (t.props ? 'light' or i.props ? 'gem_light') and i.equipped_slot is not null""",
                    (room_id,))
        row = cur.fetchone()
        torch, lamp = row["torch"] or 0, row["lamp"] or 0
        if (torch or lamp) and (fog := _fog(cur, room_id)):
            cur.execute("""select 1 from item_instances i join item_templates t on t.id = i.template_id join players p on p.id = i.player_id
                           where p.room_id = %s and i.equipped_slot is not null and (t.props->>'fog_ignore')::boolean limit 1""",
                        (room_id,))
            mult = 1.0 if cur.fetchone() else fog.get("torch_mult", 0.5)        # 灯塔的提灯在雾里照常亮
            torch, lamp = round(torch * mult), round(lamp * mult)
        level += torch + lamp
    if player and env and env.get("shift") and gear_has(cur, player, "star_cycle_immune"):
        level -= int(env["shift"])              # 不受星光明暗（梦境浮动）影响，按房间本来的光亮算
    if env and (scroll := env.get("scroll")):
        cur.execute("select 1 from players where id = %s and room_id = %s", (scroll["by"], room_id))
        if cur.fetchone():
            level += scroll["value"]
    return max(0, min(100, level))


def light_info(cur: Cursor, player: Player, room: Room) -> Optional[dict]:
    """给界面看的光亮：数值、说法、这个亮度现在对这个人有什么影响、光从哪来。没设环境的房间（村里）是 None"""
    env = room.props.get("env")
    if not env:
        return None
    base = _base_light(env)
    light = _light(cur, room.id, env, player=player)
    lines = []
    if _effect(player, "blind"):
        lines.append(f"你看不清东西，这一回合命中 ×{BLIND_HIT}")
    hit_light = max(light, LIGHT_FULL) if gear_has(cur, player, "darkvision") and not _effect(player, "blind") else light
    hit = round(light_hit(hit_light, MELEE_HIT[0]) * _blind(player) * 100)
    lines.append(f"你贴身普通攻击的命中 {hit}%" + ("（夜视，不受暗处影响）" if hit_light > light
                                                  else "（亮度 50 以上不打折）" if light >= LIGHT_FULL else "（越暗越低，带火把能补）"))
    if light < LIGHT_DARK:
        lines.append("几乎漆黑：做花样难一级，躲起来容易一级")
    atk = dark_attack(light)
    lines.append("怪下手更狠（攻击 +10）" if atk > 0 else "怪被照得缩手缩脚（攻击 −10）" if atk < 0 else "怪的攻击正常")
    if light >= LIGHT_BRIGHT:
        lines.append("怕光的怪攻击再 −1")
    lines.append(f"打怪掉的钱 ×{1 + 0.5 * dark_factor(light):.2f}（越暗越多）")
    parts = [f"房间 {base}"]
    cur.execute("""select max((t.props->>'light')::int) filter (where t.props ? 'burning') as torch,
                          greatest(max((t.props->>'light')::int) filter (where not t.props ? 'burning'),
                                   max((i.props->>'gem_light')::int)) as lamp
                   from item_instances i join item_templates t on t.id = i.template_id join players p on p.id = i.player_id
                   where p.room_id = %s and (t.props ? 'light' or i.props ? 'gem_light') and i.equipped_slot is not null""",
                (room.id,))
    row = cur.fetchone()
    if row["torch"]:
        parts.append(f"火把 +{row['torch']}")
    if row["lamp"]:
        parts.append(f"随身的光 +{row['lamp']}")
    if (scroll := env.get("scroll")) and light > base:
        parts.append(f"光明卷轴 +{scroll['value']}")
    if env.get("ground") == "water":
        lines.append("地上积水：闪避效果减半，逃跑难一级")
    if env.get("cover"):
        lines.append("有掩体：躲起来容易一级")
    return {"value": light, "word": dungeon.light_word(light), "source": "，".join(parts), "lines": lines}


def env_text(cur: Cursor, room: Room) -> str:
    """给 AI 看的环境说明，没设环境的房间是空的"""
    env = room.props.get("env")
    return dungeon.env_text(env, _light(cur, room.id, env)) if env else ""


def _save_stealth(cur: Cursor, player: Player, st: Stealth) -> None:
    if st.detected:
        st.alerted = True                   # 发现过一次就一直戒备着，躲起来也只是暂时找不到，偷袭不了
    cur.execute("update players set stealth = %s where id = %s", (Jsonb(st.model_dump()), player.id))


def _enemies(cur: Cursor, room_id: str) -> list[Npc]:
    """在场、活着的敌人"""
    return [n for n in load_npcs(cur, f"n.room_id = %s and n.alive and {AWAKE_SQL}", (room_id,), lock=True)
            if n.template.hostile and n.combatable]


# 沉睡的（石面守像，props.dormant）：房间声响到 wake_noise 或者挨了打才醒（npcs.tally.awake），醒之前不算敌人
AWAKE_SQL = "not (coalesce((t.props->>'dormant')::boolean, false) and not coalesce((n.tally->>'awake')::boolean, false))"


def _sand(cur: Cursor, player: Player) -> Optional[dict]:
    """这个人脚下是流沙（ground: sand，本主题 theme.sand 的规则），沙行靴不怕"""
    if _room_env(cur, player.room_id).get("ground") != "sand" or not dungeon.is_dungeon(player.room_id) \
            or gear_has(cur, player, "sand_immune"):
        return None
    return dungeon.data()["themes"][dungeon.floor_info(cur, player.room_id)["theme"]].get("sand") \
        or {"move_max": 1, "stuck_chance": 0.3, "athletics_step": 0.05, "sink_after": 1}


SAND_STILL = "_sand_still"              # players.flags：在流沙里站着没挪窝几轮了


def _sand_sink(cur: Cursor, player: Player, done: list) -> list[str]:
    """流沙：战斗里一轮都没挪（走、靠近退开、闪避、挣脱）就记一轮，超过 sink_after 轮往下陷（缠住）"""
    sand = _sand(cur, player)
    if not sand or player.hp <= 0:
        return []
    if any(a.action in ("move", "maneuver", "dodge", "struggle", "stand") for a, _ in done):
        cur.execute("update players set flags = flags - %s where id = %s", (SAND_STILL, player.id))
        return []
    still = int(player.flags.get(SAND_STILL, 0)) + 1
    if still <= sand.get("sink_after", 1):
        cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::int) where id = %s",
                    (SAND_STILL, still, player.id))
        return [f"（{player.name}脚下的沙子在慢慢往下陷：下一轮再不挪一挪就要陷进去了）"]
    cur.execute("update players set flags = flags - %s where id = %s", (SAND_STILL, player.id))
    return _inflict(cur, load_player(cur, player.id), "restrained", "陷进了流沙，半截身子埋在沙里", 0, "流沙", escape=1)


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


def do_dodge(cur: Cursor, player: Player, view: RoomView, a: Dodge) -> list[str]:
    """闪避：打敌人时在 enemy_turn 里算；决斗中记下来，对手下一次攻击他时生效"""
    cur.execute("""update duels set dodging = array_append(array_remove(dodging, %(p)s), %(p)s)
                   where accepted and (challenger = %(p)s or target = %(p)s)""", {"p": player.id})
    return [f"{player.name}{a.description or '摆好架势，准备闪避'}"]


STILL_ACTIONS = {"hide", "look", "close_eyes", "reject"}     # 不算"动了身子"的（聆听者听不见）


def _heard(enemies: list[Npc], done: list) -> Optional[str]:
    """聆听者（props.hearing）：躲着的人这一轮除了躲藏还动了身子，就听得出他在哪"""
    ears = [n for n in enemies if n.template.props.get("hearing") and n.status is None]
    if ears and any(a.action not in STILL_ACTIONS for a, _ in done):
        return f"{ears[0].name}的大耳朵一张，转向了{{who}}：它听见了动静"
    return None


def _dodge_bonus(player: Player) -> float:
    """闪避让对方这一下命中率降多少：察觉越高降得越多"""
    return DODGE_BONUS + DODGE_PER_LEVEL * skill_level(player.skills.get("perception", 0))


def _evade(player: Player, dodged: bool) -> float:
    """这一轮对方命中率降多少：闪避了按察觉算，另外加许愿池的闪避祝福"""
    return (_dodge_bonus(player) if dodged else 0.0) + _bless(player, "dodge")


PERCEPTION_DETECT = {-1: 0.5, 0: 1.0, 1: 1.5}     # 怪的察觉（迟钝 / 普通 / 敏锐）：被发现的几率乘多少


def _perception(npc: Npc) -> int:
    return max(-1, min(1, int(npc.template.props.get("perception", 0))))


def _sharpness(enemies: list[Npc]) -> float:
    """这群怪里最敏锐的那只决定被发现的几率倍数"""
    return PERCEPTION_DETECT[max([_perception(n) for n in enemies] or [0])]


def _keen_line(npc: Npc, who: str) -> str:
    """警觉的怪在场、藏不住：按它为什么警觉换说法（以前一律"鼻子灵"，骷髅也鼻子灵很出戏）"""
    p = npc.template.props
    if p.get("dungeon", {}).get("rank") == "boss":
        return f"{npc.name}早就察觉到{who}了，藏不住"
    if p.get("animal"):
        return f"鼻子灵的{npc.name}一直追着{who}的气味，藏不住"
    if "gargoyle" in npc.template.id:
        return f"{npc.name}一动不动地盯着{who}，藏不住"
    if p.get("undead"):
        return f"{npc.name}空洞的眼眶一直对着{who}，藏不住"
    return f"{npc.name}的眼睛一直跟着{who}，藏不住"


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


def _turn_over(cur: Cursor, player_id: UUID, room_id: str) -> list[str]:
    """一个敌人回合结束：闭眼、掐自己的清掉；梦境回廊的光亮浮动、距离打乱"""
    cur.execute("update players set flags = flags - %s - %s where id = %s", (EYES_CLOSED, PINCHED, player_id))
    return []


def _room_turn(cur: Cursor, room_id: str) -> list[str]:
    """这个房间的敌人回合过了一轮：光亮浮动（light_jitter）、距离打乱（dislocate）"""
    if not dungeon.is_dungeon(room_id):
        return []
    theme = dungeon.data()["themes"][dungeon.floor_info(cur, room_id)["theme"]]
    env = _room_env(cur, room_id)
    facts, upd = [], {}
    if j := theme.get("light_jitter"):
        upd["shift"] = random.randint(-j, j)
    if cyc := theme.get("star_cycle"):
        n = int(env.get("turns", 0)) + 1
        upd["turns"] = n
        period = int(env.get("period") or cyc.get("period", 4))
        bright = (n // period) % 2 == 0
        upd["shift"] = cyc.get("swing", 35) * (1 if bright else -1)
        if bright != (int(env.get("shift", 0)) >= 0):
            facts.append(f"头顶那颗魔星{'亮了起来，星光洒满石台' if bright else '暗了下去，四周一下子黑了'}（光亮 {_light(cur, room_id, env | upd)}）")
        left = period - n % period
        for p in load_players_in(cur, room_id):
            if any(e.kind == "bless" and e.stat == "star_chart" for e in p.effects):
                facts.append(f"（{p.name}对着星图数了数：星光还有 {left} 个回合变{'暗' if bright else '亮'}）")
    if int(env.get("fog_off", 0)) > 0:
        upd["fog_off"] = int(env["fog_off"]) - 1
        if upd["fog_off"] == 0:
            facts.append("雾又慢慢合拢了")
    if dis := theme.get("dislocate"):
        n = int(env.get("turns", 0)) + 1
        upd["turns"] = n
        if n % dis.get("every", 3) == 0 and _enemies(cur, room_id):
            moved = []
            for p in load_players_in(cur, room_id):
                if gear_has(cur, p, "dislocate_immune"):
                    continue
                st = _stealth(p)
                for npc in _enemies(cur, room_id):
                    _set_distance(st, npc, random.randint(0, 2))
                _save_stealth(cur, p, st)
                moved.append(p.name)
            if moved:
                facts.append("四周像梦一样晃了一下，再睁眼时每个人和怪的位置都变了（距离全乱了）")
    if upd:
        cur.execute("update rooms set props = jsonb_set(props, '{env}', coalesce(props->'env', '{}'::jsonb) || %s) where id = %s",
                    (Jsonb(upd), room_id))
    return facts


def load_players_in(cur: Cursor, room_id: str) -> list[Player]:
    cur.execute("select id from players where room_id = %s and hp > 0", (room_id,))
    return [load_player(cur, r["id"]) for r in cur.fetchall()]


def _glare(cur: Cursor, player: Player, room_id: str) -> list[str]:
    """炫光（水晶洞窟 env.glare）：光亮到 at 以上，每个敌人回合 chance 几率被晃得看不清一回合；闭着眼的不会"""
    g = _room_env(cur, room_id).get("glare")
    if not g or player.hp <= 0 or player.flags.get(EYES_CLOSED) or _light(cur, room_id) < g.get("at", 70) \
            or gear_has(cur, player, "glare_immune"):
        return []
    if not _roll(g.get("chance", 0.25)) or _effect(player, "blind"):
        return []
    return _inflict(cur, player, "blind", "被晶面反射的光晃花了眼", 0, "炫光")


def do_hide(cur: Cursor, player: Player, view: RoomView, a: Hide) -> list[str]:
    """躲起来（隐匿）：成功了几率不再上涨；已经被发现的，躲成功就甩掉了（难度高一级）"""
    st = _stealth(player)
    foes = [n for n in _enemies(cur, player.room_id) if n.status is None]
    if hunter := next((n for n in foes if n.template.props.get("hide_fails")), None):
        # 英仙座：在他面前躲藏必定失败，还让他下一击更狠
        _tally_merge(cur, hunter, {"hide_punish": str(player.id)})
        st.detected, st.hidden = True, False
        _save_stealth(cur, player, st)
        return [f"{player.name}想找地方藏起来", f"{hunter.name}笑了一声：“你藏的那点本事，是我玩剩下的。”（下一击冲着{player.name}，更狠）"]
    if keen := next((n for n in foes if n.template.props.get("keen")), None):
        raise ActionError(_keen_line(keen, player.name))
    env = _room_env(cur, player.room_id)
    easier = bool(env.get("cover")) + (_light(cur, player.room_id, env) < LIGHT_DARK)
    # 最敏锐的那只怪说了算（迟钝 -1、敏锐 +1）；手里拿着点着的火把 +1
    easier += int((_fog(cur, player.room_id) or {}).get("stealth_bonus", 0))       # 浓雾里好躲
    diff = a.difficulty + st.detected - easier + max([_perception(n) for n in foes] or [0]) + _holds_fire(cur, player)
    # 战斗中躲藏的下限：已经被发现至少 HIDE_SEEN，有怪贴身至少 HIDE_CLOSE（以前 AI 给 1–3，隐匿 4 级 95% 必成）
    if st.detected:
        diff = max(diff, HIDE_SEEN)
    if any(_distance(st, n) == 0 for n in foes):
        diff = max(diff, HIDE_CLOSE)
    ok, rolled = _check(cur, player, view, "stealth", max(1, min(10, diff)))
    facts = [f"{player.name}尝试：{a.description or '躲起来'}"] + rolled
    if not ok:
        return facts + [f"{player.name}没能藏好"]
    st.hidden, st.detected = True, False
    _save_stealth(cur, player, st)
    for n in foes:
        if st.alerted and n.template.props.get("dungeon", {}).get("rank") in ("boss", "elite"):
            _count(cur, n, "hides")             # fight_log：打它的时候躲过
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


# ============ 战斗回合（tick）============
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


def _tally(cur: Cursor, npc: Npc, key: str) -> int:
    cur.execute("select coalesce((tally->>%s)::int, 0) as n from npcs where id = %s", (key, npc.id))
    return cur.fetchone()["n"]


def _count(cur: Cursor, npc: Npc, key: str) -> int:
    """这只怪这场的某个本事用了一次，返回用过几次了"""
    cur.execute("""update npcs set tally = tally || jsonb_build_object(%s::text, coalesce((tally->>%s)::int, 0) + 1)
                   where id = %s returning (tally->>%s)::int as n""", (key, key, npc.id, key))
    return cur.fetchone()["n"]


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


# ============ 远程武器 ============
# props.ranged 的武器：贴身难射，离得越远越准（RANGED_HIT）；steady 的（麦琪的弩）远近都是 STEADY_HIT。
# 射完换成 props.unloaded 那个"空"的样子（弩没弦），说"装填"换回来（props.loads），算一个动作。
# 射的时候只算这件远程武器的伤害；"空"的远程武器不算近战武器
# 命中表：rules.RANGED_HIT、STEADY_HIT、BLIND_HIT、SMOKE_*


WEAK_WORDS = {"fire": "怕火", "pierce": "甲缝", "light": "怕光", "bright": "怕光", "holy": "怕圣物", "poison": "怕毒", "water": "怕水"}


def _weak_spot(cur: Cursor, player: Player, npc: Npc, melee: list[ItemInstance], shooter: Optional[ItemInstance],
               loaded: Optional[str], fired: list[dict]) -> Optional[str]:
    """这一下打没打在弱点上（头目的 props.weak）：带火的（火把、火油箭、余烬长剑）、破甲的、强光下（光亮 70 以上）"""
    weak = npc.template.props.get("weak")
    if not weak or weak in (npc.template.props.get("resist_element") or []):
        return None
    hit = False
    if any(e.get("do") == "element" and e.get("kind") == weak for e in fired):
        return WEAK_WORDS.get(weak, weak)       # 余烬石：这一击算作火
    if weak == "fire":
        ammo_fire = False
        if loaded:
            cur.execute("select (props->>'fire')::boolean as f from item_templates where id = %s", (loaded,))
            ammo_fire = bool((cur.fetchone() or {}).get("f"))
        hit = ammo_fire or any(_prop(w, "fire") for w in ([shooter] if shooter else melee))
    elif weak == "pierce":
        hit = any(e.get("do") == "pierce" for e in fired)
    elif weak in ("light", "bright"):
        hit = _light(cur, npc.room_id) >= LIGHT_BRIGHT
    return WEAK_WORDS.get(weak, weak) if hit else None


def _choose_weapons(weapons: list[ItemInstance], d: int, want: Optional[ItemInstance]
                    ) -> tuple[list[ItemInstance], Optional[ItemInstance]]:
    """这一下用什么打：(近战用的武器, 射的远程武器)。说了用哪件就用哪件；
    没说：手上有装好的远程武器，对方又没贴身（或者这件远近都准、或者手上没有别的武器）就射，不然近战"""
    loaded = [w for w in weapons if _prop(w, "ranged")]
    melee = [w for w in weapons if not _prop(w, "ranged") and not _prop(w, "loads")]
    if want is not None:
        if _prop(want, "loads"):
            raise ActionError(f"{want.name}还没装填，射不了（先说「装填」）")
        if _prop(want, "ranged"):
            return [], want
        # 说了用哪把近战武器也按面板打：双持两把都算；AI 解析时指了背包里没拿着的那把，也照手上的打
        # （以前报"没拿在手上"，这一轮白白浪费）
        return melee, None
    if loaded and (d > 0 or not melee or _prop(loaded[0], "steady")):
        return [], loaded[0]
    return melee, None


def _how(player: Player, melee: list[ItemInstance], shooter: Optional[ItemInstance]) -> tuple[str, int]:
    """(怎么打的说法, 攻击力)"""
    cheer = 1 + (_effect(player, "cheer").value / 100 if _effect(player, "cheer") else 0) + _bless(player, "atk_pct")
    if shooter:
        verb = f"从{shooter.name}里抽出一把掷向" if _prop(shooter, "thrown") else f"端起{shooter.name}射向"
        return verb, int((player.attack + shooter.damage) * cheer + 0.5)
    return f"用{_wielding(melee)}攻击", int((player.attack + weapon_damage(melee)) * cheer + 0.5)


def _hit_base(shooter: Optional[ItemInstance], d: int) -> float:
    if shooter is None:
        return MELEE_HIT.get(d, 0)
    return STEADY_HIT if _prop(shooter, "steady") else RANGED_HIT.get(d, RANGED_HIT[max(RANGED_HIT)])


def _swap_template(cur: Cursor, item: ItemInstance, template_id: str) -> str:
    """换成另一个模板（弩：装好的 ↔ 空的），升级过的名字跟着换（麦琪的轻弩 +1 ↔ 麦琪的轻弩（空） +1），返回新名字。
    装着的弹药、弹匣剩几发、绞盘摇了几圈这些一换就清掉"""
    cur.execute("select name from item_templates where id = %s", (template_id,))
    base = cur.fetchone()["name"]
    props = {k: v for k, v in item.props.items() if k not in ("loaded_ammo", "shots", "wound")}
    if props.get("plus"):
        props["name"] = f"{base} +{props['plus']}"
    else:
        props.pop("name", None)
    cur.execute("update item_instances set template_id = %s, props = %s where id = %s", (template_id, Jsonb(props), item.id))
    return props.get("name", base)


def _spend(cur: Cursor, player: Player, shooter: Optional[ItemInstance]) -> list[str]:
    """射出去了：弹匣（props.magazine，连弩、飞刀）还有剩就少一发；空了换成空的样子，要重新装填。
    箭袋腰带（free_reload）每场白给一次装填：第一次射空时顺手就装好了"""
    if shooter is None or not (empty := _prop(shooter, "unloaded")):
        return []
    if mag := _prop(shooter, "magazine"):
        left = shooter.props.get("shots", mag) - 1
        if left > 0:
            cur.execute("update item_instances set props = props || %s where id = %s", (Jsonb({"shots": left}), shooter.id))
            return [f"{shooter.name}还剩 {left} 发"]
    st = _stealth(player)
    if not mag and not st.reloaded and gear_has(cur, player, "free_reload"):
        st.reloaded = True
        _save_stealth(cur, player, st)
        if shooter.props.get("loaded_ammo"):
            cur.execute("update item_instances set props = props - 'loaded_ammo' where id = %s", (shooter.id,))
        return [f"{player.name}手一垂就从箭袋腰带里摸出一支，顺手又装好了（这一场用过了）"]
    name = _swap_template(cur, shooter, empty)
    if _prop(shooter, "thrown"):
        return [f"{name}：都扔出去了，打完这一架（房间里没有敌人了）自然会捡回来"]
    return [f"{name}射空了，要重新装填（说「装填」）才能再射"]


def _ammo_fx(cur: Cursor, shooter: Optional[ItemInstance], npc: Optional[Npc]) -> list[dict]:
    """装着的特殊弹药（钩索箭、火油箭、铅弹）这一发带上的效果：只对某类怪（vs）的对不上就不算"""
    if shooter is None or not (ammo := shooter.props.get("loaded_ammo")):
        return []
    cur.execute("select props->'effects' as fx from item_templates where id = %s", (ammo,))
    row = cur.fetchone()
    return [e for e in (row["fx"] if row else None) or []
            if not e.get("vs") or (npc and any(npc.template.props.get(t) for t in e["vs"]))]


def _weapon_fit(fired: list[dict], shooter: Optional[ItemInstance]) -> list[dict]:
    """只对远程（weapon: ranged）或只对近战（weapon: melee）的效果（猎手之戒）：这一下对不上就不算"""
    kind = "ranged" if shooter else "melee"
    return [e for e in fired if e.get("weapon") in (None, kind)]


def _cover(cur: Cursor, room_id: str, shooter) -> float:
    """有掩体的房间，远程的命中 −RANGED_COVER（对双方都一样）；近战不管"""
    return RANGED_COVER if shooter and _room_env(cur, room_id).get("cover") else 0.0


def _smoky(cur: Cursor, room_id: str) -> bool:
    return (_room_env(cur, room_id).get("smoke") or 0) > 0


def _shot_smoke(cur: Cursor, room_id: str, shooter) -> float:
    """烟雾弹的烟没散：远程命中减半（对双方都一样）"""
    return SMOKE_HIT if shooter and _smoky(cur, room_id) else 1.0


def _blind(player: Player) -> float:
    return BLIND_HIT if _effect(player, "blind") else 1.0


def _recover_thrown(cur: Cursor, player: Player) -> list[str]:
    """扔出去的飞刀（空了、props.recover）：房间里没有敌人了就捡回来"""
    thrown = [i for i in load_items(cur, "i.player_id = %s", (player.id,)) if _prop(i, "recover") and _prop(i, "loads")]
    if not thrown or _enemies(cur, player.room_id):
        return []
    return [f"{player.name}把扔出去的{_swap_template(cur, i, _prop(i, 'loads'))}一把把捡了回来" for i in thrown]


def do_reload(cur: Cursor, player: Player, view: RoomView, a: Reload) -> list[str]:
    """装填：空的换回装好的。带了特殊弹药就装上它（下一发带它的效果，弹药用掉一个）；
    要摇好几次的（绞盘重弩 props.reload_steps）每次摇一点；飞刀扔完了要打完这一架才能捡回来"""
    ammo = _inv_item(cur, view, player, a.ammo) if a.ammo else None
    if ammo and not _prop(ammo, "ammo"):
        raise ActionError(f"{ammo.name}不是能装的弹药")
    if a.item:
        item = _inv_item(cur, view, player, a.item)
        if not _prop(item, "loads"):
            if _prop(item, "ranged") and ammo:
                raise ActionError(f"{item.name}已经装好了，射出去才能换装{ammo.name}")
            raise ActionError(f"{item.name}用不着装填" if not _prop(item, "ranged") else f"{item.name}已经装好了")
    else:
        empties = [i for i in view.inventory if _prop(i, "loads")]
        if ammo:
            empties = [i for i in empties if _prop(i, "ammo_kind") == _prop(ammo, "ammo")] or empties
        if not empties:
            if ammo and any(_prop(i, "ammo_kind") == _prop(ammo, "ammo") and _prop(i, "ranged") for i in view.inventory):
                raise ActionError(f"已经装好了，射出去才能换装{ammo.name}")
            raise ActionError(f"{player.name}身上没有要装填的东西")
        item = sorted(empties, key=lambda i: i.equipped_slot is None)[0]     # 拿在手上的先装
    if _prop(item, "recover"):
        raise ActionError(f"{item.name}都扔出去了，打完这一架（房间里没有敌人了）自然会捡回来")
    if ammo and _prop(item, "ammo_kind") != _prop(ammo, "ammo"):
        raise ActionError(f"{ammo.name}装不进{item.name}")
    steps = _prop(item, "reload_steps") or 1
    wound = item.props.get("wound", 0) + 1
    if wound < steps:
        cur.execute("update item_instances set props = props || %s where id = %s", (Jsonb({"wound": wound}), item.id))
        return [f"{player.name}咬着牙摇了几圈绞盘，弦才上到一半（还要再装填 {steps - wound} 次）"]
    name = _swap_template(cur, item, _prop(item, "loads"))
    if ammo:
        _consume(cur, ammo)
        cur.execute("update item_instances set props = props || %s where id = %s", (Jsonb({"loaded_ammo": ammo.template.id}), item.id))
        return [f"{player.name}把一支{ammo.name}装了上去，{name}又能射了（下一发带上它的效果）"]
    return [f"{player.name}拉开弦、装好了，{name}又能射了"]


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


def do_rest(cur: Cursor, player: Player, view: RoomView, a: Rest) -> list[str]:
    """住店：这里有开店的 NPC（world.yaml 的 props.inn）就付钱睡一觉，回满血、醒酒、状态清掉"""
    host = next((n for n in view.npcs if n.template.props.get("inn") and n.status is None), None)
    if host is None:
        raise ActionError("这里没有能住的地方")
    price = host.template.props["inn"].get("price", 0)
    patron = _pay(cur, player, price, host)
    cur.execute(f"""update players set hp = max_hp + {RESTORE_MAX_HP}, max_hp = max_hp + {RESTORE_MAX_HP}, effects = coalesce((select jsonb_agg(e) from jsonb_array_elements(effects) e where e->>'kind' in ('whet', 'cheer')), '[]'::jsonb),
                   status = null, drunk_until = null, drinks = 0, updated_at = now()
                   where id = %s""", (player.id,))
    return [f"{player.name}付了 {price} 金币，在{host.name}这儿要了间房，美美睡了一觉",
            f"{player.name}精神饱满，HP {player.max_hp}/{player.max_hp}"
            + ("，酒也醒了" if player.drunk else "")] + patron


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
        hp = min(target.max_hp, (1 + skill_level(player.skills.get("medicine", 0))) * SCALE)
        cur.execute(f"""update players set hp = %s, max_hp = max_hp + {RESTORE_MAX_HP}, effects = coalesce((select jsonb_agg(e) from jsonb_array_elements(effects) e where e->>'kind' in ('whet', 'cheer')), '[]'::jsonb), updated_at = now()
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
    """退队；跟着他的人不再跟着他（还留在队里），队伍只剩一个人就解散"""
    cur.execute("update players set party_id = null where id = %s", (player.id,))
    cur.execute("update players set following = null where following = %s", (player.id,))
    cur.execute(
        """update players set party_id = null
           where party_id = %(p)s and (select count(*) from players where party_id = %(p)s) = 1""",
        {"p": player.party_id},
    )


UPGRADE_NUDGE = "_upgrade_nudge"         # players.flags：莉娜提醒过升级了（下划线开头的是内部记号，侧栏不显示）
NUDGE_GOLD, NUDGE_DEPTH = 20, 3


BOW_NUDGE = "_bow_nudge"


def _bow_nudge(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """第一次拿着弓弩回村：麦琪顺口提一句，拿远程的最好找人挡在前面（每人一次）"""
    if not npc.template.props.get("inn") or player.flags.get(BOW_NUDGE) or dungeon.is_dungeon(player.room_id):
        return []
    bow = next((w for w in _weapons(cur, player) if _prop(w, "ranged") or _prop(w, "loads")), None)
    if bow is None:
        return []
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, true) where id = %s", (BOW_NUDGE, player.id))
    player.flags[BOW_NUDGE] = True
    return [f"{npc.name}瞄了一眼{player.name}手上的{bow.name}，晃着酒杯说拿这玩意儿的杂鱼一个人下去，被怪贴上身就只能干瞪眼，"
            f"最好找个皮厚的挡在前面（拿远程武器的，跟近战的人组队最稳）"]


NUDGE_AGAIN = 5                         # 提醒过以后，最深层数每再深这么多、条件还满足，就再提一次（"忘了"的人也能再听到）


def _deepest(cur: Cursor, player: Player) -> int:
    cur.execute("select deepest_floor from players where id = %s", (player.id,))
    return cur.fetchone()["deepest_floor"]


def _nudge_due(cur: Cursor, player: Player, key: str) -> bool:
    """这条提醒现在该不该说：没提过，或者上次提的时候最深层数比现在浅 NUDGE_AGAIN 层以上（flags 里记着上次的层数；
    以前记的 true 当作第 0 层）"""
    last = player.flags.get(key)
    return last is None or last is False or _deepest(cur, player) >= int(last) + NUDGE_AGAIN


def _nudge_mark(cur: Cursor, player: Player, key: str) -> None:
    deepest = _deepest(cur, player)
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::int) where id = %s", (key, deepest, player.id))
    player.flags[key] = deepest


# 更新告示：改了玩法就往 NEWS 前面加一条（版本号、麦琪的八卦、更新说明），玩家上线后第一次进酒馆听到最新那条
# （不然玩家只觉得"被热补丁削弱了"）。看过的版本记在 players.flags._news
NEWS = [
    ("2026-09-27", "诺艾尔的店里多了一盏小油灯，她说是照着书上的图样做的。",
     "诺艾尔卖腰挂油灯了（挂在腰上不占手，光亮 +15）；萤石更亮了，深层掉的炉芯、晶母之心、渡魂者的绿灯、法老面具、星陨剑、星盘"
     "也都会发光，越深的越亮。随身的光只算最亮的一件，火把另外叠加，留着应急。第 16 层往下，一个房间刷三群怪没那么常见了；"
     "在其山岳之上者面前能看到自己攒了几层罪"),
    ("2026-09-26d","有人说第二十五层的楼梯口站着一个不该出现在地牢里的东西，没人从它面前走过去。",
     "每 25 层有一位关卡头目，打不倒就下不去：在其山岳之上者会称量你的心（这场里喝药、闪避、后退、逃跑都会让天平那一端更沉），"
     "英仙座会隐身、会飞（飞着的时候拿绳子、网、钩索箭或者抓他的脚踝把他拽下来）、会用大陵五的光把人石化（闭眼能躲），在他面前藏不住。"
     "楼梯间隔壁的前厅有传送石和休息的地方，倒下了能直接传回来重来，每失败一次它下次少一点血；到过更深的人也能回来挑战。"
     "打倒了有称号和专属的装备"),
    ("2026-09-26c", "往下走的人越来越多，带回来的故事也越来越怪。",
     "第 16 层起新增静默神殿：出声会引来东西，说话、念书、砸东西都会攒声响，沉睡的石像会被吵醒。"
     "第 21 层起新增沙漠迷城（路会变、流沙站着不动会往下陷、裹尸布用火烧得断）、梦境回廊（光一明一暗、距离会乱、"
     "掐自己能不睡过去）、迷雾葬海（怪一进门就在跟前、渡魂者会伸手要船费）；第 26 层起新增星界秘境"
     "（魔星一明一暗，边上的石台被撞倒会掉下去）。深层装备多了不少新本事，晶粉能给诺艾尔当刷宝石的垫子"),
    ("2026-09-26b","挖矿的说第十六层往下有个洞，墙上长满了会发光的石头，看久了眼睛疼。",
     "新区域「水晶洞窟」（第 16 层起）：每间屋子亮度不一样，太亮的地方晶面反光会晃花眼；说「闭眼」能背过身躲一轮，代价是自己也看不见；"
     "打碎大晶簇房间会暗下来；棱镜元素会把箭折回来，共鸣者给同伴套一层能挡一下的光膜；"
     "晶母身上的晶簇活着时她硬得很，先把晶簇敲掉；晶脉里能撬宝石，镜厅里会走出你自己的倒影。"
     "过了第 16 层，所有区域都会随机出现"),
    ("2026-09-26","有人说第十六层往下挖到了一座矮人的熔炉，锤声到现在都没停……",
     "新区域「地底熔炉」（第 16 层起）：整层灼热，每个动作都掉一点血，喝泉水能撑 4 个动作，每层有两间冷却水池；"
     "这里的怪不怕火，破甲和泉水才好使；守着熔炉的是矮人王的铸像，会在冷却和熔化之间来回变，熔化的时候泼它一瓶泉水。"
     "深层装备（孔更多）、熔岩吊桥上的锻造纹拓片送给莉娜，深层装备能升到 +15"),
    ("2026-09-25b", "地牢深处的东西醒了。",
     "第 16 层起的头目一轮出手两次，会预告大招（这一轮狠狠砍它能打断，被盯上的人闪避能躲开一半）、"
     "一招挂好几个效果，血掉到一半会变阵：灯灭了、泥水涨上来、牢门封死、古树扎根回血……"
     "新的负面效果「重伤」：受到的治疗打折，药草、绷带解不了；第 11 层往下，防御堆得越高，挡的比例涨得越慢"),
    ("2026-09-25", "听说地牢里的东西最近变机灵了……",
     "躲起来时贴在身边、早发现你的怪还会摸黑乱挥；被发现过就偷袭不了，警觉的怪面前藏不住；"
     "中毒、流血按那一下打出的伤害算，防御也挡得住了，再中只多挂一回合；"
     "挣脱、爬起来以后这一轮不会马上又被同样的招控住；狗和狼一个房间最多两只，领头的一倒剩下的就跑；"
     "楼梯间的守卫都很警觉，绕不过去；升级失败不再掉级，连着失败越来越容易成；莉娜能把用不上的地牢装备拆成碎铁"),
]
NEWS_SEEN = "_news"


def _news(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """麦琪在酒馆里八卦最近的更新（每个版本每人一次）"""
    seen = player.flags.get(NEWS_SEEN)
    if not npc.template.props.get("inn") or not NEWS or seen == NEWS[0][0]:
        return []
    fresh = []                                  # 没看过的几条（新的在前，看到上次看过的那条为止）
    for entry in NEWS:
        if entry[0] == seen:
            break
        fresh.append(entry)
    player.flags[NEWS_SEEN] = NEWS[0][0]
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::text) where id = %s",
                (NEWS_SEEN, NEWS[0][0], player.id))
    return [f"{npc.name}一边擦杯子一边压低声音：“{fresh[0][1]}”"] + [f"（更新告示：{notes}）" for _, _, notes in fresh]


ARMOR_NUDGE, GEM_NUDGE, POTION_NUDGE = "_armor_nudge", "_gem_nudge", "_potion_nudge"
ARMOR_NUDGE_GOLD, POTION_NUDGE_DEPTH = 100, 8


def _armor_nudge(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """身上的防具一件都没升过、兜里有 100 金以上：莉娜提防具"""
    if not npc.template.props.get("upgrades") or player.gold < ARMOR_NUDGE_GOLD or dungeon.is_dungeon(player.room_id):
        return []
    worn = [i for i in _worn(cur, player) if i.template.type == "armor" and i.defense > 0]
    if not worn or any(i.props.get("plus") for i in worn) or not _nudge_due(cur, player, ARMOR_NUDGE):
        return []
    _nudge_mark(cur, player, ARMOR_NUDGE)
    return [f"{npc.name}敲了敲{player.name}身上的{worn[0].name}：“光磨刀不补甲，下面的怪可不跟你讲道理。”"
            f"（防具也能升级：说「升级{worn[0].name}」，+1 要 {upgrade_terms(worn[0])[1]} 金币）"]


def _gem_nudge(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """背包里有宝石、身上的装备有空孔：莉娜说孔空着也是空着"""
    if not npc.template.props.get("upgrades") or dungeon.is_dungeon(player.room_id):
        return []
    cur.execute("""select exists (select 1 from item_instances i join item_templates t on t.id = i.template_id
                                  where i.player_id = %(p)s and t.type = 'gem') as gem,
                          (select coalesce(i.props->>'name', t.name) from item_instances i join item_templates t on t.id = i.template_id
                           where i.player_id = %(p)s and coalesce((i.props->>'sockets')::int, 0)
                                 > coalesce(jsonb_array_length(i.props->'gems'), 0) limit 1) as holed""", {"p": player.id})
    r = cur.fetchone()
    if not (r["gem"] and r["holed"]) or not _nudge_due(cur, player, GEM_NUDGE):
        return []
    _nudge_mark(cur, player, GEM_NUDGE)
    return [f"{npc.name}瞄了一眼{player.name}的{r['holed']}：“孔空着也是空着。宝石揣在兜里又不会自己长上去。”"
            f"（镶宝石免费：说「把某某宝石镶到{r['holed']}上」）"]


def _potion_nudge(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """到过第 8 层以下、身上一瓶血药都没有：麦琪提一句"""
    if not npc.template.props.get("inn") or dungeon.is_dungeon(player.room_id):
        return []
    cur.execute("select 1 from item_instances where player_id = %s and template_id = 'blood_potion' limit 1", (player.id,))
    if cur.fetchone() or _deepest(cur, player) < POTION_NUDGE_DEPTH or not _nudge_due(cur, player, POTION_NUDGE):
        return []
    _nudge_mark(cur, player, POTION_NUDGE)
    return [f"{npc.name}上下打量了{player.name}一圈，撇撇嘴：“又空着手往下跑？连瓶血药都不带，等着我去捡你啊？”"
            "（杂货铺的诺艾尔卖血药，倒下的队友也能灌）"]


def _upgrade_nudge(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """带着钱回村、下过第 3 层、手上的武器还没升过：铁匠见到时主动提一句（之后每深 5 层还没升就再提）。
    不然新人不知道能升级，第 6 层撞墙也不知道为什么"""
    if (not npc.template.props.get("upgrades") or player.gold < NUDGE_GOLD
            or dungeon.is_dungeon(player.room_id)):
        return []
    if _deepest(cur, player) < NUDGE_DEPTH or not _nudge_due(cur, player, UPGRADE_NUDGE):
        return []
    weapon = next((w for w in _weapons(cur, player) if not w.props.get("plus") and not _prop(w, "lights")), None)
    if weapon is None:
        return []
    _nudge_mark(cur, player, UPGRADE_NUDGE)
    return [f"{npc.name}瞥见{player.name}手上那把没回过炉的{weapon.name}，皱起了眉头，像是看不下去"
            f"（她能帮你升级：说「升级{weapon.name}」，+1 要 {upgrade_terms(weapon)[1]} 金币）"]


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
    facts = ([f"{player.name}对{npc.name}说：“{message}”"] + _stele(cur, npc, player) + _upgrade_nudge(cur, player, npc)
             + _bow_nudge(cur, player, npc))
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
    if npc.template.props.get("fare"):
        due = _tally(cur, npc, "fare_due")
        if not due:
            raise ActionError(f"{npc.name}这会儿没伸手要钱")
        if a.amount < due:
            raise ActionError(f"{npc.name}要的是 {due} 金币，{player.name}只给了 {a.amount}")
        cur.execute("update players set gold = gold - %s where id = %s", (a.amount, player.id))
        _tally_merge(cur, npc, {"fare_paid": 1})
        return [f"{player.name}把 {a.amount} 金币放进了{npc.name}干枯的手心"]
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
BUY_GIFT = 0.2                          # 礼物道具（矿石、古酒、古书）谁收都只给两成：它的价值在送人换好感
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
    rate = (BUY_GIFT if _prop(item, "gift") else BUY_LIKED if liked else BUY_OTHER) \
        + max(-BUY_AFFINITY_MAX, min(BUY_AFFINITY_MAX, affinity // 10 * BUY_AFFINITY))
    return max(1, round(base_price(item_stats(item)) * rate)) * item.quantity


def buy_quotes(conn: Connection, player_id: UUID, npc: Npc, items: list[ItemInstance]) -> list[tuple[str, int]]:
    """给 AI 看的：她收玩家身上这些东西各给多少（玩家问收不收、值多少时照这个说）"""
    if not npc.template.props.get("buys"):
        return []
    with conn.transaction():
        cur = _cursor(conn)
        affinity = _affinity(cur, load_player(cur, player_id), npc)
        return [(i.name, buy_price(npc, i, affinity)) for i in items if not _precious(cur, i) and not _prop(i, "donated")]


def do_sell(cur: Cursor, player: Player, view: RoomView, a: Sell) -> list[str]:
    npc = _room_npc(cur, view, player, a.target)
    if not npc.template.props.get("buys"):
        raise ActionError(f"{npc.name}不收东西")
    item = _inv_item(cur, view, player, a.item)
    _no_curse(item, "拿")
    if _burning(item):
        raise ActionError(f"点着的{item.name}{npc.name}可不收")
    if _precious(cur, item):
        raise ActionError(f"{item.name}太要紧了，{npc.name}不收")
    if _prop(item, "donated"):
        raise ActionError(f"{item.name}是酒馆武器桶里别人留下的，{npc.name}不收（用不上了可以放回桶里）")
    price = buy_price(npc, item, _affinity(cur, player, npc))
    cur.execute("delete from item_instances where id = %s", (item.id,))
    cur.execute("update players set gold = gold + %s where id = %s", (price, player.id))
    return [f"{player.name}把{_label(item)}卖给了{npc.name}，得到 {price} 金币"]


LIKED_GIFT = 10                      # 送心爱的礼物加的好感（不受花钱加好感的上限）
GIFT_FACT = "心爱的礼物"                # 台词那边认这几个字，演出特别的反应
RETURNED_FACT = "她自己卖出去的那件"     # 从她那买的礼物又送回给她（诺艾尔的古书），台词那边演认出来


def do_give(cur: Cursor, player: Player, view: RoomView, a: Give) -> list[str]:
    item = _inv_item(cur, view, player, a.item)
    _no_curse(item, "拿")
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
    if (gift := _prop(item, "gift")) and (gift == "any" or gift in npc.template.props.get("gift_likes", [])):
        # 小礼物（props.gift_value，村里互相卖的怪酒、木炭、游记）：好感加得少，每人每天只收一件，跨得过好感的坎
        if (unlock := _prop(item, "unlock")) and npc.template.props.get("upgrades") and not player.flags.get(unlock):
            _consume(cur, item)
            new = min(100, _affinity(cur, player, npc) + int(_prop(item, "gift_value") or 0))
            cur.execute("""insert into player_npc_relations (player_id, npc_template, affinity) values (%s, %s, %s)
                           on conflict (player_id, npc_template) do update set affinity = excluded.affinity""",
                        (player.id, npc.template.id, new))
            cur.execute("update players set flags = flags || jsonb_build_object(%s::text, true) where id = %s", (unlock, player.id))
            return [f"{player.name}把{item.name}送给了{npc.name}",
                    f"{npc.name}盯着拓片上的锻造纹看了很久，手指跟着纹路一圈圈描过去，最后低声说了句：“……原来是这么打的。”",
                    f"（莉娜学会了深层锻造：深层装备（第 16 层以后掉的）能升到 +{UPGRADE_DEEP_MAX}）",
                    f"{npc.name}对{player.name}的好感上升（当前 {new}）"]
        if small := _prop(item, "gift_value"):
            cur.execute("""select 1 from player_npc_relations where player_id = %s and npc_template = %s
                           and gift_day = (now() at time zone 'Asia/Shanghai')::date""", (player.id, npc.template.id))
            if cur.fetchone():
                raise ActionError(f"{npc.name}今天已经收过{player.name}的小礼物了，明天再送")
            _consume(cur, item)
            new = min(100, _affinity(cur, player, npc) + int(small))
            cur.execute("""insert into player_npc_relations (player_id, npc_template, affinity, gift_day)
                           values (%s, %s, %s, (now() at time zone 'Asia/Shanghai')::date)
                           on conflict (player_id, npc_template) do update
                           set affinity = excluded.affinity, gift_day = excluded.gift_day""",
                        (player.id, npc.template.id, new))
            facts = [f"{player.name}把{item.name}送给了{npc.name}，是她喜欢的小东西",
                     f"{npc.name}对{player.name}的好感上升（当前 {new}，收到小礼物）"]
            if (lore := LORE.get(_prop(item, "lore"))) and lore[0] == npc.template.id:
                # 壁画拓片、船牌这类：每送一件，她多讲一段（讲什么由叙事写，按第几段往下接）
                seen = dict(player.flags.get("_lore") or {})
                seen[_prop(item, "lore")] = n = int(seen.get(_prop(item, "lore"), 0)) + 1
                cur.execute("update players set flags = flags || jsonb_build_object('_lore', %s::jsonb) where id = %s",
                            (Jsonb(seen), player.id))
                cur.execute("select text from lore_texts where key = %s and n = %s", (_prop(item, "lore"), n))
                told = cur.fetchone()
                facts.append(f"（{npc.name}看着它出了一会儿神，讲起了{lore[1]}的第 {n} 段往事）")
                facts.append(f"{npc.name}讲的往事：{told['text']}" if told else f"{LORE_MARK}{_prop(item, 'lore')}::{n}::{npc.name}")
            return facts
        back = item.props.get("sold_by") == npc.template.id
        _consume(cur, item)                 # 叠着的古酒只送出一瓶
        new = min(100, _affinity(cur, player, npc) + LIKED_GIFT)
        cur.execute("""insert into player_npc_relations (player_id, npc_template, affinity) values (%s, %s, %s)
                       on conflict (player_id, npc_template) do update set affinity = excluded.affinity""",
                    (player.id, npc.template.id, new))
        return [f"{player.name}把{item.name}送给了{npc.name}，这正是她最想要的东西（{GIFT_FACT}）"
                + (f"，而且是{RETURNED_FACT}" if back else ""),
                f"{npc.name}对{player.name}的好感上升（当前 {new}，收到心爱的礼物）"]
    _move_item(cur, item, npc_id=npc.id)
    return [f"{player.name}把{_label(item)}交给了{npc.name}"]


# 铁匠升级武器：每级伤害 +1，名字后面标 +N。升到第 N 级有 N × UPGRADE_BREAK_STEP 的几率失败（最多 UPGRADE_BREAK_MAX），
# 失败不会碎，而是退一级（+3 升 +4 失败变 +2，+0 失败还是 +0），钱照收。
# 等级不封顶。费用是升级后和升级前建议价的差（至少 UPGRADE_MIN_COST），跟着伤害指数涨。
# 失败不掉级、钱照收；同一级连续失败有保底（rules.upgrade_chance）。
# 奥利哈刚（UPGRADE_ORE）：这一次成功率翻倍，用掉；碎铁（SCRAP，莉娜拆装备得来）每份 +5%，一次最多四份。
# 莉娜的淬火油（UPGRADE_OIL，好感 40 的回礼）：这一次必定成功，不收钱。等级上限 UPGRADE_MAX
UPGRADE_ORE = "ore"
UPGRADE_DEEP_MAX, DEEP_FORGE = 15, "lina_deep_forge"     # 深层锻造（拓片解锁）：深层装备（props.tier）升级上限


def upgrade_cap(player: Player, item: ItemInstance) -> int:
    """这件能升到几级：一般 +10；深层装备、而且莉娜学会了深层锻造（players.flags.lina_deep_forge）是 +15"""
    return UPGRADE_DEEP_MAX if _prop(item, "tier") and player.flags.get(DEEP_FORGE) else UPGRADE_MAX
SCRAP = "scrap_iron"
UPGRADE_OIL = "lina_oil"


STAT_WORDS = {"damage": "伤害", "defense": "防御"}


def upgrade_stat(item: ItemInstance) -> Optional[str]:
    """升级加的是哪项：武器加伤害，有防御的防具（盔甲、盾、帽子……）加防御，别的升不了"""
    if item.template.type == "weapon":
        return "damage"
    if item.template.type == "armor" and item.defense > 0:
        return "defense"
    return None


def upgradable(items: list[ItemInstance]) -> list[ItemInstance]:
    return [i for i in items if upgrade_stat(i)]


def _step(item: ItemInstance, stat: str) -> float:
    """这件升一级加多少（远程武器伤害 +15）"""
    return upgrade_step(stat, bool(_prop(item, "ranged") or _prop(item, "loads")))


def upgrade_terms(item: ItemInstance) -> tuple[int, int, float]:
    """(升到几级, 费用, 这次什么都不垫的失败几率，算上保底)：费用只看升到第几级（rules.upgrade_cost），稀有的东西乘 props.upgrade_mult"""
    level = item.props.get("plus", 0) + 1
    cost, _ = upgrade_cost(level, float(_prop(item, "upgrade_mult") or 1))
    return level, cost, 1 - upgrade_chance(level, _upgrade_pity(item, level), armor=upgrade_stat(item) == "defense")


def _upgrade_pity(item: ItemInstance, level: int) -> int:
    """这件在这一级已经连续失败了几次（props.upgrade_fails = {"level", "n"}）"""
    f = item.props.get("upgrade_fails") or {}
    return f.get("n", 0) if f.get("level") == level else 0


def _upgrade_text(item: ItemInstance, price: Optional[int] = None, chance: Optional[float] = None) -> str:
    level, cost, risk = upgrade_terms(item)
    cost = price if price is not None else cost
    chance = 1 - risk if chance is None else chance
    stat = upgrade_stat(item)
    now = getattr(item, stat)
    pity = _upgrade_pity(item, level)
    return (f"升到 +{level}（{STAT_WORDS[stat]} {stat_text(now)} → {stat_text(now + _step(item, stat))}）要 {cost} 金币，"
            f"成功率 {round(chance * 100)}%，失败不掉级、钱照收" + (f"（这一级失败过 {pity} 次，火候摸清楚了些）" if pity else ""))


def do_upgrade(cur: Cursor, player: Player, view: RoomView, a: Upgrade) -> list[str]:
    npc = _room_npc(cur, view, player, a.target)
    if not npc.template.props.get("upgrades"):
        raise ActionError(f"{npc.name}不会升级装备")
    offers = _offers(cur, player.id, npc)
    gear = upgradable(view.inventory)
    if a.quote:
        items = [_inv_item(cur, view, player, a.item)] if a.item else gear
        if not items or not all(upgrade_stat(i) for i in items):
            raise ActionError(f"{player.name}身上没有能升级的武器、防具")
        disc = UPGRADE_DISCOUNT if "discount" in perks(cur, player.id, npc.template.id) else 1.0
        return [f"{npc.name}看了看{player.name}的{i.name}：" + (f"已经 +{upgrade_cap(player, i)}，锻到头了" if i.props.get("plus", 0) >= upgrade_cap(player, i)
                                                             else _upgrade_text(i, max(1, round(upgrade_terms(i)[1] * disc)))
                                                             + ("（熟客价九折）" if disc < 1 else "")) for i in items]
    if a.item:
        item = _inv_item(cur, view, player, a.item)
        if not upgrade_stat(item):
            raise ActionError(f"{item.name}不是武器，也不是带防御的防具，{npc.name}没法升级")
    else:
        # 没说哪件：刚问过要不要用矿石的那件（回"用""不用"），或者身上只有一件；不止一件就列出来问
        asked = next((i for i in gear if f"upgrade:{i.id}" in offers), None)
        if asked is None and a.ore is False:
            raise ActionError(f"{npc.name}没在问{player.name}用不用矿石")
        if asked is None and len(gear) != 1:
            if not gear:
                raise ActionError(f"{player.name}身上没有能升级的武器、防具")
            disc = UPGRADE_DISCOUNT if "discount" in perks(cur, player.id, npc.template.id) else 1.0
            return ([f"{npc.name}问{player.name}要升级哪一件："]
                    + [f"{i.name}：{_upgrade_text(i, max(1, round(upgrade_terms(i)[1] * disc)))}" for i in gear]
                    + [f"{player.name}说「升级」加名字，{npc.name}就动手"])
        item = asked or gear[0]
    stat = upgrade_stat(item)
    now = getattr(item, stat)
    level, cost, risk = upgrade_terms(item)
    if level > upgrade_cap(player, item):
        raise ActionError(f"{item.name}已经 +{upgrade_cap(player, item)} 了，{npc.name}说再锻就要废了"
                          + ("（深层装备把矮人锻造纹拓片送给她，她能锻到 +15）" if _prop(item, "tier") and not player.flags.get(DEEP_FORGE) else ""))
    if "discount" in perks(cur, player.id, npc.template.id):
        cost = max(1, round(cost * UPGRADE_DISCOUNT))
    key = f"upgrade:{item.id}"
    ore = next((i for i in view.inventory if i.template.id == UPGRADE_ORE), None)
    oil = next((i for i in view.inventory if i.template.id == UPGRADE_OIL), None)
    scrap = next((i for i in view.inventory if i.template.id == SCRAP), None)
    if a.ore and ore is None:
        raise ActionError(f"{player.name}身上没有奥利哈刚矿石")
    if a.scrap and scrap is None:
        raise ActionError(f"{player.name}身上没有碎铁（找{npc.name}拆解用不上的地牢装备得来）")
    use_scrap = min(a.scrap, UPGRADE_SCRAP_MAX, scrap.quantity if scrap else 0)
    pity = _upgrade_pity(item, level)
    chance = upgrade_chance(level, pity, bool(a.ore), use_scrap, armor=stat == "defense")
    terms = _upgrade_text(item, cost, chance)
    if a.oil:
        # 莉娜的淬火油：必定成功，不收钱
        if oil is None:
            raise ActionError(f"{player.name}身上没有莉娜的淬火油")
        _consume(cur, oil)
        name = re.sub(r" \+\d+$", "", item.name) + f" +{level}"
        cur.execute("update item_instances set props = props || %s where id = %s",
                    (Jsonb({"plus": level, stat: now + _step(item, stat), "name": name}), item.id))
        return [f"{player.name}递上莉娜的淬火油，{npc.name}把{item.name}烧红了往油里一浸，滋的一声冒起白烟",
                f"升级成功：{item.name}变成了{name}，{STAT_WORDS[stat]} {stat_text(now + _step(item, stat))}（淬火油用掉了，没收钱）"]
    if cost > player.gold:
        raise ActionError(f"{npc.name}看了看{player.name}的{item.name}：{terms}。{player.name}身上只有 {player.gold} 金币，不够")
    if a.ore is None and not a.scrap and (ore is not None or scrap is not None) and chance < 1 and key not in offers:
        # 兜里有矿石、碎铁又没说用不用：先问一句，回"用""不用"（或者"垫两份碎铁"）才动手
        _put_offer(cur, player.id, npc, key, cost)
        return [f"{npc.name}看了看{player.name}的{item.name}：{terms}",
                f"{npc.name}瞥见{player.name}带着"
                + "、".join(([f"奥利哈刚矿石（锻进去这次成功率翻倍到 {round(upgrade_chance(level, pity, True, armor=stat == "defense") * 100)}%，矿石用掉）"] if ore else [])
                            + ([f"{scrap.quantity} 份碎铁（每份 +{round(UPGRADE_SCRAP * 100)}%，一次最多垫 {UPGRADE_SCRAP_MAX} 份）"] if scrap else []))
                + (f"；还有莉娜的淬火油，用了必定成功、不收钱（说「用淬火油」）" if oil else ""),
                f"{player.name}说「用矿石」「垫两份碎铁」或者「不用」，{npc.name}就动手"]
    patron = _pay(cur, player, cost, npc)
    cur.execute("update player_npc_relations set offers = offers - %s where player_id = %s and npc_template = %s",
                (key, player.id, npc.template.id))
    facts = [f"{player.name}付了 {cost} 金币，{npc.name}把{item.name}放进炉火里重新锻打（{terms}）"] + patron
    base = re.sub(r" \+\d+$", "", item.name)
    if a.ore:
        _consume(cur, ore)
        facts.append(f"{player.name}递上的奥利哈刚矿石化进了铁水里")
    if use_scrap:
        _consume_n(cur, scrap, use_scrap)
        facts.append(f"{npc.name}往炉子里添了 {use_scrap} 份碎铁")
    if not _roll(chance):
        # 失败不掉级，钱照收；这一级连续失败的次数记在装备上，下一次成功率 +UPGRADE_PITY
        cur.execute("update item_instances set props = props || %s where id = %s",
                    (Jsonb({"upgrade_fails": {"level": level, "n": pity + 1}}), item.id))
        return facts + [f"淬火的时候火候没掌握好，{item.name}没升上去，好在也没伤着（钱照收）",
                        f"{npc.name}盯着炉火琢磨了一会儿：这把的火候摸清楚一点了（下次成功率 +{round((UPGRADE_PITY_ARMOR if stat == "defense" else UPGRADE_PITY) * 100)}%）"]
    name = base + f" +{level}"
    cur.execute("update item_instances set props = (props - 'upgrade_fails') || %s where id = %s",
                (Jsonb({"plus": level, stat: now + _step(item, stat), "name": name}), item.id))
    return facts + [f"升级成功：{item.name}变成了{name}，{STAT_WORDS[stat]} {stat_text(now + _step(item, stat))}"]


# ============ 莉娜的回礼：传承锻造、刷新词条、专属武器 ============
TRANSFER_SHARE = 0.5                    # 传承锻造：按"把接过去的那件从现在升到那么多级"的升级费的一半收
REROLL_BASE = 20                        # 刷新词条：参考价的一半（至少 20）×（这件刷过几次 + 1）
BAD_EFFECTS = {"self_damage", "cheat_death"}      # 刷词条、专属武器不会抽到的：咬手的、太稀有的


def _affix_pool(cur: Cursor, kind: str) -> list[dict]:
    """某一类装备（weapon / armor）能刷出来的词条：所有这类装备身上的特效（不含诅咒装备、坏效果、负数）"""
    cur.execute("""select props->'effects' as effects from item_templates
                   where type = %s and props ? 'effects' and not coalesce((props->>'cursed')::boolean, false)""", (kind,))
    seen, pool = set(), []
    for r in cur.fetchall():
        for e in r["effects"] or []:
            key = (e.get("when"), e.get("do"), e.get("kind"), tuple(e.get("vs") or []))
            if e.get("do") in BAD_EFFECTS or (isinstance(e.get("value"), (int, float)) and e["value"] < 0) or key in seen:
                continue
            seen.add(key)
            pool.append(e)
    return pool


def _lina(cur: Cursor, view: RoomView, player: Player, ref: str, perk: str, what: str) -> Npc:
    npc = _room_npc(cur, view, player, ref)
    if not npc.template.props.get("upgrades"):
        raise ActionError(f"{npc.name}不会{what}")
    if perk not in perks(cur, player.id, npc.template.id):
        raise ActionError(f"{npc.name}还没教过{player.name}这个（{what}要跟她交情更深才行）")
    return npc


def do_transfer(cur: Cursor, player: Player, view: RoomView, a: Transfer) -> list[str]:
    """传承锻造：A 的强化等级转到 B 上（同一类），A 变回 +0。按 B 从现在升到那一级的升级费的一半收钱"""
    npc = _lina(cur, view, player, a.target, "transfer", "传承锻造")
    src, dst = _inv_item(cur, view, player, a.item), _inv_item(cur, view, player, a.to)
    stat = upgrade_stat(src)
    ranged = lambda i: bool(_prop(i, "ranged") or _prop(i, "loads"))
    if not stat or upgrade_stat(dst) != stat or ranged(src) != ranged(dst):
        raise ActionError("传承只能在同一类装备之间：近战武器给近战武器、远程给远程、带防御的防具给防具")
    have, now = src.props.get("plus", 0), dst.props.get("plus", 0)
    if have <= now:
        raise ActionError(f"{src.name}的强化（+{have}）不比{dst.name}（+{now}）高，没什么可传的")
    price, probe = 0, dst.model_copy(deep=True)
    for lv in range(now + 1, have + 1):            # 按接过去那件一级一级升上去的费用算
        price += upgrade_terms(probe)[1]
        probe.props = {**probe.props, "plus": lv, stat: getattr(probe, stat) + _step(probe, stat)}
    price = max(UPGRADE_MIN_COST, round(price * TRANSFER_SHARE))
    patron = _pay(cur, player, price, npc)
    gain = have - now
    base_src, base_dst = re.sub(r" \+\d+$", "", src.name), re.sub(r" \+\d+$", "", dst.name)
    cur.execute("update item_instances set props = props || %s where id = %s",
                (Jsonb({"plus": 0, stat: getattr(src, stat) - have * _step(src, stat), "name": base_src}), src.id))
    cur.execute("update item_instances set props = props || %s where id = %s",
                (Jsonb({"plus": have, stat: getattr(dst, stat) + gain * _step(dst, stat), "name": f"{base_dst} +{have}"}), dst.id))
    return [f"{player.name}付了 {price} 金币，{npc.name}把{src.name}和{dst.name}一起放进炉火，锤了一整个下午",
            f"{src.name}上的锻纹褪了下去，变回了{base_src}；{base_dst}接过了这份锻打，变成了{base_dst} +{have}，"
            f"{STAT_WORDS[stat]} {stat_text(getattr(dst, stat) + gain * _step(dst, stat))}"] + patron


def do_reroll(cur: Cursor, player: Player, view: RoomView, a: Reroll) -> list[str]:
    """刷新词条：带特效的装备重新锻出别的特效（条数不变），同一件越刷越贵"""
    npc = _lina(cur, view, player, a.target, "reroll", "刷新词条")
    item = _inv_item(cur, view, player, a.item)
    old = _prop(item, "effects") or []
    kind = item.template.type
    if item.template.props.get("cursed"):
        raise ActionError(f"{npc.name}摇摇头：{item.name}上的诅咒长在铁里，重淬也淬不掉，只会把诅咒一起锻进去")
    if item.template.id in dungeon.boss_items():
        raise ActionError(f"{npc.name}摸着{item.name}看了半天：这是头目身上的东西，那股劲是它自己的，重淬会毁了它")
    if not old or kind not in ("weapon", "armor"):
        raise ActionError(f"{item.name}身上没有特效，没什么可刷的（只有带特效的武器、防具能刷）")
    times = item.props.get("rerolls", 0)
    price = max(REROLL_BASE, base_price(item_stats(item)) // 2) * (times + 1)
    pool = [e for e in _affix_pool(cur, kind) if e not in old]
    if len(pool) < len(old):
        raise ActionError(f"{npc.name}想不出还能给{item.name}锻出什么别的特效了")
    patron = _pay(cur, player, price, npc)
    new = random.sample(pool, len(old))
    cur.execute("update item_instances set props = props || %s where id = %s",
                (Jsonb({"effects": new, "rerolls": times + 1}), item.id))
    return ([f"{player.name}付了 {price} 金币（这件第 {times + 1} 次刷），{npc.name}说了声「重淬」，把{item.name}烧红了重新锻打，"
             f"原来的特效随着火星散掉了"] + patron
            + [f"{item.name}新的特效：{'；'.join(_effect_line(e) for e in new)}"])


def _exclusive_blade(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """莉娜的回礼（好感 100）：一把专属武器。伤害按他走到过的最深层数（(5 + 层数/3，最多 12) × 10），带一个武器词条"""
    cur.execute("select deepest_floor from players where id = %s", (player.id,))
    deep = cur.fetchone()["deepest_floor"] or 0
    dmg = min(12, 5 + deep // 3) * SCALE
    affix = random.choice(_affix_pool(cur, "weapon") or [{}])
    props = {"damage": dmg, "effects": [affix] if affix else []}
    cur.execute("insert into item_instances (template_id, player_id, props) values ('lina_blade', %s, %s)",
                (player.id, Jsonb(props)))
    return [f"无铭：伤害 {dmg}（跟着走过的最深层数一起长，最多 {12 * SCALE}）" + (f"，{_effect_line(affix)}" if affix else ""),
            f"{npc.name}只说了一句：名字，你起"]


def do_rename(cur: Cursor, player: Player, view: RoomView, a: Rename) -> list[str]:
    """给专属武器起名（莉娜打的那把）"""
    item = _inv_item(cur, view, player, a.item)
    if not _prop(item, "exclusive"):
        raise ActionError(f"{item.name}不是你的专属武器，名字改不了")
    name = a.name.strip().strip("「」“”\"'")
    if not (1 <= len(name) <= 10) or re.search(r"[<>]", name):
        raise ActionError("名字 1 到 10 个字")
    plus = item.props.get("plus", 0)
    full = name + (f" +{plus}" if plus else "")
    cur.execute("update item_instances set props = props || %s where id = %s", (Jsonb({"name": full}), item.id))
    return [f"{player.name}给{item.name}起了名字：{full}"]


# ============ 宝石：找莉娜镶、取（第一期没有打孔、合成）============

def _regem(cur: Cursor, item: ItemInstance, gems: list[dict]) -> None:
    """重算装备上宝石的效果（按品质、装备类型落成 effects 存 props.gem_fx，萤石的光存 props.gem_light）"""
    cat = gem_category(item.template.type, item.template.slot)
    cur.execute("select id, props from item_templates where id = any(%s)", ([g["id"] for g in gems],))
    tpl = {r["id"]: r["props"] for r in cur.fetchall()}
    fx = [e for g in gems for e in gem_effects(tpl[g["id"]], g["tier"], cat)]
    light = max([int(e["value"]) for e in fx if e.get("do") == "light"] or [0])
    cur.execute("update item_instances set props = (props - 'gem_light') || %s where id = %s",
                (Jsonb({"gems": gems, "gem_fx": fx} | ({"gem_light": light} if light else {})), item.id))


def _smith(cur: Cursor, view: RoomView, player: Player, ref: str, what: str) -> Npc:
    npc = _room_npc(cur, view, player, ref)
    if not npc.template.props.get("upgrades"):
        raise ActionError(f"{npc.name}不会{what}，找铁匠莉娜")
    return npc


def do_socket(cur: Cursor, player: Player, view: RoomView, a: Socket) -> list[str]:
    """镶宝石：装备上有空孔、宝石对得上这类装备（武器、护具、饰品），免费"""
    npc = _smith(cur, view, player, a.target, "镶宝石")
    item, gem = _inv_item(cur, view, player, a.item), _inv_item(cur, view, player, a.gem)
    if gem.template.type != "gem":
        raise ActionError(f"{gem.name}不是宝石")
    cat = gem_category(item.template.type, item.template.slot)
    holes, gems = item.props.get("sockets", 0), list(item.props.get("gems") or [])
    if not holes:
        raise ActionError(f"{item.name}上没有孔，镶不了（只有地牢里掉的装备才带孔）")
    if len(gems) >= holes:
        raise ActionError(f"{item.name}的 {holes} 个孔都镶满了，先取下来一颗（说「把{item.name}上的{gems[0]['name']}取下来」）")
    if not gem_fits(gem.template.props.get("gem_slot", "any"), cat):
        raise ActionError(f"{gem.name}只能镶在{GEM_SLOT_WORDS.get(gem.template.props.get('gem_slot'), '')}上，{item.name}镶不了")
    gems.append({"id": gem.template.id, "tier": gem.props.get("tier", 1), "name": gem.name})
    _consume(cur, gem)
    _regem(cur, item, gems)
    facts = [f"{npc.name}把{gem.name}按进{item.name}的孔里，用小锤敲了几下固定好（镶宝石不收钱）",
             f"{item.name}：宝石孔 {len(gems)}/{holes}"] + [_effect_line(e) for e in gem_effects(
                 gem.template.props, gem.props.get("tier", 1), cat)]
    return facts + (_sync_gear_hp(cur, player.id) if item.equipped_slot else [])


def do_unsocket(cur: Cursor, player: Player, view: RoomView, a: Unsocket) -> list[str]:
    """取宝石：宝石还给玩家，按品质收钱；诅咒装备解咒前取不出来"""
    npc = _smith(cur, view, player, a.target, "取宝石")
    item = _inv_item(cur, view, player, a.item)
    gems = list(item.props.get("gems") or [])
    if not gems:
        raise ActionError(f"{item.name}上没有镶宝石")
    if _cursed(item):
        raise ActionError(f"{item.name}被诅咒了，宝石跟它长在了一起，得先找诺艾尔解咒才取得下来")
    want = (a.gem or "").strip()
    pick = next((g for g in gems if want and (want == g["name"] or want in g["name"])), None) \
        or (gems[0] if not want or len(gems) == 1 else None)
    if pick is None:
        raise ActionError(f"{item.name}上镶的是{'、'.join(g['name'] for g in gems)}，要取哪一颗？")
    fee = dungeon.gem_rules()["unsocket_fee"][pick["tier"] - 1]
    patron = _pay(cur, player, fee, npc)
    gems.remove(pick)
    _regem(cur, item, gems)
    cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                (pick["id"], player.id, Jsonb(dungeon.gem_props(cur, pick["id"], pick["tier"]))))
    facts = [f"{player.name}付了 {fee} 金币，{npc.name}用细錾子把{pick['name']}从{item.name}上撬了下来，完好无损"] + patron
    return facts + (_sync_gear_hp(cur, player.id) if item.equipped_slot else [])


# ============ 捐赠（酒馆武器桶）============
# 老手把用不上的武器护具放进武器桶留给新人：强化等级清掉（伤害防御回到原样），镶的宝石退回捐的人背包；
# 诅咒装备、礼物（NPC 回礼给的、送 NPC 的）、委托要交的东西、钥匙不能放。拿的人每人每天一件，拿到的标 donated，NPC 不收
DONATE_TAKE_WINDOW = "1 day"
UPGRADE_PROPS = ("plus", "damage", "defense", "name", "gems", "gem_fx", "gem_light")


def _gift_templates(cur: Cursor) -> set[str]:
    """NPC 回礼送的东西（连同弩空着、装好的另一个模板）"""
    cur.execute("""select g.value->>'item' as item from npc_templates t, jsonb_each(coalesce(t.props->'return_gifts', '{}')) g
                   where g.value ? 'item'""")
    ids = {r["item"] for r in cur.fetchall()}
    cur.execute("""select id from item_templates where props->>'loads' = any(%(i)s) or props->>'unloaded' = any(%(i)s)
                   or props->>'refill_to' = any(%(i)s)""", {"i": list(ids)})
    return ids | {r["id"] for r in cur.fetchall()}


def _donation_box(view: RoomView, ref: Optional[str]) -> Dispenser:
    boxes = [d for d in view.dispensers if d.donate]
    if ref:
        uid = view.resolve(ref)
        if box := next((d for d in boxes if d.id == uid), None):
            return box
    if len(boxes) == 1:
        return boxes[0]
    raise ActionError("这里没有能放东西的地方（酒馆门边的武器桶可以）" if not boxes else "要放进哪里？")


def do_donate(cur: Cursor, player: Player, view: RoomView, a: Donate) -> list[str]:
    box = _donation_box(view, a.target)
    item = _inv_item(cur, view, player, a.item)
    if item.template.type not in box.donate:
        raise ActionError(f"{box.container}只收武器和护具，{item.name}放不进去")
    if item.equipped_slot:
        raise ActionError(f"{item.name}还装备在身上，先卸下来再放")
    if _prop(item, "cursed"):                   # 没穿上时诅咒不发作，但也不能坑新人
        raise ActionError(f"{item.name}被诅咒了，不能留给别人（先找诺艾尔解咒）")
    if _precious(cur, item) or _prop(item, "gift") or item.template.id in _gift_templates(cur):
        raise ActionError(f"{item.name}是别人的心意或者要交的东西，不能放进{box.container}")
    facts = []
    if item.props.get("plus"):
        facts.append(f"{item.name}的强化没了，放进去的是一把普通的{item.template.name}")
    for g in item.props.get("gems") or []:
        cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                    (g["id"], player.id, Jsonb(dungeon.gem_props(cur, g["id"], g["tier"]))))
        facts.append(f"镶在上面的{g['name']}撬下来，退回了{player.name}的背包")
    props = {k: v for k, v in item.props.items() if k not in UPGRADE_PROPS + ("donated",)}
    cur.execute("insert into donations (room_id, container, template_id, props, donor) values (%s, %s, %s, %s, %s)",
                (box.room, box.key, item.template.id, Jsonb(props), player.id))
    cur.execute("delete from item_instances where id = %s", (item.id,))
    return [f"{player.name}把{item.template.name}放进了{box.container}，留给缺家伙的人"] + facts


SCRAP_BY_RARITY = {"common": 1, "uncommon": 2, "rare": 3}


def _not_material(cur: Cursor, item: ItemInstance, what: str) -> None:
    """诅咒装备、礼物（NPC 回礼、送 NPC 的）、委托物品、钥匙：不能捐、不能拆"""
    if _prop(item, "cursed"):
        raise ActionError(f"{item.name}被诅咒了，{what}不了（先找诺艾尔解咒）")
    if _precious(cur, item) or _prop(item, "gift") or item.template.id in _gift_templates(cur):
        raise ActionError(f"{item.name}是别人的心意或者要交的东西，{what}不了")


def do_dismantle(cur: Cursor, player: Player, view: RoomView, a: Dismantle) -> list[str]:
    """莉娜拆解用不上的地牢装备：出碎铁（垫升级用），镶的宝石退回背包"""
    npc = _smith(cur, view, player, a.target, "拆解")
    item = _inv_item(cur, view, player, a.item)
    if item.template.id not in dungeon.dungeon_items() or not gem_category(item.template.type, item.template.slot):
        raise ActionError(f"{npc.name}只拆地牢里带出来的武器、护具、饰品，{item.name}拆不出好铁")
    if item.equipped_slot:
        raise ActionError(f"{item.name}还装备在身上，先卸下来")
    if _prop(item, "donated"):
        raise ActionError(f"{item.name}是酒馆武器桶里别人留给新人的，{npc.name}不拆")
    _not_material(cur, item, "拆")
    n = SCRAP_BY_RARITY[dungeon.item_rarity(item.template.id, item.props)] + item.props.get("plus", 0) // 2
    facts = [f"{npc.name}把{item.name}拆开，挑出了 {n} 份碎铁（升级时垫上，每份成功率 +{round(UPGRADE_SCRAP * 100)}%）"]
    for g in item.props.get("gems") or []:
        cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                    (g["id"], player.id, Jsonb(dungeon.gem_props(cur, g["id"], g["tier"]))))
        facts.append(f"镶在上面的{g['name']}撬下来，还给了{player.name}")
    cur.execute("delete from item_instances where id = %s", (item.id,))
    cur.execute("""update item_instances set quantity = quantity + %s
                   where player_id = %s and template_id = %s and equipped_slot is null returning id""", (n, player.id, SCRAP))
    if not cur.fetchone():
        cur.execute("insert into item_instances (template_id, player_id, quantity) values (%s, %s, %s)", (SCRAP, player.id, n))
    return facts


def do_take_donated(cur: Cursor, player: Player, view: RoomView, a: TakeDonated) -> list[str]:
    box = _donation_box(view, a.target)
    want = a.name.strip()
    pick = next((d for d in box.donated if d["name"] == want), None) or next((d for d in box.donated if want and want in d["name"]), None)
    if pick is None:
        raise ActionError(f"{box.container}里没有{want}" + (f"（里面有：{'、'.join(d['name'] for d in box.donated)}）" if box.donated else "（里面没有别人放的东西）"))
    cur.execute(f"""select 1 from dispenser_log where player_id = %s and room_id = %s and key = %s
                    and taken_at > now() - interval '{DONATE_TAKE_WINDOW}'""", (player.id, box.room, f"{box.key}#donated"))
    if cur.fetchone():
        raise ActionError(f"{player.name}今天已经从{box.container}拿过一件了，留点给别人（明天再来）")
    cur.execute("delete from donations where id = %s returning template_id, props", (pick["id"],))
    row = cur.fetchone()
    if row is None:
        raise ActionError(f"{pick['name']}刚被别人拿走了")
    cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                (row["template_id"], player.id, Jsonb(row["props"] | {"donated": True})))
    cur.execute("""insert into dispenser_log (player_id, room_id, key) values (%s, %s, %s)
                   on conflict (player_id, room_id, key) do update set taken_at = now()""", (player.id, box.room, f"{box.key}#donated"))
    return [f"{player.name}从{box.container}{box.where}挑了一件{pick['name']}（别人留下的，店里不收）"]


def do_refine(cur: Cursor, player: Player, view: RoomView, a: Refine) -> list[str]:
    """诺艾尔刷宝石品质：只升不降（loot.yaml gems.refine），宝石得在背包里、没镶上去；最深层数不够时封顶，封顶了不收钱"""
    npc = _room_npc(cur, view, player, a.target)
    if not npc.template.props.get("refine"):
        raise ActionError(f"{npc.name}不懂宝石，找杂货铺的诺艾尔")
    gem = _inv_item(cur, view, player, a.item)
    if gem.template.type != "gem":
        if gem.props.get("gems"):
            raise ActionError(f"宝石镶在{gem.name}上刷不了，先找莉娜取下来（说「把{gem.name}上的{gem.props['gems'][0]['name']}取下来」）")
        raise ActionError(f"{gem.name}不是宝石")
    rules = dungeon.gem_rules()["refine"]
    tier = gem.props.get("tier", 1)
    if tier >= GEM_TOP:
        raise ActionError(f"{gem.name}已经是完美的了，再唤醒也不会更好")
    cur.execute("select deepest_floor from players where id = %s", (player.id,))
    deepest = cur.fetchone()["deepest_floor"]
    cap = refine_cap(bool(gem.template.props.get("numeric")), deepest, rules)
    if tier >= cap:
        need = rules["min_deepest_floor"]["shiny"] if cap < 3 else rules["min_deepest_floor"]["perfect_numeric"]
        raise ActionError(f"{npc.name}翻了半天书，小声说书上写着……还差一点什么（最深走到第 {need} 层以后，才唤得醒更好的{GEM_TIER_WORDS[tier + 1]}品质）")
    catalyst = None
    if a.catalyst:
        catalyst = min((i for i in view.inventory if i.template.id == gem.template.id and i.id != gem.id),
                       key=lambda i: i.props.get("tier", 1), default=None)      # 垫子先用品质最低的
        if catalyst is None:
            catalyst = next((i for i in view.inventory if _prop(i, "refine_catalyst")), None)       # 晶粉：什么宝石都能垫
        if catalyst is None:
            raise ActionError(f"身上没有另一颗{gem.template.name}（或者晶粉）能当垫子")
    cost = rules["cost"][tier - 1]
    patron = _pay(cur, player, cost, npc)
    if catalyst:
        _consume(cur, catalyst)
    dust = float(_prop(catalyst, "refine_catalyst") or 0) if catalyst else 0
    new = refine_roll(tier, cap, rules, bool(catalyst) and not dust, dust or 1.0)
    facts = [f"{player.name}付了 {cost} 金币，{npc.name}翻开一本旧书，照着上面的法子对着{gem.name}念念有词"
             + (f"，{catalyst.name}当了垫子，化成一小撮粉末" if catalyst else "")] + patron
    if new == tier:
        return facts + [f"{gem.name}闪了一下又暗了下去，品质没变（{npc.name}一个劲地小声道歉）"]
    props = dungeon.gem_props(cur, gem.template.id, new)
    cur.execute("update item_instances set props = props || %s where id = %s", (Jsonb(props), gem.id))
    return facts + [f"{gem.name}亮了起来，变成了{props['name']}（{GEM_TIER_WORDS[new]}品质）"
                    + ("，一下跳了两档！" if new - tier == 2 else "") + f"（{npc.name}忍不住小声欢呼了一下）"]


NOTE_MAX = 100


def do_write(cur: Cursor, player: Player, view: RoomView, a: Write) -> list[str]:
    """在纸条（props.writable）上写字：文字存进这一张的描述，名字变成"写了字的纸条"，写过的不能再改。
    玩家之间留言用：给队友、丢在地牢房间里提醒后来的人、当信物。字是玩家写的，不是 AI 编的"""
    item = _inv_item(cur, view, player, a.item)
    if item.props.get("note"):
        raise ActionError(f"{item.name}上已经写过字了，改不了")
    if not _prop(item, "writable"):
        raise ActionError(f"{item.name}上写不了字")
    text = re.sub(r"\s+", " ", a.message).strip().strip("「」“”\"'").strip()
    if not text:
        raise ActionError("要写什么？（说「在纸条上写……」）")
    if len(text) > NOTE_MAX:
        raise ActionError(f"纸条就这么大，最多写 {NOTE_MAX} 个字（这段有 {len(text)} 个）")
    if item.quantity > 1:                   # 叠着的只写最上面一张
        cur.execute("update item_instances set quantity = quantity - 1 where id = %s", (item.id,))
        cur.execute("insert into item_instances (template_id, player_id) values (%s, %s) returning id",
                    (item.template.id, player.id))
        note_id = cur.fetchone()["id"]
    else:
        note_id = item.id
    cur.execute("update item_instances set props = props || %s where id = %s",
                (Jsonb({"note": text, "writable": False, "name": "写了字的纸条",
                        "description": f"{item.template.description}上面用炭笔写着：“{text}”（{player.name}写的）"}), note_id))
    return [f"{player.name}在纸条上写下：“{text}”"]


# ============ 诅咒 ============
# 诅咒装备（props.cursed）：装上就粘在身上，卸不下、扔不掉、给不出、卖不掉，别的东西也顶不掉它。
# 找诺艾尔（props.uncurse）按参考价付钱解咒：解了就是普通装备（实例上记 cursed: false），特效还在

def _cursed(item: ItemInstance) -> bool:
    return bool(item.equipped_slot and _prop(item, "cursed"))


def _no_curse(item: Optional[ItemInstance], what: str = "卸") -> None:
    if item is not None and _cursed(item):
        raise ActionError(f"{item.name}被诅咒了，粘在身上{what}不下来（找杂货铺的诺艾尔付钱解咒）")


def do_uncurse(cur: Cursor, player: Player, view: RoomView, a: Uncurse) -> list[str]:
    npc = _room_npc(cur, view, player, a.target)
    if not npc.template.props.get("uncurse"):
        raise ActionError(f"{npc.name}不会解咒")
    if a.item:
        item = _inv_item(cur, view, player, a.item)
        if not _cursed(item):
            raise ActionError(f"{item.name}身上没有诅咒" if not _prop(item, "cursed") else f"{item.name}没戴在身上，用不着解咒，直接扔掉就行")
    else:
        worn = [i for i in _worn(cur, player) if _cursed(i)]
        if not worn:
            raise ActionError(f"{player.name}身上没有被诅咒的装备")
        item = worn[0]
    price = base_price(item_stats(item))
    patron = _pay(cur, player, price, npc)
    cur.execute("update item_instances set props = props || '{\"cursed\": false}'::jsonb where id = %s", (item.id,))
    return ([f"{player.name}付了 {price} 金币，{npc.name}翻开怀里那本旧书，小声念了一段古老的句子，"
             f"{item.name}上缠着的诅咒散开了：现在能卸下来了"] + patron)


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
    if _effect(player, "silence"):
        raise ActionError(f"{player.name}喉咙像被堵住了，一个字也说不出来（禁声，过一会儿才好）")
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
    "follow": do_follow, "unfollow": do_unfollow, "leave_party": do_leave_party, "kick": do_kick,
    "maneuver": do_maneuver, "dodge": do_dodge, "tame": do_tame, "uncurse": do_uncurse, "reload": do_reload, "refill": do_refill,
    "transfer": do_transfer, "reroll": do_reroll, "rename": do_rename, "write": do_write, "socket": do_socket, "unsocket": do_unsocket, "refine": do_refine, "donate": do_donate, "take_donated": do_take_donated, "dismantle": do_dismantle, "close_eyes": do_close_eyes, "pinch": do_pinch, "hide": do_hide, "search": do_search, "reject": do_reject,
    "upgrade": do_upgrade, "pay": do_pay, "sell": do_sell, "respawn": do_respawn, "stand": do_stand, "rest": do_rest, "camp": do_camp, "teleport": do_teleport, "challenge": do_challenge, "accept_duel": do_accept_duel, "decline_duel": do_decline_duel, "flee": do_flee,
}


# ============ 入口 ============

HEAT_HINT = ("热浪扑面：这一层一直在烤人，每做一个动作都掉一点血（说话、看不算）；喝一口泉水能撑 4 个动作，"
             "这一层有两间冷却水池能接泉水")
HEAT_FREE = {"say", "talk", "look", "reject"}     # 灼热的楼层里不算动作、不掉血的
QUENCH = "_quench"                                # players.flags：喝了泉水，还有几个动作不怕灼热


def _heat(cur: Cursor, player_id: UUID, action: str) -> list[str]:
    """灼热（地底熔炉，房间 env.heat）：常驻，这一层每做一个动作掉血量上限的 heat 比例（打不打仗都算，不看防御，最少 1 点）。
    喝泉水（props.quench）停几个动作；装备 heat_resist 减半或免疫；头目转阶段把房间的 heat_mult 翻倍"""
    if action in HEAT_FREE:
        return []
    cur.execute("select room_id from players where id = %s", (player_id,))
    room = cur.fetchone()["room_id"]
    env = _room_env(cur, room)
    if not env.get("heat"):
        return []
    player = load_player(cur, player_id)
    if player.hp <= 0:
        return []
    if (left := int(player.flags.get(QUENCH, 0))) > 0:
        cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::int) where id = %s",
                    (QUENCH, left - 1, player_id))
        return [] if left > 1 else [f"{player.name}喝下去的那点凉意散了，热浪又贴了上来"]
    resist = min([float(e.get("value", 1)) for e in _fx(_worn(cur, player), "heat_resist")] or [1.0])
    if resist <= 0:
        return []
    dmg = max(1, round(player.max_hp * env["heat"] * env.get("heat_mult", 1) * resist))
    hurt, _ = _hurt_player(cur, player, dmg, "other", "灼热")
    return [f"热浪烤得{player.name}头昏眼花，掉了 {dmg} 点血"] + hurt


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


NOISY = {"say": "talk", "talk": "talk", "flee": "flee"}


def _floor_clock(cur: Cursor, player_id: UUID) -> list[str]:
    """地牢里每做一个动作：沙漠、梦境的出口到点重新连接；沙漏房里的沙子往下流"""
    player = load_player(cur, player_id)
    room = player.room_id
    if not dungeon.is_dungeon(room):
        return []
    facts = []
    theme = dungeon.data()["themes"][dungeon.floor_info(cur, room)["theme"]]
    if shift := theme.get("shifting_exits"):
        n = int(dungeon.floor_state(cur, room).get("acts", 0)) + 1
        dungeon.set_floor_state(cur, room, {"acts": n})
        if n % shift.get("every", 8) == 0 and dungeon.shift_exits(cur, room, random.randint(2, 3)):
            facts.append(shift.get("label") or f"远处传来石墙挪动的闷响，{theme['name']}里有几条路变了（走过的地方可能不再相通）")
    glass = player.flags.get(HOURGLASS)
    if glass and glass.get("room") != room:
        cur.execute("update players set flags = flags - %s where id = %s", (HOURGLASS, player.id))    # 及时走出来了
        glass = None
    timed = (_room_props(cur, room).get("dispensers") or {}).get("event") or {}
    if not glass and timed.get("service") == "timed":
        cur.execute("select 1 from dispenser_log where player_id = %s and room_id = %s and key = 'event'", (player.id, room))
        if not cur.fetchone():
            # 刚进沙漏房：身后的石门开始往下落
            glass = {"room": room, "left": int(timed.get("actions", 3)) + 1}
            cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::jsonb) where id = %s",
                        (HOURGLASS, Jsonb(glass), player.id))
    if glass:
        left = int(glass["left"]) - 1
        if left > 0:
            cur.execute("update players set flags = jsonb_set(flags, %s, to_jsonb(%s::int)) where id = %s",
                        ([HOURGLASS, "left"], left, player.id))
            facts.append(f"（沙漏里的沙子还够 {left} 个动作）")
        else:
            cur.execute("update players set flags = flags - %s where id = %s", (HOURGLASS, player.id))
            cur.execute("""insert into dispenser_log (player_id, room_id, key) values (%s, %s, 'event')
                           on conflict do nothing""", (player.id, room))
            name = dungeon.spawn_wanderer(cur, room)
            st = _stealth(player)
            st.detected, st.hidden = True, False
            _save_stealth(cur, player, st)
            facts.append(f"最后一粒沙子落了下去，石门轰地关死，墙里走出了{name}：得打一架才能出去")
    return facts


HOURGLASS = "_hourglass"                # players.flags：沙漏房里还剩几个动作 {"room", "left"}


def _room_props(cur: Cursor, room_id: str) -> dict:
    cur.execute("select props from rooms where id = %s", (room_id,))
    row = cur.fetchone()
    return (row and row["props"]) or {}      # 执行完加声响的动作（静默神殿）


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
    # 私酿的劲头：下到下一层就过去了（CHEER_FLOORS 层）
    for p in [load_player(cur, r["id"]) for r in _rows_by_names(cur, names)]:
        if e := _effect(p, "cheer"):
            e.left -= 1
            if e.left <= 0:
                p.effects.remove(e)
                facts.append(f"{p.name}身上那股浑身是劲的感觉过去了")
            _save_effects(cur, p)
    return facts


def _rows_by_names(cur: Cursor, names: list[str]) -> list[dict]:
    cur.execute("select id from players where name = any(%s)", (names,))
    return cur.fetchall()


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


def shop_directory(conn: Connection, npc: Npc) -> list[dict]:
    """村里别的店卖什么（不含这个 NPC 自己）：[{npc, room, items}]。客人找她要别家的货，她指路，不自己报价"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("""select distinct t.id, t.name, r.name as room, t.props->'sells' as sells from npcs n
                       join npc_templates t on t.id = n.template_id join rooms r on r.id = n.room_id
                       where n.alive and t.props ? 'sells' and t.id <> %s""", (npc.template.id,))
        shops = cur.fetchall()
        ids = [i for s in shops for i in s["sells"] or []]
        cur.execute("select id, name from item_templates where id = any(%s)", (ids,))
        names = dict((r["id"], r["name"]) for r in cur.fetchall())
    return [{"npc": s["name"], "room": s["room"], "items": [names[i] for i in s["sells"] or [] if i in names]} for s in shops]


def sellable(conn: Connection, npc: Npc, rare: Optional[str] = None, player_id: Optional[UUID] = None) -> list[dict]:
    """NPC 能卖的货（world.yaml 的 sells），给交易 AI 看：物品 id、名字、说明、伤害防御、按效果算的原价。
    rare 是这会儿有的稀罕货（rare_stock），标 rare，价钱固定；给了 player_id 就加上回礼解锁给他的货"""
    extra = []
    if player_id:
        with conn.transaction():
            extra = perk_sells(_cursor(conn), player_id, npc)
    ids = npc.template.props.get("sells", []) + extra + ([rare] if rare else [])
    if not ids:
        return []
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("select id, name, description, type, damage, defense, heal, props->'price' as price,"
                    " coalesce(props->>'kind', case when (props->>'alcohol')::boolean then 'drink' end) as kind"
                    " from item_templates where id = any(%s)", (ids,))
        return [r | ({"base_price": _rare_price(npc, r), "rare": True} if r["id"] == rare
                     else {"base_price": math.ceil(base_price(r) * markup(npc, r["id"]))})
                for r in cur.fetchall()]


LIKE_WORDS = {"food": "吃的", "drink": "酒水", "herb": "草药", "wine": "酒", "weapon": "武器", "armor": "护甲", "ore": "矿石",
              "misc": "杂物", "book": "书"}


def npc_menu(cur: Cursor, player_id: UUID, npc: Npc) -> Optional[dict]:
    """侧栏里店主下面的提示：这儿能办的事、墙上的货和标价（不用每次去问）。点一下把命令填进输入框。
    稀罕货只有问出来以后才列（问货时才判有没有），回礼解锁的本事和货也列上"""
    p = npc.template.props
    if npc.template.hostile:
        return None
    name, services = npc.name, []
    if p.get("leaderboard"):
        services += [{"text": "看排行榜", "fill": f"看{name}"},
                     {"text": "在地窖里说「传送到第 N 层」，直接去到过的传送石（每 5 层一块）", "fill": "传送到第 层"}]
    cur.execute("""select q.goal, q.done_flag, r.name as reward from quests q
                   left join item_templates r on r.id = q.reward_item
                   left join player_quests pq on pq.quest_id = q.id and pq.player_id = %s
                   where q.giver = %s and not q.hidden and coalesce(pq.status, '') <> 'rewarded' order by q.id""",
                (player_id, npc.template.id))
    for q in cur.fetchall():
        cur.execute("select flags ? %s as done from players where id = %s", (q["done_flag"] or "", player_id))
        done = bool(q["done_flag"]) and cur.fetchone()["done"]
        services.append({"text": f"委托：{q['goal']}" + (f"（奖励{q['reward']}）" if q["reward"] else "")
                         + ("，做完了，跟她说一声领奖" if done else ""), "fill": f"对{name} "})
    if likes := p.get("gift_likes"):
        services.append({"text": "喜欢收到：" + "、".join(LIKE_WORDS.get(k, k) for k in likes), "fill": f"给{name}"})
    cur.execute("""select t.name from item_instances i join item_templates t on t.id = i.template_id
                   where i.player_id = %s and t.props->>'refill_by' = %s""", (player_id, npc.template.id))
    if empties := [r["name"] for r in cur.fetchall()]:
        services.append({"text": f"续杯：{'、'.join(empties)}空了，找她灌满（不收钱）", "fill": f"对{name} 续杯"})
    if buys := p.get("buys"):
        services.append({"text": "收东西：" + "、".join(LIKE_WORDS.get(k, k) for k in buys.get("likes", [])) + "给价高",
                         "fill": f"把 卖给{name}"})
    if inn := p.get("inn"):
        services.append({"text": f"住店 {inn.get('price', 0)} 金币：回满血、醒酒", "fill": "住店"})
    if p.get("upgrades"):
        services += [{"text": "升级武器、防具（最多 +10）", "fill": f"对{name} 升级"},
                     {"text": "镶宝石 · 取宝石（地牢里掉的装备才带孔）", "fill": "把 镶到 上"}]
    if p.get("refine"):
        services.append({"text": "刷宝石品质（20 / 60 / 150 金币，宝石要先取下来）", "fill": "刷"})
    if p.get("uncurse"):
        services.append({"text": "解除装备上的诅咒", "fill": f"对{name} 解咒"})
    if p.get("lore"):
        services.append({"text": "讲地牢怪物的习性和打法", "fill": f"对{name} 怎么打"})
    table = p.get("return_gifts") or {}
    mine = perks(cur, player_id, npc.template.id)
    services += [{"text": g["text"], "fill": f"对{name} "} for g in table.values()
                 if g.get("perk") in mine and g.get("text") and g["perk"] not in ("map_scrap",)]
    ids = p.get("sells", []) + perk_sells(cur, player_id, npc)
    rare = _rare_now(cur, player_id, npc)
    goods = []
    if ids or rare:
        cur.execute("select id, name, damage, defense, heal, props->'price' as price from item_templates where id = any(%s)",
                    (ids + ([rare] if rare else []),))
        rows = {r["id"]: r for r in cur.fetchall()}
        for i in dict.fromkeys(ids + ([rare] if rare else [])):
            if r := rows.get(i):
                price = _rare_price(npc, r) if i == rare else clamp_price(r, base_price(r), markup(npc, i))
                goods.append({"name": r["name"], "price": price, "rare": i == rare, "fill": f"对{name} 买{r['name']}"})
    return {"services": services, "goods": goods} if services or goods else None


OFFER_WINDOW = "10 minutes"             # NPC 报的价多久内有效

# 偶尔进的稀罕货（world.yaml rare_sells）：客人问有什么卖的时判一次，不管中没中这段时间里不再判
RARE_WINDOW = "1 hour"
RARE_ASK_RE = re.compile(r"卖什么|卖啥|卖些|卖点|有什么|有啥|有没有|什么货|新货|进货|进了|看看货|货架|稀罕|好东西|宝贝|东西卖|什么可以买|能买")
RELUCTANT_FACT = "舍不得卖"              # 台词那边认这几个字，演舍不得又礼貌道谢


def rare_stock(conn: Connection, player_id: UUID, npc: Npc, text: str) -> Optional[str]:
    """这个客人现在能在 NPC 这儿买到的稀罕货（物品 id），没有就是 None。这段时间还没判过、他又在问有什么卖，就判一次"""
    rare = npc.template.props.get("rare_sells")
    if not rare:
        return None
    with conn.transaction():
        cur = _cursor(conn)
        if (row := _rare_row(cur, player_id, npc)) is not None:
            return row.get("item")
        if not RARE_ASK_RE.search(text):
            return None
        item = random.choice(rare["items"]) if _roll(rare.get("chance", 0.15)) else None
        cur.execute(
            """insert into player_npc_relations (player_id, npc_template, rare)
               values (%s, %s, jsonb_build_object('at', extract(epoch from now()), 'item', %s::text))
               on conflict (player_id, npc_template) do update set rare = excluded.rare""",
            (player_id, npc.template.id, item))
        return item


def _rare_row(cur: Cursor, player_id: UUID, npc: Npc) -> Optional[dict]:
    """这段时间里判过的稀罕货 {at, item}；没判过或者过期了是 None"""
    cur.execute(
        f"""select rare from player_npc_relations where player_id = %s and npc_template = %s and rare is not null
              and to_timestamp((rare->>'at')::float) > now() - interval '{RARE_WINDOW}'""",
        (player_id, npc.template.id))
    row = cur.fetchone()
    return row["rare"] if row else None


def _rare_now(cur: Cursor, player_id: UUID, npc: Npc) -> Optional[str]:
    return (_rare_row(cur, player_id, npc) or {}).get("item") if npc.template.props.get("rare_sells") else None


def _rare_price(npc: Npc, stats: dict) -> int:
    return round(base_price(stats) * npc.template.props["rare_sells"].get("markup", 2))


PERK_SELLS = {"map_scrap": "map_scrap"}  # 回礼解锁的货：本事 → 物品（诺艾尔好感 20 以后卖地图残片）


def perk_sells(cur: Cursor, player_id: UUID, npc: Npc) -> list[str]:
    return [item for perk, item in PERK_SELLS.items() if perk in perks(cur, player_id, npc.template.id)]


def _sells(cur: Cursor, player_id: UUID, npc: Npc) -> list[str]:
    """这个客人能在 NPC 这儿买的货：墙上的 + 回礼解锁的 + 这会儿有的稀罕货"""
    rare = _rare_now(cur, player_id, npc)
    return npc.template.props.get("sells", []) + perk_sells(cur, player_id, npc) + ([rare] if rare else [])


def _sell_rare(cur: Cursor, player: Player, npc: Npc, template_id: str, name: str, price: int) -> list[str]:
    """卖出稀罕货：只有一件，卖掉就没了；舍不得卖的那几样多一句，买走的东西记上是谁卖的"""
    cur.execute("update player_npc_relations set rare = rare || '{\"item\": null}' where player_id = %s and npc_template = %s",
                (player.id, npc.template.id))
    cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                (template_id, player.id, Jsonb({"sold_by": npc.template.id})))
    facts = [_deal(npc, player, name, price)]
    if template_id in npc.template.props["rare_sells"].get("reluctant", []):
        facts.insert(0, f"{npc.name}抱着{name}犹豫了好一会儿，{RELUCTANT_FACT}，最后还是依依不舍地推了过去")
    return facts


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
    with conn.transaction():
        unlocked = perk_sells(_cursor(conn), player_id, npc)
    if key not in npc.template.props.get("sells", []) + unlocked and key not in offers:
        return                              # 稀罕货不在这里：价钱固定，不砍价
    with conn.transaction():
        cur = _cursor(conn)
        if key.startswith("made:"):
            stats = offers[key].get("spec", {})
        else:
            cur.execute("select damage, defense, heal, props->'price' as price from item_templates where id = %s", (key,))
            stats = cur.fetchone()
        _put_offer(cur, player_id, npc, key, clamp_price(stats, price, markup(npc, key)))


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
                if key not in _sells(cur, player_id, npc):
                    raise ActionError(f"{npc.name}不卖这个")
                cur.execute("select name, damage, defense, heal, props->'price' as price from item_templates where id = %s",
                            (key,))
                stats = cur.fetchone()
                name = stats["name"]
                if key == _rare_now(cur, player_id, npc):
                    price = _rare_price(npc, stats)
                    patron = _pay(cur, player, price, npc)
                    return ActionResult(action="npc_sell", success=True,
                                        facts=_sell_rare(cur, player, npc, key, name, price) + patron)
            listed = clamp_price(stats, base_price(stats), markup(npc, key))
            price = 0 if price <= 0 and can_gift(affinity) else (
                min(listed, clamp_price(stats, price, markup(npc, key))) if price > 0 and not key.startswith("made:")
                else clamp_price(stats, price if price > 0 else base_price(stats), markup(npc, key)))       # 墙上的货不超过标价
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
        with conn.transaction():
            cur = _cursor(conn)
            if template_id not in _sells(cur, player_id, npc):
                raise ActionError(f"{npc.name}不卖这个")
            if template_id == _rare_now(cur, player_id, npc):
                # 稀罕货：只有一件，价钱固定
                cur.execute("select name, damage, defense, heal, props->'price' as price from item_templates where id = %s",
                            (template_id,))
                stats = cur.fetchone()
                player = load_player(cur, player_id, lock=True)
                price = _rare_price(npc, stats)
                patron = _pay(cur, player, price, npc)
                return ActionResult(action="npc_sell", success=True,
                                    facts=_sell_rare(cur, player, npc, template_id, stats["name"], price) + patron)
        offer = get_offers(conn, player_id, npc).get(template_id)
        with conn.transaction():
            cur = _cursor(conn)
            if offer is None:
                # 墙上的货直接下单：按标价收；AI 这回合给的价只能往下（交情好打个折），不能往上抬
                # （以前限在标价的两倍：诺艾尔嘴上说 20，交易那步收了 40）
                cur.execute("select damage, defense, heal, props->'price' as price from item_templates where id = %s",
                            (template_id,))
                stats = cur.fetchone()
                listed = clamp_price(stats, base_price(stats), markup(npc, template_id))
                offer = {"price": min(listed, clamp_price(stats, price or listed, markup(npc, template_id)))}
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
# 建议价、报价区间：rules.base_price、clamp_price

def markup(npc: Npc, key: str) -> float:
    """这家店对这样货的加价倍数（props.markup），不加价是 1"""
    return float((npc.template.props.get("markup") or {}).get(key, 1))


# 白送的门槛：NPC 现做的东西、AI 决定给不给的东西（地图），好感够高才白送，不然模型第一次见面就把剑白送了。
# 任务奖励、按条件给的（打完哥布林给钥匙）不受这个限制
GIFT_AFFINITY = 50


def can_gift(affinity: int) -> bool:
    return affinity >= GIFT_AFFINITY


# ============ 物品详情（界面上悬停显示）============
TYPE_NAMES = {"weapon": "武器", "armor": "防具", "consumable": "吃喝", "key": "钥匙", "misc": "杂物", "gem": "宝石"}
PART_NAMES = {"hand": "手", "head": "头", "chest": "胸", "neck": "项链", "feet": "脚", "ring": "戒指", "legs": "腿",
              "belt": "腰"}
STATE_NAMES = {"poison": "中毒", "bleed": "流血", "blind": "看不清", "corrode": "腐蚀", "restrained": "被缠住",
               "prone": "倒地", "stun": "定住"}
TAG_NAMES = {"undead": "亡灵", "animal": "野兽", "light_averse": "怕光的怪", "ranged": "远程的敌人"}
WHEN_NAMES = {"passive": "", "attack": "每次出手", "hit": "打中时", "kill": "杀死敌人时", "hurt": "被打中时",
              "fight": "每场第一次攻击", "enter": "走进新房间时"}
GIFT_FOR = {"ore": "莉娜", "wine": "麦琪", "book": "诺艾尔"}


def _effect_line(e: dict) -> str:
    v, k = e.get("value", 0), e.get("kind", "")
    what = {
        "splash": f"其他敌人各挨 {v} 点", "chain": f"电到另一个敌人 {v} 点", "bonus": f"伤害 +{v}",
        "pierce": f"无视 {v} 点防御", "guard": f"受到的伤害 -{v}", "heal": f"回 {v} 点血",
        "leech": f"吸回造成伤害的 {round(v * 100)}%", "status": f"让对方{STATE_NAMES.get(k, k)}",
        "status_all": f"让所有敌人{STATE_NAMES.get(k, k)}", "reflect": f"打你的敌人挨 {v} 点", "dodge": "完全躲开这一下",
        "resist": f"免疫{STATE_NAMES.get(k, k)}" if v == 0 else f"{STATE_NAMES.get(k, k)}的几率 ×{v}",
        "max_hp": f"血量上限 {'+' if v >= 0 else ''}{v}", "skill": f"{SKILL_NAMES.get(k, k)} +{v}", "gold": f"打怪掉的金币 +{round(v * 100)}%",
        "flee": "逃跑更容易" if v < 0 else "逃跑更难", "wade": "积水里行动不受影响", "darkvision": "暗处攻击不打折",
        "self_damage": f"自己掉 {v} 点血", "cheat_death": "本该倒下时留 10 点血",
        "aura": f"同房间的队友和自己{'攻击' if k == 'attack' else '防御'} +{v}",
        "defense": f"防御 +{stat_text(v)}", "light": f"光亮 +{v}（跟别的随身光源只取最亮的）",
        "crit": f"会心一击：伤害 ×{e.get('mult', 2)}（几件只取几率最高的一件）",
        "element": f"这一下算作{ {'fire': '火'}.get(k, k) }（打在怕{ {'fire': '火'}.get(k, k) }的弱点上伤害 ×{WEAK_MULT:g}）",
    }.get(e.get("do"), e.get("do", ""))
    if e.get("do") == "bonus" and not float(v).is_integer():
        what += f"（小数部分按几率多打 1 点）"
    cond = e.get("if") or {}
    pre = []                                # 条件在前，"什么时候"贴着效果：有队友倒下时，打中时伤害 +30
    if vs := e.get("vs"):
        pre.append("对" + "、".join(TAG_NAMES.get(t, t) for t in vs))
    if "hp_below" in cond:
        pre.append(f"自己血量低于 {round(cond['hp_below'] * 100)}% 时")
    if "target_hp_below" in cond:
        pre.append(f"对方血量低于 {round(cond['target_hp_below'] * 100)}% 时")
    if cond.get("ally_down"):
        pre.append("有队友倒下时")
    tail = (f"（{round(e['chance'] * 100)}% 几率）" if e.get("chance", 1) < 1 else "") \
        + ("（每层一次）" if e.get("once") == "per_floor" else "")
    when = WHEN_NAMES.get(e.get("when"), "")
    return "，".join(p for p in pre + [when + ("：" if when and not pre else "") + what] if p) + tail


# 诺艾尔（props.lore）平时就能口头讲地牢怪物的打法：客人问起怪物怎么打，她照这个说；60 好感的图鉴解锁的是侧栏里的数字
MONSTER_LORE = ("远程的怪（投石手、弓手、弩手、猎手、喷毒蛙、墨咒书记）不会靠近，被贴身会往后跳，最多跳两次就撞墙了；"
                "冲上去一句话里靠近加砍，砍中了它就跳不开；有掩体的地方它射不准，烟雾弹也能挡。"
                "会治疗的招魂修女要先杀，或者耗光她的念珠（最多治三次）。狗头人盾卫护着身后的同伴，同伴死光它就跑。"
                "狼、恶犬、石像鬼、头目鼻子灵，偷袭不了；野兽可以试着安抚，扔根肉骨头能引开。狼和恶犬成群，先打领头的那只，它一倒剩下的就跑。亡灵怕圣水和银器，怕光的怪在亮处软弱，"
                "带火把能压它们，但远程的怪会先射拿火把的人。拿弓弩的最好有人在前面挡着：一个人下去被怪贴上身，"
                "弓只剩一半的准头。头目都有自己的招数、不吃的东西和弱点，打之前先看清楚")


def monster_notes(npc: Npc) -> str:
    """诺艾尔的怪物图鉴：怪的习性，玩家点怪看"""
    p = npc.template.props
    notes = [f"{npc.name}（攻 {npc.template.attack} · 防 {npc.template.defense}）"]
    notes += [text for key, text in (("keen", "嗅觉、警觉极好：一进门就会发现你，偷袭不了"),
                                     ("animal", "野兽：可以试着安抚它避战（驯兽）"),
                                     ("undead", "亡灵：怕圣水"),
                                     ("light_averse", "怕光：光亮 70 以上攻击变弱，也会避开拿火把的人"),
                                     ("ranged", "远程：不会主动靠近，离远了射人；贴身了会往后跳，冲上去一口气砍中它就跳不开；掩体能挡一些"),
                                     ("healer", "会给受伤的同伴回血，一场最多三次：先打它，或者耗光它"),
                                     ("guard_allies", "举着挡板护着同伴：近战砍它身后的同伴，一半会被它挡下；同伴都倒下它就跑"))
              if p.get(key)]
    if hit := p.get("on_hit"):
        notes.append(f"打中人时有几率让人{EFFECT_NAMES.get(hit['kind'], STATE_NAMES.get(hit['kind'], hit['kind']))}")
    if (a := p.get("attacks", 1)) > 1:
        notes.append(f"一轮出手 {a} 次")
    if affix := ELITE_AFFIXES.get(p.get("affix")):
        notes.append(f"{affix['name']}：{affix['note']}")
    for sk in p.get("skills") or []:
        notes.append(_skill_note(sk))
    depth = p.get("dungeon", {}).get("depth", 0)
    if p.get("dungeon", {}).get("rank") == "boss" and depth >= ENRAGE_FROM:
        notes.append(f"血量低于 {round(ENRAGE_BELOW * 100)}% 会狂暴，攻击 +{ENRAGE_ATK}")
    if immune := p.get("immune"):
        notes.append("不吃：" + "、".join(EFFECT_NAMES.get(k, STATE_NAMES.get(k, k)) for k in immune))
    if "fire" in (p.get("resist_element") or []):
        notes.append("不怕火：火把、火油箭、余烬石打它都不算弱点")
    if st := p.get("stances"):
        notes.append(f"每出手 {st.get('every', 3)} 次在冷却和熔化之间换一次：冷却时又硬又手软，熔化时变软但下手更重；"
                     "熔化的时候泼一瓶泉水，它会淬火裂开，下一次出手之前防御归零、挨的伤害 ×1.5")
    if weak := p.get("weak"):
        notes.append(f"弱点：{WEAK_WORDS.get(weak, weak)}（{WEAK_HOW.get(weak, '')}伤害 ×{WEAK_MULT:g}）")
    if p.get("pack"):
        notes.append("成群的：一个房间最多两只，领头的那只一倒，剩下的夹着尾巴跑；扔根肉骨头（麦琪后厨卖）能引开，安抚容易些")
    return "\n".join(notes + ["（诺艾尔的怪物图鉴）"])


WEAK_HOW = {"fire": "火把、火油箭、带火的剑打它", "pierce": "破甲的武器打它", "light": "光亮 70 以上打它",
            "holy": "圣水泼它", "poison": "它中毒时", "water": "泼泉水（铸像要等它熔化）"}


def _skill_note(s: dict) -> str:
    """头目的一招，图鉴里怎么写"""
    waves = s.get("waves")
    when = ("血量低于 " + " 和 ".join(f"{round(w * 100)}%" for w in waves) + " 时各一次，" if waves else
            {"hp_below": f"血量低于 {round(s.get('value', 0) * 100)}% 时", "every": f"每出手 {s.get('value')} 次",
             "fight_start": "一开打就"}.get(s.get("when"), ""))
    helper = dungeon.data()["monsters"].get(s.get("kind"), {}).get("name", s.get("kind"))
    count = (f"每次一只，第 5 层只叫第一次" if waves else f"第 5 层一只、第 10 层起 {s.get('count', 1)} 只")
    what = {"summon": f"叫来帮手（{helper}，{count}，最多同时 {SUMMON_MAX} 只，"
                      f"不掉东西，主子一死就跑）",
            "status_all": f"让所有人{STATE_NAMES.get(s.get('kind'), s.get('kind'))}",
            "effect_all": f"让所有人{EFFECT_NAMES.get(s.get('kind'), s.get('kind'))}",
            "telegraph": "先预告一招大的，下一次出手放出来（预告那一轮狠狠砍它一下能打断）",
            "mark": f"判罪：盯住一个人，打他 +{s.get('bonus', 2)}，打中一次就了结",
            "self_heal": f"回 {round(s.get('heal', 0.2) * 100)}% 的血",
            "silence": f"禁声：之后出手 {s.get('turns', 2)} 次之内念不了书和卷轴"}.get(s.get("do"), s.get("do"))
    return f"招数：{when}{what}"


def _gem_lines(item: ItemInstance) -> list[str]:
    """宝石的详情：能镶在哪、这个品质镶上去是什么效果"""
    p, tier = item.template.props, item.props.get("tier", 1)
    out = [f"{GEM_TIER_WORDS[tier]}品质，能镶在{GEM_SLOT_WORDS.get(p.get('gem_slot'), '')}上（找莉娜，免费；取出要收钱）"
           + ("；没镶上去的可以找诺艾尔刷品质" if tier < GEM_TOP else "")]
    cats = ["weapon", "armor", "trinket"] if p.get("gem_slot") == "any" else [p.get("gem_slot")]
    for c in cats:
        out += [(f"镶在{GEM_SLOT_WORDS[c]}上：" if p.get("adapts") else "") + _effect_line(e) for e in gem_effects(p, tier, c)]
    if p.get("numeric"):
        caps = dungeon.gem_rules()["body_caps"]
        out.append(f"纯数值的宝石：全身合计最多防御 +{caps['defense']}、血量上限 +{caps['max_hp']}")
    return out


def item_detail(item: ItemInstance, curse_sense: bool = False) -> str:
    """给人看的物品详情：类型、数值、特殊效果、参考价、描述。诅咒平时看不出来，
    有诺艾尔的诅咒辨识笔记（回礼 curse_sense）才看得出；戴上了的自己知道"""
    t = item.template
    head = item.name + f"（{USE_KINDS[_use_kind(item)] if t.type == 'consumable' else TYPE_NAMES.get(t.type, t.type)}"
    if t.slot:
        head += f" · {'双手' if _prop(item, 'two_handed') else PART_NAMES.get(t.slot, t.slot)}"
    lines = [head + "）"] + (["酒馆武器桶里别人留下的：店里不收，用不上了可以放回桶里"] if item.props.get("donated") else [])
    stats = []
    if item.damage:
        stats.append(f"伤害 {stat_text(item.damage)}")
    if item.defense:
        stats.append(f"防御 {stat_text(item.defense)}")
    if item.heal:
        stats.append(f"回血 {item.heal * (HEAL_PCT_ALCOHOL if _is_alcohol(item) else HEAL_PCT)}%（按血量上限）")
    if item.harm:
        stats.append(f"有毒，掉 {item.harm} 点血" + ("（认得出来就没事）" if _prop(item, "harm_check") else ""))
    if light := _prop(item, "light"):
        stats.append(f"光亮 +{light}")
    if stats:
        lines.append("，".join(stats))
    extra = {
        "lights": "拿到手上就点燃（光亮 +35），在地牢里撑两层", "cursed": "诅咒：戴上就卸不下来",
        "cure": f"能治{STATE_NAMES.get(_prop(item, 'cure'), '')}", "refuel": "倒在快灭的火把上让它重新烧旺",
        "whet": f"这一层普通攻击伤害 +{_prop(item, 'whet')}", "room_light": f"这个房间光亮 +{_prop(item, 'room_light')}，走开就散",
        "holy": "泼向亡灵或怕光的怪能重创它", "throw": "能掷出去", "stun": "念出来房间里所有敌人定住",
        "recall": "在地牢里用，传回村子", "camp": "扎营时多回 30% 血", "alcohol": "酒，喝多了会醉",
        "sober": "喝了马上酒醒",
        "ranged": "远程：贴身难射、离远了准，射完要装填", "steady": "远近一样准",
        "ammo": "特殊弹药：装填时装上，下一发带上它的效果",
        "smoke": "摔碎了冒一大团烟：两轮里远程命中减半，敌我都算",
        "recover": "扔完了，打完这一架会捡回来",
        "bait": "安抚野兽时先扔一根过去，驯兽难度 -1（成不成都用掉一根）",
        "writable": "能写几句话（说「在纸条上写……」），写了就改不了",
    }
    if t.type == "consumable" and _use_kind(item) in MEDICINE_KINDS:
        lines.append("能给别人用：用药的人医药越高回得越多，给人用药能练医药")
    lines += [text for key, text in extra.items() if _prop(item, key)
              and (key != "cursed" or curse_sense or item.equipped_slot)]
    if block := _prop(item, "block"):
        lines.append(f"格挡：{round(block * 100)}% 几率完全挡下一击（跟闪避合计最多 {round(AVOID_CAP * 100)}%，挡不住中毒流血）")
    if mag := _prop(item, "magazine"):
        lines.append(f"一匣 {mag} 发，还剩 {item.props.get('shots', mag)} 发")
    if (steps := _prop(item, "reload_steps")) and steps > 1:
        lines.append(f"装填要 {steps} 次")
    if item.props.get("loaded_ammo"):
        lines.append("装着一发特殊弹药")
    if uses := _prop(item, "uses"):
        lines.append(f"能用 {item.props.get('uses_left', uses)} 次")
    if gift := _prop(item, "gift"):
        lines.append(f"{GIFT_FOR.get(gift, '')}喜欢的小礼物（每天收一件）" if _prop(item, "gift_value")
                     else f"{GIFT_FOR.get(gift, '')}最想要的礼物")
    if t.type != "gem":                     # 宝石的 effects 是按品质的数组，下面按这颗的品质单独列
        lines += [_effect_line(e) for e in _prop(item, "effects") or []]
    if t.type == "gem":
        lines += _gem_lines(item)
    if (n := item.props.get("sockets")):
        gems = item.props.get("gems") or []
        lines.append(f"宝石孔 {len(gems)}/{n}" + ("：" + "、".join(g["name"] for g in gems) if gems else "（找莉娜镶宝石）"))
        lines += ["  " + _effect_line(e) for e in item.props.get("gem_fx") or []]
    if price := base_price(item_stats(item)):
        lines.append(f"参考价 {price} 金币")
    lines.append(item.description)
    return "\n".join(lines)


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


def made_allowed(npc: Npc, spec: dict) -> bool:
    """这个 NPC 现在还做这种东西吗。knockout_only：只调下了药的特调（麦琪放倒闹事的人），别的酒水不现做"""
    caps = create_limits(npc).get(spec.get("kind"))
    return caps is not None and (not caps.get("knockout_only") or bool(spec.get("knockout")))


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
        if caps.get("knockout_only") and not spec.get("knockout"):
            raise ActionError(f"{npc.name}不现做酒水，吧台上有什么卖什么")
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


# 现做的杂物（made_misc）什么用处都没有：名字像有用的东西就不做，不然玩家拿到"麻绳"爬不了、"火把"照不亮
FAKE_MISC_WORDS = ("绳", "钥匙", "火把", "火炬", "灯", "卷轴", "箭", "弹", "药", "水晶", "地图", "绷带", "帐篷", "磨刀石")


def _no_fake(cur: Cursor, npc: Npc, spec: dict) -> None:
    name = spec["name"]
    if spec["kind"] != "misc":
        # 吃的喝的、武器可以现做，但不能跟正式的东西同名（两杯效果不一样的"矮人黑啤"）
        cur.execute("select 1 from item_templates where id not like 'made%%' and name = %s", (name,))
        if cur.fetchone():
            raise ActionError(f"{name}是正经的货，{npc.name}不另做一份")
        return
    cur.execute("""select name from item_templates where id not like 'made%%' and length(name) >= 2
                   and strpos(%s, name) > 0 limit 1""", (name,))
    if cur.fetchone() or any(w in name for w in FAKE_MISC_WORDS):
        raise ActionError(f"{npc.name}现做不出能用的{name}：有用处的东西得是店里正经的货")


def _make(cur: Cursor, player_id: UUID, spec: dict) -> None:
    cur.execute("insert into item_instances (template_id, player_id, props) values (%s, %s, %s)",
                (MADE_TEMPLATES[spec["kind"]], player_id, Jsonb({k: v for k, v in spec.items() if k != "kind"})))


def npc_gift(conn: Connection, player_id: UUID, npc: Npc, spec: dict) -> ActionResult:
    """NPC 高兴了现造一件白送（请客），不用报价；同一个玩家 3 分钟一次"""
    try:
        with conn.transaction():
            cur = _cursor(conn)
            player = load_player(cur, player_id, lock=True)
            _no_fake(cur, npc, spec)
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
        _no_fake(cur, npc, spec)
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
        if not made_allowed(npc, spec):
            raise ActionError(f"{npc.name}现在不做{spec['name']}了")     # 现做关掉以前开过的价
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
    """NPC 做过、现在还做的东西 [{name, spec, price}]，最近的在前"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("select name, spec, price from npc_goods where npc_template = %s order by updated_at desc limit %s",
                    (npc.template.id, GOODS_SHOWN))
        return [g for g in cur.fetchall() if made_allowed(npc, g["spec"] or {})]


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


def last_bought(conn: Connection, player_id: UUID, npc: Npc) -> Optional[str]:
    """这个客人上次在这个 NPC 这儿买的东西（"再来一捆"说的就是它）"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("""select entry from npc_memory_log where player_id = %s and npc_template = %s and entry like '%%卖给了%%'
                       order by id desc limit 1""", (player_id, npc.template.id))
        row = cur.fetchone()
    m = re.search(rf"{re.escape(npc.name)}把(.+?)(?: ×\d+)?卖给了", row["entry"]) if row else None
    return m[1] if m else None


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


# 好感像可攻略角色：最高一档是特别喜欢的人。(下限, 关系)，每个玩家各算各的
AFFINITY_TIERS = [(95, "特别喜欢的人"), (80, "喜欢"), (60, "心动"), (40, "朋友"), (20, "熟客"), (0, "客人"),
                  (-30, "戒备"), (-100, "讨厌")]
AFFINITY_GATES = (60, 80, 95)           # 聊天涨不过这几道坎（停在坎下一点），要送她心爱的礼物才跨得过去
CHAT_STEP = ((60, 1), (40, 2))          # 好感到了这个数，聊天一回合最多再涨几（越往上越慢）
CHAT_DAILY_FROM, CHAT_DAILY = 40, 10    # 好感 40 以上，每天靠聊天最多涨 10


def affinity_word(affinity: int) -> str:
    return next(word for low, word in AFFINITY_TIERS if affinity >= low)


def adjust_affinity(conn: Connection, player_id: UUID, npc_id: UUID, delta: int) -> ActionResult:
    """对话 AI 提议调整好感度，单次幅度和总范围都由这里限制，AI 给多大都没用。
    往上涨时越高越慢（CHAT_STEP）、40 以上每天有上限（CHAT_DAILY）、过不了 AFFINITY_GATES 的坎；往下掉不受这些限制"""
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
                """select affinity, chat_gain, chat_day = (now() at time zone 'Asia/Shanghai')::date as today
                   from player_npc_relations where player_id = %s and npc_template = %s for update""",
                (player.id, npc.template.id))
            row = cur.fetchone() or {"affinity": 0, "chat_gain": 0, "today": False}
            now, gained = row["affinity"], row["chat_gain"] if row["today"] else 0
            wanted, why = delta, ""
            if delta > 0:
                delta = min(delta, next((step for low, step in CHAT_STEP if now >= low), AFFINITY_STEP))
                if now >= CHAT_DAILY_FROM and delta > CHAT_DAILY - gained:
                    delta, why = max(0, CHAT_DAILY - gained), "今天已经聊得够多了，改天再来"
                if (gate := next((g for g in AFFINITY_GATES if now < g), None)) and now + delta >= gate:
                    delta, why = max(0, gate - 1 - now), f"要有特别的契机（比如送{npc.name}最想要的东西）才能更进一步"
            value = max(lo, min(hi, now + delta))
            cur.execute(
                """insert into player_npc_relations (player_id, npc_template, affinity, chat_day, chat_gain)
                   values (%(p)s, %(t)s, %(v)s, (now() at time zone 'Asia/Shanghai')::date, %(g)s)
                   on conflict (player_id, npc_template) do update
                     set affinity = %(v)s, chat_day = excluded.chat_day, chat_gain = %(g)s""",
                {"p": player.id, "t": npc.template.id, "v": value, "g": gained + max(0, value - now)},
            )
        trend = "上升" if value > now else "下降" if value < now else "没有变化"
        fact = f"{npc.name}对{player.name}的好感{trend}（当前 {value}，{affinity_word(value)}）"
        if wanted > 0 and value == now and why:
            fact = f"{npc.name}对{player.name}的好感停在 {value}（{affinity_word(value)}）：{why}"
        return ActionResult(action="affinity", success=True, facts=[fact])
    except ActionError as e:
        return ActionResult(action="affinity", success=False, facts=[str(e)])


# ============ 回礼 ============
# NPC 的 props.return_gifts：好感到了某一档（20/40/60/80/100），玩家来聊天时送一次；领过的档记在 player_npc_relations.gifts，
# 好感掉下去再涨回来也不会重领。送的是东西（item），或者解锁一样本事（perk：九折、买地图残片、看出诅咒、怪物图鉴……）
GIFT_BACK_FACT = "回礼"                  # 台词那边认这两个字，演送礼
REFILL_FACT = "说「续杯」就能灌满"       # 送的东西用完能找她续：台词那边认这句，台词里一定要说到


def return_gift(conn: Connection, player_id: UUID, npc: Npc) -> list[ActionResult]:
    """聊天时看看有没有该送的回礼：送最低的那一档（一次一档）"""
    gifts = npc.template.props.get("return_gifts") or {}
    if not gifts:
        return []
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("select affinity, gifts from player_npc_relations where player_id = %s and npc_template = %s for update",
                    (player_id, npc.template.id))
        row = cur.fetchone()
        if row is None:
            return []
        due = sorted(int(t) for t in gifts if int(t) <= row["affinity"] and int(t) not in (row["gifts"] or []))
        if not due:
            return []
        tier, g = due[0], gifts[str(due[0])] if str(due[0]) in gifts else gifts[due[0]]
        player = load_player(cur, player_id)
        cur.execute("update player_npc_relations set gifts = array_append(gifts, %s) where player_id = %s and npc_template = %s",
                    (tier, player_id, npc.template.id))
        facts = [f"{npc.name}送给{player.name}：{g['text']}（{GIFT_BACK_FACT}，好感到了 {tier}）"]
        if scene := g.get("scene"):
            facts.append(f"{npc.name}{scene}")               # 送的时候的动作（诺艾尔从怀里的书里抽出书签）
        if item := g.get("item"):
            if item == "lina_blade":
                facts += _exclusive_blade(cur, player, npc)
            else:
                cur.execute("insert into item_instances (template_id, player_id) values (%s, %s)", (item, player_id))
                cur.execute("select props ? 'empty' as refill from item_templates where id = %s", (item,))
                if cur.fetchone()["refill"]:
                    # 用完会空的（麦琪的酒壶、迷药）：告诉他能回来续
                    facts.append(f"用完了会空，回来找{npc.name}{REFILL_FACT}")
        return [ActionResult(action="gift_back", success=True, facts=facts)]


# 服务类的本事（熟客价、传承锻造、刷新词条、卖地图残片）要当前好感还在那一档以上：把人惹毛了就没了；
# 知识类的（辨咒笔记、怪物图鉴）学会了就是会了
SERVICE_PERKS = {"discount", "transfer", "reroll", "map_scrap"}


def perks(cur: Cursor, player_id: UUID, npc_template: Optional[str] = None) -> set[str]:
    """这个玩家从回礼里解锁了哪些本事（npc_template 给了就只看这个 NPC 的）"""
    cur.execute("""select r.gifts, r.affinity, t.props->'return_gifts' as table from player_npc_relations r
                   join npc_templates t on t.id = r.npc_template
                   where r.player_id = %s and cardinality(r.gifts) > 0 and t.props ? 'return_gifts'"""
                + (" and r.npc_template = %s" if npc_template else ""),
                (player_id, npc_template) if npc_template else (player_id,))
    return {g["perk"] for r in cur.fetchall() for t, g in (r["table"] or {}).items()
            if int(t) in r["gifts"] and g.get("perk") and (g["perk"] not in SERVICE_PERKS or r["affinity"] >= int(t))}


def _once_per_floor(cur: Cursor, player: Player, key: str) -> bool:
    """每层地牢一次的东西（不熄的羽毛、无底酒壶、守护书签、诺艾尔的古书）：这一层用过了就是 False，没用过记下来返回 True"""
    here = ":".join(map(str, dungeon.parse_room(player.room_id))) if dungeon.is_dungeon(player.room_id) else "surface"
    if player.flags.get(key) == here:
        return False
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::text) where id = %s", (key, here, player.id))
    player.flags[key] = here
    return True


def _carries(cur: Cursor, player: Player, prop: str) -> Optional[ItemInstance]:
    """身上（背包里、装备着都算）带着有这个特性的东西"""
    cur.execute("""select i.id from item_instances i join item_templates t on t.id = i.template_id
                   where i.player_id = %s and (t.props ? %s or i.props ? %s) limit 1""", (player.id, prop, prop))
    row = cur.fetchone()
    return load_items(cur, "i.id = %s", (row["id"],))[0] if row else None


def npc_bonds(conn: Connection, player_id: UUID, npc: Npc) -> list[str]:
    """NPC 台词里可以提的别人的交情：这个玩家跟别的 NPC（熟客以上），别的玩家跟这个 NPC（朋友以上）"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("""select t.name, r.affinity from player_npc_relations r join npc_templates t on t.id = r.npc_template
                       where r.player_id = %s and r.npc_template <> %s and r.affinity >= 20 order by r.affinity desc""",
                    (player_id, npc.template.id))
        bonds = [f"他跟{r['name']}是{affinity_word(r['affinity'])}" for r in cur.fetchall()]
        cur.execute("""select p.name, r.affinity from player_npc_relations r join players p on p.id = r.player_id
                       where r.npc_template = %s and r.player_id <> %s and r.affinity >= 40
                       order by r.affinity desc limit 3""", (npc.template.id, player_id))
        return bonds + [f"你跟另一个客人{r['name']}是{affinity_word(r['affinity'])}" for r in cur.fetchall()]
