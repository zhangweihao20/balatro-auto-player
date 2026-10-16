"""配对测量汇总：底注均值、逐局判定、以及"底注 ≤2 早死"这个专项指标。"""

import json
import statistics
import sys
from pathlib import Path


def load(path: str) -> dict:
    return {r["seed"]: r for r in json.load(open(path, encoding="utf-8"))}


def report(name: str, runs: dict) -> None:
    antes = [r["ante"] for r in runs.values()]
    early = [r for r in runs.values() if r["ante"] <= 2]
    print(f"  {name}: 平均 {statistics.mean(antes):.2f}  中位 {statistics.median(antes):.0f}  "
          f"最高 {max(antes)}  通关 {sum(1 for r in runs.values() if r['won'])}/{len(runs)}  "
          f"出错 {sum(1 for r in runs.values() if r.get('error'))}")
    print(f"      底注≤2 早死 {len(early)}/{len(runs)} = {len(early)/len(runs)*100:.0f}%")


def main() -> int:
    a, b = load(sys.argv[1]), load(sys.argv[2])
    print("===== 总体 =====")
    report("A 基线（crisis=现状）", a)
    report("B 修复（crisis=潜力）", b)

    shared = sorted(set(a) & set(b))
    better = worse = same = 0
    print(f"\n===== 逐局（{len(shared)} 个种子）=====")
    for seed in shared:
        x, y = a[seed], b[seed]
        if (x["ante"], x["round_num"]) == (y["ante"], y["round_num"]):
            same += 1
            mark = "相同"
        elif (y["ante"], y["round_num"]) > (x["ante"], x["round_num"]):
            better += 1
            mark = "B 更好"
        else:
            worse += 1
            mark = "B 更差"
        print(f'  {seed:<8} A {x["ante"]:>2}/{x["round_num"]:<3} '
              f'B {y["ante"]:>2}/{y["round_num"]:<3}  {mark}')
    print()
    print(f"  B 更好 {better} / 相同 {same} / 更差 {worse}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
