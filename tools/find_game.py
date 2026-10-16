"""按"步数回落"切局，找出并打印指定局面（用来查确定性早死）。

用法：
    python tools/find_game.py runs/journal_fix3_20.jsonl 17
    python tools/find_game.py runs/journal_fix3_20.jsonl 17 --full
"""

import json
import sys
from pathlib import Path


def split(path: str) -> list[list[dict]]:
    games: list[list[dict]] = []
    previous = -1
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        step = rec.get("step") or 0
        if step <= previous or not games:
            games.append([])
        previous = step
        games[-1].append(rec)
    return games


def main() -> int:
    path = sys.argv[1]
    target = int(sys.argv[2])
    full = "--full" in sys.argv
    games = split(path)
    print(f"{path}: {len(games)} 局")

    # 先列出所有局的规模，方便对照
    sizes = [len(g) for g in games]
    print(f"  各局步数: {sizes}")

    hits = [i for i, g in enumerate(games) if len(g) == target]
    if not hits:
        print(f"  没有恰好 {target} 步的局")
        return 1
    for index in hits:
        seg = games[index]
        print(f"\n===== 第 {index + 1} 局（{len(seg)} 步）=====")
        for rec in seg if full else seg:
            nums = rec.get("numbers") or {}
            snap = rec.get("snapshot") or {}
            shown = "  ".join(f"{k}={v}" for k, v in nums.items())
            detail = rec.get("detail") or ""
            ctx = (f"钱{snap.get('money')} 分{snap.get('chips')} 差{snap.get('need')} "
                   f"手{snap.get('hands')} 弃{snap.get('discards')}")
            print(f"  {rec.get('step'):>4} {ctx:<34} {rec['kind']:<8} "
                  f"{rec['why'][:40]:<42} {shown[:58]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
