"""The rules engine: validates and executes player actions, writes the database, and returns facts.

规则引擎：校验并执行玩家动作，写数据库，输出 facts。不调用 AI，全是确定性代码。

The engine used to be one 7,000-line file whose functions call each other freely (combat calls stealth,
stealth calls environment, bosses call combat ...). It is split by topic into the modules below, and all
of them share one namespace, exactly as they did in the single file:

1. modules load in MODULES order; each one starts with the names of the modules before it, so module-level
   code (constants, the action table in dispatch) can use them;
2. once all are loaded, every module gets every name, so functions can call functions defined later;
3. the package exposes the union, so callers keep using ``engine.load_view``, ``engine.execute_all`` ...

以前是一个七千多行的文件，函数之间互相调用。按主题拆成下面这些模块，但仍然共用一个命名空间（跟拆之前一样）：
按顺序加载、每个模块先拿到前面模块的名字；全部加载完再把所有名字补给每个模块；包本身对外暴露全部名字。
"""
import importlib.util
import sys

MODULES = [
    "core",            # imports, tuning constants, ActionError
    "helpers",         # ref resolution, item lookup, dice, skill checks, NPC tallies
    "loading",         # reading state from the database
    "gear",            # equipment effects, defense, equip / unequip
    "effects",         # status effects and control on players
    "environment",     # light, ground, heat, fog, star cycle, noise, features
    "stealth",         # detection, hiding, distance, dodging
    "combat",          # attacks, stunts, damage, enemy turns
    "ranged",          # ranged weapons, ammo, cover, smoke
    "bosses",          # boss skills, phases, gate bosses
    "rounds",          # party combat rounds
    "duel",            # player-versus-player duels
    "party",           # following and parties
    "actions",         # moving, taking, using items, resting
    "dungeon_events",  # event-room services
    "smith",           # upgrades, gems, donations, curses
    "npc_trade",       # talking, trading, NPC giving and making items
    "npc_social",      # affinity, memory, quests, gifts, news
    "item_info",       # item and monster descriptions for the UI
    "dispatch",        # execute / execute_all and the action table
]

_shared: dict = {}
for _name in MODULES:
    _spec = importlib.util.find_spec(f"{__name__}.{_name}")
    _module = importlib.util.module_from_spec(_spec)
    _module.__dict__.update(_shared)                 # names from the modules loaded so far
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)
    _shared.update({k: v for k, v in vars(_module).items() if not k.startswith("__")})

for _name in MODULES:                                # back-fill: functions may call later modules at run time
    _module = sys.modules[f"{__name__}.{_name}"]
    for _k, _v in _shared.items():
        _module.__dict__.setdefault(_k, _v)

globals().update(_shared)
