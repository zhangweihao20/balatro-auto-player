"""对比两轮 journal 里"买入 / 用牌目标"的决策序列，看流派接入有没有真的改变选择。"""

import json
import sys
from pathlib import Path


def load(path: str) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    a, b = load(sys.argv[1]), load(sys.argv[2])
    print(f"  {Path(sys.argv[1]).name}: {len(a)} 条   {Path(sys.argv[2]).name}: {len(b)} 条")

    for kind in ("buy", "use"):
        ra = [r["why"] for r in a if r["kind"] == kind]
        rb = [r["why"] for r in b if r["kind"] == kind]
        same = sum(1 for x, y in zip(ra, rb) if x == y)
        print(f"\n  === {kind}：{len(ra)} vs {len(rb)}，前 {min(len(ra), len(rb))} 条里相同 {same} ===")
        shown = 0
        for i, (x, y) in enumerate(zip(ra, rb)):
            if x != y and shown < 6:
                print(f"    #{i}  A: {x[:52]}")
                print(f"        B: {y[:52]}")
                shown += 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
