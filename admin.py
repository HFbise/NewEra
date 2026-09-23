"""
管理后台接口（页面是 static/admin.html）。
- 环境变量 ADMIN_PASSWORD 设了才开；登录拿到签名令牌，7 天有效，请求头 X-Admin-Token 带上
- 世界：看每个房间现在的状态、一键重置（按 world.yaml 重新 seed，玩家不动）
- 玩家：列表、复活、传送回出生点、清状态、改 HP、删角色；操作会在房间里发一条系统动态
- 动态：全服事件流，带玩家输入原话和解析结果，排查 AI 解析、叙事问题用
- AI 用量：ai_calls 按天、按玩家汇总，最近的调用
"""
import hashlib
import hmac
import os
import re
import time
from typing import Literal, Optional
from uuid import UUID

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel

import engine
import seed as seeding
from db import pool

TOKEN_DAYS = 7
WORLD_FILE = os.path.join(os.path.dirname(__file__), "world.yaml")


def _password() -> str:
    pw = os.environ.get("ADMIN_PASSWORD")
    if not pw:
        raise HTTPException(403, "后台没开：服务器没设 ADMIN_PASSWORD")
    return pw


def _sign(exp: int) -> str:
    # 令牌用密码签名：改了 ADMIN_PASSWORD 之前发出去的令牌全部失效
    return hmac.new(_password().encode(), f"admin:{exp}".encode(), hashlib.sha256).hexdigest()


def require_admin(x_admin_token: str = Header(default="")) -> None:
    exp, _, sig = x_admin_token.partition(".")
    if not exp.isdigit() or int(exp) < time.time() or not hmac.compare_digest(sig, _sign(int(exp))):
        raise HTTPException(401, "请重新登录后台")


router = APIRouter(prefix="/api/admin")
protected = APIRouter(dependencies=[Depends(require_admin)])


class AdminLogin(BaseModel):
    password: str


@router.post("/login")
def login(req: AdminLogin):
    if not hmac.compare_digest(req.password.encode(), _password().encode()):
        raise HTTPException(403, "密码不对")
    exp = int(time.time()) + TOKEN_DAYS * 86400
    return {"token": f"{exp}.{_sign(exp)}"}


def _rows(conn, sql: str, params=()) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()


def _announce(conn, room_id: str, text: str) -> None:
    """给房间发一条系统动态，在场的人都能看到"""
    conn.execute("insert into events (room_id, kind, observer) values (%s, 'admin', %s)",
                 (room_id, f"【系统】{text}"))


# ============ 世界 ============

@protected.get("/world")
def world():
    with pool.connection() as conn:
        rooms = _rows(conn, "select id, name from rooms order by id")
        npcs = _rows(conn, """select n.room_id, t.name, n.hp, t.max_hp, n.alive, n.died_at, n.status
                              from npcs n join npc_templates t on t.id = n.template_id order by t.name""")
        ground = _rows(conn, """select i.room_id, t.name, i.quantity from item_instances i
                                join item_templates t on t.id = i.template_id where i.room_id is not null order by t.name""")
        carried = _rows(conn, """select n.room_id, nt.name as npc, t.name, i.quantity from item_instances i
                                 join item_templates t on t.id = i.template_id join npcs n on n.id = i.npc_id
                                 join npc_templates nt on nt.id = n.template_id order by t.name""")
        exits = _rows(conn, """select e.room_id, e.direction, r.name as to_name, e.locked, e.key_item
                               from room_exits e join rooms r on r.id = e.to_room order by e.direction""")
        features = _rows(conn, """select room_id, name, max_tier, uses_left, max_uses, used_at, respawn_seconds
                                  from room_features order by key""")
        players = _rows(conn, f"""select room_id, name, hp, max_hp,
                                    coalesce(last_active_at > now() - interval '{engine.ONLINE_WINDOW}', false) as awake
                                  from players order by name""")
    by_room = lambda rows, rid: [r for r in rows if r["room_id"] == rid]
    return [{**r, "npcs": by_room(npcs, r["id"]), "ground": by_room(ground, r["id"]),
             "carried": by_room(carried, r["id"]), "exits": by_room(exits, r["id"]),
             "features": by_room(features, r["id"]), "players": by_room(players, r["id"])} for r in rooms]


@protected.post("/reset")
def reset():
    """按 world.yaml 重新布置 NPC、地上物品、门锁、地形；玩家的位置、背包、标记、NPC 记忆和好感度都不动"""
    with open(WORLD_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    with pool.connection() as conn:
        seeding.seed(conn, data)
        with conn.transaction():
            for rid in data["rooms"]:
                _announce(conn, rid, "管理员重置了世界：NPC、物品、门锁和地形都恢复了初始状态。")
    return {"ok": True}


# ============ 玩家 ============

@protected.get("/players")
def players():
    with pool.connection() as conn:
        rows = _rows(conn, f"""
            select p.id, p.name, p.room_id, r.name as room_name, p.hp, p.max_hp, p.attack, p.defense, p.flags,
                   p.status, p.party_id, f.name as following, p.last_active_at, p.created_at,
                   coalesce(p.last_active_at > now() - interval '{engine.ONLINE_WINDOW}', false) as awake
            from players p join rooms r on r.id = p.room_id left join players f on f.id = p.following
            order by p.last_active_at desc nulls last, p.name""")
        items = _rows(conn, """select i.player_id, t.name, i.quantity, i.equipped_slot from item_instances i
                               join item_templates t on t.id = i.template_id where i.player_id is not null order by t.name""")
    for p in rows:
        p["inventory"] = [i for i in items if i["player_id"] == p["id"]]
        p["party"] = [q["name"] for q in rows if p["party_id"] and q["party_id"] == p["party_id"] and q is not p]
    return rows


class PlayerOp(BaseModel):
    op: Literal["revive", "home", "clear_status", "set_hp", "delete"]
    hp: Optional[int] = None


@protected.post("/players/{player_id}")
def player_op(player_id: UUID, req: PlayerOp):
    with pool.connection() as conn:
        with conn.transaction():
            cur = engine._cursor(conn)
            try:
                p = engine.load_player(cur, player_id, lock=True)
            except engine.ActionError:
                raise HTTPException(404, "没有这个玩家")
        if req.op == "delete":
            engine.delete_player(conn, player_id)
            with conn.transaction():
                _announce(conn, p.room_id, f"{p.name}被管理员删除了，身上的东西掉在了地上。")
            return {"ok": True}
        with conn.transaction():
            if req.op == "revive":
                conn.execute("update players set hp = max_hp, status = null where id = %s", (player_id,))
                _announce(conn, p.room_id, f"{p.name}被管理员治好了，HP 回满，身上的负面状态也消失了。")
            elif req.op == "clear_status":
                conn.execute("update players set status = null where id = %s", (player_id,))
                _announce(conn, p.room_id, f"管理员解除了{p.name}身上的负面状态。")
            elif req.op == "set_hp":
                if req.hp is None:
                    raise HTTPException(400, "要填 hp")
                hp = max(0, min(p.max_hp, req.hp))
                conn.execute("update players set hp = %s where id = %s", (hp, player_id))
                _announce(conn, p.room_id, f"管理员把{p.name}的 HP 改成了 {hp}。")
            elif req.op == "home" and p.room_id != engine.START_ROOM:
                conn.execute("update players set room_id = %s, following = null where id = %s",
                             (engine.START_ROOM, player_id))
                _announce(conn, p.room_id, f"{p.name}被管理员传送走了。")
                _announce(conn, engine.START_ROOM, f"{p.name}被管理员传送了过来。")
    return {"ok": True}


# ============ 动态 ============

@protected.get("/events")
def events(limit: int = 100, before: Optional[int] = None, player: Optional[str] = None,
           room: Optional[str] = None):
    """全服事件流，新的在前；before 翻页，player / room 筛选"""
    with pool.connection() as conn:
        return _rows(conn, """
            select e.id, e.created_at, p.name as player, e.room_id, r.name as room_name, e.kind,
                   e.input, e.meta, e.facts, e.narrative, e.observer
            from events e join rooms r on r.id = e.room_id left join players p on p.id = e.player_id
            where (%(before)s::bigint is null or e.id < %(before)s)
              and (%(player)s::text is null or p.name = %(player)s)
              and (%(room)s::text is null or e.room_id = %(room)s)
            order by e.id desc limit %(limit)s""",
            {"before": before, "player": player or None, "room": room or None, "limit": min(limit, 500)})


# ============ AI 用量 ============

@protected.get("/ai")
def ai_usage(days: int = 7, tz: str = "UTC"):
    """按天（浏览器所在时区）和按玩家汇总，外加最近 50 次调用"""
    if not re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z_+\-]+)*", tz):
        tz = "UTC"
    params = {"days": max(1, min(days, 90)), "tz": tz}
    since = "a.created_at > now() - make_interval(days => %(days)s)"
    with pool.connection() as conn:
        if not conn.execute("select 1 from pg_timezone_names where name = %s", (tz,)).fetchone():
            params["tz"] = "UTC"
        totals = _rows(conn, f"""
            select count(*) as calls, coalesce(avg(ok::int), 0)::float as ok_rate,
                   coalesce(sum(input_tokens), 0) as input, coalesce(sum(output_tokens), 0) as output,
                   coalesce(avg(latency_ms), 0)::int as avg_ms,
                   coalesce(percentile_cont(0.95) within group (order by latency_ms), 0)::int as p95_ms
            from ai_calls a where {since}""", params)[0]
        daily = _rows(conn, f"""
            select to_char(date_trunc('day', a.created_at at time zone %(tz)s), 'MM-DD') as day, a.kind,
                   count(*) as calls, avg(a.ok::int)::float as ok_rate, avg(a.latency_ms)::int as avg_ms,
                   sum(a.input_tokens) as input, sum(a.output_tokens) as output
            from ai_calls a where {since} group by 1, 2 order by 1 desc, 2""", params)
        by_player = _rows(conn, f"""
            select coalesce(p.name, '（已删除）') as player, count(*) as calls,
                   sum(a.input_tokens) as input, sum(a.output_tokens) as output, avg(a.latency_ms)::int as avg_ms
            from ai_calls a left join players p on p.id = a.player_id
            where {since} group by 1 order by 2 desc""", params)
        recent = _rows(conn, """
            select a.created_at, coalesce(p.name, '（已删除）') as player, a.kind, a.model, a.input_tokens,
                   a.output_tokens, a.latency_ms, a.ok
            from ai_calls a left join players p on p.id = a.player_id order by a.id desc limit 50""")
    return {"tz": params["tz"], "totals": totals, "daily": daily, "by_player": by_player, "recent": recent}


router.include_router(protected)
