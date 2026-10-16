"""观测主 build 流派识别：每局识别到什么、稳不稳、证据是什么。

阶段一不改决策，所以这里只看"识别得对不对"，不看输赢。
"""

import collections
import json
import sys
from pathlib import Path


def main() -> int:
    recs = [
        json.loads(line)
        for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    games: list[list[dict]] = []
    previous_step = -1
    for rec in recs:
        # 分界用"步数回落"：一局内 step 递增，新一局会重置。按固定的
        # `== 1` 或 `<= 1` 判断都踩过坑（一局里同时有 step 0 和 step 1）。
        step = rec.get("step") or 0
        if step <= previous_step or not games:
            games.append([])
        previous_step = step
        games[-1].append(rec)

    print(f"=== {sys.argv[1]}：{len(games)} 段，{len(recs)} 条记录 ===\n")

    flips = 0
    seen = collections.Counter()
    for idx, seg in enumerate(games, 1):
        builds = [
            (r["step"], (r.get("snapshot") or {}).get("build"))
            for r in seg
            if isinstance((r.get("snapshot") or {}).get("build"), dict)
        ]
        if not builds:
            print(f"第 {idx} 局（{len(seg)} 步）：**没有任何流派记录**")
            continue
        # 主流派的演变（合并连续相同）
        path: list[tuple[int, str]] = []
        for step, build in builds:
            key = build.get("主流派") or "—"
            if not path or path[-1][1] != key:
                path.append((step, key))
        switches = len(path) - 1
        flips += switches
        final = path[-1][1]
        seen[final] += 1
        evidence = builds[-1][1]
        detail = "  ".join(
            f"{k}={v}" for k, v in evidence.items()
            if k not in ("牌型", "等级领先", "牌库", "人头", "主流派")
        )
        print(f"第 {idx:>2} 局（{len(seg):>3} 步）最终主流派 = {final:<6} "
              f"切换 {switches} 次")
        print(f"        轨迹: " + " → ".join(f"{k}@{s}" for s, k in path))
        print(f"        证据: 牌型={evidence.get('牌型')} 领先={evidence.get('等级领先')} "
              f"牌库={evidence.get('牌库')} 人头={evidence.get('人头')}  {detail}")
        print()

    print("===== 汇总 =====")
    print(f"  最终主流派分布：{dict(seen.most_common())}")
    print(f"  平均切换次数：{flips / max(1, len(games)):.2f} 次/局")
    return 0


if __name__ == "__main__":
    sys.exit(main())
