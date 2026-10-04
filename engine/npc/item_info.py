"""Item and monster descriptions shown in the UI. / 界面上的物品、怪物说明"""
import dungeon
from rules import (
    AVOID_CAP, ELITE_AFFIXES, ENRAGE_ATK, ENRAGE_BELOW, ENRAGE_FROM, GEM_SLOT_WORDS, GEM_TIER_WORDS, GEM_TOP,
    HEAL_PCT, HEAL_PCT_ALCOHOL, SUMMON_MAX, WEAK_MULT, base_price, gem_effects, stat_text,
)
from schema import ItemInstance, Npc, SKILL_NAMES

from . import npc_trade
from ..base import helpers
from ..fight import afflictions, ranged_combat
from ..world import environment


TYPE_NAMES = {"weapon": "武器", "armor": "防具", "consumable": "吃喝", "key": "钥匙", "misc": "杂物", "gem": "宝石"}


PART_NAMES = {"hand": "手", "head": "头", "chest": "胸", "neck": "项链", "feet": "脚", "ring": "戒指", "legs": "腿",
              "belt": "腰"}


STATE_NAMES = {"poison": "中毒", "bleed": "流血", "blind": "看不清", "corrode": "腐蚀", "restrained": "被缠住",
               "prone": "倒地", "stun": "定住"}


TAG_NAMES = {"undead": "亡灵", "animal": "野兽", "light_averse": "怕光的怪", "ranged": "远程的敌人"}


WHEN_NAMES = {"passive": "", "attack": "每次出手", "hit": "打中时", "kill": "杀死敌人时", "hurt": "被打中时",
              "fight": "每场第一次攻击", "enter": "走进新房间时"}


GIFT_FOR = {"ore": "莉娜", "wine": "麦琪", "book": "诺艾尔"}


def effect_line(e: dict) -> str:
    v, k = e.get("value", 0), e.get("kind", "")
    what = {
        "splash": f"其他敌人各挨 {v} 点", "chain": f"电到另一个敌人 {v} 点", "bonus": f"伤害 +{v}",
        "pierce": f"无视 {v} 点防御", "guard": f"受到的伤害 -{v}", "heal": f"回 {v} 点血",
        "leech": f"吸回造成伤害的 {round(v * 100)}%", "status": f"让对方{STATE_NAMES.get(k, k)}",
        "status_all": f"让所有敌人{STATE_NAMES.get(k, k)}", "reflect": f"打你的敌人挨 {v} 点", "dodge": "完全躲开这一下",
        "resist": f"免疫{STATE_NAMES.get(k, k)}" if v == 0 else f"{STATE_NAMES.get(k, k)}的几率 ×{v}",
        "max_hp": f"血量上限 {'+' if v >= 0 else ''}{v}", "skill": f"{SKILL_NAMES.get(k, k)} +{v}", "gold": f"打怪掉的金币 +{round(v * 100)}%",
        "flee": "逃跑更容易" if v < 0 else "逃跑更难", "wade": "积水里行动不受影响", "darkvision": "暗处攻击不打折",
        "self_damage": f"自己掉 {v} 点血", "cheat_death": "本该倒下时留 10 点血",
        "aura": f"同房间的队友和自己{'攻击' if k == 'attack' else '防御'} +{v}",
        "defense": f"防御 +{stat_text(v)}", "light": f"光亮 +{v}（跟别的随身光源只取最亮的）",
        "crit": f"会心一击：伤害 ×{e.get('mult', 2)}（几件只取几率最高的一件）",
        "element": f"这一下算作{ {'fire': '火'}.get(k, k) }（打在怕{ {'fire': '火'}.get(k, k) }的弱点上伤害 ×{WEAK_MULT:g}）",
    }.get(e.get("do"), e.get("do", ""))
    if e.get("do") == "bonus" and not float(v).is_integer():
        what += f"（小数部分按几率多打 1 点）"
    cond = e.get("if") or {}
    pre = []                                # 条件在前，"什么时候"贴着效果：有队友倒下时，打中时伤害 +30
    if vs := e.get("vs"):
        pre.append("对" + "、".join(TAG_NAMES.get(t, t) for t in vs))
    if "hp_below" in cond:
        pre.append(f"自己血量低于 {round(cond['hp_below'] * 100)}% 时")
    if "target_hp_below" in cond:
        pre.append(f"对方血量低于 {round(cond['target_hp_below'] * 100)}% 时")
    if cond.get("ally_down"):
        pre.append("有队友倒下时")
    tail = (f"（{round(e['chance'] * 100)}% 几率）" if e.get("chance", 1) < 1 else "") \
        + ("（每层一次）" if e.get("once") == "per_floor" else "")
    when = WHEN_NAMES.get(e.get("when"), "")
    return "，".join(p for p in pre + [when + ("：" if when and not pre else "") + what] if p) + tail


# 诺艾尔（props.lore）平时就能口头讲地牢怪物的打法：客人问起怪物怎么打，她照这个说；60 好感的图鉴解锁的是侧栏里的数字
MONSTER_LORE = ("远程的怪（投石手、弓手、弩手、猎手、喷毒蛙、墨咒书记）不会靠近，被贴身会往后跳，最多跳两次就撞墙了；"
                "冲上去一句话里靠近加砍，砍中了它就跳不开；有掩体的地方它射不准，烟雾弹也能挡。"
                "会治疗的招魂修女要先杀，或者耗光她的念珠（最多治三次）。狗头人盾卫护着身后的同伴，同伴死光它就跑。"
                "狼、恶犬、石像鬼、头目鼻子灵，偷袭不了；野兽可以试着安抚，扔根肉骨头能引开。狼和恶犬成群，先打领头的那只，它一倒剩下的就跑。亡灵怕圣水和银器，怕光的怪在亮处软弱，"
                "带火把能压它们，但远程的怪会先射拿火把的人。拿弓弩的最好有人在前面挡着：一个人下去被怪贴上身，"
                "弓只剩一半的准头。头目都有自己的招数、不吃的东西和弱点，打之前先看清楚")


def monster_notes(npc: Npc) -> str:
    """诺艾尔的怪物图鉴：怪的习性，玩家点怪看"""
    p = npc.template.props
    notes = [f"{npc.name}（攻 {npc.template.attack} · 防 {npc.template.defense}）"]
    notes += [text for key, text in (("keen", "嗅觉、警觉极好：一进门就会发现你，偷袭不了"),
                                     ("animal", "野兽：可以试着安抚它避战（驯兽）"),
                                     ("undead", "亡灵：怕圣水"),
                                     ("light_averse", "怕光：光亮 70 以上攻击变弱，也会避开拿火把的人"),
                                     ("ranged", "远程：不会主动靠近，离远了射人；贴身了会往后跳，冲上去一口气砍中它就跳不开；掩体能挡一些"),
                                     ("healer", "会给受伤的同伴回血，一场最多三次：先打它，或者耗光它"),
                                     ("guard_allies", "举着挡板护着同伴：近战砍它身后的同伴，一半会被它挡下；同伴都倒下它就跑"))
              if p.get(key)]
    if hit := p.get("on_hit"):
        notes.append(f"打中人时有几率让人{afflictions.EFFECT_NAMES.get(hit['kind'], STATE_NAMES.get(hit['kind'], hit['kind']))}")
    if (a := p.get("attacks", 1)) > 1:
        notes.append(f"一轮出手 {a} 次")
    if affix := ELITE_AFFIXES.get(p.get("affix")):
        notes.append(f"{affix['name']}：{affix['note']}")
    for sk in p.get("skills") or []:
        notes.append(_skill_note(sk))
    depth = p.get("dungeon", {}).get("depth", 0)
    if p.get("dungeon", {}).get("rank") == "boss" and depth >= ENRAGE_FROM:
        notes.append(f"血量低于 {round(ENRAGE_BELOW * 100)}% 会狂暴，攻击 +{ENRAGE_ATK}")
    if immune := p.get("immune"):
        notes.append("不吃：" + "、".join(afflictions.EFFECT_NAMES.get(k, STATE_NAMES.get(k, k)) for k in immune))
    if "fire" in (p.get("resist_element") or []):
        notes.append("不怕火：火把、火油箭、余烬石打它都不算弱点")
    if st := p.get("stances"):
        notes.append(f"每出手 {st.get('every', 3)} 次在冷却和熔化之间换一次：冷却时又硬又手软，熔化时变软但下手更重；"
                     "熔化的时候泼一瓶泉水，它会淬火裂开，下一次出手之前防御归零、挨的伤害 ×1.5")
    if weak := p.get("weak"):
        notes.append(f"弱点：{ranged_combat.WEAK_WORDS.get(weak, weak)}（{WEAK_HOW.get(weak, '')}伤害 ×{WEAK_MULT:g}）")
    if p.get("pack"):
        notes.append("成群的：一个房间最多两只，领头的那只一倒，剩下的夹着尾巴跑；扔根肉骨头（麦琪后厨卖）能引开，安抚容易些")
    return "\n".join(notes + ["（诺艾尔的怪物图鉴）"])


WEAK_HOW = {"fire": "火把、火油箭、带火的剑打它", "pierce": "破甲的武器打它", "light": "光亮 70 以上打它",
            "holy": "圣水泼它", "poison": "它中毒时", "water": "泼泉水（铸像要等它熔化）"}


def _skill_note(s: dict) -> str:
    """头目的一招，图鉴里怎么写"""
    waves = s.get("waves")
    when = ("血量低于 " + " 和 ".join(f"{round(w * 100)}%" for w in waves) + " 时各一次，" if waves else
            {"hp_below": f"血量低于 {round(s.get('value', 0) * 100)}% 时", "every": f"每出手 {s.get('value')} 次",
             "fight_start": "一开打就"}.get(s.get("when"), ""))
    helper = dungeon.data()["monsters"].get(s.get("kind"), {}).get("name", s.get("kind"))
    count = (f"每次一只，第 5 层只叫第一次" if waves else f"第 5 层一只、第 10 层起 {s.get('count', 1)} 只")
    what = {"summon": f"叫来帮手（{helper}，{count}，最多同时 {SUMMON_MAX} 只，"
                      f"不掉东西，主子一死就跑）",
            "status_all": f"让所有人{STATE_NAMES.get(s.get('kind'), s.get('kind'))}",
            "effect_all": f"让所有人{afflictions.EFFECT_NAMES.get(s.get('kind'), s.get('kind'))}",
            "telegraph": "先预告一招大的，下一次出手放出来（预告那一轮狠狠砍它一下能打断）",
            "mark": f"判罪：盯住一个人，打他 +{s.get('bonus', 2)}，打中一次就了结",
            "self_heal": f"回 {round(s.get('heal', 0.2) * 100)}% 的血",
            "silence": f"禁声：之后出手 {s.get('turns', 2)} 次之内念不了书和卷轴"}.get(s.get("do"), s.get("do"))
    return f"招数：{when}{what}"


def _gem_lines(item: ItemInstance) -> list[str]:
    """宝石的详情：能镶在哪、这个品质镶上去是什么效果"""
    p, tier = item.template.props, item.props.get("tier", 1)
    out = [f"{GEM_TIER_WORDS[tier]}品质，能镶在{GEM_SLOT_WORDS.get(p.get('gem_slot'), '')}上（找莉娜，免费；取出要收钱）"
           + ("；没镶上去的可以找诺艾尔刷品质" if tier < GEM_TOP else "")]
    cats = ["weapon", "armor", "trinket"] if p.get("gem_slot") == "any" else [p.get("gem_slot")]
    for c in cats:
        out += [(f"镶在{GEM_SLOT_WORDS[c]}上：" if p.get("adapts") else "") + effect_line(e) for e in gem_effects(p, tier, c)]
    if p.get("numeric"):
        caps = dungeon.gem_rules()["body_caps"]
        out.append(f"纯数值的宝石：全身合计最多防御 +{caps['defense']}、血量上限 +{caps['max_hp']}")
    return out


def item_detail(item: ItemInstance, curse_sense: bool = False) -> str:
    """给人看的物品详情：类型、数值、特殊效果、参考价、描述。诅咒平时看不出来，
    有诺艾尔的诅咒辨识笔记（回礼 curse_sense）才看得出；戴上了的自己知道"""
    t = item.template
    head = item.name + f"（{environment.USE_KINDS[environment.use_kind(item)] if t.type == 'consumable' else TYPE_NAMES.get(t.type, t.type)}"
    if t.slot:
        head += f" · {'双手' if helpers.prop(item, 'two_handed') else PART_NAMES.get(t.slot, t.slot)}"
    lines = [head + "）"] + (["酒馆武器桶里别人留下的：店里不收，用不上了可以放回桶里"] if item.props.get("donated") else [])
    stats = []
    if item.damage:
        stats.append(f"伤害 {stat_text(item.damage)}")
    if item.defense:
        stats.append(f"防御 {stat_text(item.defense)}")
    if item.heal:
        stats.append(f"回血 {item.heal * (HEAL_PCT_ALCOHOL if environment.is_alcohol(item) else HEAL_PCT)}%（按血量上限）")
    if item.harm:
        stats.append(f"有毒，掉 {item.harm} 点血" + ("（认得出来就没事）" if helpers.prop(item, "harm_check") else ""))
    if light := helpers.prop(item, "light"):
        stats.append(f"光亮 +{light}")
    if stats:
        lines.append("，".join(stats))
    extra = {
        "lights": "拿到手上就点燃（光亮 +35），在地牢里撑两层", "cursed": "诅咒：戴上就卸不下来",
        "cure": f"能治{STATE_NAMES.get(helpers.prop(item, 'cure'), '')}", "refuel": "倒在快灭的火把上让它重新烧旺",
        "whet": f"这一层普通攻击伤害 +{helpers.prop(item, 'whet')}", "room_light": f"这个房间光亮 +{helpers.prop(item, 'room_light')}，走开就散",
        "holy": "泼向亡灵或怕光的怪能重创它", "throw": "能掷出去", "stun": "念出来房间里所有敌人定住",
        "recall": "在地牢里用，传回村子", "camp": "扎营时多回 30% 血", "alcohol": "酒，喝多了会醉",
        "sober": "喝了马上酒醒",
        "ranged": "远程：贴身难射、离远了准，射完要装填", "steady": "远近一样准",
        "ammo": "特殊弹药：装填时装上，下一发带上它的效果",
        "smoke": "摔碎了冒一大团烟：两轮里远程命中减半，敌我都算",
        "recover": "扔完了，打完这一架会捡回来",
        "bait": "安抚野兽时先扔一根过去，驯兽难度 -1（成不成都用掉一根）",
        "writable": "能写几句话（说「在纸条上写……」），写了就改不了",
    }
    if t.type == "consumable" and environment.use_kind(item) in environment.MEDICINE_KINDS:
        lines.append("能给别人用：用药的人医药越高回得越多，给人用药能练医药")
    lines += [text for key, text in extra.items() if helpers.prop(item, key)
              and (key != "cursed" or curse_sense or item.equipped_slot)]
    if block := helpers.prop(item, "block"):
        lines.append(f"格挡：{round(block * 100)}% 几率完全挡下一击（跟闪避合计最多 {round(AVOID_CAP * 100)}%，挡不住中毒流血）")
    if mag := helpers.prop(item, "magazine"):
        lines.append(f"一匣 {mag} 发，还剩 {item.props.get('shots', mag)} 发")
    if (steps := helpers.prop(item, "reload_steps")) and steps > 1:
        lines.append(f"装填要 {steps} 次")
    if item.props.get("loaded_ammo"):
        lines.append("装着一发特殊弹药")
    if uses := helpers.prop(item, "uses"):
        lines.append(f"能用 {item.props.get('uses_left', uses)} 次")
    if gift := helpers.prop(item, "gift"):
        lines.append(f"{GIFT_FOR.get(gift, '')}喜欢的小礼物（每天收一件）" if helpers.prop(item, "gift_value")
                     else f"{GIFT_FOR.get(gift, '')}最想要的礼物")
    if t.type != "gem":                     # 宝石的 effects 是按品质的数组，下面按这颗的品质单独列
        lines += [effect_line(e) for e in helpers.prop(item, "effects") or []]
    if t.type == "gem":
        lines += _gem_lines(item)
    if (n := item.props.get("sockets")):
        gems = item.props.get("gems") or []
        lines.append(f"宝石孔 {len(gems)}/{n}" + ("：" + "、".join(g["name"] for g in gems) if gems else "（找莉娜镶宝石）"))
        lines += ["  " + effect_line(e) for e in item.props.get("gem_fx") or []]
    if price := base_price(npc_trade.item_stats(item)):
        lines.append(f"参考价 {price} 金币")
    lines.append(item.description)
    return "\n".join(lines)


def effect_text(stats: dict) -> str:
    """给人看的效果说明：伤害 4 / 回 3 点血 / 有毒，掉 3 点血 / 能把人药倒"""
    parts = [f"伤害 {stats['damage']}" if stats.get("damage") else "",
             f"防御 {stats['defense']}" if stats.get("defense") else "",
             f"回 {stats['heal'] * (HEAL_PCT_ALCOHOL if stats.get('alcohol') else HEAL_PCT)}% 体力" if stats.get("heal") else "",
             f"有毒，掉 {stats['harm']} 点血" if stats.get("harm") else "",
             "能把人药倒" if stats.get("knockout") else ""]
    return "，".join(p for p in parts if p) or "没什么效果"
