"""诊断：机器人的阵容里"乘法倍率"够不够，以及它和推进深度的关系。

巴拉丁每个底注的过关分涨 2.08 倍。加法倍率是线性增长，追不上指数；能追上的
只有**乘法倍率叠加**（×Mult 小丑牌、玻璃牌、蜡封/重触发）。

判定"是不是 ×Mult"直接用**游戏自己的配置** `JOKER_DATA[key]['config']['Xmult']`，
而不是我手维护的那个集合——后者只当兜底。

日志里 `snapshot.jokers` 存的是**显示名**（"Abstract Joker"），所以要先反查 key。
"""

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from balatro_bot.joker_data import JOKER_DATA  # noqa: E402
from balatro_bot.jokers import MULTIPLICATIVE_JOKERS, NO_SCORE_EFFECT  # noqa: E402

LABEL_TO_KEY = {d["name"]: k for k, d in JOKER_DATA.items() if d.get("name")}


def is_mult(key: str) -> bool:
    """这张牌是不是乘法倍率。优先信游戏配置里的 Xmult。"""
    config = (JOKER_DATA.get(key) or {}).get("config") or {}
    return bool(config.get("Xmult")) or key in MULTIPLICATIVE_JOKERS


def split_games(path: str) -> list[list[dict]]:
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
    paths = sys.argv[1:]
    per_ante: dict[int, list[int]] = defaultdict(list)
    final: list[tuple[int, int, list[str]]] = []
    deep, shallow = [], []
    offered: Counter[str] = Counter()
    taken: Counter[str] = Counter()
    unresolved: Counter[str] = Counter()

    for path in paths:
        for game in split_games(path):
            if not game:
                continue
            by_ante: dict[int, list[str]] = {}
            for rec in game:
                snap = rec.get("snapshot") or {}
                jokers, ante = snap.get("jokers"), snap.get("ante")
                if isinstance(jokers, list) and isinstance(ante, int):
                    by_ante[ante] = jokers
                # 商店里被估值过的牌 = 供给
                if rec.get("kind") in ("buy", "flow"):
                    for label, key in LABEL_TO_KEY.items():
                        if label in (rec.get("why") or ""):
                            offered[key] += 1
                if rec.get("kind") == "buy":
                    for label, key in LABEL_TO_KEY.items():
                        if label in (rec.get("why") or ""):
                            taken[key] += 1

            def count_mult(labels: list[str]) -> int:
                n = 0
                for label in labels:
                    key = LABEL_TO_KEY.get(label)
                    if key is None:
                        unresolved[label] += 1
                        continue
                    if is_mult(key):
                        n += 1
                return n

            for ante, labels in by_ante.items():
                per_ante[ante].append(count_mult(labels))
            top = max(by_ante) if by_ante else 0
            labels = by_ante.get(top, [])
            final.append((top, count_mult(labels), labels))
            (deep if top >= 7 else shallow).append(count_mult(labels))

    print("=" * 70)
    print("每个底注结束时，手上 ×Mult 小丑牌的平均张数")
    print("=" * 70)
    for ante in sorted(per_ante):
        vals = per_ante[ante]
        zero = sum(1 for v in vals if v == 0)
        print(f"  底注 {ante:>2}: 平均 {statistics.mean(vals):.2f} 张   "
              f"{zero}/{len(vals)} 局一张都没有（{zero/len(vals)*100:.0f}%）")

    print()
    print("=" * 70)
    print("深度推进 vs 浅局")
    print("=" * 70)
    if deep and shallow:
        print(f"  到达底注 ≥7（{len(deep)} 局）: 平均 {statistics.mean(deep):.2f} 张")
        print(f"  止步底注 ≤6（{len(shallow)} 局）: 平均 {statistics.mean(shallow):.2f} 张")

    print()
    print("=" * 70)
    print("局终阵容里 ×Mult 张数的分布")
    print("=" * 70)
    for mult, n in sorted(Counter(m for _, m, _ in final).items()):
        print(f"  {mult} 张: {n} 局")

    print()
    print("=" * 70)
    print("最深的 10 局")
    print("=" * 70)
    for ante, mult, labels in sorted(final, reverse=True)[:10]:
        print(f"  底注 {ante:>2}  ×Mult {mult}  {', '.join(labels)}")

    if unresolved:
        print()
        print("  未能识别的显示名（需要补 JOKER_DATA）:", dict(unresolved.most_common(8)))

    # 供给 vs 选择
    print()
    print("=" * 70)
    print("×Mult 牌的供给与选择（被估值过 vs 真买了）")
    print("=" * 70)
    mult_offered = sum(n for k, n in offered.items() if is_mult(k))
    mult_taken = sum(n for k, n in taken.items() if is_mult(k))
    all_offered = sum(offered.values())
    all_taken = sum(taken.values())
    if all_offered:
        print(f"  商店里出现过的牌（按日志提到次数）: {all_offered} 次，"
              f"其中 ×Mult {mult_offered} 次（{mult_offered/all_offered*100:.0f}%）")
    print(f"  实际买入: {all_taken} 次，其中 ×Mult {mult_taken} 次")
    print()
    print("  买入最多的 12 张:")
    for key, n in taken.most_common(12):
        print(f"    {n:>3} 次  {key:<22} ×Mult={is_mult(key)!s:<6} "
              f"静态估值={JOKER_DATA.get(key, {}).get('name', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
