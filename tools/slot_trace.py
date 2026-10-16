"""查"从包里拿的星球牌为什么没被用掉"：看槽位内容随时间的变化。"""

import json
import sys
from pathlib import Path

PLANET_WORDS = (
    "Mercury", "Venus", "Earth", "Mars", "Jupiter", "Saturn", "Uranus",
    "Neptune", "Pluto", "Ceres", "Eris", "Planet X",
)


def main() -> int:
    recs = [
        json.loads(line)
        for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    games: list[list[dict]] = []
    for rec in recs:
        if rec.get("step") == 1 or not games:
            games.append([])
        games[-1].append(rec)

    for idx, seg in enumerate(games, 1):
        picks = [
            r for r in seg
            if r["kind"] == "pack" and any(p in r["why"] for p in ("星球", "塔罗", "幻灵"))
            and "拿" in r["why"] and "跳过" not in r["why"]
        ]
        if not picks:
            continue
        print(f"===== 第 {idx} 局（{len(seg)} 步）=====")
        for rec in seg:
            slots = (rec.get("snapshot") or {}).get("consumables")
            hit = any(p in rec["why"] for p in PLANET_WORDS)
            if hit or rec["kind"] == "use":
                print(f"  {rec['step']:>4} {rec['kind']:<7} 槽={slots}  {rec['why'][:40]}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
