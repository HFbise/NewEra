"""
AI MUD 核心数据结构
- PlayerAction: AI 意图解析的输出格式，规则引擎的输入
- World 相关: 和数据库表一一对应（模板 + 实例）
- RoomView: 喂给意图解析 AI 的房间上下文
- ActionResult: 规则引擎的输出，交给叙事 AI
"""
from typing import Annotated, Any, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field


# ============ 玩家动作（意图解析输出） ============
# 约定：AI 解析时会拿到当前房间的上下文（RoomView），
# 输出里一律填 ref（短编号，如 "i1"、"n1"），规则引擎再换成真实 id 并校验。

class Move(BaseModel):
    action: Literal["move"]
    direction: str                      # 出口名，如 "north"


class Look(BaseModel):
    action: Literal["look"]
    target: Optional[str] = None        # 为空表示看整个房间


class Take(BaseModel):
    action: Literal["take"]
    item: str


class Drop(BaseModel):
    action: Literal["drop"]
    item: str


class Use(BaseModel):
    action: Literal["use"]
    item: str
    # 对谁/对什么使用。可以是物品/NPC 的 ref，也可以是出口方向（用钥匙开门），
    # 规则引擎先按出口方向匹配，匹配不上再当 ref 处理
    target: Optional[str] = None


class Equip(BaseModel):
    action: Literal["equip"]
    item: str


class Attack(BaseModel):
    action: Literal["attack"]
    target: str


class Talk(BaseModel):
    action: Literal["talk"]
    target: str
    message: str                        # 玩家说的话原文


class Give(BaseModel):
    action: Literal["give"]
    item: str
    target: str


class Freeform(BaseModel):
    """规则引擎覆盖不到的动作，交给 AI 自由叙事，但不能改关键状态"""
    action: Literal["freeform"]
    description: str


PlayerAction = Annotated[
    Union[Move, Look, Take, Drop, Use, Equip, Attack, Talk, Give, Freeform],
    Field(discriminator="action"),
]


class ParsedInput(BaseModel):
    actions: list[PlayerAction]         # 一句话可能拆成多个动作，如"拿起剑然后砍哥布林"


# ============ 世界数据（对应数据库表） ============

ItemType = Literal["weapon", "armor", "consumable", "key", "misc"]
Slot = Literal["weapon", "armor"]


class Room(BaseModel):
    id: str
    name: str
    description: str


class RoomExit(BaseModel):
    room_id: str
    direction: str
    to_room: str
    locked: bool = False
    key_item: Optional[str] = None      # 开锁需要的物品模板 id


class ItemTemplate(BaseModel):
    id: str
    name: str
    description: str
    type: ItemType
    takeable: bool = True
    stackable: bool = False
    damage: int = 0
    defense: int = 0
    heal: int = 0
    props: dict[str, Any] = {}


class NpcTemplate(BaseModel):
    id: str
    name: str
    description: str
    persona: str                        # 给 AI 的人设，决定说话风格
    hostile: bool = False
    max_hp: Optional[int] = None        # 为空表示不可战斗
    attack: int = 0
    defense: int = 0
    props: dict[str, Any] = {}


class ItemInstance(BaseModel):
    id: UUID
    template: ItemTemplate              # 查询时 join 模板，省得到处再查
    quantity: int = 1
    room_id: Optional[str] = None
    player_id: Optional[UUID] = None
    npc_id: Optional[UUID] = None
    equipped_slot: Optional[Slot] = None
    props: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self.template.name


class Npc(BaseModel):
    id: UUID
    template: NpcTemplate
    room_id: Optional[str] = None
    hp: Optional[int] = None
    alive: bool = True
    memory: str = ""                    # 长期记忆摘要

    @property
    def name(self) -> str:
        return self.template.name

    @property
    def combatable(self) -> bool:
        return self.template.max_hp is not None


class Player(BaseModel):
    id: UUID
    name: str
    room_id: str
    hp: int
    max_hp: int
    attack: int                         # 基础值，不含装备加成
    defense: int
    flags: dict[str, Any] = {}          # 任务标记


# ============ 意图解析的房间上下文 ============
# uuid 太长，小模型容易抄错，所以给 AI 看短编号（i1、n1），
# refs 保存短编号到真实 id 的映射，只在服务器端用，不发给 AI。

class RoomView(BaseModel):
    player: Player
    room: Room
    exits: list[RoomExit]
    items: list[ItemInstance]           # 地上的
    npcs: list[Npc]                     # 活着的
    inventory: list[ItemInstance]       # 玩家背包（含已装备）
    refs: dict[str, UUID] = {}

    def assign_refs(self) -> None:
        self.refs = {}
        for prefix, objs in (("i", self.items + self.inventory), ("n", self.npcs)):
            for n, obj in enumerate(objs, 1):
                self.refs[f"{prefix}{n}"] = obj.id

    def resolve(self, ref: str) -> Optional[UUID]:
        return self.refs.get(ref)


# ============ 规则引擎输出 ============

class ActionResult(BaseModel):
    action: str
    success: bool
    facts: list[str]                    # 客观事实，叙事 AI 只能基于这些写，不能编造结果
    # 例: ["玩家用生锈的短剑攻击哥布林", "造成 4 点伤害", "哥布林剩余 HP 4/8"]
