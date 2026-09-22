"""
开发用简易 Web 服务。
- 只绑 127.0.0.1，登录只要名字不要密码，别部署到公网
- 输入先走 commands.py 的规则解析，意图解析 AI 接上后再换
- 对话 AI 还没接，talk 时先用占位逻辑：带 requires 门槛且已满足的物品 NPC 直接给

用法: python server.py  然后打开 http://127.0.0.1:8000
"""
import os
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

import commands
import engine
from schema import ActionResult, RoomView

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=5, open=True)
app = FastAPI()

START_ROOM = "square"


class LoginReq(BaseModel):
    name: str


class CommandReq(BaseModel):
    player_id: UUID
    text: str


def state(conn, view: RoomView) -> dict:
    """给前端侧栏看的当前状态，带上短编号方便对照"""
    by_id = {uid: ref for ref, uid in view.refs.items()}
    cur = conn.cursor()
    cur.execute("select name, hp from players where room_id = %s and id <> %s", (view.room.id, view.player.id))
    others = [{"name": n, "hp": hp} for n, hp in cur.fetchall()]
    conn.commit()
    return {
        "player": view.player.model_dump(mode="json"),
        "room": view.room.model_dump(),
        "exits": [{"direction": e.direction, "locked": e.locked} for e in view.exits],
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
        return state(conn, view)


def placeholder_dialogue(conn, view: RoomView, npc_ref: str) -> list[ActionResult]:
    """对话 AI 接上之前的占位：只给 requires 门槛已满足的物品，ai 类的不给"""
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


@app.post("/api/command")
def command(req: CommandReq):
    with pool.connection() as conn:
        try:
            view = engine.load_view(conn, req.player_id)
        except engine.ActionError as e:
            raise HTTPException(404, str(e))
        actions = commands.parse(view, req.text)
        results = []
        for action in actions:
            r = engine.execute(conn, view, action)
            results.append(r)
            if not r.success:
                break
            if action.action == "talk":
                results += placeholder_dialogue(conn, view, action.target)
        return {
            "actions": [a.model_dump() for a in actions],
            "results": [r.model_dump() for r in results],
            "state": state(conn, engine.load_view(conn, req.player_id)),
        }


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
