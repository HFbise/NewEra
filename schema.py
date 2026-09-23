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
    target: str                         # NPC 的 ref，或者同房间玩家的名字（PvP）


class Talk(BaseModel):
    action: Literal["talk"]
    target: str
    message: str                        # 玩家说的话原文


class Give(BaseModel):
    action: Literal["give"]
    item: str
    target: str                         # NPC 的 ref，或者同房间玩家的名字


class Revive(BaseModel):
    """急救倒下的玩家，救起来只有 1 HP"""
    action: Literal["revive"]
    target: str                         # 玩家名字


class Invite(BaseModel):
    """邀请同房间的玩家组队"""
    action: Literal["invite"]
    target: str                         # 玩家名字


class Join(BaseModel):
    """接受邀请，加入对方的队伍"""
    action: Literal["join"]
    target: str                         # 发出邀请的玩家名字


class LeaveParty(BaseModel):
    action: Literal["leave_party"]


class Follow(BaseModel):
    """跟着同房间的某个玩家，对方移动时自动一起走"""
    action: Literal["follow"]
    target: str                         # 玩家名字


class Unfollow(BaseModel):
    action: Literal["unfollow"]


# 借环境、创意动作打人：AI 当裁判给出难度、伤害档位、负面状态，规则引擎掷骰、限幅后执行。
# AI 只能选档位，具体数字在 engine.TIER_DAMAGE，说得再夸张也超不过上限
Difficulty = Literal["easy", "normal", "hard"]
Tier = Literal["none", "light", "heavy", "lethal"]      # 无伤 / 轻伤 / 重伤 / 致命
StatusKind = Literal["incapacitated", "restrained"]     # 失去战斗能力（昏迷、砸晕） / 束缚（捆住、压住）


class Stunt(BaseModel):
    action: Literal["stunt"]
    target: str                         # NPC 的 ref，或者同房间玩家的名字
    description: str                    # 第三人称简述怎么做的
    feature: Optional[str] = None       # 用到的可利用地形 ref（f1）；只用环境里随手的东西就空，最多轻伤
    item: Optional[str] = None          # 用到的背包物品 ref（绳子、武器）；捆人（restrained）必须有 feature 或 item
    push: Optional[str] = None          # 把对方推、踹、扔进哪个出口（英文方向），成功就挪到那个房间；只能推玩家
    difficulty: Difficulty = "normal"   # 做成的难度，引擎按它掷骰
    tier: Tier = "none"                 # 做成时的伤害档位
    status: Optional[StatusKind] = None # 做成时给对方加的负面状态
    status_label: Optional[str] = None  # 状态的说法，如"被石头砸晕了""被绳子捆住了"
    escape: Difficulty = "normal"       # 这个状态挣脱或醒来的难度


class Struggle(BaseModel):
    """挣脱束缚、从昏迷里醒来。难度由 AI 看玩家怎么做来判，引擎掷骰"""
    action: Literal["struggle"]
    description: str = ""
    difficulty: Difficulty = "normal"


class Freeform(BaseModel):
    """规则引擎覆盖不到的动作，交给 AI 自由叙事，但不能改关键状态"""
    action: Literal["freeform"]
    description: str


class Say(BaseModel):
    """对同房间的玩家说话，广播给房间里所有人，不走 AI"""
    action: Literal["say"]
    message: str
    target: Optional[str] = None        # 对某个玩家说时填对方名字，为空就是对大家说


class Reject(BaseModel):
    """意图解析判定这句话不成立：目标不存在、要靠改关键状态才能实现、越权指令等"""
    action: Literal["reject"]
    reason: str                         # 客观简述为什么做不到


PlayerAction = Annotated[
    Union[Move, Look, Take, Drop, Use, Equip, Attack, Talk, Give, Say, Revive, Invite, Join, LeaveParty,
          Follow, Unfollow, Stunt, Struggle, Freeform, Reject],
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
    details: str = ""                   # 只给 AI 的环境细节


# 出口方向：数据库和动作里用英文 key，显示时翻成中文
DIR_NAMES = {"north": "北", "south": "南", "east": "东", "west": "西", "up": "上", "down": "下"}


def dir_name(direction: str) -> str:
    return DIR_NAMES.get(direction, direction)


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
    status: Optional[Status] = None

    @property
    def name(self) -> str:
        return self.template.name

    @property
    def combatable(self) -> bool:
        return self.template.max_hp is not None


class Status(BaseModel):
    """负面状态，存在 players.status / npcs.status（jsonb），一次只有一个"""
    kind: StatusKind
    label: str                          # AI 起的说法，叙事和界面显示用
    escape: Difficulty = "normal"       # 施加时 AI 判的挣脱难度
    attempts: int = 0                   # 挣脱失败过几次，每次失败下次更容易
    since: Optional[str] = None         # 施加时间（ISO），超过 engine.STATUS_MAX 自动解除

    def describe(self) -> str:
        return f"{self.label}，" + ("失去战斗能力" if self.kind == "incapacitated" else "动弹不得")


class Feature(BaseModel):
    """可利用地形：world.yaml 里声明的、能拿来做文章的环境物件（松动的大石头）"""
    id: UUID
    key: str
    name: str
    max_tier: Tier                      # 用它最多能打出几档伤害
    uses_left: int


class Player(BaseModel):
    id: UUID
    name: str
    room_id: str
    hp: int
    max_hp: int
    attack: int                         # 基础值，不含装备加成
    defense: int
    flags: dict[str, Any] = {}          # 任务标记
    party_id: Optional[UUID] = None     # 所在队伍，没组队为空
    status: Optional[Status] = None     # 负面状态
    following: Optional[UUID] = None    # 正在跟着谁


# ============ 意图解析的房间上下文 ============
# uuid 太长，小模型容易抄错，所以给 AI 看短编号（i1、n1），
# refs 保存短编号到真实 id 的映射，只在服务器端用，不发给 AI。

class OtherPlayer(BaseModel):
    name: str
    awake: bool                         # 睡着的玩家留在原地，不会回应
    downed: bool = False                # HP 归零倒下了，等人急救
    status: Optional[Status] = None


class RoomView(BaseModel):
    player: Player
    room: Room
    exits: list[RoomExit]
    items: list[ItemInstance]           # 地上的
    npcs: list[Npc]                     # 活着的
    inventory: list[ItemInstance]       # 玩家背包（含已装备）
    others: list[OtherPlayer] = []      # 同房间的其他玩家
    party: list[str] = []               # 队友名字（不含自己，不论在不在同一房间）
    invites: list[str] = []             # 还有效的、邀请自己组队的玩家名字
    features: list[Feature] = []        # 这里还能用的可利用地形
    following: Optional[str] = None     # 正在跟着的玩家名字
    refs: dict[str, UUID] = {}

    def assign_refs(self) -> None:
        self.refs = {}
        for prefix, objs in (("i", self.items + self.inventory), ("n", self.npcs), ("f", self.features)):
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
