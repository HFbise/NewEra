"""The rules engine: validates and executes player actions, writes the database, and returns facts.

规则引擎：校验并执行玩家动作，写数据库，输出 facts。不调用 AI，全是确定性代码。

One module per topic. Modules import each other explicitly (``from . import combat``) and call across with the
module name (``combat.hurt_npc(...)``); names starting with an underscore are private to their module. Several
topics depend on each other (combat needs stealth, stealth needs the environment, bosses call back into combat),
which module-level imports handle fine. This file re-exports what the server, the AI layer and the tests use.

每个主题一个模块，模块之间显式 import、用模块名调用；下划线开头的是模块私有的。这里导出 server、ai 和测试用到的名字。
"""

from . import (
    core, helpers, loading, equipment, afflictions, environment, stealth, combat, ranged_combat, bosses, rounds,
    duels, parties, everyday, dungeon_events, smith, npc_trade, npc_social, item_info, dispatch,
)
from rules import HEAL_PCT, HEAL_PCT_ALCOHOL, base_price, skill_progress
from .core import ActionError, DUEL_RULES, ONLINE_WINDOW, START_DISTANCE, START_ROOM
from .helpers import find_exit
from .loading import (
    cursor, delete_player, drop_sleepers, load_items, load_npcs, load_player, load_room, load_view,
    set_revive_hook, sleep, touch,
)
from .equipment import LORE_MARK, gear_totals, heal_scale
from .afflictions import effects_text, inflict
from .environment import glare, heat, light_info
from .stealth import distance_word
from .combat import npc_counter, pick_target
from .bosses import boss_turn
from .rounds import (
    ROUND_ACTIONS, ROUND_INSTANT, claim_round, end_round, in_round, queue_round, round_due, round_enemies,
    round_info, unstick_rounds,
)
from .everyday import do_camp, do_move, give_player_new
from .smith import do_upgrade, upgradable
from .npc_trade import (
    GIFT_FACT, RARE_ASK_RE, RELUCTANT_FACT, RETURNED_FACT, SELL_MAX_COUNT, buy_quotes, can_eject, can_gift,
    creatable_kinds, create_limits, get_offers, give_tip, giveable_items, known_goods, made_allowed, made_spec,
    npc_buy_made, npc_eject, npc_gift, npc_give, npc_hand, npc_menu, npc_sell, quote_made, rare_stock, sellable,
    set_offer, shop_directory,
)
from .npc_social import (
    NPC_LOG_LIMIT, REFILL_FACT, add_npc_log, adjust_affinity, affinity_word, clip_npc_said, get_affinity,
    get_npc_memory, last_bought, memory_material, npc_bonds, perks, quest_turn, return_gift, set_npc_memory,
)
from .item_info import MONSTER_LORE, effect_text, item_detail, monster_notes
from .dispatch import execute, execute_all

__all__ = [
    "core",
    "helpers",
    "loading",
    "equipment",
    "afflictions",
    "environment",
    "stealth",
    "combat",
    "ranged_combat",
    "bosses",
    "rounds",
    "duels",
    "parties",
    "everyday",
    "dungeon_events",
    "smith",
    "npc_trade",
    "npc_social",
    "item_info",
    "dispatch",
    "HEAL_PCT",
    "HEAL_PCT_ALCOHOL",
    "base_price",
    "skill_progress",
    "ActionError",
    "DUEL_RULES",
    "ONLINE_WINDOW",
    "START_DISTANCE",
    "START_ROOM",
    "find_exit",
    "cursor",
    "delete_player",
    "drop_sleepers",
    "load_items",
    "load_npcs",
    "load_player",
    "load_room",
    "load_view",
    "set_revive_hook",
    "sleep",
    "touch",
    "LORE_MARK",
    "gear_totals",
    "heal_scale",
    "effects_text",
    "inflict",
    "glare",
    "heat",
    "light_info",
    "distance_word",
    "npc_counter",
    "pick_target",
    "boss_turn",
    "ROUND_ACTIONS",
    "ROUND_INSTANT",
    "claim_round",
    "end_round",
    "in_round",
    "queue_round",
    "round_due",
    "round_enemies",
    "round_info",
    "unstick_rounds",
    "do_camp",
    "do_move",
    "give_player_new",
    "do_upgrade",
    "upgradable",
    "GIFT_FACT",
    "RARE_ASK_RE",
    "RELUCTANT_FACT",
    "RETURNED_FACT",
    "SELL_MAX_COUNT",
    "buy_quotes",
    "can_eject",
    "can_gift",
    "creatable_kinds",
    "create_limits",
    "get_offers",
    "give_tip",
    "giveable_items",
    "known_goods",
    "made_allowed",
    "made_spec",
    "npc_buy_made",
    "npc_eject",
    "npc_gift",
    "npc_give",
    "npc_hand",
    "npc_menu",
    "npc_sell",
    "quote_made",
    "rare_stock",
    "sellable",
    "set_offer",
    "shop_directory",
    "NPC_LOG_LIMIT",
    "REFILL_FACT",
    "add_npc_log",
    "adjust_affinity",
    "affinity_word",
    "clip_npc_said",
    "get_affinity",
    "get_npc_memory",
    "last_bought",
    "memory_material",
    "npc_bonds",
    "perks",
    "quest_turn",
    "return_gift",
    "set_npc_memory",
    "MONSTER_LORE",
    "effect_text",
    "item_detail",
    "monster_notes",
    "execute",
    "execute_all",
]
