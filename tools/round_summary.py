"""汇总几轮 eval 的关键指标 + 腾槽卖出次数（从 journal 统计）。"""

import json
import sys
from pathlib import Path

RUNS = [
    ("修复前", "runs/fast20.json", "runs/journal20.jsonl"),
    ("第二轮", "runs/fix2_20.json", "runs/journal_fix2_20.jsonl"),
    ("腾槽后", "runs/fix3_20.json", "runs/journal_fix3_20.jsonl"),
]


def main() -> int:
    print("===== 各轮总体指标 =====")
    print(f'  {"轮次":<8}{"平均底注":>9}{"最高":>6}{"通关":>7}{"平均步数":>9}')
    for name, json_path, _ in RUNS:
        if not Path(json_path).exists():
            continue
        rows = json.load(open(json_path, encoding="utf-8"))
        n = len(rows)
        print(
            f'  {name:<8}{sum(r["ante"] for r in rows) / n:>9.2f}'
            f'{max(r["ante"] for r in rows):>6}'
            f'{sum(1 for r in rows if r["won"]):>4}/{n:<2}'
            f'{sum(r["steps"] for r in rows) / n:>9.0f}'
        )

    print()
    print("===== 腾槽卖出（note 写在 journal 里，不在日志文件里）=====")
    for name, _, journal_path in RUNS:
        if not Path(journal_path).exists():
            continue
        recs = [
            json.loads(line)
            for line in Path(journal_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        junk = [r for r in recs if "腾消耗牌槽" in r["why"]]
        print(f"  {name}: {len(junk)} 次（{len(junk) / 20:.2f} 次/局）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
