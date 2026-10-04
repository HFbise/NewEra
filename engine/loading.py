"""Loading game state from the database into pydantic models. / 从数据库读出状态"""
from collections import defaultdict
from typing import Callable, Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg import Connection, Cursor
from psycopg.rows import dict_row

from schema import Dispenser, Duel, Feature, ItemInstance, Npc, OtherPlayer, Player, Room, RoomExit, RoomView

from .core import ActionError, FORAGE_CHANCE, ONLINE_WINDOW, STATUS_MAX
from . import afflictions, duels, environment, helpers, parties


ITEM_SELECT = """
select i.id, i.quantity, i.room_id, i.player_id, i.npc_id, i.equipped_slot, i.props,
       row_to_json(t) as template
from item_instances i join item_templates t on t.id = i.template_id
"""


NPC_SELECT = """
select n.id, n.room_id, n.hp, n.alive, n.memory, n.status, n.effects, row_to_json(t) as template
from npcs n join npc_templates t on t.id = n.template_id
"""


def cursor(conn: Connection) -> Cursor:
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
        cur = cursor(conn)
        player = load_player(cur, player_id, lock=True)
        if player.party_id:
            parties.leave_party(cur, player)
        for item in load_items(cur, "i.player_id = %s", (player_id,), lock=True):
            helpers.move_item(cur, item, room_id=player.room_id)
        # 开发期账号是 server.py 塞进 auth.users 的假用户；正式版走 Supabase Auth 的删除接口
        cur.execute("delete from auth.users where id = %s", (player_id,))


def _refresh_room(cur: Cursor, room_id: str) -> None:
    """刷新：NPC 复活、门自动锁回、物品重新出现、看店的 NPC 扶起倒下的人。
    不用后台任务，有人在这个房间（发命令或页面轮询）时顺手检查，没人的房间不用管"""
    respawn_npcs(cur, room_id)
    keeper_revive(cur, room_id)
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


def set_revive_hook(hook: Callable[[str, str, UUID, str, dict, str], None]) -> None:
    """server 注册扶人时让 AI 现写一句话的回调（模块共用命名空间，赋值要在这个模块里做才看得到）"""
    global REVIVE_HOOK
    REVIVE_HOOK = hook


def keeper_revive(cur: Cursor, room_id: str) -> list[str]:
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
    cur.execute(f"""update players set hp = max_hp + {afflictions.RESTORE_MAX_HP}, max_hp = max_hp + {afflictions.RESTORE_MAX_HP},
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


def respawn_npcs(cur: Cursor, room_id: str, found: bool = False) -> list[str]:
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
        cur = cursor(conn)
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
        if env := environment.env_text(cur, room):
            room.details = (room.details + "\n" + env).strip()
        duels.end_stale_duels(cur)
        duel = duels.active_duel(cur, player.id)
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
