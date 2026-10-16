"""出牌搜索的便捷入口。

真正的实现在 :mod:`balatro_bot.solver`；这里保留按 GameState 直接调用的薄封装，
方便交互式调试和测试。
"""

from __future__ import annotations

from .solver import MAX_PLAY_CARDS, Candidate, Solver
from .state import GameState

__all__ = [
    "MAX_PLAY_CARDS",
    "Candidate",
    "Solver",
    "enumerate_plays",
    "best_play",
    "top_plays",
    "rank_by_hand_type",
    "keep_indices_for_best_play",
]


def enumerate_plays(
    state: GameState,
    *,
    min_cards: int = 1,
    max_cards: int = MAX_PLAY_CARDS,
) -> list[Candidate]:
    """枚举所有合法出牌组合，按得分从高到低返回。"""
    solver = Solver.from_state(state)
    return solver.enumerate(state.hand.cards, min_cards=min_cards, max_cards=max_cards)


def best_play(state: GameState, **kwargs) -> Candidate | None:
    """得分最高的打法。"""
    return Solver.from_state(state).best(state.hand.cards, **kwargs)


def top_plays(state: GameState, limit: int = 5, **kwargs) -> list[Candidate]:
    return enumerate_plays(state, **kwargs)[:limit]


def rank_by_hand_type(state: GameState) -> list[tuple[str, float]]:
    """每种牌型的最佳得分，用于日志和弃牌判断。"""
    solver = Solver.from_state(state)
    best: dict[str, float] = {}
    for candidate in solver.enumerate(state.hand.cards):
        current = best.get(candidate.hand_name, -1.0)
        if candidate.total > current:
            best[candidate.hand_name] = candidate.total
    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)


def keep_indices_for_best_play(state: GameState) -> set[int]:
    """最优打法用到的下标；弃牌时用来判断"哪些牌是有用的"。"""
    candidate = best_play(state)
    return set(candidate.indices) if candidate else set()
