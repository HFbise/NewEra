"""
开发用简易 Web 服务。
- 只绑 127.0.0.1，登录只要名字不要密码，别部署到公网
- 一回合的链路：规则解析（commands.py）→ 有认不出的再交给 AI 解析 → 规则引擎执行
  → AI 叙事（含 NPC 对话、给东西、好感度提议，规则引擎再校验）→ 写 events
- AI 后端见 ai.py（默认 Gemini），没配 key 时退回纯规则模式：没有叙事，talk 用占位逻辑给 requires 物品

用法: python server.py  然后打开 http://127.0.0.1:8000
"""
import os
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

import ai
import commands
import engine
from schema import ActionResult, RoomView, dir_name

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=5, open=True)
app = FastAPI()

START_ROOM = "square"


class LoginReq(BaseModel):
    name: str


class LogoutReq(BaseModel):
    player_id: UUID


class CommandReq(BaseModel):
    player_id: UUID
    text: str


def state(conn, view: RoomView) -> dict:
    """给前端侧栏看的当前状态，带上短编号方便对照"""
    by_id = {uid: ref for ref, uid in view.refs.items()}
    cur = conn.cursor()
    cur.execute(
        f"""select name, hp, coalesce(last_active_at > now() - interval '{engine.ONLINE_WINDOW}', false)
            from players where room_id = %s and id <> %s order by name""",
        (view.room.id, view.player.id),
    )
    others = [{"name": n, "hp": hp, "awake": awake} for n, hp, awake in cur.fetchall()]
    cur.execute("select id, name from rooms where id = any(%s)", ([e.to_room for e in view.exits],))
    room_names = dict(cur.fetchall())
    conn.commit()
    return {
        "player": view.player.model_dump(mode="json"),
        "room": view.room.model_dump(),
        "exits": [{"direction": e.direction, "label": dir_name(e.direction), "to": room_names[e.to_room],
                   "locked": e.locked} for e in view.exits],
        "items": [{"ref": by_id[i.id], "name": i.name, "quantity": i.quantity} for i in view.items],
        "npcs": [{"ref": by_id[n.id], "name": n.name, "hp": n.hp, "max_hp": n.template.max_hp}
                 for n in view.npcs],
        "inventory": [{"ref": by_id[i.id], "name": i.name, "quantity": i.quantity,
                       "equipped": i.equipped_slot} for i in view.inventory],
        "others": others,
    }


@app.post("/api/login")
def login(req: LoginReq):
    name = req.name.strip()
    if not name or len(name) > 20:
        raise HTTPException(400, "名字 1 到 20 个字")
    with pool.connection() as conn:
        row = conn.execute("select id from players where name = %s", (name,)).fetchone()
        if row:
            return {"player_id": row[0]}
        # 开发期直接往 auth.users 塞一个假用户，正式版走 Supabase Auth
        pid = uuid4()
        with conn.transaction():
            conn.execute(
                """insert into auth.users (id, instance_id, aud, role, email)
                   values (%s, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', %s)""",
                (pid, f"{pid}@dev.local"),
            )
            conn.execute(
                """insert into players (id, name, room_id, hp, max_hp, attack, defense)
                   values (%s, %s, %s, 20, 20, 2, 0)""",
                (pid, name, START_ROOM),
            )
        return {"player_id": pid}


@app.get("/api/state")
def get_state(player_id: UUID):
    with pool.connection() as conn:
        try:
            view = engine.load_view(conn, player_id)
        except engine.ActionError as e:
            raise HTTPException(404, str(e))
        engine.touch(conn, player_id)      # 页面每 3 秒拉一次，顺便当心跳
        return state(conn, view)


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
    with pool.connection() as conn:
        try:
            view = engine.load_view(conn, req.player_id)
        except engine.ActionError as e:
            raise HTTPException(404, str(e))
        pid = view.player.id
        engine.touch(conn, pid)
        usage = {"input": 0, "output": 0}
        notes = []                        # 给前端看的调试信息

        def add(u):
            usage["input"] += u["input"]
            usage["output"] += u["output"]

        # 1. 解析：默认规则全认出来就不调 AI；PARSE_MODE=ai 时每句都先过 AI
        actions = commands.parse(view, req.text)
        source = "rules"
        ai_first = os.environ.get("PARSE_MODE", "rules") == "ai"
        if ai.enabled() and (ai_first or any(a.action == "freeform" for a in actions)):
            try:
                parsed, u = ai.parse_intent(conn, view, req.text)
                add(u)
                if parsed:
                    actions, source = parsed, "ai"
                else:
                    notes.append("AI 解析两次都没通过校验，按规则解析结果执行")
            except ai.API_ERRORS as e:
                notes.append(f"AI 解析出错：{e.__class__.__name__}")

        # 2. 规则引擎执行
        results = engine.execute_all(conn, view, actions)

        # 3. 叙事 + NPC 对话
        narrative = None
        if ai.enabled():
            talk = next((a for a, r in zip(actions, results) if a.action == "talk" and r.success), None)
            npc_id = view.resolve(talk.target) if talk else None
            npc = _load_npc(conn, npc_id) if npc_id else None
            giveable = engine.giveable_items(conn, pid, npc_id) if npc else []
            affinity = engine.get_affinity(conn, pid, npc_id) if npc else 0
            try:
                # 叙事用执行后的房间：移动之后要写新地方的环境
                after = engine.load_view(conn, pid)
                out, give_id, u = ai.narrate(conn, after, req.text, results, npc, giveable, affinity)
                add(u)
                if out:
                    narrative = out.narrative
                    if give_id:
                        results.append(engine.npc_give(conn, pid, npc_id, give_id))
                    if npc and out.affinity_delta:
                        results.append(engine.adjust_affinity(conn, pid, npc_id, out.affinity_delta))
                else:
                    notes.append("叙事两次都没通过校验")
            except ai.API_ERRORS as e:
                notes.append(f"AI 叙事出错：{e.__class__.__name__}")
        else:
            for a, r in zip(actions, results):
                if a.action == "talk" and r.success:
                    results += placeholder_dialogue(conn, view, a.target)

        # 4. 记事件
        with conn.transaction():
            conn.execute(
                "insert into events (room_id, player_id, kind, facts, narrative) values (%s, %s, %s, %s, %s)",
                (view.room.id, pid, ",".join(a.action for a in actions),
                 Jsonb([f for r in results for f in r.facts]), narrative),
            )

        return {
            "actions": [a.model_dump() for a in actions],
            "source": source,
            "results": [r.model_dump() for r in results],
            "narrative": narrative,
            "usage": usage,
            "notes": notes,
            "state": state(conn, engine.load_view(conn, pid)),
        }


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
