"""
AI MUD 核心数据结构
- PlayerAction: AI 意图解析的输出格式，规则引擎的输入
- World 相关: 和数据库表一一对应（模板 + 实例）
- RoomView: 喂给意图解析 AI 的房间上下文
- ActionResult: 规则引擎的输出，交给叙事 AI
"""
from typing import Annotated, Any, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, Field


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
    slot: Optional[str] = None          # 玩家指定了哪只手、哪个戒指位（left_hand / right_hand / ring1 / ring2），没说就空


class Unequip(BaseModel):
    """卸下、脱下、摘下装备，放回背包"""
    action: Literal["unequip"]
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


class Upgrade(BaseModel):
    """找铁匠升级武器：第一次说是问价（开价、说清碎掉的几率），开过价再说一次就动手"""
    action: Literal["upgrade"]
    item: str                           # 背包里武器的 ref
    target: str                         # 铁匠 NPC 的 ref


class Respawn(BaseModel):
    """倒下的人自己选择被抬回有人照看的地方（酒馆、铁匠铺），那里的 NPC 扶起来回满血"""
    action: Literal["respawn"]
    target: Optional[str] = None        # 想去哪（地方名字的一部分），不填就是酒馆


class Sell(BaseModel):
    """把身上的东西卖给做生意的 NPC（world.yaml props.buys）：什么都收，她用得上的给价高"""
    action: Literal["sell"]
    item: str                           # 背包物品的 ref
    target: str                         # NPC 的 ref


class Pay(BaseModel):
    """给钱。给 NPC：刚报过价（货、住店、升级）就按报价成交，多给的算小费；没在做买卖就是白给，
    NPC 先问一句"真要给我？"，玩家确认了才收。给同房间的玩家直接到他手上"""
    action: Literal["pay"]
    target: str                         # NPC 的 ref，或者同房间玩家的名字
    amount: int = Field(gt=0)


class Teleport(BaseModel):
    """传送石：在地牢的传送石边上回到地窖（floor 为空），在地窖里传送到到过的传送石那一层"""
    action: Literal["teleport"]
    floor: Optional[int] = None


class Camp(BaseModel):
    """地牢里在清完怪的房间扎营：回一部分血（空房更多、带帐篷更多、生存判定成功再多），每层每人一次"""
    action: Literal["camp"]


class Rest(BaseModel):
    """住店：在有店家的地方付钱睡一觉，回满血、醒酒"""
    action: Literal["rest"]


class Stand(BaseModel):
    """倒地（被绊倒、掀翻）后爬起来：不用掷骰，但占掉这一下"""
    action: Literal["stand"]


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


# PvP 得先申请决斗、对方接受才能造成伤害；没开决斗时对玩家只能做不伤血的整人动作（捆住、迷眼）
class Challenge(BaseModel):
    """向同房间的玩家发起决斗申请"""
    action: Literal["challenge"]
    target: str                         # 玩家名字


class AcceptDuel(BaseModel):
    action: Literal["accept_duel"]
    target: Optional[str] = None        # 发起决斗的玩家名字；只有一个人申请时可以不填


class DeclineDuel(BaseModel):
    action: Literal["decline_duel"]
    target: Optional[str] = None


class Flee(BaseModel):
    """逃跑：决斗的发起者要离开这里得先逃跑成功，被挑战的一方随时能走"""
    action: Literal["flee"]
    description: str = ""


# 借环境、创意动作打人：AI 当裁判给出难度、伤害档位、负面状态，规则引擎掷骰、限幅后执行。
# AI 只能选档位，伤害区间在 engine.TIER_RANGE，说得再夸张也超不过上限
def _difficulty(v: Any) -> Any:
    """难度是 1 到 10 的整数；老数据、老写法的 easy / normal / hard 换成 1 / 2 / 3"""
    if isinstance(v, str):
        v = {"easy": 1, "normal": 2, "hard": 3}.get(v.strip().lower(), v.strip())
    return v


Difficulty = Annotated[int, BeforeValidator(_difficulty), Field(ge=1, le=10)]

# 生活技能：AI 判一个动作用哪个技能、难度几级，引擎拿难度和技能等级比掷骰（engine.skill_chance）
SKILL_NAMES = {"acrobatics": "体操", "animal": "驯兽", "athletics": "运动", "sleight": "巧手", "stealth": "隐匿",
               "investigation": "调查", "nature": "自然", "perception": "察觉", "survival": "生存", "medicine": "医药",
               "endurance": "耐性"}
Skill = Literal["acrobatics", "animal", "athletics", "sleight", "stealth", "investigation", "nature", "perception",
                "survival", "medicine", "endurance"]
Tier = Literal["none", "light", "heavy", "lethal"]      # 无伤 / 轻伤 / 重伤 / 致命
# 失去战斗能力（昏迷、砸晕） / 束缚（捆住、压住） / 倒地（绊倒、扫腿、掀翻：站起来之前不能走、不能打）
StatusKind = Literal["incapacitated", "restrained", "prone"]


class Stunt(BaseModel):
    action: Literal["stunt"]
    target: str                         # NPC 的 ref，或者同房间玩家的名字
    description: str                    # 第三人称简述怎么做的
    feature: Optional[str] = None       # 用到的可利用地形 ref（f1）；只用环境里随手的东西就空，最多轻伤
    item: Optional[str] = None          # 用到的背包物品 ref（绳子、腰带、武器）；捆人（restrained）必须有 feature 或 item
    consume: bool = False               # item 会被用掉（泼出去的油、点着的布条）；拿来捆人的东西成功了一定用掉
    push: Optional[str] = None          # 把对方推、踹、扔进哪个出口（英文方向），成功就挪到那个房间；只能推玩家
    knockback: int = 0                  # 把 NPC 踹开、撞退几格（0 到 2），成功就拉开距离
    skill: Skill = "athletics"          # 用哪个技能判定（捆人、绊人是体操，推石头、撞人是运动，偷袭是隐匿）
    difficulty: Difficulty = 2          # 1 到 10，引擎拿它跟技能等级比；伤害档位越高引擎会抬高下限
    tier: Tier = "none"                 # 做成时的伤害档位
    status: Optional[StatusKind] = None # 做成时给对方加的负面状态
    status_label: Optional[str] = None  # 状态的说法，如"被石头砸晕了""被绳子捆住了"
    escape: Difficulty = 2              # 这个状态挣脱或醒来的难度


class Struggle(BaseModel):
    """挣脱束缚、从昏迷里醒来。难度由 AI 看玩家怎么做来判，引擎掷骰"""
    action: Literal["struggle"]
    description: str = ""
    difficulty: Difficulty = 2


class Maneuver(BaseModel):
    """同一区域里走近、退开某个 NPC 或决斗对手。格数由 AI 看玩家怎么说来判，引擎限在每次最多 engine.MAX_STEP 格"""
    action: Literal["maneuver"]
    target: str                         # NPC 的 ref，或者决斗对手的名字
    steps: int                          # 正数是靠近，负数是退开
    description: str = ""


class Dodge(BaseModel):
    """摆好架势准备闪避：这个动作之后敌人的这一下命中率降低"""
    action: Literal["dodge"]
    description: str = ""


class Hide(BaseModel):
    """躲起来，不让敌人发现。难度由 AI 看藏身的地方判，引擎掷骰"""
    action: Literal["hide"]
    description: str = ""               # 第三人称简述怎么躲的
    difficulty: Difficulty = 2          # 隐匿的难度


class Search(BaseModel):
    """四处搜寻：能找出躲着的、刚回来的敌人（NPC 到了复活时间，有人在场时要搜才会出现）"""
    action: Literal["search"]
    description: str = ""


class Freeform(BaseModel):
    """规则引擎覆盖不到的动作，交给 AI 自由叙事，但不能改关键状态。
    需要本事的（辨认草药有没有毒、翻墙、查线索）AI 填技能和难度，引擎掷骰，叙事照成败写"""
    action: Literal["freeform"]
    description: str
    skill: Optional[Skill] = None
    difficulty: Optional[Difficulty] = None


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
    Union[Move, Look, Take, Drop, Use, Equip, Unequip, Attack, Talk, Give, Sell, Pay, Upgrade, Say, Revive, Respawn, Stand, Rest, Camp, Teleport, Invite, Join, LeaveParty,
          Follow, Unfollow, Challenge, AcceptDuel, DeclineDuel, Flee, Stunt, Struggle, Maneuver, Dodge, Hide, Search,
          Freeform, Reject],
    Field(discriminator="action"),
]


class ParsedInput(BaseModel):
    actions: list[PlayerAction]         # 一句话可能拆成多个动作，如"拿起剑然后砍哥布林"


# ============ 世界数据（对应数据库表） ============

ItemType = Literal["weapon", "armor", "consumable", "key", "misc"]
# 装备栏：每格一件。物品模板的 slot 是它能放的位置：hand（左右手都行，武器、以后的盾）、ring（两个戒指位都行），
# 其余就是格子本身
Slot = Literal["head", "chest", "belt", "legs", "feet", "neck", "ring1", "ring2", "left_hand", "right_hand"]
SLOT_NAMES = {"head": "头盔", "chest": "胸甲", "belt": "腰带", "legs": "腿部", "feet": "鞋子", "neck": "项链",
              "ring1": "戒指", "ring2": "戒指", "left_hand": "左手", "right_hand": "右手"}
SLOT_CHOICES = {"hand": ["right_hand", "left_hand"], "ring": ["ring1", "ring2"]}


class Room(BaseModel):
    id: str
    name: str
    description: str
    details: str = ""                   # 只给 AI 的环境细节
    props: dict[str, Any] = {}          # world.yaml 里房间的 forage（搜索能找到的东西）、dispensers（取用处）


class Dispenser(BaseModel):
    """取用处：酒馆的武器桶这类，每人能拿一件，身上已经有 unless 里的东西就不能再拿。配在 world.yaml 房间的 dispensers 里"""
    id: UUID                            # 按房间和 key 算出来的固定 id，只用来分配短编号
    room: str
    key: str
    container: str                      # "武器桶"，在房间里跟 NPC 列在一起，但不会说话
    description: str = ""
    item: str                           # 物品模板 id
    item_name: str
    unless: list[str] = []
    where: str = "里"                   # 东西放在"桶里""桌上"
    once: bool = False                  # 每人一辈子只能拿一次（丢了也不能再拿），记在 dispenser_log
    repeat: bool = False                # 身上已经有同样的东西也能拿（地牢里挖矿石、接泉水）
    skill: Optional[str] = None         # 拿之前要过的技能判定（挖矿要运动、挑没坏的酒要自然），过了才拿得到
    difficulty: int = 0
    fail: str = ""                      # 判定失败时发生的事
    fail_damage: int = 0                # 失败掉的血
    available: bool = True              # 这个玩家还能不能拿（身上已经有了就只看得到桶、桌子本身）

    @property
    def name(self) -> str:
        return self.container

    @property
    def take_label(self) -> str:
        return f"{self.container}{self.where}的{self.item_name}"


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
    slot: Optional[str] = None          # 能装在哪：hand / ring / head / chest / belt / legs / feet / neck，不能装备就空
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
    props: dict[str, Any] = {}          # NPC 现造的东西（酒、地图）把名字和描述存在这里，覆盖模板的

    @property
    def name(self) -> str:
        return self.props.get("name") or self.template.name

    @property
    def description(self) -> str:
        return self.props.get("description") or self.template.description

    # NPC 现造的东西，数值（AI 定、引擎限过幅）也存在 props 里，没有就用模板的
    @property
    def damage(self) -> int:
        return self.props.get("damage", self.template.damage)

    @property
    def defense(self) -> int:
        return self.props.get("defense", self.template.defense)

    @property
    def heal(self) -> int:
        return self.props.get("heal", self.template.heal)

    @property
    def harm(self) -> int:
        """有毒的东西：吃喝下去掉的血，砸到别人身上造成的伤害"""
        return self.props.get("harm", self.template.props.get("harm", 0))

    @property
    def knockout(self) -> Optional[str]:
        """能把人放倒的东西（蒙汗药酒）：放倒时的说法，比如"喝了蒙汗药昏睡过去"；没有就是 None"""
        return self.props.get("knockout", self.template.props.get("knockout"))


class Npc(BaseModel):
    id: UUID
    template: NpcTemplate
    room_id: Optional[str] = None
    hp: Optional[int] = None
    alive: bool = True
    memory: str = ""                    # 长期记忆摘要
    status: Optional[Status] = None
    effects: list["Effect"] = []        # 玩家装备打上去的中毒、流血、看不清、腐蚀（engine 装备效果部分）

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
    escape: Difficulty = 2              # 施加时 AI 判的挣脱难度
    attempts: int = 0                   # 挣脱失败过几次，每次失败下次更容易
    since: Optional[str] = None         # 施加时间（ISO），超过 engine.STATUS_MAX 自动解除

    def describe(self) -> str:
        return f"{self.label}，" + {"incapacitated": "失去战斗能力", "restrained": "动弹不得",
                                     "prone": "倒在地上，还没爬起来"}[self.kind]


EffectKind = Literal["poison", "bleed", "blind", "corrode", "whet"]     # whet 是好的：磨刀石，这一层伤害 +value


class Effect(BaseModel):
    """怪打中时附带的负面效果，存在 players.effects（jsonb 列表），跟 status 可以同时有好几个。
    poison 中毒：每回合（每条消息）掉 value 血、命中降低；bleed 流血：每个动作掉 value 血、伤害降低；
    blind 看不清：这期间光亮算 0；corrode 腐蚀：防御 -value、血量上限临时 -hp。left 是还剩几回合（流血是几个动作）"""
    kind: EffectKind
    value: int = 1
    left: int = 1
    label: str = ""
    source: str = ""                    # 谁弄的（倒下时记原因）
    hp: int = 0                         # 腐蚀临时扣掉的血量上限，消退时还回去


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
    gold: int = 0                       # 金币
    party_id: Optional[UUID] = None     # 所在队伍，没组队为空
    status: Optional[Status] = None     # 负面状态
    following: Optional[UUID] = None    # 正在跟着谁
    stealth: Optional["Stealth"] = None # 在有敌人的地方有没有被发现
    skills: dict[str, int] = {}         # 每个技能攒了几次熟练（有风险的成功），等级由它算出来
    drunk: bool = False                 # 喝醉了（players.drunk_until 没过）：说话含糊，判定成功率降低
    effects: list[Effect] = []          # 中毒、流血、看不清、腐蚀（engine 的效果部分）


class Stealth(BaseModel):
    """玩家在某个区域里有没有被敌人发现，存在 players.stealth（jsonb）。换了区域就作废重来"""
    room: str
    chance: float                       # 下一个动作被发现的几率，进门时 engine.DETECT_START，每个动作涨一点
    detected: bool = False              # 被发现了：敌人每个动作都打他
    hidden: bool = False                # 躲着：几率不再上涨
    distance: dict[str, int] = {}       # 和每只 NPC（id）隔几格，没记的是 engine.START_DISTANCE
    struck: bool = False                # 这场仗已经出过手了（伏击者短刀这类"每场第一次攻击"的效果用）


Player.model_rebuild()                  # stealth 引用了后面才定义的 Stealth


# ============ 意图解析的房间上下文 ============
# uuid 太长，小模型容易抄错，所以给 AI 看短编号（i1、n1），
# refs 保存短编号到真实 id 的映射，只在服务器端用，不发给 AI。

class OtherPlayer(BaseModel):
    name: str
    awake: bool                         # 睡着的玩家留在原地，不会回应
    downed: bool = False                # HP 归零倒下了，等人急救
    status: Optional[Status] = None


class Duel(BaseModel):
    """自己正在打的决斗（已接受的）"""
    opponent: str
    challenger: bool                    # 自己是不是发起的一方（发起者要逃跑成功才能离开）
    distance: int


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
    dispensers: list[Dispenser] = []    # 取用处
    forage: list[str] = []              # 搜索能找到的东西，带几率："药草（搜索，60%）"
    following: Optional[str] = None     # 正在跟着的玩家名字
    duel: Optional[Duel] = None         # 正在进行的决斗
    challenges: list[str] = []          # 向自己申请决斗、还没回应的玩家名字
    challenging: Optional[str] = None   # 自己申请了、对方还没接受的
    trained: list[str] = []             # 这条消息里已经涨过熟练的技能，一条消息每个技能最多涨一次
    refs: dict[str, UUID] = {}

    def assign_refs(self) -> None:
        self.refs = {}
        for prefix, objs in (("i", self.items + self.inventory), ("n", self.npcs), ("f", self.features),
                             ("d", self.dispensers)):
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


# Npc.effects 用到后面定义的 Effect
Npc.model_rebuild()
