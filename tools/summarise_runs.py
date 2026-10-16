"""分析一次 eval 的结果，并可与历史基线做同期种子的对比。"""

import json
import statistics
import sys
from pathlib import Path


def load(path: str) -> dict:
    return {row["seed"]: row for row in json.load(open(path, encoding="utf-8"))}


def summarise(name: str, runs: dict) -> None:
    antes = [r["ante"] for r in runs.values()]
    steps = [r["steps"] for r in runs.values()]
    print(f"===== {name}：{len(runs)} 局 =====")
    print(f"  平均底注 {statistics.mean(antes):.2f}   中位 {statistics.median(antes):.0f}   "
          f"最高 {max(antes)}   最低 {min(antes)}")
    print(f"  平均步数 {statistics.mean(steps):.0f}    通关 {sum(1 for r in runs.values() if r['won'])}")
    dist: dict[int, int] = {}
    for a in antes:
        dist[a] = dist.get(a, 0) + 1
    print("  底注分布：" + "  ".join(f"A{a}:{n}" for a, n in sorted(dist.items())))
    errors = [r for r in runs.values() if r.get("error")]
    print(f"  出错局数 {len(errors)}")
    print()


def main() -> int:
    current = load(sys.argv[1])
    summarise("本次", current)

    if len(sys.argv) > 2:
        base = load(sys.argv[2])
        shared = sorted(set(current) & set(base))
        if shared:
            print(f"===== 同期种子对比（{len(shared)} 个共同种子）=====")
            print(f'  {"种子":<8}{"本次":>6}{"回合":>6}   {"基线":>6}{"回合":>6}   判定')
            better = worse = same = 0
            for seed in shared:
                a, b = current[seed], base[seed]
                if a["ante"] == b["ante"] and a["round_num"] == b["round_num"]:
                    same += 1
                    mark = "相同"
                elif (a["ante"], a["round_num"]) > (b["ante"], b["round_num"]):
                    better += 1
                    mark = "更好"
                else:
                    worse += 1
                    mark = "更差"
                print(f'  {seed:<8}{a["ante"]:>6}{a["round_num"]:>6}   '
                      f'{b["ante"]:>6}{b["round_num"]:>6}   {mark}')
            ca = statistics.mean(current[s]["ante"] for s in shared)
            cb = statistics.mean(base[s]["ante"] for s in shared)
            print()
            print(f"  共同种子平均底注：本次 {ca:.2f}   基线 {cb:.2f}   差 {ca - cb:+.2f}")
            print(f"  更好 {better} / 相同 {same} / 更差 {worse}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
