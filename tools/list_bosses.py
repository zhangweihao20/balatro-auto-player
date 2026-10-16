"""列出游戏里所有 Boss / 精英盲注及其 debuff 配置。

用来核对机器人的 Boss 建模覆盖度。需要先跑过 tools/extract_jokers.py
（或任何解包过 game.lua 的步骤），runs/game-source/ 里有源码。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    candidates = list((root / "runs" / "game-source").rglob("game.lua"))
    if not candidates:
        print("找不到 runs/game-source/**/game.lua —— 先解包游戏源码")
        return 1
    source = candidates[0]
    print(f"源码：{source}\n")

    pattern = re.compile(r"\s*(bl_\w+)\s*=\s*\{name\s*=\s*'([^']+)'(.*)")
    rows = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        key, name, rest = match.group(1), match.group(2), match.group(3)
        if key in ("bl_small", "bl_big"):
            continue
        debuff = re.search(r"debuff\s*=\s*\{([^}]*)\}", rest)
        mult = re.search(r"mult\s*=\s*([\d.]+)", rest)
        rows.append({
            "key": key,
            "name": name,
            "mult": mult.group(1) if mult else "2",
            "elite": "showdown" in rest,
            "debuff": debuff.group(1).strip() if debuff else "",
        })

    rows.sort(key=lambda r: (not r["elite"], r["name"]))
    print(f"共 {len(rows)} 个（不含小/大盲），其中精英 {sum(1 for r in rows if r['elite'])} 个\n")
    header = f"{'名称':17}{'key':17}{'mult':>5}  {'debuff 配置'}"
    print(header)
    print("-" * 78)
    for row in rows:
        elite = "精英 " if row["elite"] else "     "
        print(f"{row['name']:17}{row['key']:17}{row['mult']:>5}  {elite}{row['debuff']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
