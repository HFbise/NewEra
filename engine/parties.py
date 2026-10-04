"""Following and parties. / 跟随和组队"""
from typing import Optional
from uuid import UUID, uuid4

from psycopg import Cursor

import dungeon
from schema import Follow, Kick, LeaveParty, Player, RoomView, Unfollow

from .core import ActionError, PARTY_MAX
from . import duels, loading


# 组队就是跟随：跟着谁就加入谁的队伍（他没队伍就一起新建一个），不跟了就退队。
# 地牢里、战斗中不能退队也不能停止跟随，免得把队友丢在半路；地牢按队伍分副本，不是一起进来的人碰不到面
def _party_locked(cur: Cursor, player: Player) -> Optional[str]:
    """现在不能离开队伍的原因，能离开是 None"""
    if dungeon.is_dungeon(player.room_id):
        return "在地牢里"
    cur.execute("""select 1 from npcs n join npc_templates t on t.id = n.template_id
                   where n.room_id = %s and n.alive and t.hostile limit 1""", (player.room_id,))
    if cur.fetchone():
        return "正在战斗"
    cur.execute("select 1 from duels where accepted and %s in (challenger, target) limit 1", (player.id,))
    if cur.fetchone():
        return "正在决斗"
    return None


def do_follow(cur: Cursor, player: Player, view: RoomView, a: Follow) -> list[str]:
    target, _ = duels.room_player(cur, player, a.target, lock=True)
    if target.following == player.id:
        raise ActionError(f"{target.name}正跟着{player.name}，不能互相跟着")
    facts = [f"{player.name}跟上了{target.name}，以后{target.name}走到哪就跟到哪（这一回合两人都没有移动）"]
    if not (player.party_id and player.party_id == target.party_id):
        if player.party_id and (why := _party_locked(cur, player)):
            raise ActionError(f"{player.name}{why}，不能丢下现在的队伍去跟{target.name}")
        party_id = target.party_id or uuid4()
        if target.party_id and len(_party_names(cur, party_id)) >= PARTY_MAX:
            raise ActionError(f"{target.name}的队伍已经满了（最多 {PARTY_MAX} 人）")
        if player.party_id:
            leave_party(cur, player)
        if target.party_id is None:
            cur.execute("update players set party_id = %s where id = %s", (party_id, target.id))
        cur.execute("update players set party_id = %s where id = %s", (party_id, player.id))
        facts.append(f"{player.name}加入了{target.name}的队伍，队伍成员：" + "、".join(_party_names(cur, party_id)))
    cur.execute("update players set following = %s where id = %s", (target.id, player.id))
    return facts


def do_unfollow(cur: Cursor, player: Player, view: RoomView, a: Unfollow) -> list[str]:
    """不跟了就是退队"""
    if player.following is None and not player.party_id:
        raise ActionError(f"{player.name}没有在跟着谁，也不在队伍里")
    return _quit_party(cur, player)


def do_leave_party(cur: Cursor, player: Player, view: RoomView, a: LeaveParty) -> list[str]:
    if not player.party_id:
        raise ActionError(f"{player.name}没有在队伍里")
    return _quit_party(cur, player)


def do_kick(cur: Cursor, player: Player, view: RoomView, a: Kick) -> list[str]:
    """把跟着自己的人请出队伍（组队没有队长，谁跟着你你就能请他走）：他不再跟着你、离开队伍。
    他不在身边、下线了也行；地牢里、战斗中不行，跟自己退队一样"""
    cur.execute("select * from players where name = %s for update", (a.target.strip(),))
    row = cur.fetchone()
    if not row or row["id"] == player.id:
        raise ActionError(f"没有叫{a.target}的人" if not row else "不能把自己请出队伍，要走就说「离队」")
    target = loading.load_player(cur, row["id"], lock=True)
    if target.following != player.id:
        raise ActionError(f"{target.name}没有在跟着{player.name}，只能请走跟着自己的人")
    if why := _party_locked(cur, player) or _party_locked(cur, target):
        raise ActionError(f"{why}，不能把{target.name}请出队伍（回到地面、打完这一仗再说）")
    cur.execute("update players set following = null where id = %s", (target.id,))
    if target.party_id:
        leave_party(cur, target)
    return [f"{player.name}请{target.name}离开了队伍，{target.name}不再跟着{player.name}了"]


def _quit_party(cur: Cursor, player: Player) -> list[str]:
    if why := _party_locked(cur, player):
        raise ActionError(f"{player.name}{why}，不能丢下队伍自己走（回到地面、打完这一仗再说）")
    facts = []
    if player.following:
        cur.execute("select name from players where id = %s", (player.following,))
        facts.append(f"{player.name}不再跟着{cur.fetchone()['name']}了")
    cur.execute("update players set following = null where id = %s", (player.id,))
    if player.party_id:
        leave_party(cur, player)
        facts.append(f"{player.name}离开了队伍")
    return facts


def _party_names(cur: Cursor, party_id: UUID) -> list[str]:
    cur.execute("select name from players where party_id = %s order by name", (party_id,))
    return [r["name"] for r in cur.fetchall()]


def leave_party(cur: Cursor, player: Player) -> None:
    """退队；跟着他的人不再跟着他（还留在队里），队伍只剩一个人就解散"""
    cur.execute("update players set party_id = null where id = %s", (player.id,))
    cur.execute("update players set following = null where following = %s", (player.id,))
    cur.execute(
        """update players set party_id = null
           where party_id = %(p)s and (select count(*) from players where party_id = %(p)s) = 1""",
        {"p": player.party_id},
    )
