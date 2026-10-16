"""牌库改造渠道的量化：补充包种类 + 每局改造出的张数。

用来判断 ③（把牌库改造当第二条乘法通道）该挂在哪里：
* 如果机器人根本不买 Standard 包，那是一条被完全忽略的渠道
* 如果改造张数只是 1~2 张，那"分散地改"没有意义，该改成**集中改算分牌**
"""

import collections
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOURNALS = [
    "journal_fix3_20.jsonl",
    "journal_crisisA.jsonl",
    "journal_crisisB.jsonl",
    "journal_pack20.jsonl",
    "journal_packB.jsonl",
]
TYPES = ("Buffoon", "Arcana", "Spectral", "Standard", "Celestial")


def split(path: Path) -> list[list[dict]]:
    games: list[list[dict]] = []
    previous = -1
    for line in path.read_text(encoding="utf-8").splitlines():
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
    packs: collections.Counter[str] = collections.Counter()
    per_game: dict[str, list[int]] = collections.defaultdict(list)

    for name in JOURNALS:
        for game in split(ROOT / "runs" / name):
            best: collections.Counter[str] = collections.Counter()
            for rec in game:
                why = rec.get("why") or ""
                if rec.get("kind") == "pack" and "补充包" in why:
                    for kind in TYPES:
                        if kind in why:
                            packs[kind] += 1
                build = (rec.get("snapshot") or {}).get("build") or {}
                text = build.get("改造")
                if text:
                    for part in text.split(","):
                        key, _, value = part.strip().partition(":")
                        if value.isdigit():
                            best[key] = max(best[key], int(value))
            for key in ("STEEL", "GLASS", "invested_face"):
                per_game[key].append(best.get(key, 0))

    print("  买过的补充包种类（100 局）:")
    for kind, n in packs.most_common():
        print(f"    {n:>4} 次  {kind}")
    print()
    print("  每局**最多**改造出多少张:")
    for key, values in per_game.items():
        nonzero = [v for v in values if v > 0]
        print(f"    {key:<15} 全局平均 {statistics.mean(values):.2f}  最高 {max(values)}  "
              f"有改造的局 {len(nonzero)}/{len(values)}"
              + (f"  这些局平均 {statistics.mean(nonzero):.2f}" if nonzero else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
