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
    args = ap.parse_args()
    sim.VARIANT["dmg"] = args.dmg
    print(f"| 头目 | 第 5 层 | 第 10 层 | 第 15 层 |（{sim.BUILDS[args.build]}，伤害 ×{args.dmg:g}，挨的伤害 / 团灭率）")
    print("|---|---|---|---|")
    for key, th in sim.THEMES.items():
        random.seed(key)
        cells = []
        for depth in (5, 10, 15):
            a, w, _ = sim.fight_test(args.build, "normal", (depth - 1) // sim.TRIP, depth, [("boss", "boss")], key, n=args.n)
            flag = "" if 0.35 <= a <= 0.50 and w <= 0.05 else " ✗"
            cells.append(f"{a:.0%} / {w:.0%}{flag}")
        print(f"| {th['boss']['name']} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
