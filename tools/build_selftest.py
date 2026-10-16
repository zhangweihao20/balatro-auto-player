"""离线自检：流派识别在各种阵容下是否给出合理结果。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))

from conftest import card, hands_table, joker, make_state  # noqa: E402
from test_strategy import full_deck, with_area  # noqa: E402

from balatro_bot.build import archetype_scores, commit_archetype  # noqa: E402
from balatro_bot.state import GameState  # noqa: E402

CASES = [
    ("只有牌型丑 j_jolly", ["j_jolly"], []),
    ("只有男爵 j_baron", ["j_baron"], []),
    ("男爵 + 牌型丑", ["j_baron", "j_jolly"], []),
    ("钢铁丑 + 牌库 6 张 STEEL", ["j_steel_joker"],
     [card(f"H_{r}", enhancement="STEEL") for r in "234567"]),
    ("钢铁丑 + 牌库无 STEEL", ["j_steel_joker"], []),
    ("玻璃丑 + 2 张 GLASS", ["j_glass"],
     [card("H_2", enhancement="GLASS"), card("H_3", enhancement="GLASS")]),
    ("经济丑 j_bull", ["j_bull"], []),
    ("Splash 单独", ["j_splash"], []),
    ("人头丑 + 3 张改造过的人头", ["j_smiley"],
     [card(f"H_{r}", enhancement="BONUS") for r in ("K", "Q", "J")]),
    ("没有小丑牌", [], []),
]


def state_for(jokers, extra_deck) -> GameState:
    deck = (extra_deck + [c for c in full_deck()
                          if c["key"] not in {e["key"] for e in extra_deck}])[:52]
    raw = make_state(state="SHOP", jokers=[joker(k) for k in jokers],
                     hands=hands_table())
    raw["cards"] = {"count": len(deck), "limit": len(deck), "cards": list(deck)}
    for area, limit in (("discard", 500), ("play", 5), ("consumables", 2),
                        ("shop", 2), ("vouchers", 1)):
        with_area(raw, area, [], limit=limit)
    return GameState(raw)


def main() -> int:
    committed_prev = None
    for label, jokers, extra in CASES:
        scores = archetype_scores(state_for(jokers, extra))
        committed = commit_archetype(scores, None)
        shown = "  ".join(
            f"{s.label}={s.support:.2f}(阵{s.lineup_share:.2f}+库{s.deck_invest:.2f})"
            for s in scores[:3]
        ) or "（无候选）"
        mark = "✓" if committed else "·未承诺"
        print(f"  {label:<26} → {mark:<8} {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
