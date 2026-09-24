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


UPGRADE_ASK_RE = re.compile(r"多少|几个?金币|什么价|价格|价钱|贵不贵|怎么收|要花")
ORE_RE = r"(?:奥利哈刚(?:矿石|矿)?|矿石)"


def named_weapon(weapons: list, text: str):
    """按原话挑武器：名字（去掉 +N、不管空格）整个在话里的优先，+N 也对上的更优先；
    否则挑共有的字最多的（至少两个字），都对不上就是 None"""
    said = re.sub(r"\s+", "", text)
    base = lambda i: re.sub(r"\s*\+\d+$", "", i.name).replace(" ", "")
    if whole := [i for i in weapons if base(i) in said]:
        return max(whole, key=lambda i: (i.name.replace(" ", "") in said, len(base(i))))
    score = lambda i: len((set(base(i)) - set("的")) & set(said))
    best = max(weapons, key=score, default=None)
    return best if best is not None and score(best) >= 2 else None


def upgrade_request(view: RoomView, t: str, smith: str) -> Optional[dict]:
    """找铁匠升级的话 → upgrade 的 item / ore / quote（target 调用的人填）；不是升级的话返回 None"""
    t = re.sub(rf"^(?:对|跟|找|和|向)\s*{re.escape(smith)}\s*(?:说|讲|问)?[:：]?\s*", "", t).strip()
    t = re.sub(r"[。！!~～…]+$", "", t)
    if re.fullmatch(rf"(?:那就)?用(?:{ORE_RE}吧?|吧)?", t):
        return {"action": "upgrade", "ore": True}
    if re.fullmatch(rf"不用(?:{ORE_RE}(?:了|吧)?)?", t):
        return {"action": "upgrade", "ore": False}
    m = re.fullmatch(rf"(?:我要|我想|帮我|给我|请|麻烦|想)*\s*(?:(用|拿|不用)\s*{ORE_RE}\s*)?(?:来)?(?:升级|强化|锻造|改良|打磨)\s*(.*?)"
                     rf"\s*(?:(?:，|,)?\s*((?:不)?用{ORE_RE}))?\s*(?:吧|一下)?", t)
    if not m:
        return None
    rest = m[2]
    ask = UPGRADE_ASK_RE.search(rest)
    rest = re.sub(r"(?:要|得|需要)?(?:多少|几个?金币|什么价|价格|价钱|贵不贵|怎么收|要花).*$", "", rest).strip()
    rest = re.sub(r"^(?:我的|一下|下)", "", rest).strip()
    ore_word = m[1] or m[3] or ""
    out = {"action": "upgrade", "quote": bool(ask)}
    if ore_word:
        out["ore"] = not ore_word.startswith("不")
    if rest and rest not in ("武器", "家伙", "兵器", "装备", "一下武器", "防具", "护甲"):
        weapons = [i for i in view.inventory if i.template.type == "weapon" or i.template.type == "armor" and i.defense > 0]
        pick = named_weapon(weapons, rest)
        if pick is None:
            raise _Unsure(rest)
        out["item"] = {uid: ref for ref, uid in view.refs.items()}[pick.id]
    return out


def _is_refill(item, npc) -> bool:
    """这件东西空了、是这个 NPC 能续的（麦琪的酒壶、迷药）"""
    return item.template.props.get("refill_by") == npc.template.id


def _player(view: RoomView, text: str) -> Optional[str]:
    """同房间其他玩家的名字匹配，先精确后模糊"""
    text = text.strip()
    for match in (lambda n: n == text, lambda n: text in n):
        for p in view.others:
            if match(p.name):
                return p.name
    return None


def _player_or_unsure(view: RoomView, text: str) -> str:
    """必须是玩家名字的地方"""
    if name := _player(view, text):
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


NUM = r"\d+|[零一二两三四五六七八九十百]+"
CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_number(s: str) -> int:
    """"5""两""十二""三十五""一百二十" → 整数，认不出就是 0"""
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            cur = CN_DIGITS[ch]
        elif ch in "十百":
            total += (cur or 1) * (10 if ch == "十" else 100)
            cur = 0
        else:
            return 0
    return total + cur


REST_TALK_RE = re.compile(r"开.{0,2}房|住店|住一晚|投宿|睡一觉|过夜")


def _parse_one(view: RoomView, t: str) -> dict:
    # 失去战斗能力时说什么都算挣扎着醒来；有 AI 时服务器会让 AI 按怎么做来判难度
    st = view.player.status
    if st and st.kind == "incapacitated":
        return {"action": "struggle", "description": t}
    if re.fullmatch(r"扎营|露营|安营|搭帐篷|扎营休息|生火休息|camp", t, re.I):
        return {"action": "camp"}
    if re.fullmatch(r"住店|投宿|开房|开间房|要间房|住一晚|睡一觉|rest", t, re.I):
        return {"action": "rest"}
    if re.fullmatch(r"站起来|爬起来|起身|起来|站起|爬起|stand( up)?", t, re.I):
        return {"action": "stand"}
    if re.fullmatch(r"挣脱|挣扎|醒来|醒过来|struggle", t, re.I):
        return {"action": "struggle", "description": t}

    # 倒下了选择被抬回酒馆（或者说了去哪）："复活""回酒馆复活""在铁匠铺复活"
    # 传送石："传送到第 6 层"（地窖里；对着石碑说也算："对石碑说 传送到第 6 层""跟石碑 我要去第 6 层"）、
    # "回城""使用传送石""摸传送石"（地牢的传送石边上，没倒下的时候）
    if m := re.fullmatch(rf"(?:(?:对|跟|向)\s*(?:黑色)?石碑\s*(?:说|讲)?[:：]?\s*)?(?:我要|我想|带我)?\s*"
                         rf"(?:传送|传|去)\s*(?:到|去)?\s*第?\s*({NUM})\s*层", t):
        return {"action": "teleport", "floor": cn_number(m[1])}
    if view.player.hp > 0 and re.fullmatch(r"回城|传送|传送回去|回地面|(?:使用|摸|摸摸|触碰|碰|按)(?:一下)?传送石", t):
        return {"action": "teleport"}
    if m := re.fullmatch(r"(?:(?:在|回|去|到)\s*(.+?)\s*)?(?:复活|重生|回城)|respawn", t, re.I):
        return {"action": "respawn", "target": m[1]}

    # 念能定身的书（古书残卷 props.stun，房间里所有敌人一起定住）："对墨影翻开古书残卷念上面的字""念古书"
    by_id = {uid: ref for ref, uid in view.refs.items()}
    book = next((i for i in view.inventory if i.template.props.get("stun") and (i.name in t or i.name[:2] in t)), None)
    if book and re.search(r"念|读|翻开|使用|用", t) and any(n.template.hostile for n in view.npcs):
        return {"action": "use", "item": by_id[book.id]}
    # 说了能用的道具的名字又说了怎么用（"把灯油倒在火把上""泼圣水""展开光明卷轴"）就是 use；圣水泼向句子里提到的敌人
    # 名字说全了、或者说了后半截（"地图残片"认得出"地牢地图残片"）都算
    tool = next((i for i in view.inventory if any(k in i.template.props for k in ("refuel", "whet", "room_light", "holy", "reveal_floor"))
                 and (i.name in t or len(i.name) > 4 and i.name[-4:] in t)), None)
    if tool and re.search(r"用|倒|泼|洒|磨|展开|打开|念|撒|读|看", t):
        foe = next((n for n in view.npcs if n.template.hostile and (n.name in t or n.name[-2:] in t)), None)
        return {"action": "use", "item": by_id[tool.id], "target": by_id[foe.id] if foe else None}

    # 找铁匠升级武器："升级短剑""强化生锈的短剑""对莉娜 升级铁斧""用奥利哈刚升级短剑""升级短剑要多少钱"；
    # 只说"升级""升级武器"让铁匠问哪把；铁匠问要不要用矿石时回"用""不用"。铁匠就是这里会升级的那个 NPC
    if smith := next((n for n in view.npcs if n.template.props.get("upgrades")), None):
        if (up := upgrade_request(view, t, smith.name)) is not None:
            return up | {"target": {uid: ref for ref, uid in view.refs.items()}[smith.id]}

    # 解咒（找诺艾尔）："解咒""解除诅咒""对诺艾尔说 帮我解咒""找诺艾尔解开饮血双刃剑的诅咒"
    if seller := next((n for n in view.npcs if n.template.props.get("uncurse")), None):
        if m := re.fullmatch(rf"(?:(?:对|跟|找|向)\s*{re.escape(seller.name)}\s*(?:说|讲)?[:：]?\s*)?(?:帮我|请|我要|我想)?\s*"
                             r"(?:把\s*(.+?)\s*(?:的|上的)?\s*)?(?:解咒|解除诅咒|驱咒|去掉诅咒|解开诅咒|除咒)\s*(.*?)\s*(?:的诅咒)?", t):
            said = (m[1] or m[2] or "").strip()
            out = {"action": "uncurse", "target": {uid: ref for ref, uid in view.refs.items()}[seller.id]}
            if said:
                out["item"] = _find(view, said, "inv")
            return out

    # 莉娜的回礼本事：传承锻造"把铁斧的强化转到精钢短剑上"、刷新词条"刷新余烬长剑的词条""重铸余烬长剑"
    if smith := next((n for n in view.npcs if n.template.props.get("upgrades")), None):
        ref = {uid: ref for ref, uid in view.refs.items()}[smith.id]
        u = re.sub(rf"^(?:对|跟|找|向)\s*{re.escape(smith.name)}\s*(?:说|讲)?[:：]?\s*(?:帮我|请|我要|我想)?\s*", "", t)
        if m := re.fullmatch(r"(?:把)?\s*(.+?)\s*(?:的|上的)\s*(?:强化|等级|锻打|升级)?\s*(?:转到|传到|传给|转给|移到|挪到)\s*(.+?)\s*(?:上|身上)?", u):
            return {"action": "transfer", "item": _find(view, m[1], "inv"), "to": _find(view, m[2], "inv"), "target": ref}
        if m := (re.fullmatch(r"(?:刷新|重洗|洗|重铸)\s*(.+?)\s*(?:的)?\s*(?:词条|特效)?", u)
                 or re.fullmatch(r"(?:给|把)\s*(.+?)\s*(?:刷新|重洗|重铸)\s*(?:一下)?\s*(?:词条|特效)?", u)):
            return {"action": "reroll", "item": _find(view, m[1], "inv"), "target": ref}
    # 专属武器起名："给莉娜打的剑起名叫破晓""把它改名为破晓"
    if m := re.fullmatch(r"(?:给|把)\s*(.+?)\s*(?:起名|取名|改名|命名)\s*(?:叫|为|成|作)?\s*(.+)", t):
        return {"action": "rename", "item": _find(view, m[1], "inv"), "name": m[2]}

    # 找麦琪续杯："续杯""对麦琪说 续杯""帮我灌满""把酒壶灌满"
    if keeper := next((n for n in view.npcs if any(_is_refill(i, n) for i in view.inventory)), None):
        u = re.sub(rf"^(?:对|跟|找|向)\s*{re.escape(keeper.name)}\s*(?:说|讲)?[:：]?\s*(?:帮我|请|给我)?\s*", "", t)
        if re.fullmatch(r"(?:续杯|续上|续一下|再续一杯|灌满|加满|满上|续满)(?:吧|一下)?|(?:把|给)?\s*.{0,8}?\s*(?:灌满|续上|满上|加满)", u):
            return {"action": "refill", "target": {uid: ref for ref, uid in view.refs.items()}[keeper.id]}
    # 泼迷药："把特制迷药泼向哥布林""对哥布林用特制迷药"
    if (drug := next((i for i in view.inventory if "drug" in i.template.props and (i.name in t or "迷药" in t)), None)) \
            and re.search(r"泼|洒|用|喂|倒", t):
        foe = next((n for n in view.npcs if n.template.hostile and (n.name in t or n.name[-2:] in t)), None)
        return {"action": "use", "item": {uid: ref for ref, uid in view.refs.items()}[drug.id],
                "target": {uid: ref for ref, uid in view.refs.items()}[foe.id] if foe else None}

    # 驯兽："安抚狼""驯服巨鼠""安抚一下那只狼"（只对野兽有用，是不是野兽引擎查）
    if m := re.fullmatch(r"(?:试着|试试|慢慢)?(?:安抚|驯服|驯养|平息|哄走|哄哄|安慰)\s*(?:一下)?\s*(?:那只|这只|那群|这群)?\s*(.+)", t):
        return {"action": "tame", "target": _find(view, m[1], "npc")}

    # 地牢里听见怪声："循着声音去找""去看看是什么声音"
    if re.search(r"循.{0,2}声|顺着声音|找.{0,4}声音|声音.{0,6}(找|看看)", t):
        return {"action": "search", "description": "循着声音找过去"}
    if re.fullmatch(r"(?:搜索|搜寻|搜查|找找)+|四处找找|search", t, re.I):
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

    # 点火把就是把火把拿在手上
    if re.fullmatch(r"(?:点|点上|点燃|举起|举着|拿起|举)火把", t) and (torch := next(
            (i for i in view.inventory if i.template.props.get("lights")), None)):
        return {"action": "equip", "item": {uid: ref for ref, uid in view.refs.items()}[torch.id]}
    if m := re.fullmatch(r"(?:equip|wear|wield|装备|穿上|戴上)\s*(.+)", t, re.I):
        return {"action": "equip", "item": _find(view, m[1], "inv")}
    if m := re.fullmatch(r"(?:unequip|卸下|脱下|摘下|脱掉|摘掉)\s*(.+)", t, re.I):
        return {"action": "unequip", "item": _find(view, m[1], "inv")}

    # 开锁：自动找背包里的钥匙，必须放在"打"前面，不然"打开"会被当成攻击
    if m := re.fullmatch(r"(?:unlock|打开|开锁|开)\s*(.+?)(?:的?门)?", t, re.I):
        keys = [i for i in view.inventory if i.template.type == "key"]
        if (d := _dir(m[1])) and keys:
            return {"action": "use", "item": _find(view, keys[0].name, "inv"), "target": d}

    # 给别人用药："给寒风用绷带""给寒风灌血药""用绷带给寒风包扎""把药草给寒风敷上"（"把药草给寒风"是递给他，在后面认）
    if m := re.fullmatch(r"(?:给|帮)\s*(.+?)\s*(?:用|使用|敷|涂|抹|包扎|灌|喝)\s*(?:上|下|了)?\s*(.+)", t):
        if _player(view, m[1]):
            return {"action": "use", "item": _find(view, m[2], "inv"), "target": _player(view, m[1])}
    if m := (re.fullmatch(r"用\s*(.+?)\s*(?:给|帮)\s*(.+?)\s*(?:包扎|敷上|敷药|治疗|疗伤|止血|解毒|上药|治伤)(?:一下)?", t)
             or re.fullmatch(r"把\s*(.+?)\s*(?:给|帮)\s*(.+?)\s*(?:敷上|涂上|抹上|用上|灌下|灌下去|包扎上)", t)):
        if _player(view, m[2]):
            return {"action": "use", "item": _find(view, m[1], "inv"), "target": _player(view, m[2])}
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
    if re.fullmatch(r"(?:离开|退出)队伍|退队|离队|解散队伍|leave party", t, re.I):
        return {"action": "leave_party"}
    # 组队就是跟随："和寒风组队""加入寒风的队伍"就是跟着寒风
    if m := (re.fullmatch(r"(?:和|跟|与)\s*(.+?)\s*组队", t) or re.fullmatch(r"加入\s*(.+?)\s*的?(?:队伍|队)", t)):
        return {"action": "follow", "target": _player_or_unsure(view, m[1])}

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
    # 远程："装填""给弩上弦"；"射哥布林""用弩射哥布林""用短剑砍哥布林"
    if m := re.fullmatch(r"(?:重新)?(?:装填|上弦|装箭|填装)|(?:给|把)\s*(.+?)\s*(?:重新)?(?:装填|上弦|装上箭|上好弦|装好)", t):
        return {"action": "reload", "item": _find(view, m[1], "inv") if m[1] else None}
    if m := re.fullmatch(r"(?:用|拿)\s*(.+?)\s*(?:射|攻击|打|砍|刺|劈)\s*(.+)", t):
        try:
            item = _find(view, m[1], "inv")
        except _Unsure:
            item = None                     # 拿地形、手边的东西砸人是花样（stunt），交给 AI
        if item and any(i.equipped_slot and view.refs.get(item) == i.id for i in view.inventory):
            name = m[2].strip()
            target = name if any(p.name == name for p in view.others) else _find(view, name, "npc")
            return {"action": "attack", "target": target, "item": item}
    if m := re.fullmatch(r"(?:射|射击)\s*(.+)", t):
        name = m[1].strip()
        return {"action": "attack", "target": name if any(p.name == name for p in view.others) else _find(view, name, "npc")}
    if m := re.fullmatch(r"(?:attack|kill|hit|攻击|杀|打)\s*(.+)", t, re.I):
        name = m[1].strip()
        if any(p.name == name for p in view.others):
            return {"action": "attack", "target": name}
        try:
            return {"action": "attack", "target": _find(view, name, "npc")}
        except _Unsure:
            return {"action": "attack", "target": _player_or_unsure(view, name)}

    # 对卖东西的 NPC 点单、买东西（不带"说"）："对麦琪 来杯黑啤""对诺艾尔 给来一个回城水晶""找莉娜 买把铁斧"。
    # 以前交给 AI，"给来一个"被当成把东西给她，报背包里没有。只认卖东西、开店的 NPC，"对哥布林 来一刀"不会进来
    if m := re.fullmatch(r"(?:对|跟|找|向)\s*(.+?)\s+((?:(?:给我?|帮我|我要|我想|请)\s*(?:来|要|买|拿)?|来|要|买)\s*"
                         r"(?:点|些|一?[个杯条把份瓶支根块张件壶碗盒]|\d+|[一两三四五六七八九十]+)?.+)", t):
        npc = next((n for n in view.npcs if not n.template.hostile and (n.name == m[1].strip() or m[1].strip() in n.name)), None)
        if npc and (npc.template.props.get("sells") or npc.template.props.get("inn") or npc.template.props.get("creates")):
            return {"action": "talk", "target": {uid: ref for ref, uid in view.refs.items()}[npc.id], "message": m[2]}

    # 注意别的"对麦琪 某某"（没有"说"）不在这里认：同样的句式也可能是动作（"对哥布林 发动强力砍击"），交给 AI 分
    # 对某人说：对方是玩家就是 say（广播，不走 AI），是 NPC 就是 talk（AI 扮演 NPC 回话）
    if m := (re.fullmatch(r"(?:对|跟|和|向)\s*(.+?)\s*(?:说|讲|问)[:：]?\s*(.+)", t)
             or re.fullmatch(r"(?:say|talk)\s+(?:to\s+)?(\S+)\s+(.+)", t, re.I)):
        if name := _player(view, m[1]):
            return {"action": "say", "target": name, "message": m[2]}
        target = _find(view, m[1], "npc")
        # 跟开店的说要住店（"对麦琪说 住店"）就是住店；只问价钱的还是说话，让她报价
        npc = next((n for n in view.npcs if view.refs.get(target) == n.id), None)
        if (npc and npc.template.props.get("inn") and REST_TALK_RE.search(m[2])
                and not re.search(r"多少|几个?金币|什么价|价格|价钱|贵不贵|怎么收", m[2])):
            return {"action": "rest"}
        return {"action": "talk", "target": target, "message": m[2]}

    # 对大家说
    if m := re.fullmatch(r"(?:说|喊|say)[:：]?\s*(.+)", t, re.I):
        return {"action": "say", "message": m[1]}

    # 喂别人吃（跟"给"不一样：给是东西到他手上，喂是吃下去）："把药草喂给寒风""喂寒风吃药草"
    if m := re.fullmatch(r"把\s*(.+?)\s*喂给\s*(.+)", t):
        return {"action": "use", "item": _find(view, m[1], "inv"), "target": _player_or_unsure(view, m[2])}
    if m := re.fullmatch(r"喂\s*(.+?)\s*(?:吃|喝)\s*(?:了|下|点)?\s*(.+)", t):
        return {"action": "use", "item": _find(view, m[2], "inv"), "target": _player_or_unsure(view, m[1])}

    # 卖东西给 NPC："把药草卖给麦琪""卖矿石给莉娜"
    if m := (re.fullmatch(r"把\s*(.+?)\s*(?:卖给|卖了给|出给)\s*(.+)", t)
             or re.fullmatch(r"卖\s*(.+?)\s*给\s*(.+)", t)):
        return {"action": "sell", "item": _find(view, m[1], "inv"), "target": _find(view, m[2], "npc")}

    # 给钱："给麦琪两金币""把 5 金币给寒风""付麦琪5个金币"
    if m := (re.fullmatch(rf"(?:给|付给?|塞给)\s*(.+?)\s*({NUM})\s*(?:个|枚)?\s*(?:金币|金|块钱|块)", t)
             or re.fullmatch(rf"把\s*({NUM})\s*(?:个|枚)?\s*(?:金币|金)\s*(?:给|付给|塞给|交给)\s*(.+)", t)):
        who, num = (m[1], m[2]) if not re.fullmatch(NUM, m[1]) else (m[2], m[1])
        if (amount := cn_number(num)) and amount > 0:
            name = next((p.name for p in view.others if p.name == who.strip()), None)
            return {"action": "pay", "target": name or _find(view, who, "npc"), "amount": amount}

    # 给东西：对方是玩家就填名字，是 NPC 就填 ref
    if m := (re.fullmatch(r"把\s*(.+?)\s*(?:给|交给|递给)\s*(.+)", t)
             or re.fullmatch(r"give\s+(\S+)\s+(?:to\s+)?(\S+)", t, re.I)):
        who = next((p.name for p in view.others if p.name == m[2].strip()), None)
        return {"action": "give", "item": _find(view, m[1], "inv"), "target": who or _find(view, m[2], "npc")}

    return {"action": "freeform", "description": t}


def parse(view: RoomView, text: str) -> list:
    parts = [p for p in re.split(r"[;；]", text) if p.strip()]
    return [_action(parse_one(view, p)) for p in parts]
