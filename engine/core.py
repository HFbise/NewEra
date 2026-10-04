"""
Shared imports, tuning constants and ActionError. / 公共 import、常量、ActionError

规则引擎：校验并执行玩家动作，写数据库，输出 facts。
- 不调用 AI，全是确定性代码
- 每个动作一个事务，玩家行先 for update 锁住，失败就回滚
- 动作里的 ref（i1、n1）用 RoomView.refs 换成真实 id，之后一律以数据库为准，
  所以同一句话里的多个动作可以共用一份 refs（拿起 i1 之后还能装备 i1）

用法:
  view = load_view(conn, player_id)
  results = execute_all(conn, view, parsed.actions)
"""


import math


import random


import re


from collections import defaultdict


from types import SimpleNamespace


from datetime import datetime, timezone


from typing import Any, Callable, Optional


from uuid import NAMESPACE_URL, UUID, uuid4, uuid5


from psycopg import Connection, Cursor


from psycopg import errors as pg_errors


from psycopg.rows import dict_row


from psycopg.types.json import Jsonb


import dungeon


from rules import *                 # noqa: F403  纯规则（命中、减伤、价钱、升级、怪物数值）见 rules.py


from commands import REST_TALK_RE


from schema import (
    ActionResult, Attack, Drop, Equip, Feature, Follow, Freeform, Give, ItemInstance, LeaveParty, Kick, Look,
    Move, Npc, OtherPlayer, Player, PlayerAction, Reject, Revive, Room, RoomExit, RoomView, Say, Status, Struggle,
    Stunt, Take, Talk, Unfollow, Unequip, Use, dir_name, Dispenser, SLOT_CHOICES, SLOT_NAMES, Dodge, Hide, Maneuver, Search, Stealth, Tame,
    Uncurse, Reload, Refill, Transfer, Reroll, Rename, Write, Socket, Unsocket, Refine, Donate, TakeDonated, Dismantle, CloseEyes, Pinch,
    AcceptDuel, Challenge, DeclineDuel, Duel, Flee, SKILL_NAMES, Upgrade, Respawn, Stand, Rest, Pay, Camp, Teleport, Sell,
    Effect,
)


START_ROOM = "square"                   # 新角色出生、后台传送回去的地方


ONLINE_WINDOW = "90 seconds"            # 超过这么久没有心跳的玩家算睡着。后台标签页浏览器会降低定时器频率，给宽一点


AFFINITY_STEP = 5                       # 对话 AI 每次最多调整的好感度


AFFINITY_RANGE = (-100, 100)


PARTY_MAX = 5                           # 一支队伍最多几个人


DOWNED_ALLOWED = {"look", "say", "respawn"}   # 倒下的人只能看、说话（喊人来救），或者选择被抬回酒馆


RESPAWN_ROOM = "tavern"                 # 倒下的人选择复活时默认被抬去的地方


# 创意攻击（stunt）和负面状态。AI 只选档位和难度，数字都在这里
TIERS = ["none", "light", "heavy", "lethal"]


# 伤害在区间里随机：自由动作是控场用的，新手拿它打伤害不如老老实实砍一刀
TIER_RANGE = {"none": (0, 0), "light": (10, 30), "heavy": (30, 60), "lethal": (50, 90)}      # 战斗数值 ×10（rules.SCALE）


TIER_MIN_DIFFICULTY = {"none": 1, "light": 2, "heavy": 3, "lethal": 4}   # 伤得越重难度越高（对无力反抗的补刀不算）


IMPROVISED_MAX_TIER = "light"           # 空手、随手的东西（没在 world.yaml 声明成可利用地形）最多轻伤


WEAPON_MAX_TIER = "heavy"               # 拿武器的花样（挑、劈、刺喉）最多重伤


WEAPON_STUNT_SHARE = 0.5                # 拿武器的花样打中了，档位伤害之外再加武器伤害的一半（向下取整）


STATUS_MAX = "3 minutes"                # 负面状态最长多久自动解除，防止 AI 一直判醒不过来把人卡死


# 被束缚时做不了的动作；失去战斗能力时只能挣脱（醒来）
RESTRAINED_BLOCKED = {"move", "attack", "stunt", "take", "drop", "equip", "unequip", "use", "give", "revive", "hide", "search",
                      "maneuver", "dodge", "flee"}


# 倒地时做不了的（得先站起来）；吃喝、说话、看、换装备都行
PRONE_BLOCKED = {"move", "attack", "stunt", "maneuver", "dodge", "flee", "hide", "search", "take", "revive", "respawn"}


PRONE_HIT_BONUS = 0.20                  # 打倒在地上的目标，近战命中率加这么多


# 敌人发现玩家：进门时几率 DETECT_START，之后玩家每发一条消息掷一次骰，没被发现就涨 DETECT_STEP（躲着不涨）。
# 被发现了，在场能动的敌人每轮都打他一下（玩家每做 ENEMY_EVERY 个动作敌人行动一次，见 execute_all）
DETECT_START = 0.05


DETECT_PER_FOE = 0.02                   # 一屋子怪比一只难溜过去：每多一只，每条消息被发现的几率多涨这么多


DETECT_STEP = 0.10


DETECT_STEP_MIN = 0.02                  # 隐匿每级让每次上涨少 1%，最少涨这么多


# 同一区域里的距离（格）：刚发现时隔 START_DISTANCE 格。近战（普通攻击，玩家打敌人、敌人打玩家）按距离算命中，
# 够不着的格数命中率是 0。扔东西、推石头这类 stunt 不看距离。
# 玩家挪几格、把敌人踹开几格由 AI 判（每次最多 MAX_STEP）；敌人被发现后每条消息自己逼近 ENEMY_STEP 格再出手
START_DISTANCE = 2


MAX_DISTANCE = 4


MAX_STEP = 2


ENEMY_STEP = 1


DODGE_BONUS = 0.15                      # 闪避动作让敌人这一下的命中率降低这么多，察觉每级再多降 DODGE_PER_LEVEL


DODGE_PER_LEVEL = 0.05


DISTANCE_WORDS = {0: "贴身", 1: "一步之遥", 2: "几步开外"}


# 玩家每做这么多个动作，敌人行动一次（一句话结尾不够数也算一次）。敌人有动静（发现、逼近、出手）就打断后面的，
# 免得一条超长的消息一口气打出一串伤害；被绊倒的敌人轮到时只是爬起来，不打断

# 徒手一击毙命、直接打晕：只有敌人没发现你时能偷袭，按隐匿判，难度至少这么高；对方有防备就不可能
ASSASSINATE_DIFFICULTY = 4


def assassinate_difficulty(depth: int) -> int:
    """偷袭（暗杀）的难度下限：4 起，每 6 层 +1（连续涨），深层的怪不再跟第 1 层一样好秒"""
    return ASSASSINATE_DIFFICULTY + round(steps(depth, 6))


KEEN_EXTRA = 2                          # 对警觉的怪偷袭暗杀难度 +2（它们一见面就发现人，见 _keen_spotted）


PRONE_DECISIVE_DIFFICULTY = 3           # 对刚被绊倒在地的下狠手（打晕、断手、一击毙命）难度至少这么高


# 技能判定、升级要攒几次熟练：见 rules.SKILL_*
# 耐性：每升一级 HP 上限 +ENDURANCE_HP。除了硬扛的判定，挨打（活下来的）、扛住没喝醉有 ENDURE_HIT_CHANCE 的几率涨熟练，
# 吃了有毒的东西活下来一定涨（ENDURANCE_HP、ENDURE_HIT_CHANCE 在 rules）；耐性每级让喝醉的几率乘 DRUNK_RESIST
DRUNK_RESIST = 0.85


FLEE_DIFFICULTY = {0: 3, 1: 2, 2: 1}    # 决斗逃跑（运动）按距离的难度，更远不用判定


REVIVE_DIFFICULTY = 1                   # 急救倒下的人（医药）


# 喝酒（名字带"酒"字，或者 props.alcohol；毒酒也算，毒照样生效）有几率喝醉：一段时间里说话含糊
# （系统往原话里插"嗝""……"），技能判定和普通攻击命中率都降低。几率随连着喝的杯数指数上升：
# 第 n 杯是 DRUNK_CHANCE × 2^(n-1)，DRINK_WINDOW 里没再喝就重新数。醉着再喝重新计时；吃解酒药（props.sober）马上清醒
DRUNK_CHANCE = 0.2


DRINK_WINDOW = "10 minutes"


DRUNK_TIME = "5 minutes"


DRUNK_PENALTY = 0.2


# 搜索找东西（world.yaml 房间 forage）的默认几率和冷却秒数：默认一搜就有、不冷却，房间里可以单独配
FORAGE_CHANCE = 1.0


FORAGE_COOLDOWN = 0


# 决斗（PvP）：申请多久内有效；接受后双方隔几格开打；发起者逃跑按当时的距离掷骰，逃掉了才能离开
DUEL_WINDOW = "5 minutes"


DUEL_DISTANCE = 3


DUEL_RULES = ("决斗规则：对方接受后才开打，只有决斗中才会对彼此造成伤害；双方起始相隔 3 格；"
              "按回合打，两个人都出完手才一起结算；"
              "装备的特效在决斗里也生效（毒、流血、吸血、破甲、加伤害这些），只对怪有用的（专打亡灵、野兽的）不算；"
              "任何一方离开这里决斗就结束，但发起者必须先逃跑成功才能离开，被挑战的一方随时可以走开")


class ActionError(Exception):
    """动作不合法。事务回滚，message 作为失败的 fact"""
