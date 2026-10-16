"""配对比较：软筛开 / 关，同一批种子。"""

import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    data = json.load(open(path, encoding="utf-8"))
    return {row["seed"]: row for row in data}


def main() -> int:
    on = load(sys.argv[1])
    off = load(sys.argv[2])

    print("===== 配对比较（软筛 开 vs 关，同一种子集）=====")
    header = f'  {"种子":<8}{"开底注":>7}{"开回合":>7}{"开步数":>7}   {"关底注":>7}{"关回合":>7}{"关步数":>7}   判定'
    print(header)

    same = 0
    better = worse = 0
    for seed in sorted(on):
        a, b = on[seed], off[seed]
        key_a = (a["ante"], a["round_num"], a["steps"])
        key_b = (b["ante"], b["round_num"], b["steps"])
        if key_a == key_b:
            same += 1
            mark = "相同"
        elif (a["ante"], a["round_num"]) > (b["ante"], b["round_num"]):
            better += 1
            mark = "开更好"
        else:
            worse += 1
            mark = "开更差"
        print(
            f'  {seed:<8}{a["ante"]:>7}{a["round_num"]:>7}{a["steps"]:>7}   '
            f'{b["ante"]:>7}{b["round_num"]:>7}{b["steps"]:>7}   {mark}'
        )

    def avg(d, key):
        return sum(r[key] for r in d.values()) / len(d)

    print()
    print(f'  平均底注：开 {avg(on, "ante"):.2f}   关 {avg(off, "ante"):.2f}')
    print(f'  平均步数：开 {avg(on, "steps"):.0f}   关 {avg(off, "steps"):.0f}')
    print(f'  通关数：  开 {sum(1 for r in on.values() if r["won"])}   '
          f'关 {sum(1 for r in off.values() if r["won"])}')
    print(f"  完全相同 {same} 局，开更好 {better} 局，开更差 {worse} 局")
    return 0


if __name__ == "__main__":
    sys.exit(main())
