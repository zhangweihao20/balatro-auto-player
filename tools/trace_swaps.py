"""从 mod 日志里抽出商店的买卖时序，用于复盘换牌决策。

mod 的两个端点会记牌名：
    Selling joker '<name>' for $<n>
    Buying JOKER '<name>' for $<n>
所以不需要额外埋点就能还原"卖了什么、买了什么、花了多少钱"。

    python tools/trace_swaps.py                     # 当前最新日志
    python tools/trace_swaps.py --from 13:18 --to 13:41
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SELL_RE = re.compile(r"(\d\d:\d\d:\d\d).*Selling \w+ '([^']+)' for \$(\d+)")
BUY_RE = re.compile(r"(\d\d:\d\d:\d\d).*Buying (\w+) '([^']+)' for \$(\d+)")


def latest_log() -> Path | None:
    root = Path(os.environ.get("APPDATA", "")) / "Balatro" / "Mods" / "lovely" / "log"
    if not root.exists():
        return None
    files = sorted(root.glob("lovely-*.log"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def main() -> int:
    parser = argparse.ArgumentParser(description="复盘商店里的买卖时序")
    parser.add_argument("--log", default=None, help="指定 lovely 日志")
    parser.add_argument("--from", dest="start", default="00:00:00")
    parser.add_argument("--to", dest="end", default="23:59:59")
    args = parser.parse_args()

    path = Path(args.log) if args.log else latest_log()
    if path is None or not path.exists():
        print("找不到 lovely 日志")
        return 1
    print(f"日志：{path.name}\n")

    events: list[tuple[str, str, str, int]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        found = SELL_RE.search(line)
        if found:
            if args.start <= found.group(1) <= args.end:
                events.append((found.group(1), "卖", found.group(2), int(found.group(3))))
            continue
        found = BUY_RE.search(line)
        if found and args.start <= found.group(1) <= args.end:
            kind = "买丑" if found.group(2) == "JOKER" else "买牌"
            events.append((found.group(1), kind, found.group(3), int(found.group(4))))

    if not events:
        print("这个时间窗口里没有买卖动作。检查 --from/--to。")
        return 1

    sells = sum(1 for e in events if e[1] == "卖")
    buys = sum(1 for e in events if e[1].startswith("买"))
    print(f"窗口 {args.start}~{args.end}：卖出 {sells} 次，买入 {buys} 次\n")
    print(f"{'时间':10}{'动作':6}{'牌名':28}{'金额'}")
    print("-" * 56)
    for stamp, kind, name, amount in events:
        print(f"{stamp:10}{kind:6}{name:28}${amount}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
