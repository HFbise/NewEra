"""
简易命令解析：不调 AI，把常见的中英文命令转成 PlayerAction。
- 只处理有把握的输入，省 token。认不出的句式、或者名字对不上房间里任何东西，
  一律归 freeform，服务器再交给意图解析 AI（它看得到完整上下文，能理解"那把剑""胖老板"）
- 名字按当前 RoomView 匹配成短编号，也可以直接写编号（i1、n1）
- 用 ; 或 ； 分隔可以一次输入多个命令
"""
import re
from typing import Optional

from pydantic import TypeAdapter

from schema import PlayerAction, RoomView

_action = TypeAdapter(PlayerAction).validate_python

DIRS = {
    "n": "north", "north": "north", "北": "north",
    "s": "south", "south": "south", "南": "south",
    "e": "east", "east": "east", "东": "east",
    "w": "west", "west": "west", "西": "west",
    "u": "up", "up": "up", "上": "up",
    "d": "down", "down": "down", "下": "down",
}


# "看周围"这类说法等于看整个房间
LOOK_ALL = {"周围", "四周", "附近", "环境", "房间", "这里", "一下", "看", "一圈", "around", "room"}


class _Unsure(Exception):
    """名字对不上，这句交给 AI"""


def _dir(text: str) -> Optional[str]:
    t = text.strip().lower()
    t = re.sub(r"^(往|向|朝)", "", t)
    t = re.sub(r"(边|面|方|去|走)$", "", t)
    return DIRS.get(t)


def _find(view: RoomView, text: str, where: str = "all") -> str:
    """名字 → 短编号。先在 where（room 地上 / inv 背包 / npc）里找，找不到再全范围找，
    因为"拿短剑；装备短剑"解析时短剑还在地上。位置对不对由引擎查库判断。
    只认整个名字或名字的一部分（"短剑"认得出"生锈的短剑"）；反过来名字只是句子一部分的
    （"面包塞在寒风嘴里"）不认，否则后半句会被吞掉。都找不到就抛 _Unsure，整句交给 AI"""
    text = text.strip()
    if text in view.refs:
        return text
    everything = view.items + view.inventory + view.npcs + view.dispensers
    preferred = {"room": view.items + view.dispensers, "inv": view.inventory, "npc": view.npcs, "all": everything}[where]
    by_id = {uid: ref for ref, uid in view.refs.items()}
    for pool in (preferred, everything):
        for match in (lambda n: n == text, lambda n: text in n):
            for obj in pool:
                if match(obj.name):
                    return by_id[obj.id]
    # 拿武器桶里、桌上的东西："拿短剑""拿武器桶里的生锈的短剑""拿桌上的护符"
    if where == "room":
        for d in view.dispensers:
            if text in d.item_name or d.container in text and (d.item_name in text or text.endswith(d.item_name[-2:])):
                return by_id[d.id]
    raise _Unsure(text)


def _player(view: RoomView, text: str) -> Optional[str]:
    """同房间其他玩家的名字匹配，先精确后模糊"""
    text = text.strip()
    for match in (lambda n: n == text, lambda n: text in n):
        for p in view.others:
            if match(p.name):
                return p.name
    return None


def _player_or_unsure(view: RoomView, text: str, invites: bool = False) -> str:
    """必须是玩家名字的地方。invites=True 时也认邀请过自己的人（接受邀请时对方可能不在同一房间）"""
    if name := _player(view, text):
        return name
    if invites:
        text = text.strip()
        for match in (lambda n: n == text, lambda n: text in n):
            for name in view.invites:
                if match(name):
                    return name
    raise _Unsure(text)


def _target(view: RoomView, text: str) -> str:
    """use / look 的目标：先当方向，再当玩家名字，再当物品、NPC"""
    return _dir(text) or next((p.name for p in view.others if p.name == text.strip()), None) or _find(view, text)


def parse_one(view: RoomView, text: str) -> dict:
    try:
        return _parse_one(view, text.strip())
    except _Unsure:
        return {"action": "freeform", "description": text.strip()}


def _parse_one(view: RoomView, t: str) -> dict:
    # 失去战斗能力时说什么都算挣扎着醒来；有 AI 时服务器会让 AI 按怎么做来判难度
    st = view.player.status
    if st and st.kind == "incapacitated":
        return {"action": "struggle", "description": t}
    if re.fullmatch(r"站起来|爬起来|起身|起来|站起|爬起|stand( up)?", t, re.I):
        return {"action": "stand"}
    if re.fullmatch(r"挣脱|挣扎|醒来|醒过来|struggle", t, re.I):
        return {"action": "struggle", "description": t}

    # 倒下了选择被抬回酒馆（或者说了去哪）："复活""回酒馆复活""在铁匠铺复活"
    if m := re.fullmatch(r"(?:(?:在|回|去|到)\s*(.+?)\s*)?(?:复活|重生|回城)|respawn", t, re.I):
        return {"action": "respawn", "target": m[1]}

    if re.fullmatch(r"搜索|搜寻|搜查|找找|四处找找|search", t, re.I):
        return {"action": "search", "description": "四处搜寻"}
    if re.fullmatch(r"闪避|闪躲|躲闪|闪开|dodge", t, re.I):
        return {"action": "dodge", "description": "摆好架势，准备闪避"}
    # 躲在哪、怎么躲要 AI 看环境判难度，这里只认光秃秃的一句"躲起来"
    if re.fullmatch(r"躲起来|躲一躲|藏起来|hide", t, re.I):
        return {"action": "hide", "description": "找地方躲了起来", "difficulty": "normal"}

    if t.lower() == "l":
        return {"action": "look", "target": None}
    if m := re.fullmatch(r"(?:look|查看|观察|看看|看)(?:\s*(.+))?", t, re.I):
        target = (m[1] or "").strip()
        return {"action": "look", "target": None if not target or target.lower() in LOOK_ALL
                else _target(view, target)}

    if d := _dir(t):
        return {"action": "move", "direction": d}
    if m := re.fullmatch(r"(?:go|走|去|往|向)\s*(.+)", t, re.I):
        if d := _dir(m[1]):
            return {"action": "move", "direction": d}

    if m := re.fullmatch(r"(?:take|get|拿起|捡起|拿|捡)\s*(.+)", t, re.I):
        return {"action": "take", "item": _find(view, m[1], "room")}

    if m := re.fullmatch(r"(?:drop|扔掉|丢掉|放下|扔|丢)\s*(.+)", t, re.I):
        return {"action": "drop", "item": _find(view, m[1], "inv")}

    if m := re.fullmatch(r"(?:equip|wear|wield|装备|穿上|戴上)\s*(.+)", t, re.I):
        return {"action": "equip", "item": _find(view, m[1], "inv")}
    if m := re.fullmatch(r"(?:unequip|卸下|脱下|摘下|脱掉|摘掉)\s*(.+)", t, re.I):
        return {"action": "unequip", "item": _find(view, m[1], "inv")}

    # 开锁：自动找背包里的钥匙，必须放在"打"前面，不然"打开"会被当成攻击
    if m := re.fullmatch(r"(?:unlock|打开|开锁|开)\s*(.+?)(?:的?门)?", t, re.I):
        keys = [i for i in view.inventory if i.template.type == "key"]
        if (d := _dir(m[1])) and keys:
            return {"action": "use", "item": _find(view, keys[0].name, "inv"), "target": d}

    if m := re.fullmatch(r"(?:use|使用|用|吃|喝)\s*(\S+?)(?:\s+(?:on\s+)?(.+))?", t, re.I):
        return {"action": "use", "item": _find(view, m[1], "inv"),
                "target": _target(view, m[2]) if m[2] else None}

    # 急救、组队只对玩家，名字对不上就交给 AI
    if m := (re.fullmatch(r"(?:revive|急救|救起|扶起|救醒|叫醒|喊醒|松绑|救)\s*(.+)", t, re.I)
             or re.fullmatch(r"(?:给|帮)\s*(.+?)\s*(?:松绑|解开)", t)):
        return {"action": "revive", "target": _player_or_unsure(view, m[1])}
    if re.fullmatch(r"停止跟随|不跟了|别跟了|不再跟着.*|unfollow", t, re.I):
        return {"action": "unfollow"}
    if m := re.fullmatch(r"(?:跟着|跟随|跟上|尾随|follow)\s*(.+?)(?:走)?", t, re.I):
        return {"action": "follow", "target": _player_or_unsure(view, m[1])}
    if re.fullmatch(r"(?:离开|退出)队伍|退队|leave party", t, re.I):
        return {"action": "leave_party"}
    if m := (re.fullmatch(r"(?:加入|接受)\s*(.+?)\s*的?(?:队伍|邀请|队)", t)
             or re.fullmatch(r"join\s+(.+)", t, re.I)):
        return {"action": "join", "target": _player_or_unsure(view, m[1], invites=True)}
    if m := re.fullmatch(r"(?:和|跟|与)\s*(.+?)\s*组队", t):
        # 对方邀请过自己就是接受，否则是发邀请
        name = _player_or_unsure(view, m[1], invites=True)
        return {"action": "join" if name in view.invites else "invite", "target": name}
    if m := (re.fullmatch(r"(?:邀请|拉)\s*(.+?)\s*(?:组队|入队|进队|加入队伍)?", t)
             or re.fullmatch(r"invite\s+(.+)", t, re.I)):
        return {"action": "invite", "target": _player_or_unsure(view, m[1])}

    # 找铁匠升级武器："升级短剑""强化生锈的短剑"；铁匠就是这里会升级的那个 NPC
    if m := re.fullmatch(r"(?:升级|强化|锻造|改良)\s*(.+)", t):
        smith = next((n for n in view.npcs if n.template.props.get("upgrades")), None)
        if smith:
            by_id = {uid: ref for ref, uid in view.refs.items()}
            return {"action": "upgrade", "item": _find(view, m[1], "inv"), "target": by_id[smith.id]}

    # 决斗（PvP）：申请、接受、拒绝、逃跑
    if re.fullmatch(r"逃跑|逃走|逃|跑路|撤退|脱战|脱身|flee", t, re.I):
        return {"action": "flee", "description": "转身逃跑"}
    duel = r"(?:决斗|pvp|单挑|挑战)(?:申请|邀请)?"
    if m := re.fullmatch(rf"(?:接受|同意)\s*(?:(.+?)\s*的)?\s*{duel}|应战", t, re.I):
        return {"action": "accept_duel", "target": _player_or_unsure(view, m[1]) if m[1] else None}
    if m := re.fullmatch(rf"拒绝\s*(?:(.+?)\s*的)?\s*{duel}", t, re.I):
        return {"action": "decline_duel", "target": _player_or_unsure(view, m[1]) if m[1] else None}
    if m := (re.fullmatch(r"(?:申请|发起)?(?:决斗|pvp|单挑|挑战)\s*(.+)", t, re.I)
             or re.fullmatch(r"(?:向|跟|和|与|对)\s*(.+?)\s*(?:申请|发起)?(?:决斗|pvp|单挑|挑战)", t, re.I)):
        return {"action": "challenge", "target": _player_or_unsure(view, m[1])}

    # 打的是玩家就是 PvP，target 填名字；否则是 NPC 的 ref。
    # 先认准确的玩家名，再找 NPC，最后才模糊匹配玩家，免得有人叫"布"时"打哥布林"打到他
    if m := re.fullmatch(r"(?:attack|kill|hit|攻击|杀|打)\s*(.+)", t, re.I):
        name = m[1].strip()
        if any(p.name == name for p in view.others):
            return {"action": "attack", "target": name}
        try:
            return {"action": "attack", "target": _find(view, name, "npc")}
        except _Unsure:
            return {"action": "attack", "target": _player_or_unsure(view, name)}

    # 注意"对麦琪 来杯黑啤"（没有"说"）不在这里认：同样的句式也可能是动作（"对哥布林 发动强力砍击"），交给 AI 分
    # 对某人说：对方是玩家就是 say（广播，不走 AI），是 NPC 就是 talk（AI 扮演 NPC 回话）
    if m := (re.fullmatch(r"(?:对|跟|和|向)\s*(.+?)\s*(?:说|讲|问)[:：]?\s*(.+)", t)
             or re.fullmatch(r"(?:say|talk)\s+(?:to\s+)?(\S+)\s+(.+)", t, re.I)):
        if name := _player(view, m[1]):
            return {"action": "say", "target": name, "message": m[2]}
        return {"action": "talk", "target": _find(view, m[1], "npc"), "message": m[2]}

    # 对大家说
    if m := re.fullmatch(r"(?:说|喊|say)[:：]?\s*(.+)", t, re.I):
        return {"action": "say", "message": m[1]}

    # 喂别人吃（跟"给"不一样：给是东西到他手上，喂是吃下去）："把药草喂给寒风""喂寒风吃药草"
    if m := re.fullmatch(r"把\s*(.+?)\s*喂给\s*(.+)", t):
        return {"action": "use", "item": _find(view, m[1], "inv"), "target": _player_or_unsure(view, m[2])}
    if m := re.fullmatch(r"喂\s*(.+?)\s*(?:吃|喝)\s*(?:了|下|点)?\s*(.+)", t):
        return {"action": "use", "item": _find(view, m[2], "inv"), "target": _player_or_unsure(view, m[1])}

    # 给东西：对方是玩家就填名字，是 NPC 就填 ref
    if m := (re.fullmatch(r"把\s*(.+?)\s*(?:给|交给|递给)\s*(.+)", t)
             or re.fullmatch(r"give\s+(\S+)\s+(?:to\s+)?(\S+)", t, re.I)):
        who = next((p.name for p in view.others if p.name == m[2].strip()), None)
        return {"action": "give", "item": _find(view, m[1], "inv"), "target": who or _find(view, m[2], "npc")}

    return {"action": "freeform", "description": t}


def parse(view: RoomView, text: str) -> list:
    parts = [p for p in re.split(r"[;；]", text) if p.strip()]
    return [_action(parse_one(view, p)) for p in parts]
