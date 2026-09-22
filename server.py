"""
开发用简易 Web 服务。
- 本地跑 python server.py 只绑 127.0.0.1；部署（Render）时用 uvicorn 绑 0.0.0.0，见 render.yaml
- 登录：角色名 + 角色密码；设了 ACCESS_CODE 环境变量时还要邀请码
- 一回合的链路：规则解析（commands.py）→ 有认不出的再交给 AI 解析 → 规则引擎执行
  → AI 叙事（含 NPC 对话、给东西、好感度提议，规则引擎再校验）→ 写 events
- AI 后端见 ai.py（默认智谱 glm-4.7-flash），没配 key 时退回纯规则模式：没有叙事，talk 用占位逻辑给 requires 物品

用法: python server.py  然后打开 http://127.0.0.1:8000
"""
import hashlib
import hmac
import json
import os
import secrets
from typing import Optional
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

import ai
import commands
import engine
from schema import ActionResult, RoomView, dir_name

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
# 一个回合会占着连接等 AI（可能十几秒），连接数给多一点，免得几个人同时玩就排队
pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=10, open=True)
app = FastAPI()

START_ROOM = "square"


class LoginReq(BaseModel):
    name: str
    password: str
    access_code: str = ""


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def _check_password(password: str, stored: str) -> bool:
    salt, _ = stored.split("$")
    return hmac.compare_digest(_hash_password(password, bytes.fromhex(salt)), stored)


class LogoutReq(BaseModel):
    player_id: UUID


class CommandReq(BaseModel):
    player_id: UUID
    text: str
    after: Optional[int] = None         # 前端已经看过的最后一条房间动态 id，不给就不带动态


def state(conn, view: RoomView, after: Optional[int] = None) -> dict:
    """给前端侧栏看的当前状态，带上短编号方便对照。
    after 给了就顺便带回这个房间 id 比它大的别人的动态；last_event_id 是前端下次要传的 after"""
    by_id = {uid: ref for ref, uid in view.refs.items()}
    cur = conn.cursor()
    # 先取最大 id 再查动态，查询中途插进来的新动态留到下次，不会漏
    cur.execute("select coalesce(max(id), 0) from events")
    last_event_id = cur.fetchone()[0]
    events = []
    if after is not None:
        cur.execute(
            """select id, observer from events
               where room_id = %s and id > %s and id <= %s and observer is not null
                 and player_id is distinct from %s
               order by id limit 30""",
            (view.room.id, after, last_event_id, view.player.id),
        )
        events = [{"id": i, "text": t} for i, t in cur.fetchall()]
    cur.execute(
        f"""select name, hp, coalesce(last_active_at > now() - interval '{engine.ONLINE_WINDOW}', false), status
            from players where room_id = %s and id <> %s order by name""",
        (view.room.id, view.player.id),
    )
    others = [{"name": n, "hp": hp, "awake": awake, "status": st and st["label"]}
              for n, hp, awake, st in cur.fetchall()]
    cur.execute("select id, name from rooms where id = any(%s)", ([e.to_room for e in view.exits],))
    room_names = dict(cur.fetchall())
    conn.commit()
    return {
        "player": view.player.model_dump(mode="json"),
        "room": view.room.model_dump(),
        "exits": [{"direction": e.direction, "label": dir_name(e.direction), "to": room_names[e.to_room],
                   "locked": e.locked} for e in view.exits],
        "items": [{"ref": by_id[i.id], "name": i.name, "quantity": i.quantity} for i in view.items],
        "npcs": [{"ref": by_id[n.id], "name": n.name, "hp": n.hp, "max_hp": n.template.max_hp,
                  "status": n.status and n.status.label} for n in view.npcs],
        "inventory": [{"ref": by_id[i.id], "name": i.name, "quantity": i.quantity,
                       "equipped": i.equipped_slot} for i in view.inventory],
        "others": others,
        "party": view.party,
        "invites": view.invites,
        "events": events,
        "last_event_id": last_event_id,
    }


@app.get("/api/config")
def config():
    return {"access_code": bool(os.environ.get("ACCESS_CODE"))}


@app.post("/api/login")
def login(req: LoginReq):
    # 设了 ACCESS_CODE（部署到公网时）就要邀请码，防止陌生人进来刷 AI 额度
    code = os.environ.get("ACCESS_CODE")
    if code and not hmac.compare_digest(req.access_code.strip().encode(), code.encode()):
        raise HTTPException(403, "邀请码不对")
    name = req.name.strip()
    if not name or len(name) > 20:
        raise HTTPException(400, "名字 1 到 20 个字")
    if len(req.password) < 4:
        raise HTTPException(400, "密码至少 4 位")
    with pool.connection() as conn:
        row = conn.execute("select id, password_hash from players where name = %s", (name,)).fetchone()
        if row:
            pid, stored = row
            if stored is None:
                # 加密码之前建的老角色：第一次登录填的密码就成为它的密码
                with conn.transaction():
                    conn.execute("update players set password_hash = %s where id = %s",
                                 (_hash_password(req.password), pid))
            elif not _check_password(req.password, stored):
                raise HTTPException(403, "密码不对（这个名字已经有人用了）")
            return {"player_id": pid}
        # 开发期直接往 auth.users 塞一个假用户，正式版走 Supabase Auth
        pid = uuid4()
        with conn.transaction():
            conn.execute(
                """insert into auth.users (id, instance_id, aud, role, email)
                   values (%s, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', %s)""",
                (pid, f"{pid}@dev.local"),
            )
            conn.execute(
                """insert into players (id, name, room_id, hp, max_hp, attack, defense, password_hash)
                   values (%s, %s, %s, 20, 20, 2, 0, %s)""",
                (pid, name, START_ROOM, _hash_password(req.password)),
            )
        return {"player_id": pid}


@app.get("/api/state")
def get_state(player_id: UUID, after: Optional[int] = None):
    with pool.connection() as conn:
        try:
            view = engine.load_view(conn, player_id)
        except engine.ActionError as e:
            raise HTTPException(404, str(e))
        engine.touch(conn, player_id)      # 页面每 3 秒拉一次，顺便当心跳
        return state(conn, view, after)


@app.post("/api/delete")
def delete(req: LogoutReq):
    with pool.connection() as conn:
        try:
            engine.delete_player(conn, req.player_id)
        except engine.ActionError as e:
            raise HTTPException(404, str(e))
    return {"ok": True}


@app.post("/api/logout")
def logout(req: LogoutReq):
    with pool.connection() as conn:
        engine.sleep(conn, req.player_id)
    return {"ok": True}


def placeholder_dialogue(conn, view: RoomView, npc_ref: str) -> list[ActionResult]:
    """没有 AI 时的占位：只给 requires 门槛已满足的物品，ai 类的不给"""
    npc_id = view.resolve(npc_ref)
    results = []
    with conn.transaction():
        npcs = engine.load_npcs(engine._cursor(conn), "n.id = %s", (npc_id,))
    if not npcs:
        return results
    gives = npcs[0].template.props.get("gives", {})
    for item in engine.giveable_items(conn, view.player.id, npc_id):
        if isinstance(gives.get(item.template.id), dict):
            results.append(engine.npc_give(conn, view.player.id, npc_id, item.id))
    return results


def _load_npc(conn, npc_id: UUID):
    with conn.transaction():
        npcs = engine.load_npcs(engine._cursor(conn), "n.id = %s", (npc_id,))
    return npcs[0] if npcs else None


@app.post("/api/command")
def command(req: CommandReq):
    """流式返回（NDJSON，一行一个事件），让前端能显示进行到哪一步：
    {"stage": "parse" | "execute" | "narrate"} ... 最后 {"done": {...}} 或 {"error": "..."}"""
    def events():
        try:
            yield from run_turn(req)
        except Exception as e:           # 流已经开始了，没法再改状态码，报成一个事件
            yield {"error": f"服务器出错：{e.__class__.__name__}"}
            raise

    return StreamingResponse((json.dumps(e, ensure_ascii=False, default=str) + "\n" for e in events()),
                             media_type="application/x-ndjson")


def run_turn(req: CommandReq):
    # 数据库连接只在读写的那一小段借用，等 AI 的十几秒里还回池子，
    # 否则几个人同时说话就会把池子占满，别人的轮询和心跳跟着卡住
    with pool.connection() as conn:
        try:
            view = engine.load_view(conn, req.player_id)
        except engine.ActionError as e:
            yield {"error": str(e)}
            return
        engine.touch(conn, view.player.id)
    pid = view.player.id
    usage = {"input": 0, "output": 0}
    notes = []                            # 给前端看的调试信息

    def add(u):
        usage["input"] += u["input"]
        usage["output"] += u["output"]

    # 1. 解析：默认规则全认出来就不调 AI；PARSE_MODE=ai 时每句都先过 AI
    actions = commands.parse(view, req.text)
    source = "rules"
    ai_first = os.environ.get("PARSE_MODE", "rules") == "ai"
    # 身上有负面状态时也交给 AI：挣脱、醒来的难度要它看玩家怎么做来判
    if ai.enabled() and (ai_first or view.player.status or any(a.action == "freeform" for a in actions)):
        yield {"stage": "parse"}
        try:
            parsed, u = ai.parse_intent(pool, view, req.text)
            add(u)
            if parsed:
                actions, source = parsed, "ai"
            else:
                notes.append("AI 解析两次都没通过校验，按规则解析结果执行")
        except ai.API_ERRORS as e:
            notes.append(f"AI 解析出错：{e.__class__.__name__}")

    # 2. 规则引擎执行。解析期间别人可能动了东西，引擎按 ref 重新查库加锁校验，不会用旧状态
    yield {"stage": "execute"}
    use_ai = ai.enabled() and not all(a.action == "say" for a in actions)
    npc_id = npc = None
    giveable, affinity, memory, recent = [], 0, "", []
    with pool.connection() as conn:
        results = engine.execute_all(conn, view, actions)
        now_view = engine.load_view(conn, pid)       # 执行后的房间：移动之后要写新地方
        talk = next((a for a, r in zip(actions, results) if a.action == "talk" and r.success), None)
        if use_ai:
            # 叙事要用的东西一次查好，后面调 AI 时不占连接
            npc_id = view.resolve(talk.target) if talk else None
            npc = _load_npc(conn, npc_id) if npc_id else None
            if npc:
                giveable = engine.giveable_items(conn, pid, npc_id)
                affinity = engine.get_affinity(conn, pid, npc_id)
                memory = engine.get_npc_memory(conn, pid, npc_id)
            recent = _recent_events(conn, now_view.room.id, pid)
        elif not ai.enabled() and talk:
            results += placeholder_dialogue(conn, view, talk.target)

    # 3. 叙事 + NPC 对话。只是和玩家说话的回合不用 AI，facts 本身就是要说的话
    narrative, observer, out = None, None, None
    if use_ai:
        yield {"stage": "narrate"}
        try:
            # NPC 身上有能给的东西时，先单独决定给不给，执行完变成 fact，叙事再照着写
            if giveable:
                give_id, u = ai.decide_give(pool, view, req.text, npc, giveable, affinity, memory)
                add(u)
                if give_id:
                    with pool.connection() as conn:
                        results.append(engine.npc_give(conn, pid, npc_id, give_id))
            out, u = ai.narrate(pool, now_view, req.text, results, npc, affinity, memory, recent)
            add(u)
            if out:
                narrative, observer = out.narrative, out.observer or None
            else:
                notes.append("叙事两次都没通过校验")
        except ai.API_ERRORS as e:
            notes.append(f"AI 叙事出错：{e.__class__.__name__}")

    # 4. 好感度、NPC 记忆、记事件：出发的房间记一条给旁人看的描述；换了房间，新房间再记一条"走了进来"
    name = view.player.name
    moved = now_view.room.id != view.room.id
    # 换了房间时 AI 是按新房间写的，原房间的人看到的直接用 facts（"某某往北走，来到了酒馆"）
    if moved or not observer:
        observer = _observer_fallback(name, actions, results)
    with pool.connection() as conn:
        if npc and out:
            if out.affinity_delta:
                results.append(engine.adjust_affinity(conn, pid, npc_id, out.affinity_delta))
            if out.npc_memory:
                engine.set_npc_memory(conn, pid, npc_id, out.npc_memory)
        with conn.transaction():
            conn.execute(
                """insert into events (room_id, player_id, kind, facts, narrative, observer)
                   values (%s, %s, %s, %s, %s, %s)""",
                (view.room.id, pid, ",".join(a.action for a in actions),
                 Jsonb([f for r in results for f in r.facts]), narrative, observer),
            )
            if moved:
                conn.execute(
                    "insert into events (room_id, player_id, kind, observer) values (%s, %s, 'arrive', %s)",
                    (now_view.room.id, pid, f"{name}走了进来。"),
                )
        final_state = state(conn, engine.load_view(conn, pid), req.after)

    yield {"done": {
        "actions": [a.model_dump() for a in actions],
        "source": source,
        "results": [r.model_dump() for r in results],
        "narrative": narrative,
        "usage": usage,
        "notes": notes,
        "state": final_state,
    }}


def _recent_events(conn, room_id: str, player_id: UUID, limit: int = 5) -> list[str]:
    """这个房间最近 10 分钟里别人的动态，给叙事 AI 接上下文"""
    rows = conn.execute(
        """select observer from events
           where room_id = %s and observer is not null and player_id is distinct from %s
             and created_at > now() - interval '10 minutes'
           order by id desc limit %s""",
        (room_id, player_id, limit),
    ).fetchall()
    conn.commit()
    return [r[0] for r in reversed(rows)]


def _observer_fallback(name: str, actions: list, results: list[ActionResult]) -> Optional[str]:
    """没有 AI 叙事时（只说话的回合、AI 出错）给旁人看的描述：成功动作的 facts 本身就是第三人称。
    查看不写看到了什么，失败的动作旁人看不出来就不写"""
    lines = []
    for a, r in zip(actions, results):
        if a.action == "look":
            lines.append(f"{name}四下打量了一番。")
        elif r.success:
            lines += r.facts
    return "；".join(lines) or None


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
