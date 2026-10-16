"""统计星球牌 / 天体包的供给来源与实际用量。

用法： python tools/planet_supply.py runs/journal20.jsonl
"""

import collections
import json
import re
import sys
from pathlib import Path

PLANETS = (
    "Pluto", "Mercury", "Uranus", "Venus", "Saturn", "Jupiter",
    "Earth", "Mars", "Neptune", "Planet X", "Ceres", "Eris",
)

# 中文名（游戏跑中文时 label 可能是中文）
PLANETS_CN = (
    "冥王星", "水星", "天王星", "金星", "土星", "木星",
    "地球", "火星", "海王星", "X行星", "谷神星", "厄里斯",
)


def is_planet_text(text: str) -> bool:
    return any(p in text for p in PLANETS + PLANETS_CN)


def main() -> int:
    path = sys.argv[1]
    recs = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    games = 0
    for rec in recs:
        if rec.get("step") == 1:
            games += 1
    print(f"=== {path}：{len(recs)} 条记录，约 {games} 局 ===\n")

    shelf_planet = [r for r in recs if r["kind"] == "buy" and is_planet_text(r["why"])]
    pack_buy = [r for r in recs if r["kind"] == "pack" and "买" in r["why"]]
    pack_pick = [r for r in recs if r["kind"] == "pack" and "从包里拿" in r["why"]]
    use = [r for r in recs if r["kind"] == "use" and "星球牌" in r["why"]]
    reroll = [r for r in recs if r["kind"] == "reroll"]

    celestial_buy = [r for r in pack_buy if "天体" in r["why"] or "Celestial" in r["why"]]
    other_buy = [r for r in pack_buy if r not in celestial_buy]
    celestial_pick = [r for r in pack_pick if is_planet_text(r["why"]) or "星球" in r["why"]]
    other_pick = [r for r in pack_pick if r not in celestial_pick]

    def avg(n):
        return n / games if games else 0

    print("=== 星球牌来源（每局平均）===")
    print(f"  卡片货架直接买 : {len(shelf_planet):>4}  → {avg(len(shelf_planet)):.2f} 张/局")
    print(f"  天体包拿到的   : {len(celestial_pick):>4}  → {avg(len(celestial_pick)):.2f} 张/局")
    print(f"  合计得到       : {len(shelf_planet) + len(celestial_pick):>4}  "
          f"→ {avg(len(shelf_planet) + len(celestial_pick)):.2f} 张/局")
    print(f"  实际用掉       : {len(use):>4}  → {avg(len(use)):.2f} 次/局")
    print()

    print("=== 补充包 ===")
    print(f"  买包总数       : {len(pack_buy):>4}  → {avg(len(pack_buy)):.2f} 个/局")
    print(f"    其中天体包   : {len(celestial_buy):>4}  → {avg(len(celestial_buy)):.2f} 个/局")
    print(f"    其它包       : {len(other_buy):>4}  → {avg(len(other_buy)):.2f} 个/局")
    print(f"  开包取牌       : {len(pack_pick):>4}  → {avg(len(pack_pick)):.2f} 次/局")
    print(f"    取自天体包   : {len(celestial_pick):>4}")
    print(f"    取自其它包   : {len(other_pick):>4}")
    print()

    print(f"=== 重摇：{len(reroll)} 次 → {avg(len(reroll)):.2f} 次/局 ===")
    kinds = collections.Counter(
        (r.get("numbers") or {}).get("预算", "-") for r in reroll
    )
    print()

    print("=== 买到的星球牌种类 ===")
    c = collections.Counter()
    for r in shelf_planet + celestial_pick:
        m = re.search(r"买 (\S+)|拿了=(\S+)", r["why"] + " " + str(r.get("numbers")))
        c[r["why"][:40]] += 1
    for k, n in c.most_common(8):
        print(f"  {k:<44} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
