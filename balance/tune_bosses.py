"""
头目加了技能以后按"技能换数值"找血量倍率和攻击：在内存里试几组，看第 5 / 10 / 15 层是不是都落在目标线里
（伤害 ×1.5、正常装备、剑盾、单人、满血不喝药；挨的伤害 35%–50%，团灭率 5% 以内）。找到了手动写回 dungeon.yaml。
用法：python balance/tune_bosses.py mine 0.8,0.9,1.0 -1,0 [--n 300]
"""
import argparse
import itertools
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("theme")
    ap.add_argument("hps")
    ap.add_argument("atks")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--build", default="sword_shield")
    args = ap.parse_args()
    sim.VARIANT["dmg"] = 1.5
    boss = sim.THEMES[args.theme]["boss"]
    for hp, atk in itertools.product([float(x) for x in args.hps.split(",")], [int(x) for x in args.atks.split(",")]):
        boss["hp"], boss["atk"] = hp, atk
        cells, ok = [], True
        for depth in (5, 10, 15):
            random.seed(f"{args.theme}-{depth}")
            a, w, _ = sim.fight_test(args.build, "normal", (depth - 1) // sim.TRIP, depth, [("boss", "boss")], args.theme, n=args.n)
            ok &= 0.35 <= a <= 0.50 and w <= 0.05
            cells.append(f"{a:.0%}/{w:.0%}")
        print(f"{boss['name']} 血 {hp} 攻 {atk:+d}：" + "  ".join(cells) + ("  ✓" if ok else ""))


if __name__ == "__main__":
    main()
