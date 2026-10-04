"""Affinity, NPC memory, quests, gifts, news and nudges. / 好感、NPC 记忆、委托、回礼、更新告示和提醒"""
# 这个包里的模块共用一个命名空间：engine/__init__.py 按 MODULES 的顺序加载，每个模块都能直接用别的模块里的名字
# （跟拆分前在同一个文件里一样）。All modules in this package share one namespace; see engine/__init__.py.


UPGRADE_NUDGE = "_upgrade_nudge"         # players.flags：莉娜提醒过升级了（下划线开头的是内部记号，侧栏不显示）


NUDGE_GOLD, NUDGE_DEPTH = 20, 3


BOW_NUDGE = "_bow_nudge"


def _bow_nudge(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """第一次拿着弓弩回村：麦琪顺口提一句，拿远程的最好找人挡在前面（每人一次）"""
    if not npc.template.props.get("inn") or player.flags.get(BOW_NUDGE) or dungeon.is_dungeon(player.room_id):
        return []
    bow = next((w for w in _weapons(cur, player) if _prop(w, "ranged") or _prop(w, "loads")), None)
    if bow is None:
        return []
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, true) where id = %s", (BOW_NUDGE, player.id))
    player.flags[BOW_NUDGE] = True
    return [f"{npc.name}瞄了一眼{player.name}手上的{bow.name}，晃着酒杯说拿这玩意儿的杂鱼一个人下去，被怪贴上身就只能干瞪眼，"
            f"最好找个皮厚的挡在前面（拿远程武器的，跟近战的人组队最稳）"]


NUDGE_AGAIN = 5                         # 提醒过以后，最深层数每再深这么多、条件还满足，就再提一次（"忘了"的人也能再听到）


def _deepest(cur: Cursor, player: Player) -> int:
    cur.execute("select deepest_floor from players where id = %s", (player.id,))
    return cur.fetchone()["deepest_floor"]


def _nudge_due(cur: Cursor, player: Player, key: str) -> bool:
    """这条提醒现在该不该说：没提过，或者上次提的时候最深层数比现在浅 NUDGE_AGAIN 层以上（flags 里记着上次的层数；
    以前记的 true 当作第 0 层）"""
    last = player.flags.get(key)
    return last is None or last is False or _deepest(cur, player) >= int(last) + NUDGE_AGAIN


def _nudge_mark(cur: Cursor, player: Player, key: str) -> None:
    deepest = _deepest(cur, player)
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::int) where id = %s", (key, deepest, player.id))
    player.flags[key] = deepest


# 更新告示：改了玩法就往 NEWS 前面加一条（版本号、麦琪的八卦、更新说明），玩家上线后第一次进酒馆听到最新那条
# （不然玩家只觉得"被热补丁削弱了"）。看过的版本记在 players.flags._news
NEWS = [
    ("2026-09-27", "诺艾尔的店里多了一盏小油灯，她说是照着书上的图样做的。",
     "诺艾尔卖腰挂油灯了（挂在腰上不占手，光亮 +15）；萤石更亮了，深层掉的炉芯、晶母之心、渡魂者的绿灯、法老面具、星陨剑、星盘"
     "也都会发光，越深的越亮。随身的光只算最亮的一件，火把另外叠加，留着应急。第 16 层往下，一个房间刷三群怪没那么常见了；"
     "在其山岳之上者面前能看到自己攒了几层罪"),
    ("2026-09-26d","有人说第二十五层的楼梯口站着一个不该出现在地牢里的东西，没人从它面前走过去。",
     "每 25 层有一位关卡头目，打不倒就下不去：在其山岳之上者会称量你的心（这场里喝药、闪避、后退、逃跑都会让天平那一端更沉），"
     "英仙座会隐身、会飞（飞着的时候拿绳子、网、钩索箭或者抓他的脚踝把他拽下来）、会用大陵五的光把人石化（闭眼能躲），在他面前藏不住。"
     "楼梯间隔壁的前厅有传送石和休息的地方，倒下了能直接传回来重来，每失败一次它下次少一点血；到过更深的人也能回来挑战。"
     "打倒了有称号和专属的装备"),
    ("2026-09-26c", "往下走的人越来越多，带回来的故事也越来越怪。",
     "第 16 层起新增静默神殿：出声会引来东西，说话、念书、砸东西都会攒声响，沉睡的石像会被吵醒。"
     "第 21 层起新增沙漠迷城（路会变、流沙站着不动会往下陷、裹尸布用火烧得断）、梦境回廊（光一明一暗、距离会乱、"
     "掐自己能不睡过去）、迷雾葬海（怪一进门就在跟前、渡魂者会伸手要船费）；第 26 层起新增星界秘境"
     "（魔星一明一暗，边上的石台被撞倒会掉下去）。深层装备多了不少新本事，晶粉能给诺艾尔当刷宝石的垫子"),
    ("2026-09-26b","挖矿的说第十六层往下有个洞，墙上长满了会发光的石头，看久了眼睛疼。",
     "新区域「水晶洞窟」（第 16 层起）：每间屋子亮度不一样，太亮的地方晶面反光会晃花眼；说「闭眼」能背过身躲一轮，代价是自己也看不见；"
     "打碎大晶簇房间会暗下来；棱镜元素会把箭折回来，共鸣者给同伴套一层能挡一下的光膜；"
     "晶母身上的晶簇活着时她硬得很，先把晶簇敲掉；晶脉里能撬宝石，镜厅里会走出你自己的倒影。"
     "过了第 16 层，所有区域都会随机出现"),
    ("2026-09-26","有人说第十六层往下挖到了一座矮人的熔炉，锤声到现在都没停……",
     "新区域「地底熔炉」（第 16 层起）：整层灼热，每个动作都掉一点血，喝泉水能撑 4 个动作，每层有两间冷却水池；"
     "这里的怪不怕火，破甲和泉水才好使；守着熔炉的是矮人王的铸像，会在冷却和熔化之间来回变，熔化的时候泼它一瓶泉水。"
     "深层装备（孔更多）、熔岩吊桥上的锻造纹拓片送给莉娜，深层装备能升到 +15"),
    ("2026-09-25b", "地牢深处的东西醒了。",
     "第 16 层起的头目一轮出手两次，会预告大招（这一轮狠狠砍它能打断，被盯上的人闪避能躲开一半）、"
     "一招挂好几个效果，血掉到一半会变阵：灯灭了、泥水涨上来、牢门封死、古树扎根回血……"
     "新的负面效果「重伤」：受到的治疗打折，药草、绷带解不了；第 11 层往下，防御堆得越高，挡的比例涨得越慢"),
    ("2026-09-25", "听说地牢里的东西最近变机灵了……",
     "躲起来时贴在身边、早发现你的怪还会摸黑乱挥；被发现过就偷袭不了，警觉的怪面前藏不住；"
     "中毒、流血按那一下打出的伤害算，防御也挡得住了，再中只多挂一回合；"
     "挣脱、爬起来以后这一轮不会马上又被同样的招控住；狗和狼一个房间最多两只，领头的一倒剩下的就跑；"
     "楼梯间的守卫都很警觉，绕不过去；升级失败不再掉级，连着失败越来越容易成；莉娜能把用不上的地牢装备拆成碎铁"),
]


NEWS_SEEN = "_news"


def _news(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """麦琪在酒馆里八卦最近的更新（每个版本每人一次）"""
    seen = player.flags.get(NEWS_SEEN)
    if not npc.template.props.get("inn") or not NEWS or seen == NEWS[0][0]:
        return []
    fresh = []                                  # 没看过的几条（新的在前，看到上次看过的那条为止）
    for entry in NEWS:
        if entry[0] == seen:
            break
        fresh.append(entry)
    player.flags[NEWS_SEEN] = NEWS[0][0]
    cur.execute("update players set flags = flags || jsonb_build_object(%s::text, %s::text) where id = %s",
                (NEWS_SEEN, NEWS[0][0], player.id))
    return [f"{npc.name}一边擦杯子一边压低声音：“{fresh[0][1]}”"] + [f"（更新告示：{notes}）" for _, _, notes in fresh]


ARMOR_NUDGE, GEM_NUDGE, POTION_NUDGE = "_armor_nudge", "_gem_nudge", "_potion_nudge"


ARMOR_NUDGE_GOLD, POTION_NUDGE_DEPTH = 100, 8


def _armor_nudge(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """身上的防具一件都没升过、兜里有 100 金以上：莉娜提防具"""
    if not npc.template.props.get("upgrades") or player.gold < ARMOR_NUDGE_GOLD or dungeon.is_dungeon(player.room_id):
        return []
    worn = [i for i in _worn(cur, player) if i.template.type == "armor" and i.defense > 0]
    if not worn or any(i.props.get("plus") for i in worn) or not _nudge_due(cur, player, ARMOR_NUDGE):
        return []
    _nudge_mark(cur, player, ARMOR_NUDGE)
    return [f"{npc.name}敲了敲{player.name}身上的{worn[0].name}：“光磨刀不补甲，下面的怪可不跟你讲道理。”"
            f"（防具也能升级：说「升级{worn[0].name}」，+1 要 {upgrade_terms(worn[0])[1]} 金币）"]


def _gem_nudge(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """背包里有宝石、身上的装备有空孔：莉娜说孔空着也是空着"""
    if not npc.template.props.get("upgrades") or dungeon.is_dungeon(player.room_id):
        return []
    cur.execute("""select exists (select 1 from item_instances i join item_templates t on t.id = i.template_id
                                  where i.player_id = %(p)s and t.type = 'gem') as gem,
                          (select coalesce(i.props->>'name', t.name) from item_instances i join item_templates t on t.id = i.template_id
                           where i.player_id = %(p)s and coalesce((i.props->>'sockets')::int, 0)
                                 > coalesce(jsonb_array_length(i.props->'gems'), 0) limit 1) as holed""", {"p": player.id})
    r = cur.fetchone()
    if not (r["gem"] and r["holed"]) or not _nudge_due(cur, player, GEM_NUDGE):
        return []
    _nudge_mark(cur, player, GEM_NUDGE)
    return [f"{npc.name}瞄了一眼{player.name}的{r['holed']}：“孔空着也是空着。宝石揣在兜里又不会自己长上去。”"
            f"（镶宝石免费：说「把某某宝石镶到{r['holed']}上」）"]


def _potion_nudge(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """到过第 8 层以下、身上一瓶血药都没有：麦琪提一句"""
    if not npc.template.props.get("inn") or dungeon.is_dungeon(player.room_id):
        return []
    cur.execute("select 1 from item_instances where player_id = %s and template_id = 'blood_potion' limit 1", (player.id,))
    if cur.fetchone() or _deepest(cur, player) < POTION_NUDGE_DEPTH or not _nudge_due(cur, player, POTION_NUDGE):
        return []
    _nudge_mark(cur, player, POTION_NUDGE)
    return [f"{npc.name}上下打量了{player.name}一圈，撇撇嘴：“又空着手往下跑？连瓶血药都不带，等着我去捡你啊？”"
            "（杂货铺的诺艾尔卖血药，倒下的队友也能灌）"]


def _upgrade_nudge(cur: Cursor, player: Player, npc: Npc) -> list[str]:
    """带着钱回村、下过第 3 层、手上的武器还没升过：铁匠见到时主动提一句（之后每深 5 层还没升就再提）。
    不然新人不知道能升级，第 6 层撞墙也不知道为什么"""
    if (not npc.template.props.get("upgrades") or player.gold < NUDGE_GOLD
            or dungeon.is_dungeon(player.room_id)):
        return []
    if _deepest(cur, player) < NUDGE_DEPTH or not _nudge_due(cur, player, UPGRADE_NUDGE):
        return []
    weapon = next((w for w in _weapons(cur, player) if not w.props.get("plus") and not _prop(w, "lights")), None)
    if weapon is None:
        return []
    _nudge_mark(cur, player, UPGRADE_NUDGE)
    return [f"{npc.name}瞥见{player.name}手上那把没回过炉的{weapon.name}，皱起了眉头，像是看不下去"
            f"（她能帮你升级：说「升级{weapon.name}」，+1 要 {upgrade_terms(weapon)[1]} 金币）"]


# 任务定义在 world.yaml 的 quests，进度按玩家记在 player_quests：没记录 = 没接，offered = NPC 提过，
# rewarded = 了结。做没做完看玩家 flags 里有没有 done_flag。跟发布任务的 NPC 说话时推进

def quest_turn(conn: Connection, player_id: UUID, npc_id: UUID) -> tuple[list[ActionResult], list[tuple[str, dict]]]:
    """跟 NPC 说话时处理这个 NPC 发布的任务：做完了就自动发奖励，没接过的记成已提起。
    返回 (奖励的执行结果, 给叙事的任务情况 [(new|active|done|closed, 任务)])"""
    results, context = [], []
    with conn.transaction():
        cur = _cursor(conn)
        player = load_player(cur, player_id, lock=True)
        npcs = load_npcs(cur, "n.id = %s", (npc_id,))
        if not npcs:
            return results, context
        npc = npcs[0]
        cur.execute(
            """select q.*, pq.status from quests q
               left join player_quests pq on pq.quest_id = q.id and pq.player_id = %s
               where q.giver = %s order by q.id""",
            (player_id, npc.template.id),
        )
        for q in cur.fetchall():
            # 两种完成条件：身上有标记（打死哥布林），或者带着要的东西来找发布者（锈剑）
            brought = None
            if q["needs_item"]:
                brought = next(iter(load_items(cur, "i.player_id = %s and i.template_id = %s",
                                               (player_id, q["needs_item"]), lock=True)), None)
            done = bool(q["done_flag"] and player.flags.get(q["done_flag"])) or brought is not None
            if q["status"] == "rewarded":
                context.append(("closed", q))
            elif done:
                # 做完了：奖励直接给（身上已经有就不重复给），不用玩家开口要；要带的东西收走
                facts = []
                if brought:
                    if brought.quantity > 1:
                        cur.execute("update item_instances set quantity = quantity - 1 where id = %s", (brought.id,))
                    else:
                        cur.execute("delete from item_instances where id = %s", (brought.id,))
                    facts.append(f"{npc.name}收走了{player.name}的{brought.name}")
                if q["reward_item"]:
                    cur.execute("select 1 from item_instances where player_id = %s and template_id = %s",
                                (player_id, q["reward_item"]))
                    if not cur.fetchone():
                        cur.execute("insert into item_instances (template_id, player_id) values (%s, %s)",
                                    (q["reward_item"], player_id))
                        cur.execute("select name from item_templates where id = %s", (q["reward_item"],))
                        facts.append(f"{npc.name}把{cur.fetchone()['name']}交给了{player.name}（任务奖励）")
                results.append(ActionResult(action="quest", success=True,
                                            facts=[f"{player.name}完成了{npc.name}的委托：{q['goal']}"] + facts))
                _set_quest(cur, player_id, q["id"], "rewarded")
                context.append(("done", q))
            elif q["status"] == "offered":
                context.append(("active", q))
            elif q["hidden"]:
                continue                        # 隐藏委托：NPC 不提，玩家自己碰上（带着东西来）才触发
            else:
                # 这次对话 NPC 提起委托：写成 fact，叙事一定会写到，玩家界面上也能看到这条
                _set_quest(cur, player_id, q["id"], "offered")
                results.append(ActionResult(action="quest", success=True,
                                            facts=[f"{npc.name}有件事想托{player.name}：{q['goal']}"]))
                context.append(("new", q))
    return results, context


def _set_quest(cur: Cursor, player_id: UUID, quest_id: str, status: str) -> None:
    cur.execute(
        """insert into player_quests (player_id, quest_id, status) values (%s, %s, %s)
           on conflict (player_id, quest_id) do update set status = excluded.status, updated_at = now()""",
        (player_id, quest_id, status),
    )


def get_affinity(conn: Connection, player_id: UUID, npc_id: UUID) -> int:
    with conn.transaction():
        cur = _cursor(conn)
        npcs = load_npcs(cur, "n.id = %s", (npc_id,))
        return _affinity(cur, load_player(cur, player_id), npcs[0]) if npcs else 0


NPC_MEMORY_LIMIT = 300                  # NPC 对每个玩家的长期记忆总结上限（字）


NPC_LOG_RECENT = 10                     # 给 AI 看的最近几条完整来往记录（全部记录都留在 npc_memory_log）


NPC_LOG_LIMIT = 400                     # 每条记录上限（字）


NPC_LOG_RELATED = 5                     # 再从更早的记录里按玩家这句话翻出几条相关的


NPC_SUMMARY_WINDOW = 40                 # 后台整理摘要时看最近几条完整记录


# 翻旧账时不算数的字：常见虚词
MEMORY_STOP = set("我你他她它的了吗呢吧啊呀是在有和就都也还要去来这那个一不没么什怎给把被说对")


def ago(when: datetime) -> str:
    """给 AI 看的相对时间："刚才""20 分钟前""昨天"。具体钟点它用不上（不知道现在几点，而且是 UTC）"""
    minutes = (datetime.now(timezone.utc) - when).total_seconds() / 60
    if minutes < 3:
        return "刚才"
    if minutes < 60:
        return f"{int(minutes)} 分钟前"
    if minutes < 24 * 60:
        return f"{int(minutes // 60)} 小时前"
    days = int(minutes // (24 * 60))
    return "昨天" if days == 1 else f"{days} 天前" if days < 14 else f"{days // 7} 周前"


NPC_REPLY_SHOWN = 12                    # 给叙事看的记录里，NPC 自己以前的回话只留开头这么多字，免得它整句照抄


def clip_npc_said(entry: str, name: str) -> str:
    """房间动态里 NPC 对别人说的话（"莉娜：“……”"）去掉原话：留着开头模型也会照抄给下一个人"""
    return re.sub(rf"{re.escape(name)}：“[^”]*”", f"{name}回了几句（原话略）", entry)


def _clip_replies(entry: str) -> str:
    """NPC 自己说过的话只留个开头（"你回：“哟，杂鱼，这就……”"），玩家说的话完整留着。完整记录在库里不动"""
    return re.sub(r"你回：“([^”]{%d})[^”]+”" % NPC_REPLY_SHOWN, r"你回：“\1……”", entry)


def _bigrams(text: str, skip: set[str]) -> set[str]:
    """中文按两个字一组切（没有分词器，够用来找相关的旧记录）；含虚词的组不要"""
    chars = [c for c in text if "\u4e00" <= c <= "\u9fff" or c.isalnum()]
    return {a + b for a, b in zip(chars, chars[1:]) if a not in MEMORY_STOP and b not in MEMORY_STOP} - skip


def get_npc_memory(conn: Connection, player_id: UUID, npc_id: UUID, query: str = "") -> str:
    """NPC 对这个玩家记得什么：长期记忆总结 + 最近几条完整来往 + 按玩家这句话（query）从更早的记录里翻出的相关几条。
    按 NPC 模板记，NPC 复活、重新 seed 都不丢"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute(
            """select r.memory from player_npc_relations r join npcs n on n.template_id = r.npc_template
               where r.player_id = %s and n.id = %s""",
            (player_id, npc_id),
        )
        row = cur.fetchone()
        cur.execute(
            """select * from (
                 select l.id, l.entry, l.created_at from npc_memory_log l join npcs n on n.template_id = l.npc_template
                 where l.player_id = %s and n.id = %s order by l.id desc limit %s) t order by id""",
            (player_id, npc_id, NPC_LOG_RECENT),
        )
        recent = cur.fetchall()
        log = [f"[{ago(r['created_at'])}] {_clip_replies(r['entry'])}" for r in recent]
        # 翻旧账：更早的记录里跟这句话重合最多的几条（两人的名字每条都有，不算）
        related = []
        if query and recent:
            cur.execute("select p.name as player, t.name as npc from players p, npcs n join npc_templates t on t.id = n.template_id"
                        " where p.id = %s and n.id = %s", (player_id, npc_id))
            names = cur.fetchone()
            skip = (_bigrams(names["player"], set()) | _bigrams(names["npc"], set())) if names else set()
            words = _bigrams(query, skip)
            cur.execute(
                """select l.entry, l.created_at from npc_memory_log l join npcs n on n.template_id = l.npc_template
                   where l.player_id = %s and n.id = %s and l.id < %s order by l.id""",
                (player_id, npc_id, recent[0]["id"]))
            scored = [(len(words & _bigrams(r["entry"], skip)), r) for r in cur.fetchall()] if words else []
            related = [f"[{ago(r['created_at'])}] {_clip_replies(r['entry'])}"
                       for n, r in sorted(scored, key=lambda x: -x[0])[:NPC_LOG_RELATED] if n >= 2]
    summary = row["memory"] if row and row["memory"] else ""
    if not log:
        return summary
    return ((summary or "（还没有总结）")
            + ("\n更早的、跟他这次说的有关的来往：\n" + "\n".join(related) if related else "")
            + "\n最近的来往（从早到晚，原话记录）：\n" + "\n".join(log))


def memory_material(conn: Connection, player_id: UUID, npc_template: str) -> tuple[str, list[str]]:
    """后台整理摘要用：(旧摘要, 最近 NPC_SUMMARY_WINDOW 条完整记录，从早到晚)"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("select memory from player_npc_relations where player_id = %s and npc_template = %s",
                    (player_id, npc_template))
        row = cur.fetchone()
        cur.execute(
            """select * from (select id, entry, created_at from npc_memory_log where player_id = %s and npc_template = %s
                              order by id desc limit %s) t order by id""",
            (player_id, npc_template, NPC_SUMMARY_WINDOW))
        return (row["memory"] if row else ""), [f"[{ago(r['created_at'])}] {r['entry']}" for r in cur.fetchall()]


def last_bought(conn: Connection, player_id: UUID, npc: Npc) -> Optional[str]:
    """这个客人上次在这个 NPC 这儿买的东西（"再来一捆"说的就是它）"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("""select entry from npc_memory_log where player_id = %s and npc_template = %s and entry like '%%卖给了%%'
                       order by id desc limit 1""", (player_id, npc.template.id))
        row = cur.fetchone()
    m = re.search(rf"{re.escape(npc.name)}把(.+?)(?: ×\d+)?卖给了", row["entry"]) if row else None
    return m[1] if m else None


def add_npc_log(conn: Connection, player_id: UUID, npc: Npc, entry: str) -> None:
    """记一条完整的来往：他说了什么、NPC 回了什么、结果如何。全部留着，不删"""
    with conn.transaction():
        conn.execute("insert into npc_memory_log (player_id, npc_template, entry) values (%s, %s, %s)",
                     (player_id, npc.template.id, entry.strip()[:NPC_LOG_LIMIT]))


def set_npc_memory(conn: Connection, player_id: UUID, npc_template: str, memory: str) -> None:
    """更新 NPC 对这个玩家的记忆摘要（后台整理好的）。只是 NPC 的印象，不是游戏状态，这里只截断长度"""
    memory = memory.strip()[:NPC_MEMORY_LIMIT]
    with conn.transaction():
        conn.execute(
            """insert into player_npc_relations (player_id, npc_template, memory) values (%s, %s, %s)
               on conflict (player_id, npc_template) do update set memory = excluded.memory""",
            (player_id, npc_template, memory),
        )


# 好感像可攻略角色：最高一档是特别喜欢的人。(下限, 关系)，每个玩家各算各的
AFFINITY_TIERS = [(95, "特别喜欢的人"), (80, "喜欢"), (60, "心动"), (40, "朋友"), (20, "熟客"), (0, "客人"),
                  (-30, "戒备"), (-100, "讨厌")]


AFFINITY_GATES = (60, 80, 95)           # 聊天涨不过这几道坎（停在坎下一点），要送她心爱的礼物才跨得过去


CHAT_STEP = ((60, 1), (40, 2))          # 好感到了这个数，聊天一回合最多再涨几（越往上越慢）


CHAT_DAILY_FROM, CHAT_DAILY = 40, 10    # 好感 40 以上，每天靠聊天最多涨 10


def affinity_word(affinity: int) -> str:
    return next(word for low, word in AFFINITY_TIERS if affinity >= low)


def adjust_affinity(conn: Connection, player_id: UUID, npc_id: UUID, delta: int) -> ActionResult:
    """对话 AI 提议调整好感度，单次幅度和总范围都由这里限制，AI 给多大都没用。
    往上涨时越高越慢（CHAT_STEP）、40 以上每天有上限（CHAT_DAILY）、过不了 AFFINITY_GATES 的坎；往下掉不受这些限制"""
    delta = max(-AFFINITY_STEP, min(AFFINITY_STEP, delta))
    lo, hi = AFFINITY_RANGE
    try:
        with conn.transaction():
            cur = _cursor(conn)
            player = load_player(cur, player_id, lock=True)
            npcs = load_npcs(cur, "n.id = %s", (npc_id,))
            if not npcs or not npcs[0].alive or npcs[0].room_id != player.room_id:
                raise ActionError("对方不在这里")
            npc = npcs[0]
            cur.execute(
                """select affinity, chat_gain, chat_day = (now() at time zone 'Asia/Shanghai')::date as today
                   from player_npc_relations where player_id = %s and npc_template = %s for update""",
                (player.id, npc.template.id))
            row = cur.fetchone() or {"affinity": 0, "chat_gain": 0, "today": False}
            now, gained = row["affinity"], row["chat_gain"] if row["today"] else 0
            wanted, why = delta, ""
            if delta > 0:
                delta = min(delta, next((step for low, step in CHAT_STEP if now >= low), AFFINITY_STEP))
                if now >= CHAT_DAILY_FROM and delta > CHAT_DAILY - gained:
                    delta, why = max(0, CHAT_DAILY - gained), "今天已经聊得够多了，改天再来"
                if (gate := next((g for g in AFFINITY_GATES if now < g), None)) and now + delta >= gate:
                    delta, why = max(0, gate - 1 - now), f"要有特别的契机（比如送{npc.name}最想要的东西）才能更进一步"
            value = max(lo, min(hi, now + delta))
            cur.execute(
                """insert into player_npc_relations (player_id, npc_template, affinity, chat_day, chat_gain)
                   values (%(p)s, %(t)s, %(v)s, (now() at time zone 'Asia/Shanghai')::date, %(g)s)
                   on conflict (player_id, npc_template) do update
                     set affinity = %(v)s, chat_day = excluded.chat_day, chat_gain = %(g)s""",
                {"p": player.id, "t": npc.template.id, "v": value, "g": gained + max(0, value - now)},
            )
        trend = "上升" if value > now else "下降" if value < now else "没有变化"
        fact = f"{npc.name}对{player.name}的好感{trend}（当前 {value}，{affinity_word(value)}）"
        if wanted > 0 and value == now and why:
            fact = f"{npc.name}对{player.name}的好感停在 {value}（{affinity_word(value)}）：{why}"
        return ActionResult(action="affinity", success=True, facts=[fact])
    except ActionError as e:
        return ActionResult(action="affinity", success=False, facts=[str(e)])


# NPC 的 props.return_gifts：好感到了某一档（20/40/60/80/100），玩家来聊天时送一次；领过的档记在 player_npc_relations.gifts，
# 好感掉下去再涨回来也不会重领。送的是东西（item），或者解锁一样本事（perk：九折、买地图残片、看出诅咒、怪物图鉴……）
GIFT_BACK_FACT = "回礼"                  # 台词那边认这两个字，演送礼


REFILL_FACT = "说「续杯」就能灌满"       # 送的东西用完能找她续：台词那边认这句，台词里一定要说到


def return_gift(conn: Connection, player_id: UUID, npc: Npc) -> list[ActionResult]:
    """聊天时看看有没有该送的回礼：送最低的那一档（一次一档）"""
    gifts = npc.template.props.get("return_gifts") or {}
    if not gifts:
        return []
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("select affinity, gifts from player_npc_relations where player_id = %s and npc_template = %s for update",
                    (player_id, npc.template.id))
        row = cur.fetchone()
        if row is None:
            return []
        due = sorted(int(t) for t in gifts if int(t) <= row["affinity"] and int(t) not in (row["gifts"] or []))
        if not due:
            return []
        tier, g = due[0], gifts[str(due[0])] if str(due[0]) in gifts else gifts[due[0]]
        player = load_player(cur, player_id)
        cur.execute("update player_npc_relations set gifts = array_append(gifts, %s) where player_id = %s and npc_template = %s",
                    (tier, player_id, npc.template.id))
        facts = [f"{npc.name}送给{player.name}：{g['text']}（{GIFT_BACK_FACT}，好感到了 {tier}）"]
        if scene := g.get("scene"):
            facts.append(f"{npc.name}{scene}")               # 送的时候的动作（诺艾尔从怀里的书里抽出书签）
        if item := g.get("item"):
            if item == "lina_blade":
                facts += _exclusive_blade(cur, player, npc)
            else:
                cur.execute("insert into item_instances (template_id, player_id) values (%s, %s)", (item, player_id))
                cur.execute("select props ? 'empty' as refill from item_templates where id = %s", (item,))
                if cur.fetchone()["refill"]:
                    # 用完会空的（麦琪的酒壶、迷药）：告诉他能回来续
                    facts.append(f"用完了会空，回来找{npc.name}{REFILL_FACT}")
        return [ActionResult(action="gift_back", success=True, facts=facts)]


# 服务类的本事（熟客价、传承锻造、刷新词条、卖地图残片）要当前好感还在那一档以上：把人惹毛了就没了；
# 知识类的（辨咒笔记、怪物图鉴）学会了就是会了
SERVICE_PERKS = {"discount", "transfer", "reroll", "map_scrap"}


def perks(cur: Cursor, player_id: UUID, npc_template: Optional[str] = None) -> set[str]:
    """这个玩家从回礼里解锁了哪些本事（npc_template 给了就只看这个 NPC 的）"""
    cur.execute("""select r.gifts, r.affinity, t.props->'return_gifts' as table from player_npc_relations r
                   join npc_templates t on t.id = r.npc_template
                   where r.player_id = %s and cardinality(r.gifts) > 0 and t.props ? 'return_gifts'"""
                + (" and r.npc_template = %s" if npc_template else ""),
                (player_id, npc_template) if npc_template else (player_id,))
    return {g["perk"] for r in cur.fetchall() for t, g in (r["table"] or {}).items()
            if int(t) in r["gifts"] and g.get("perk") and (g["perk"] not in SERVICE_PERKS or r["affinity"] >= int(t))}


def npc_bonds(conn: Connection, player_id: UUID, npc: Npc) -> list[str]:
    """NPC 台词里可以提的别人的交情：这个玩家跟别的 NPC（熟客以上），别的玩家跟这个 NPC（朋友以上）"""
    with conn.transaction():
        cur = _cursor(conn)
        cur.execute("""select t.name, r.affinity from player_npc_relations r join npc_templates t on t.id = r.npc_template
                       where r.player_id = %s and r.npc_template <> %s and r.affinity >= 20 order by r.affinity desc""",
                    (player_id, npc.template.id))
        bonds = [f"他跟{r['name']}是{affinity_word(r['affinity'])}" for r in cur.fetchall()]
        cur.execute("""select p.name, r.affinity from player_npc_relations r join players p on p.id = r.player_id
                       where r.npc_template = %s and r.player_id <> %s and r.affinity >= 40
                       order by r.affinity desc limit 3""", (npc.template.id, player_id))
        return bonds + [f"你跟另一个客人{r['name']}是{affinity_word(r['affinity'])}" for r in cur.fetchall()]
