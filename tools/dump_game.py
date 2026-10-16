"""按"步数归 1"把 journal 切成每局一段，打印指定那一局的决策流水。"""

import json
import sys
from pathlib import Path


def games(path: str) -> list[list[dict]]:
    out: list[list[dict]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("step") == 1 or not out:
            out.append([])
        out[-1].append(rec)
    return out


def main() -> int:
    path, index = sys.argv[1], int(sys.argv[2])
    all_games = games(path)
    print(f"共切出 {len(all_games)} 局")
    seg = all_games[index - 1]
    print(f"===== 第 {index} 局：{len(seg)} 条决策 =====")
    for rec in seg:
        nums = rec.get("numbers") or {}
        shown = "  ".join(f"{k}={v}" for k, v in nums.items())
        print(f"  {rec['step']:>4} {rec['kind']:<9} {rec['why'][:52]:<54} {shown[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
