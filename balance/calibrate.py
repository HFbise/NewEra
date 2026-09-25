"""
用真人的聊天框记录（player_log）校准模拟：按层统计挨了多少伤害（占血量上限）、倒下几次、打了几轮。
只读数据库，要 DATABASE_URL。用法：python balance/calibrate.py 玩家A 玩家B
"""
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402


def facts_of(data: dict) -> list[str]:
    if "results" in data:
        return [f for r in data["results"] for f in r.get("facts", [])]
    return [data.get("text", "")]


def main(names: list[str]) -> None:
    with db.pool.connection() as c:
        for name in names:
            rows = c.execute("""select l.data from player_log l join players p on p.id = l.player_id
                                where p.name = %s order by l.id""", (name,)).fetchall()
            floor, max_hp = 0, 20
            stat = defaultdict(lambda: {"dmg": 0, "down": 0, "rounds": 0, "potions": 0, "turns": 0, "max": 20})
            for (data,) in rows:
                text = "\n".join(facts_of(data))
                for line in text.split("\n"):
                    if m := re.search(r"来到了第 (\d+) 层", line):
                        floor = int(m[1])
                    elif re.search(r"来到了(?!第 \d+ 层)", line) and name in line:
                        floor = 0                       # 回到地面
                if not floor:
                    continue
                s = stat[floor]
                s["turns"] += 1
                for m in re.finditer(rf"对{re.escape(name)}造成 (\d+) 点伤害", text):
                    s["dmg"] += int(m[1])
                for m in re.finditer(rf"{re.escape(name)}(?:中毒|流血)，掉了 (\d+) 点血", text):
                    s["dmg"] += int(m[1])
                for m in re.finditer(rf"{re.escape(name)} HP \d+/(\d+)", text):
                    max_hp = int(m[1])
                s["max"] = max_hp
                s["down"] += len(re.findall(rf"{re.escape(name)}倒下了", text))
                s["rounds"] += len(re.findall(r"【第 \d+ 轮】", text))
                s["potions"] += len(re.findall(rf"{re.escape(name)}(?:喝下了|吃掉了|喝掉了)", text))
            print(f"== {name}（记录 {len(rows)} 条）")
            print("层 | 挨的伤害 | 占上限 | 倒下 | 回合 | 吃喝 | 这层的命令数")
            for f in sorted(stat):
                s = stat[f]
                print(f"{f} | {s['dmg']} | {s['dmg'] / s['max'] * 100:.0f}% | {s['down']} | {s['rounds']} | {s['potions']} | {s['turns']}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["玩家A", "玩家B"])
