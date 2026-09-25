"""
精英词缀单测：每种词缀的精英打一场（伤害 ×1.5、正常装备、剑盾、单人、满血不喝血药），第 5 / 10 / 15 层；
第 10、15 层再测"精英 + 一群普通怪"的两群房间（号令的跟两群怪叠在一起会不会冒尖）。对照是不带词缀的精英。
用法：python balance/elites.py [--n 500] [--kind kobold] [--theme mine]
"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--kind", default="kobold")
    ap.add_argument("--theme", default="mine")
    ap.add_argument("--build", default="sword_shield")
    ap.add_argument("--dmg", type=float, default=1.5)
    args = ap.parse_args()
    sim.VARIANT["dmg"] = args.dmg
    print(f"| 词缀 | 5 层 | 10 层 | 15 层 | 10 层两群 | 15 层两群 |（{sim.MONSTERS[args.kind]['name']}，{sim.BUILDS[args.build]}，"
          f"伤害 ×{args.dmg:g}，挨的伤害 / 团灭率）")
    print("|---|---|---|---|---|---|")
    for affix in ["none"] + list(sim.R.ELITE_AFFIXES):
        cells = []
        for depth, groups in ((5, 1), (10, 1), (15, 1), (10, 2), (15, 2)):
            random.seed(f"{affix}-{depth}-{groups}")
            g = [(args.kind, "elite")] + [(args.kind, "normal")] * (groups - 1)
            a, w, _ = sim.fight_test(args.build, "normal", (depth - 1) // sim.TRIP, depth, g, args.theme, n=args.n, affix=affix)
            cells.append(f"{a:.0%} / {w:.0%}")
        name = "（没有词缀）" if affix == "none" else sim.R.ELITE_AFFIXES[affix]["name"]
        print(f"| {name} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
