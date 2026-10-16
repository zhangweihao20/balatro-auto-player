"""提取每个牌型的基础值与**每级增量**。

游戏里等级换算的权威公式（functions_common_events.lua 的 level_up_hand）：

    chips = max(s_chips + l_chips * (level - 1), 0)
    mult  = max(s_mult  + l_mult  * (level - 1), 1)

这张表在 game.lua 的 G.GAME.hands 定义里。机器人需要它来建模
**The Arm**（打出的牌型等级 -1，而且降级发生在算分之前）。
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
        print("找不到 runs/game-source/**/game.lua")
        return 1
    source = candidates[0]

    # ["Pair"] = {visible = true, order = 12, mult = 2, chips = 10, s_mult = 2, s_chips = 10,
    #             level = 1, l_mult = 1, l_chips = 15, played = 0, ...}
    pattern = re.compile(r'\["([^"]+)"\]\s*=\s*\{(.*)\}')
    rows: list[tuple[str, int, int, int, int]] = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        name, body = match.group(1), match.group(2)
        if "s_chips" not in body or "l_chips" not in body:
            continue

        def pick(field: str) -> int | None:
            found = re.search(rf"\b{field}\s*=\s*(-?\d+)", body)
            return int(found.group(1)) if found else None

        values = (pick("s_chips"), pick("s_mult"), pick("l_chips"), pick("l_mult"))
        if any(v is None for v in values):
            continue
        rows.append((name, *values))  # type: ignore[arg-type]

    if not rows:
        print("没解析到牌型表——检查 game.lua 里 G.GAME.hands 的格式")
        return 1

    print(f"共 {len(rows)} 个牌型（源码：{source.name}）\n")
    print(f"{'牌型':18}{'1级筹码':>9}{'1级倍率':>9}{'每级+筹码':>11}{'每级+倍率':>11}")
    print("-" * 60)
    for name, s_chips, s_mult, l_chips, l_mult in rows:
        print(f"{name:18}{s_chips:>9}{s_mult:>9}{l_chips:>11}{l_mult:>11}")

    # 顺便验证一下：机器人的兜底表是否和这里对得上
    print("\n与 balatro_bot.cards.BASE_HAND_SCORE 的对照：")
    from balatro_bot.cards import BASE_HAND_SCORE

    mismatched = []
    for name, s_chips, s_mult, _, _ in rows:
        ours = BASE_HAND_SCORE.get(name)
        if ours is not None and tuple(ours) != (s_chips, s_mult):
            mismatched.append((name, ours, (s_chips, s_mult)))
    if mismatched:
        for name, ours, theirs in mismatched:
            print(f"  ✗ {name}: 我们 {ours} vs 游戏 {theirs}")
    else:
        print("  ✓ 全部一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
