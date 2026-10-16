"""从 mod 日志里复盘一次评测的商店投入结构。

mod 会把每次购买记成：
    Buying JOKER 'Scholar' for $4
    Buying PLANET 'Mercury' for $3
    Buying VOUCHER 'Overstock' for $10
用牌型升级用的天体牌使用也记成：
    Using 'Mercury' (no targets)

于是可以还原出"钱花在哪了、牌型升了几级"。

    python tools/trace_shop.py --from 14:00 --to 14:26
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BUY_RE = re.compile(r"(\d\d:\d\d:\d\d).*Buying (\w+) '([^']+)' for \$(\d+)")
USE_RE = re.compile(r"(\d\d:\d\d:\d\d).*Using '([^']+)'")
# 开包里选中的牌。注意这和 Using 是**两条不同的日志**：
#   Pack: selecting Planet 'Mercury'    ← 从包里拿走
#   Using 'Mercury' (no targets)        ← 在消耗牌区使用
# 只统计 Using 会严重低估牌型等级——实测漏掉了一大半。
PACK_PICK_RE = re.compile(r"(\d\d:\d\d:\d\d).*Pack: selecting \w+ '([^']+)'")
PACK_SKIP_RE = re.compile(r"(\d\d:\d\d:\d\d).*Pack: skipping")

# 天体牌 -> 对应牌型（升一级）
PLANET_HAND = {
    "Pluto": "High Card", "Mercury": "Pair", "Uranus": "Two Pair",
    "Venus": "Three of a Kind", "Saturn": "Straight", "Jupiter": "Flush",
    "Earth": "Full House", "Mars": "Four of a Kind", "Neptune": "Straight Flush",
    "Planet X": "Five of a Kind", "Ceres": "Flush House", "Eris": "Flush Five",
}


def latest_log() -> Path | None:
    root = Path(os.environ.get("APPDATA", "")) / "Balatro" / "Mods" / "lovely" / "log"
    if not root.exists():
        return None
    files = sorted(root.glob("lovely-*.log"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def main() -> int:
    parser = argparse.ArgumentParser(description="复盘商店投入结构与牌型等级")
    parser.add_argument("--log", default=None)
    parser.add_argument("--from", dest="start", default="00:00:00")
    parser.add_argument("--to", dest="end", default="23:59:59")
    args = parser.parse_args()

    path = Path(args.log) if args.log else latest_log()
    if path is None or not path.exists():
        print("找不到 lovely 日志")
        return 1
    print(f"日志：{path.name}   窗口 {args.start} ~ {args.end}\n")

    kinds: collections.Counter[str] = collections.Counter()
    details: collections.Counter[tuple[str, str]] = collections.Counter()
    spent = collections.Counter()
    planets_bought: collections.Counter[str] = collections.Counter()
    planets_used: collections.Counter[str] = collections.Counter()
    other_used: collections.Counter[str] = collections.Counter()
    pack_skips = 0

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        found = BUY_RE.search(line)
        if found and args.start <= found.group(1) <= args.end:
            kind, name, cost = found.group(2), found.group(3), int(found.group(4))
            kinds[kind] += 1
            details[(kind, name)] += 1
            spent[kind] += cost
            if kind == "PLANET":
                planets_bought[name] += 1
            continue
        found = PACK_PICK_RE.search(line)
        if found and args.start <= found.group(1) <= args.end:
            name = found.group(2)
            if name in PLANET_HAND:
                planets_used[name] += 1
            continue
        found = PACK_SKIP_RE.search(line)
        if found and args.start <= found.group(1) <= args.end:
            pack_skips += 1
            continue
        found = USE_RE.search(line)
        if found and args.start <= found.group(1) <= args.end:
            name = found.group(2)
            if name in PLANET_HAND:
                planets_used[name] += 1
            else:
                other_used[name] += 1

    total = sum(kinds.values())
    if not total:
        print("这个窗口里没有购买记录。检查 --from/--to。")
        return 1

    print(f"购买合计 {total} 次，花费 ${sum(spent.values())}")
    print(f"{'类别':10}{'次数':>6}{'占比':>8}{'花费':>8}")
    print("-" * 34)
    for kind, count in kinds.most_common():
        print(f"{kind:10}{count:>6}{count / total * 100:>7.1f}%{spent[kind]:>8}")
    print()

    non_joker = [(kind, name, count) for (kind, name), count in details.items()
                 if kind != "JOKER"]
    if non_joker:
        print("非小丑牌明细：")
        for kind, name, count in sorted(non_joker):
            print(f"  {kind:9} {name:22} {count} 张")
        print()

    print("天体牌（牌型等级的唯一来源）：")
    print(f"{'天体牌':10}{'对应牌型':16}{'货架买':>7}{'实际升级':>9}")
    print("-" * 46)
    for name, hand in PLANET_HAND.items():
        if planets_bought[name] or planets_used[name]:
            print(f"{name:10}{hand:16}{planets_bought[name]:>7}{planets_used[name]:>9}")

    # 只按 `planets_used` 计等级，**不能**再加 `planets_bought`：
    #   * 天体包里选中的星球牌是**立即生效**的（只记 `Pack: selecting`）
    #   * 货架买的会进消耗牌区，之后记一次 `Using`
    # 两者合起来才是唯一一次计数；再加货架买入量就把它们算了两遍。
    investment: collections.Counter[str] = collections.Counter()
    for name, count in planets_used.items():
        investment[PLANET_HAND[name]] += count
    print("\n牌型等级投入（每张天体牌 = 该牌型 +1 级）：")
    if investment:
        for hand, count in investment.most_common():
            print(f"  {hand:16} +{count} 级")
        print(f"\n  最高单一牌型：+{max(investment.values())} 级")
    else:
        print("  （一次都没升过——牌型等级全是 1 级）")

    if pack_skips:
        print(f"\n买了却整个跳过的补充包：{pack_skips} 个（钱白花）")

    if other_used:
        print("\n其它消耗牌的使用：")
        for name, count in other_used.most_common(10):
            print(f"  {name:22} {count} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
