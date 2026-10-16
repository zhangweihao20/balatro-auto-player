"""决策耗时基准。

弃牌决策要对每个候选方案做蒙特卡洛采样，每次采样都要重新求解最优出牌，
是最可能拖慢整个机器人的地方。这个脚本用接近真实的对局数据测一下单步耗时，
用来决定 discard_samples 取多大。

    python tools/benchmark.py
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from balatro_bot.solver import Solver  # noqa: E402
from balatro_bot.state import GameState  # noqa: E402
from balatro_bot.strategy import Config, choose_play_action  # noqa: E402

RANKS = "23456789TJQKA"
SUITS = "HDCS"
SUIT_NAMES = {"H": "Hearts", "D": "Diamonds", "C": "Clubs", "S": "Spades"}
RANK_CHIPS = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
              "T": 10, "J": 10, "Q": 10, "K": 10, "A": 11}
STANDARD_HANDS = {
    "Flush Five": (1, 160, 16), "Flush House": (2, 140, 14),
    "Five of a Kind": (3, 120, 12), "Straight Flush": (4, 100, 8),
    "Four of a Kind": (5, 60, 7), "Full House": (6, 40, 4), "Flush": (7, 35, 4),
    "Straight": (8, 30, 4), "Three of a Kind": (9, 30, 3), "Two Pair": (10, 20, 2),
    "Pair": (11, 10, 2), "High Card": (12, 5, 1),
}

counter = [0]


def make_card(key: str, *, enhancement=None, edition=None) -> dict:
    counter[0] += 1
    return {
        "id": counter[0], "key": key,
        "set": "ENHANCED" if enhancement else "DEFAULT",
        "label": key,
        "value": {"suit": key[0], "rank": key[2:], "effect": ""},
        "modifier": {"seal": None, "edition": edition, "enhancement": enhancement,
                     "eternal": False, "perishable": None, "rental": False},
        "state": {"debuff": False, "hidden": False, "highlight": False},
        "cost": {"sell": 1, "buy": 0},
    }


def make_joker(key: str) -> dict:
    counter[0] += 1
    return {
        "id": counter[0], "key": key, "set": "JOKER", "label": key,
        "value": {"effect": ""},
        "modifier": {"seal": None, "edition": None, "enhancement": None,
                     "eternal": False, "perishable": None, "rental": False},
        "state": {"debuff": False, "hidden": False, "highlight": False},
        "cost": {"sell": 2, "buy": 5},
    }


def build_state() -> GameState:
    rng = random.Random(7)
    deck_keys = [f"{s}_{r}" for s in SUITS for r in RANKS]
    rng.shuffle(deck_keys)
    hand = [make_card(k) for k in deck_keys[:8]]
    rest = [make_card(k) for k in deck_keys[8:]]
    jokers = [make_joker(k) for k in
              ("j_greedy_joker", "j_abstract", "j_baron", "j_blue_joker", "j_duo")]

    hands = {
        name: {"order": order, "level": 1, "chips": chips, "mult": mult,
               "played": 0, "played_this_round": 0}
        for name, (order, chips, mult) in STANDARD_HANDS.items()
    }
    return GameState({
        "state": "SELECTING_HAND", "round_num": 3, "ante_num": 4, "money": 14,
        "deck": "RED", "stake": "WHITE", "seed": "BENCH", "won": False,
        "used_vouchers": {}, "hands": hands,
        "round": {"hands_left": 3, "hands_played": 1, "discards_left": 3,
                  "discards_used": 0, "reroll_cost": 5, "chips": 0},
        "blinds": {"small": {"type": "SMALL", "status": "UPCOMING", "name": "Small Blind",
                             "effect": "", "score": 800},
                   "big": {"type": "BIG", "status": "UPCOMING", "name": "Big Blind",
                           "effect": "", "score": 1200},
                   "boss": {"type": "BOSS", "status": "CURRENT", "name": "The Hook",
                            "effect": "", "score": 20000}},
        "jokers": {"count": len(jokers), "limit": 5, "cards": jokers},
        "consumables": {"count": 0, "limit": 2, "cards": []},
        "hand": {"count": len(hand), "limit": 8, "cards": hand},
        "cards": {"count": len(rest), "limit": 0, "cards": rest},
        "shop": {"count": 0, "limit": 2, "cards": []},
        "vouchers": {"count": 0, "limit": 1, "cards": []},
        "packs": {"count": 0, "limit": 2, "cards": []},
        "pack": {"count": 0, "limit": 0, "cards": []},
    })


def main() -> int:
    state = build_state()
    solver = Solver.from_state(state)

    start = time.perf_counter()
    plays = solver.enumerate(state.hand.cards)
    enumerate_ms = (time.perf_counter() - start) * 1000
    print(f"枚举全部 {len(plays)} 种打法：{enumerate_ms:.1f} ms")
    print(f"最优：{plays[0].score.describe()}")

    start = time.perf_counter()
    for _ in range(20):
        solver.best_score(state.hand.cards)
    per_solve = (time.perf_counter() - start) / 20 * 1000
    print(f"单次求解（只要分数）：{per_solve:.2f} ms")

    if "--profile" in sys.argv:
        import cProfile
        import io
        import pstats

        profile = cProfile.Profile()
        profile.enable()
        choose_play_action(state, Config(discard_samples=20), random.Random(0))
        profile.disable()
        stream = io.StringIO()
        pstats.Stats(profile, stream=stream).sort_stats("tottime").print_stats(15)
        print(stream.getvalue())
        return 0

    if "--explain" in sys.argv:
        # 用很大的样本量把"真值"逼近出来，看看决策到底有多边缘
        from balatro_bot.strategy import (
            decision_rng,
            greedy_discard_order,
            sample_draws,
            expected_after_discard,
        )

        solver = Solver.from_state(state)
        best = solver.best(state.hand.cards)
        order = greedy_discard_order(solver, state.hand.cards)
        pool = state.draw_pile.cards
        max_discard = min(5, state.round.discards_left, len(state.hand.cards) - 1)
        print(f"当前最优：{best.score.describe()}")
        print(f"弃牌优先级（最该丢的在前）：{order}")
        for samples in (32, 256, 2048):
            rng = decision_rng(state)
            draws = sample_draws(rng, pool, max_discard, samples)
            print(f"--- 样本数 {samples} ---")
            for size in range(1, max_discard + 1):
                candidate = tuple(sorted(order[:size]))
                expected = expected_after_discard(solver, state.hand, candidate, draws)
                print(f"  弃 {size} 张 {candidate}：期望 {expected:10.0f}  "
                      f"（当前 {best.total:.0f}，需要 {best.total * 1.25:.0f}）")
        return 0

    # 不传 rng：真实运行时按局面确定性播种，同一个局面抽样序列一致。
    #
    # 注意：这里故意改变采样数，是为了观察"结论对采样数有多敏感"。
    # 当期望收益恰好落在 discard_min_gain 阈值附近时（用 --explain 可以看到具体
    # 数字），结论确实会被噪声左右——这是蒙特卡洛的固有限制，不是缺陷。
    # 实际运行时 discard_samples 是固定值，所以同一局面每次都会得到同一答案。
    for samples in (10, 20, 40):
        config = Config(discard_samples=samples)
        start = time.perf_counter()
        action = choose_play_action(state, config)
        elapsed = (time.perf_counter() - start) * 1000
        print(f"discard_samples={samples:3d}：一次出牌决策 {elapsed:7.1f} ms  决定 {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
