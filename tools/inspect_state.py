"""对真实游戏做一次体检，并把原始状态存下来。

用途：代码里对牌型名（"Pair" / "Two Pair" ...）、状态名、字段结构都做了假设，
一旦有一处对不上，算分就会静默退化到兜底值。这个脚本把这些假设逐条对照真机验证。

    python tools/inspect_state.py           看当前状态
    python tools/inspect_state.py --start   先开一局再看
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from balatro_bot.cards import BASE_HAND_SCORE, HAND_NAMES  # noqa: E402
from balatro_bot.rpc import BalatroClient  # noqa: E402
from balatro_bot.scoring import describe_jokers  # noqa: E402
from balatro_bot.state import GameState  # noqa: E402


def main() -> int:
    client = BalatroClient()
    if "--start" in sys.argv:
        client.menu()
        raw = client.start("RED", "WHITE")
        print("已开新局")
    else:
        raw = client.gamestate()

    out_dir = Path(__file__).resolve().parent.parent / "runs"
    out_dir.mkdir(exist_ok=True)
    dump = out_dir / "last-state.json"
    dump.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    state = GameState(raw)
    print(f"状态      {state.state}")
    print(f"底注/回合 {state.ante} / {state.round_num}")
    print(f"钱        ${state.money}")
    print(f"分数      {state.score} / 需要 {state.required_score}")
    blind = state.current_blind
    print(f"当前盲注  {blind.type} {blind.name!r} status={blind.status} "
          f"需要 {blind.score}")
    print(f"回合信息  手 {state.round.hands_left} / 弃 {state.round.discards_left} "
          f"已打 {state.round.hands_played} 次，重摇价 ${state.round.reroll_cost}")
    print(f"小丑牌    {describe_jokers(state)}")
    print(f"原始状态已存到 {dump}")
    print()

    # --- 最关键的检查：牌型名是否和游戏一致
    table = state.hands
    print(f"游戏返回的牌型表共 {len(table)} 项")
    missing = [name for name in HAND_NAMES if name not in table]
    extra = [name for name in table if name not in HAND_NAMES]
    if missing:
        print(f"  ⚠ 我们认识但游戏里没有：{missing}")
    if extra:
        print(f"  ⚠ 游戏里有但我们不认识：{extra}")
    if not missing and not extra:
        print("  ✓ 12 种牌型名完全一致")
    for name in HAND_NAMES:
        info = table.get(name)
        if info:
            fallback = BASE_HAND_SCORE.get(name, (0, 0))
            mark = "" if (info.chips, info.mult) == fallback else "  ← 已升级/与兜底值不同"
            print(f"    {name:<16} lv{info.level}  {info.chips} chips × {info.mult} mult{mark}")
    print()

    # --- 手牌结构
    print(f"手牌 {len(state.hand.cards)} 张（上限 {state.hand.limit}）：")
    for index, card in enumerate(state.hand.cards):
        print(f"  [{index}] {card.key:<6} {card.label:<18} "
              f"suit={card.suit} rank={card.rank} set={card.set} "
              f"enh={card.enhancement} ed={card.edition} seal={card.seal} "
              f"debuff={card.debuff} 点数筹码={card.base_chips}")
    print(f"牌堆剩余 {len(state.draw_pile.cards)} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
