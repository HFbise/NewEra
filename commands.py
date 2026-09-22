"""
简易命令解析：不调 AI，把常见的中英文命令转成 PlayerAction。
- 标准动作走这里省 token，解析不了的归 freeform（以后交给意图解析 AI）
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


def _dir(text: str) -> Optional[str]:
    t = text.strip().lower()
    t = re.sub(r"^(往|向|朝)", "", t)
    t = re.sub(r"(边|面|方|去|走)$", "", t)
    return DIRS.get(t)


def _find(view: RoomView, text: str, where: str = "all") -> str:
    """名字 → 短编号。先在 where（room 地上 / inv 背包 / npc）里找，找不到再全范围找，
    因为"拿短剑；装备短剑"解析时短剑还在地上。位置对不对由引擎查库判断。
    都找不到就原样返回，交给引擎报错"""
    text = text.strip()
    if text in view.refs:
        return text
    everything = view.items + view.inventory + view.npcs
    preferred = {"room": view.items, "inv": view.inventory, "npc": view.npcs, "all": everything}[where]
    by_id = {uid: ref for ref, uid in view.refs.items()}
    for pool in (preferred, everything):
        for match in (lambda n: n == text, lambda n: text in n or n in text):
            for obj in pool:
                if match(obj.name):
                    return by_id[obj.id]
    return text


def _target(view: RoomView, text: str) -> str:
    """use / look 的目标：先当方向，再当名字"""
    return _dir(text) or _find(view, text)


def parse_one(view: RoomView, text: str) -> dict:
    t = text.strip()

    if t.lower() == "l":
        return {"action": "look", "target": None}
    if m := re.fullmatch(r"(?:look|查看|观察|看)(?:\s*(.+))?", t, re.I):
        return {"action": "look", "target": _target(view, m[1]) if m[1] else None}

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

    # 开锁：自动找背包里的钥匙，必须放在"打"前面，不然"打开"会被当成攻击
    if m := re.fullmatch(r"(?:unlock|打开|开锁|开)\s*(.+?)(?:的?门)?", t, re.I):
        keys = [i for i in view.inventory if i.template.type == "key"]
        if (d := _dir(m[1])) and keys:
            return {"action": "use", "item": _find(view, keys[0].name, "inv"), "target": d}

    if m := re.fullmatch(r"(?:use|使用|用|吃|喝)\s*(\S+?)(?:\s+(?:on\s+)?(.+))?", t, re.I):
        return {"action": "use", "item": _find(view, m[1], "inv"),
                "target": _target(view, m[2]) if m[2] else None}

    if m := re.fullmatch(r"(?:attack|kill|hit|攻击|杀|打)\s*(.+)", t, re.I):
        return {"action": "attack", "target": _find(view, m[1], "npc")}

    if m := re.fullmatch(r"(?:对|跟|和|向)\s*(.+?)\s*(?:说|讲|问)[:：]?\s*(.+)", t):
        return {"action": "talk", "target": _find(view, m[1], "npc"), "message": m[2]}
    if m := re.fullmatch(r"(?:say|talk)\s+(\S+)\s+(.+)", t, re.I):
        return {"action": "talk", "target": _find(view, m[1], "npc"), "message": m[2]}

    if m := re.fullmatch(r"把\s*(.+?)\s*(?:给|交给|递给)\s*(.+)", t):
        return {"action": "give", "item": _find(view, m[1], "inv"), "target": _find(view, m[2], "npc")}
    if m := re.fullmatch(r"give\s+(\S+)\s+(?:to\s+)?(\S+)", t, re.I):
        return {"action": "give", "item": _find(view, m[1], "inv"), "target": _find(view, m[2], "npc")}

    return {"action": "freeform", "description": t}


def parse(view: RoomView, text: str) -> list:
    parts = [p for p in re.split(r"[;；]", text) if p.strip()]
    return [_action(parse_one(view, p)) for p in parts]
