"""
开发用简易 Web 服务。
- 本地跑 python server.py 只绑 127.0.0.1；部署（Render）时用 uvicorn 绑 0.0.0.0，见 render.yaml
- 登录：角色名 + 角色密码；设了 ACCESS_CODE 环境变量时还要邀请码
- 一回合的链路：规则解析（commands.py）→ 有认不出的再交给 AI 解析 → 规则引擎执行
  → AI 叙事（含 NPC 对话、给东西、好感度提议，规则引擎再校验）→ 写 events
- AI 后端见 ai.py（默认智谱 glm-4.7-flash），没配 key 时退回纯规则模式：没有叙事，talk 用占位逻辑给 requires 物品
- 管理后台在 admin.py（/admin.html），要设 ADMIN_PASSWORD

用法: python server.py  然后打开 http://127.0.0.1:8000
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from typing import Optional
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb
from pydantic import BaseModel, TypeAdapter

import admin
import ai
import commands
import dungeon
import engine
from db import pool
from schema import SKILL_NAMES, SLOT_NAMES, ActionResult, PlayerAction, RoomView, dir_name

app = FastAPI()
app.include_router(admin.router)

# 这些动作的回合不调叙事 AI：facts 已经说清楚了，AI 反而容易替别的玩家编动作
NO_NARRATION = {"say", "follow", "unfollow", "leave_party", "challenge", "accept_duel", "decline_duel"}
# NPC 对话里这些结果旁人也看得到（交东西、提委托、轰人），跟在对话原文后面
AFFINITY_BY_RULE = {"npc_give", "npc_create", "npc_sell", "upgrade", "rest", "pay", "sell", "give"}
NPC_OUTCOMES = {"npc_give", "npc_create", "npc_sell", "quote", "quest", "npc_eject", "upgrade", "rest", "pay", "sell"}


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
            """select id, observer, kind from events
               where room_id = %s and id > %s and id <= %s and observer is not null
                 and player_id is distinct from %s
               order by id limit 30""",
            (view.room.id, after, last_event_id, view.player.id),
        )
        # 战斗回合的结算（combat）和叙事（combat_story）不记在谁名下，全队都看得到
        events = [{"id": i, "text": t, "kind": k} for i, t, k in cur.fetchall()]
        # 看到的动态记进聊天框记录，轮询和命令可能带回同一条，靠唯一索引只记一次
        cur.executemany(
            """insert into player_log (player_id, kind, event_id, data) values (%s, 'event', %s, %s)
               on conflict (player_id, event_id) where event_id is not null do nothing""",
            [(view.player.id, e["id"], Jsonb({"text": e["text"], "kind": e["kind"]})) for e in events],
        )
    cur.execute(
        f"""select name, hp, coalesce(last_active_at > now() - interval '{engine.ONLINE_WINDOW}', false), status
            from players where room_id = %s and id <> %s order by name""",
        (view.room.id, view.player.id),
    )
    others = [{"name": n, "hp": hp, "awake": awake, "status": st and st["label"]}
              for n, hp, awake, st in cur.fetchall()]
    cur.execute("select id, name from rooms where id = any(%s)", ([e.to_room for e in view.exits],))
    room_names = dict(cur.fetchall())
    light = engine.light_info(engine._cursor(conn), view.player, view.room)      # 地牢里的光亮和它的效果
    combat = engine.round_info(conn, view.player)                                  # 战斗回合：谁出手了、在等谁
    minimap = dungeon.minimap(engine._cursor(conn), view.room.id)                   # 地牢这一层的小地图
    totals = engine.gear_totals(view.player.attack, view.player.defense, view.inventory)
    conn.commit()
    return {
        "player": view.player.model_dump(mode="json"),
        "room": view.room.model_dump(),
        "light": light,
        "combat": combat,
        "minimap": minimap,
        "exits": [{"direction": e.direction, "label": dir_name(e.direction), "to": room_names[e.to_room],
                   "locked": e.locked} for e in view.exits],
        "items": [{"ref": by_id[i.id], "name": i.name, "quantity": i.quantity, "detail": engine.item_detail(i)}
                  for i in view.items]
                 # 搜索能找到的：点一下填"搜索"
                 + [{"ref": "", "name": n, "quantity": 1, "fill": "搜索"} for n in view.forage],
        "npcs": [{"ref": by_id[n.id], "name": n.name, "hp": n.hp, "max_hp": n.template.max_hp,
                  "beast": bool(n.template.hostile and n.template.props.get("animal")
                                and n.template.props.get("dungeon", {}).get("rank") != "boss"),
                  "status": n.status and n.status.label} for n in view.npcs]
                # 武器桶这类物件跟 NPC 列在一起，点一下填"拿…"
                + [{"ref": by_id[d.id], "name": d.container, "status": f"{d.where}面有{d.item_name}",
                    "fill": f"拿{d.take_label}"} if d.available
                   else {"ref": by_id[d.id], "name": d.container, "fill": f"看{d.container}"}
                   for d in view.dispensers],
        # 装备栏按固定顺序列全部格子，空的 item 为 null；背包只列没装备的
        "equipment": [{"slot": slot, "label": label,
                       "item": next(({"ref": by_id[i.id], "name": i.name, "detail": engine.item_detail(i)}
                                     for i in view.inventory
                                     if i.equipped_slot == slot), None)} for slot, label in SLOT_NAMES.items()],
        "inventory": [{"ref": by_id[i.id], "name": i.name, "quantity": i.quantity, "detail": engine.item_detail(i)}
                      for i in view.inventory if not i.equipped_slot],
        # 攻防算上装备（engine.gear_totals）
        "attack_total": totals[0],
        "defense_total": totals[1],
        "others": others,
        "party": view.party,
        "following": view.following,
        "stealth": ai.stealth_text(view),
        "duel": view.duel and view.duel.model_dump(),
        # 技能：等级、这一级攒了几次、升下一级要几次
        "skills": [dict(zip(("level", "have", "need"), engine.skill_progress(view.player.skills.get(k, 0))), name=n)
                   for k, n in SKILL_NAMES.items()],
        "challenges": view.challenges,
        "challenging": view.challenging,
        "duel_rules": engine.DUEL_RULES,
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
                (pid, name, engine.START_ROOM, _hash_password(req.password)),
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
        _kick_round(conn, view.room.id)    # 战斗回合：等的人掉线了就不等他，结算（没有后台定时器，靠轮询推一把）
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


def _summarize_later(player_id: UUID, player_name: str, npc_template: str, npc_name: str) -> None:
    """记完一条来往，后台让 AI 按完整记录重新整理 NPC 对这个玩家的记忆摘要（玩家不用等）"""
    threading.Thread(target=_summarize, args=(player_id, player_name, npc_template, npc_name), daemon=True).start()


def _summarize(player_id: UUID, player_name: str, npc_template: str, npc_name: str) -> None:
    try:
        with pool.connection() as conn:
            old, log = engine.memory_material(conn, player_id, npc_template)
            # 世界里真实存在的委托、人物：旧摘要里凭空多出来的（"蜘蛛任务"）靠这个认出来删掉
            quests = conn.execute("select q.name, q.goal, t.name from quests q join npc_templates t on t.id = q.giver").fetchall()
            npcs = [r[0] for r in conn.execute("select name from npc_templates").fetchall()]
            conn.commit()
        world = ("委托：" + "；".join(f"{n}（{g}，找{who}）" for n, g, who in quests) + "\n"
                 + "人物：" + "、".join(npcs) + "，以及各位玩家")
        summary = ai.summarize_memory(pool, player_id, npc_name, player_name, old, log, world)
        if summary:
            with pool.connection() as conn:
                engine.set_npc_memory(conn, player_id, npc_template, summary)
    except Exception:                    # 后台整理失败就留着旧摘要，不影响游戏
        pass


def _revive_hook(room_id: str, keeper_tid: str, player_id: UUID, player_name: str, why: dict, fallback: str) -> None:
    """看店 NPC 扶起人了：后台让 AI 按人设、好感、倒下的原因现写一句话，写好了作为房间动态出现（扶人本身已经生效）"""
    threading.Thread(target=_revive_quip, args=(room_id, keeper_tid, player_id, player_name, why, fallback),
                     daemon=True).start()


def _revive_quip(room_id: str, keeper_tid: str, player_id: UUID, player_name: str, why: dict, fallback: str) -> None:
    line = None
    try:
        with pool.connection() as conn:
            name, persona = conn.execute("select name, persona from npc_templates where id = %s", (keeper_tid,)).fetchone()
            rel = conn.execute("select affinity, memory from player_npc_relations where player_id = %s and npc_template = %s",
                               (player_id, keeper_tid)).fetchone() or (0, "")
            conn.commit()
        if ai.enabled():
            line = ai.keeper_quip(pool, name, persona, player_id, player_name, why, rel[0], rel[1], fallback)
    except Exception:                    # 后台线程出错不能影响游戏，退回保底台词
        pass
    line = line or fallback
    if not line:
        return
    with pool.connection() as conn, conn.transaction():
        conn.execute("insert into events (room_id, kind, observer) values (%s, 'keeper_quip', %s)", (room_id, line))
        conn.execute("insert into npc_memory_log (player_id, npc_template, entry) values (%s, %s, %s)",
                     (player_id, keeper_tid, f"{player_name}倒在你店里，你把他扶起来照料好了，说：{line}"[:engine.NPC_LOG_LIMIT]))


engine.REVIVE_HOOK = _revive_hook


def _floor_hook(run, depth: int) -> None:
    """新生成了一层地牢：后台让 AI 按主题重写还没人进过的房间（玩家不用等，写不好就留着模板）"""
    if ai.enabled() and AI_ROOMS:
        threading.Thread(target=_describe_floor, args=(run, depth), daemon=True).start()


AI_ROOMS = os.environ.get("AI_ROOMS", "1") != "0"      # 设 AI_ROOMS=0 就只用模板


def _describe_floor(run, depth: int) -> None:
    try:
        material = None
        for _ in range(20):                  # 生成那一层的事务提交了才看得到
            with pool.connection() as conn:
                material = dungeon.rooms_for_ai(engine._cursor(conn), run, depth)
                conn.commit()
            if material:
                break
            time.sleep(0.5)
        if not material or not material["rooms"]:
            return
        texts = ai.dungeon_rooms(pool, material)
        if texts:
            with pool.connection() as conn, conn.transaction():
                dungeon.apply_ai_rooms(engine._cursor(conn), run, depth, material["theme"], texts)
    except Exception:                        # 后台失败就留着模板，不影响游戏
        import traceback
        traceback.print_exc()


dungeon.FLOOR_HOOK = _floor_hook

_busy: set[UUID] = set()               # 正在判定的玩家
_busy_lock = threading.Lock()


@app.post("/api/command")
def command(req: CommandReq):
    """流式返回（NDJSON，一行一个事件），让前端能显示进行到哪一步：
    {"stage": "parse" | "execute" | "narrate"} ... 最后 {"done": {...}} 或 {"error": "..."}"""
    def events():
        # 同一个玩家上一句还没判定完就不接新的（前端会锁输入框，这里防开两个标签页连发）
        with _busy_lock:
            if req.player_id in _busy:
                yield {"error": "上一句还在判定，等结果出来再发"}
                return
            _busy.add(req.player_id)
        try:
            for ev in run_turn(req):
                if "done" in ev:
                    _log_turn(req, ev["done"])
                yield ev
        except Exception as e:           # 流已经开始了，没法再改状态码，报成一个事件
            yield {"error": f"服务器出错：{e.__class__.__name__}"}
            raise
        finally:
            with _busy_lock:
                _busy.discard(req.player_id)

    return StreamingResponse((json.dumps(e, ensure_ascii=False, default=str) + "\n" for e in events()),
                             media_type="application/x-ndjson")


LOG_KEEP = 500                          # 每个玩家的聊天框记录最多留多少条


def _log_turn(req: CommandReq, done: dict) -> None:
    """把这一回合记进聊天框记录（不含侧栏状态），顺手删掉太旧的"""
    data = {"input": req.text, **{k: v for k, v in done.items() if k != "state"}}
    with pool.connection() as conn:
        conn.execute("insert into player_log (player_id, kind, data) values (%s, 'turn', %s)",
                     (req.player_id, Jsonb(data)))
        conn.execute(
            """delete from player_log where player_id = %(p)s and id < (
                 select id from player_log where player_id = %(p)s order by id desc offset %(n)s limit 1)""",
            {"p": req.player_id, "n": LOG_KEEP},
        )


@app.get("/api/history")
def history(player_id: UUID, limit: int = 200):
    """聊天框记录，旧的在前"""
    with pool.connection() as conn:
        rows = conn.execute(
            """select kind, event_id, data from (
                 select id, kind, event_id, data from player_log where player_id = %s order by id desc limit %s
               ) t order by id""",
            (player_id, min(limit, LOG_KEEP)),
        ).fetchall()
    return [{"kind": k, "event_id": e, **d} for k, e, d in rows]


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

    # 战斗中（房间里有怪、有人被发现了；或者自己在决斗）：命令先排队，在场的人都出完手一起结算（_resolve_round）。
    # 说话、查看马上生效；倒下的人不用排；看别人决斗的人不用排
    if view.player.hp > 0 and not all(a.action in engine.ROUND_INSTANT for a in actions):
        with pool.connection() as conn:
            fighting = engine.in_round(engine._cursor(conn), view.room.id, pid)
            conn.commit()
            if fighting:
                engine.queue_round(conn, pid, view.room.id, req.text, [a.model_dump() for a in actions], source, notes)
                _kick_round(conn, view.room.id)
                final_state = state(conn, engine.load_view(conn, pid), req.after)
        if fighting:
            yield {"done": {
                "actions": [a.model_dump() for a in actions], "source": source,
                "results": [ActionResult(action="queued", success=True,
                                         facts=["出手了，等这一轮一起结算（结果会出现在下面）"]).model_dump()],
                "narrative": None, "usage": usage, "notes": notes, "state": final_state, "queued": True,
            }}
            return

    # 2. 规则引擎执行。解析期间别人可能动了东西，引擎按 ref 重新查库加锁校验，不会用旧状态
    yield {"stage": "execute"}
    use_ai = ai.enabled() and not all(a.action in NO_NARRATION for a in actions)
    npc_id = npc = eject_to = None
    giveable, creatable, sells, quests, affinity, memory, recent = [], [], [], [], 0, "", []
    offers, made_before, bonds = {}, [], []
    with pool.connection() as conn:
        results = engine.execute_all(conn, view, actions)
        # 跟 NPC 说话、把东西交给 NPC、找铁匠升级、住店都算跟他打交道：委托结算、NPC 回应
        talk = next((a for a, r in zip(actions, results) if r.success and (
            a.action in ("talk", "upgrade", "rest") or a.action in ("give", "pay", "sell") and a.target in view.refs)), None)
        if talk and talk.action == "rest":
            npc_id = next((n.id for n in view.npcs if n.template.props.get("inn")), None)
        else:
            npc_id = view.resolve(talk.target) if talk else None
        npc = _load_npc(conn, npc_id) if npc_id else None
        if npc:
            # 跟发布任务的 NPC 说话：做完的委托自动发奖励（有没有 AI 都一样），没接过的这次提起
            quest_results, quests = engine.quest_turn(conn, pid, npc_id)
            results += quest_results
        now_view = engine.load_view(conn, pid)       # 执行后的房间：移动之后要写新地方
        if npc and npc.room_id != now_view.room.id:
            npc = None                               # 说完话就走了（"先赊账，然后往西走"）：不再跟他交易、他也不回话
        if use_ai:
            # 叙事要用的东西一次查好，后面调 AI 时不占连接
            if npc:
                giveable = engine.giveable_items(conn, pid, npc_id)
                creatable = engine.creatable_kinds(conn, pid, npc)
                # 偶尔进的稀罕货：问有什么卖的时判一次，有的话这一小时里跟墙上的货一起卖
                sells = engine.sellable(conn, npc, engine.rare_stock(conn, pid, npc, req.text))
                offers = engine.get_offers(conn, pid, npc) if sells or creatable else {}
                made_before = engine.known_goods(conn, npc) if creatable else []
                affinity = engine.get_affinity(conn, pid, npc_id)
                bonds = engine.npc_bonds(conn, pid, npc)          # 台词里可以提的别人的交情
                memory = engine.get_npc_memory(conn, pid, npc_id, req.text)
                if engine.can_eject(npc):
                    ex = engine._find_exit(engine._cursor(conn), npc.room_id, npc.template.props["eject_to"])
                    eject_to = engine.load_room(engine._cursor(conn), ex["to_room"]).name if ex else None
                    conn.commit()
            recent = _recent_events(conn, now_view.room.id, pid)
            if npc:
                recent = [engine.clip_npc_said(r, npc.name) for r in recent]
        elif not ai.enabled() and talk:
            results += placeholder_dialogue(conn, view, talk.target)

    # 3. 叙事 + NPC 对话。只是说话、组队、跟随的回合不用 AI，facts 本身就够了
    narrative, observer, out = None, None, None
    if use_ai:
        yield {"stage": "narrate"}
        try:
            # NPC 身上有能给的东西、或者能现造东西时，先单独决定给不给，执行完变成 fact，叙事再照着写
            # 交易：给现有的、现造、卖货，价钱 AI 定，引擎查钱够不够、扣钱、交货
            # 升级武器、住店这回合就只是升级、住店，不再另外做买卖
            if (giveable or creatable or sells) and talk.action not in ("upgrade", "rest", "pay", "sell"):
                trade, u = ai.decide_give(pool, view, req.text, npc, giveable, creatable, affinity, memory,
                                          recent, sells, offers, made_before)
                add(u)
                if trade:
                    with pool.connection() as conn:
                        rule = next((npc.template.props.get("gives", {}).get(i.template.id)
                                     for i in giveable if i.id == trade.give_id), None)
                        if trade.give_id and trade.price == 0 and rule == "ai" and not engine.can_gift(affinity):
                            pass                    # 看心情给的东西，交情不够不白送（任务奖励、按条件给的不走这里）
                        elif trade.give_id:
                            results.append(engine.npc_give(conn, pid, npc_id, trade.give_id, trade.price))
                        elif trade.made:
                            # 现做：AI 给的效果按上限裁剪；白送当场给，要收钱就先按效果开价。
                            # 值钱的东西交情不够不能白送，改成开价
                            # 做过的东西（名字一样）照原来的规格做，免得同一样东西每次效果都不一样
                            m = trade.made
                            if old := next((g["spec"] for g in made_before if g["name"] == m.name.strip()), None):
                                m = ai.MadeItem(**old)
                            try:
                                spec = engine.made_spec(npc, m.kind, m.name, m.description, m.heal, m.harm,
                                                        m.damage, m.knockout, m.alcohol)
                                free = trade.price == 0 and engine.can_gift(affinity)
                                results.append(engine.npc_gift(conn, pid, npc, spec) if free
                                               else engine.quote_made(conn, pid, npc, spec, trade.price))
                            except engine.ActionError as e:
                                results.append(ActionResult(action="npc_create", success=False, facts=[str(e)]))
                        elif trade.sell_id.startswith("made:"):
                            results.append(engine.npc_buy_made(conn, pid, npc, trade.sell_id))
                        else:
                            results.append(engine.npc_sell(conn, pid, npc, trade.sell_id, trade.price, trade.count))
                        now_view = engine.load_view(conn, pid)   # 叙事要看到新拿到的东西和剩下的钱
                        offers = engine.get_offers(conn, pid, npc)
            # NPC 说话单独演一次（只管角色扮演），叙事再把台词原样包进场景
            line = None
            if npc and talk:
                # 问收不收、值多少（或者刚卖了东西）：让她知道收玩家身上东西的价
                buys = []
                if re.search(r"卖|收|回收|值多少|值钱", req.text):
                    with pool.connection() as conn:
                        buys = engine.buy_quotes(conn, pid, npc, now_view.inventory)
                line = ai.npc_line(pool, now_view, req.text, results, npc, affinity, memory, recent, quests,
                                   sells, made_before, buys, bonds)
            out, u = ai.narrate(pool, now_view, req.text, results, npc, affinity, memory, recent,
                                quests, eject_to, sells, offers, made_before, line)
            add(u)
            if out and npc and out.npc_handed and out.npc_handed.key:
                # 叙事里 NPC 把货递给了他：真的给、按规矩收钱；钱不够就补一句收了回去
                with pool.connection() as conn:
                    handed = engine.npc_hand(conn, pid, npc, out.npc_handed.key, out.npc_handed.price, affinity)
                results.append(handed)
                if not handed.success:
                    out.narrative += f"（{handed.facts[0]}，{npc.name}又把{out.npc_handed.item}收了回去）"
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
            # 花钱、送东西、升级、住店那回合好感已经按规矩算过（照顾生意、心爱的礼物），AI 不再另外往上加
            delta = out.affinity_delta
            if delta > 0 and (talk.action != "talk" or any(r.success and r.action in AFFINITY_BY_RULE for r in results)):
                delta = 0
            if delta:
                results.append(engine.adjust_affinity(conn, pid, npc_id, delta))
            # 叙事判定 NPC 把玩家轰出去：叙事和旁人描述已经写了，这里真的挪人（门外那边的人会看到他被轰出来）
            if out.eject and (kicked := engine.npc_eject(conn, pid, npc)):
                results.append(kicked)
            # 跟 NPC 的对话给同房间的人看原文，大家能接着聊，NPC 下回合也能从最近动态里看到别人说了什么
            # NPC 报了价就记下来，玩家下回合同意才按这个价成交（名字对上卖货清单里的哪件）
            if out.npc_offer:
                # 能报价的：墙上的货（物品 id）、开过价的现做东西（"made:名字"）
                named = [(s["id"], s["name"]) for s in sells] + \
                    [(k, v["spec"]["name"]) for k, v in offers.items() if k.startswith("made:") and v.get("spec")]
                item = out.npc_offer.item
                key = next((k for k, n in named if n == item), None) or \
                    next((k for k, n in named if n in item or item in n), None)
                if key:
                    engine.set_offer(conn, pid, npc, key, out.npc_offer.price)
            if talk and out.npc_reply:
                observer = "\n".join(
                    [_said(name, npc, talk, actions, results), f"{npc.name}：“{out.npc_reply}”"]
                    + [f for r in results if r.success and r.action in NPC_OUTCOMES for f in r.facts])
        # NPC 的完整记忆：每次打交道记一条（说了什么、回了什么、给了什么、开了什么价），全部留着
        if npc and talk:
            engine.add_npc_log(conn, pid, npc, "；".join(
                [_said(name, npc, talk, actions, results)]
                + ([f"你回：“{out.npc_reply}”"] if out and out.npc_reply else [])
                + [f for r in results if r.success and r.action in NPC_OUTCOMES | {"affinity"} for f in r.facts]))
            if ai.enabled():
                _summarize_later(pid, name, npc.template.id, npc.name)
        with conn.transaction():
            conn.execute(
                """insert into events (room_id, player_id, kind, facts, narrative, observer, input, meta)
                   values (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (view.room.id, pid, ",".join(a.action for a in actions),
                 Jsonb([f for r in results for f in r.facts]), narrative, observer, req.text,
                 # 后台排查用：解析来源、解析出的动作、提示
                 Jsonb({"source": source, "actions": [a.model_dump() for a in actions], "notes": notes})),
            )
            # 被抬回来复活的不算走进来：那边已经有"扶起来"的动态了
            if moved and not any(a.action == "respawn" for a in actions):
                # 跟着他一起过来的人也写上（engine.do_move 里一起移动的）
                followers = [r[0] for r in conn.execute(
                    "select name from players where following = %s and room_id = %s order by name",
                    (pid, now_view.room.id)).fetchall()]
                conn.execute(
                    "insert into events (room_id, player_id, kind, observer) values (%s, %s, 'arrive', %s)",
                    (now_view.room.id, pid, "、".join([name] + followers) + "走了进来。"),
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


# ============ 战斗回合（tick）============

_ACTION = TypeAdapter(PlayerAction)


def _kick_round(conn, room_id: str) -> None:
    """这一轮该结算了（在场的人都出手了）就在后台结算，别让发命令、轮询的请求等着。
    轮询也要推：有人掉线（没心跳）后就不等他了，那时没人发命令"""
    if engine.round_due(conn, room_id):
        threading.Thread(target=_resolve_round, args=(room_id,), daemon=True).start()


def _resolve_round(room_id: str) -> None:
    """结算一轮：队员按出手先后执行，敌人统一行动，结果马上作为房间动态发出去（全队都看得到）；
    再让 AI 写一段整队共用的叙事，写完才开始下一轮的计时"""
    try:
        with pool.connection() as conn:
            claimed = engine.claim_round(conn, room_id)
            if claimed is None:
                return                                  # 别的线程已经在结算了
            rnd, entries = claimed
            turns, done, hits, healers = [], {}, {}, set()
            for e in entries:
                try:
                    view = engine.load_view(conn, e["player_id"])
                except engine.ActionError:
                    continue
                if view.room.id != room_id or view.player.hp <= 0:
                    continue                            # 出手之后被打倒了、被带走了
                actions = [_ACTION.validate_python(a) for a in e["actions"]]
                results = engine.execute_all(conn, view, actions, enemies=False)
                done[view.player.id] = list(zip(actions, results))
                for a, r in zip(actions, results):
                    if not r.success:
                        continue
                    if a.action in ("attack", "stunt") and getattr(a, "target", None) in view.refs:
                        hits[view.resolve(a.target)] = view.player.id          # 仇恨：怪先打上一下打它的人
                    if a.action == "revive" or a.action == "use" and getattr(a, "target", None) and a.target not in view.refs:
                        healers.add(view.player.id)                            # 头目先打给人用药、急救的
                turns.append((view.player.name, e["text"], [f for r in results for f in r.facts]))
                after = engine.load_view(conn, view.player.id)
                if after.room.id != room_id:            # 逃出去了：那边的人看到他进来
                    with conn.transaction():
                        conn.execute("insert into events (room_id, player_id, kind, observer) values (%s, %s, 'arrive', %s)",
                                     (after.room.id, view.player.id, f"{view.player.name}走了进来。"))
            enemy = engine.round_enemies(conn, room_id, done, hits, healers)
            lines = [f"【第 {rnd} 轮】"] + [f"{name}：{'；'.join(facts)}" for name, _, facts in turns]                 + ([f"敌人：{'；'.join(enemy)}"] if enemy else [])
            with conn.transaction():
                conn.execute("insert into events (room_id, kind, facts, observer, meta) values (%s, 'combat', %s, %s, %s)",
                             (room_id, Jsonb(lines), "\n".join(lines),
                              Jsonb({"round": rnd, "inputs": [(n, t) for n, t, _ in turns]})))
            room = engine.load_room(engine._cursor(conn), room_id)
            conn.commit()
        story = None
        if ai.enabled() and (turns or enemy):
            try:
                story = ai.narrate_round(pool, entries[0]["player_id"] if entries else None, room, turns, enemy)
            except ai.API_ERRORS:
                story = None
        with pool.connection() as conn:
            if story:
                with conn.transaction():
                    conn.execute("insert into events (room_id, kind, observer) values (%s, 'combat_story', %s)",
                                 (room_id, story))
            engine.end_round(conn, room_id)
            _kick_round(conn, room_id)                  # 写叙事的时候大家已经都出手了：马上结算下一轮
    except Exception:
        # 出错也要把这一轮收掉，不然这个房间一直卡在"结算中"
        import traceback
        traceback.print_exc()
        with pool.connection() as conn:
            engine.end_round(conn, room_id)


def _recent_events(conn, room_id: str, player_id: UUID, limit: int = 6) -> list[str]:
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


def _said(name: str, npc, talk, actions: list, results: list[ActionResult]) -> str:
    """这回合玩家对 NPC 做了什么：动作的第一条 fact（"A对莉娜说：……"已经去掉了解析带进来的"对莉娜"，
    交东西的是"把锈剑递给了莉娜"）"""
    return next((f for a, r in zip(actions, results) if a is talk for f in r.facts[:1]),
                f"{name}对{npc.name}说：“{getattr(talk, 'message', '')}”")


def _observer_fallback(name: str, actions: list, results: list[ActionResult]) -> Optional[str]:
    """没有 AI 叙事时（只说话的回合、AI 出错）给旁人看的描述：成功动作的 facts 本身就是第三人称。
    查看不写看到了什么，失败的动作旁人看不出来就不写"""
    lines = []
    for a, r in zip(actions, results):
        if a.action == "look":
            lines.append(f"{name}四下打量了一番。")
        elif a.action == "respawn" and r.success:
            lines.append(r.facts[0])            # 原地的人只看到被带走了，那边扶起来的事他们看不到
        elif r.success:
            lines += r.facts
    return "；".join(lines) or None


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
