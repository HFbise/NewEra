"""
六个主题的头目拉平用：伤害 ×1.5、正常装备、剑盾、单人、满血不喝血药，第 5 / 10 / 15 层各打 N 场。
目标线（盾格挡上线后定的）：挨的伤害 35%–50%，团灭率 5% 以内。
用法：python balance/bosses.py [--n 600] [--dmg 1.5] [--build sword_shield]
"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--dmg", type=float, default=1.5)
    ap.add_argument("--build", default="sword_shield")
    ap.add_argument("--focus-boss", action="store_true", help="召唤战先打头目")
    ap.add_argument("--quality", default="normal", help="normal 正常装备 / cap 上限档（照玩家A）")
    ap.add_argument("--themes", default="", help="只测这几个主题（逗号隔开，默认全部接好了的）")
    ap.add_argument("--depths", default="5,10,15,20,25,30", help="测哪几层（深层头目 16 层起按房间怪数涨血、一轮两动）")
    args = ap.parse_args()
    sim.VARIANT["dmg"] = args.dmg
    sim.VARIANT["focus_boss"] = args.focus_boss
    depths = [int(d) for d in args.depths.split(",")]
    print("| 头目 | " + " | ".join(f"第 {d} 层" for d in depths) + f" |（{sim.BUILDS[args.build]}，伤害 ×{args.dmg:g}，挨的伤害 / 团灭率）")
    print("|---|" + "---|" * len(depths))
    only = set(args.themes.split(",")) if args.themes else None
    for key, th in sim.THEMES.items():
        if (only and key not in only) or (not only and th.get("min_depth") and key not in sim.dungeon.DEEP_READY):
            continue
        random.seed(key)
        cells = []
        for depth in depths:
            a, w, _ = sim.fight_test(args.build, args.quality, (depth - 1) // sim.TRIP, depth, [("boss", "boss")], key, n=args.n)
            flag = "" if 0.35 <= a <= 0.50 and w <= 0.05 else " ✗"
            cells.append(f"{a:.0%} / {w:.0%}{flag}")
        print(f"| {th['boss']['name']} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
