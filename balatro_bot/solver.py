"""Solver：把"不随选哪几张牌变化"的算分上下文固定下来，然后反复求解。

为什么单独抽一层
----------------
弃牌决策要对每个候选弃牌方案做蒙特卡洛采样：换一批牌 → 重新算最优出牌。
如果每次都重新构造 GameState，开销会大得没法用。Solver 只持有算分真正需要的
几个字段（小丑牌、牌型表、回合信息、牌堆组成），因此可以在同一个回合里
对上万种假设手牌反复求解。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import combinations, permutations
from typing import Callable, Iterable, Sequence

from .cards import Card
from .jokers import COPY_JOKERS, is_multiplicative
from .scoring import ScoreBreakdown, score_from_cards
from .state import GameState

MAX_PLAY_CARDS = 5


@dataclass(slots=True)
class Candidate:
    """一个候选打法及其算分。"""

    indices: tuple[int, ...]
    score: ScoreBreakdown

    @property
    def total(self) -> float:
        return self.score.total

    @property
    def hand_name(self) -> str:
        return self.score.hand_name

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"Candidate({list(self.indices)} -> {self.score.describe()})"


@dataclass(slots=True)
class Solver:
    """固定上下文下的出牌求解器。"""

    jokers: tuple[Card, ...]
    base_fn: Callable[[str], tuple[int, int]]
    hand_played: int = 0
    discards_left: int = 0
    hands_left: int = 1
    deck_size: int = 0
    money: int = 0
    deck_composition: tuple[Card, ...] = ()
    hands_played_this_round: frozenset[str] = frozenset()
    hand_plays_run: tuple[tuple[str, int], ...] = ()
    # Boss 盲注 The Flint 会把牌型的基础筹码和倍率减半
    base_scale: float = 1.0
    # 只允许这些牌型（The Eye / The Mouth）；None 表示不限制
    allowed_hands: frozenset[str] | None = None

    @classmethod
    def from_state(cls, state: GameState, **overrides) -> "Solver":
        hands = state.hands
        base = cls(
            jokers=tuple(state.jokers.cards),
            base_fn=state.hand_base,
            hand_played=state.round.hands_played,
            discards_left=state.round.discards_left,
            hands_left=state.round.hands_left,
            deck_size=len(state.draw_pile.cards),
            money=state.money,
            deck_composition=tuple(state.hand.cards) + tuple(state.draw_pile.cards),
            hands_played_this_round=frozenset(
                name for name, info in hands.items() if info.played_this_round > 0
            ),
            hand_plays_run=tuple((name, info.played) for name, info in hands.items()),
        )
        return replace(base, **overrides) if overrides else base

    def reordered(self, order: Sequence[int]) -> "Solver":
        """换一个槽位顺序的副本（用于搜索最优摆放）。"""
        return replace(self, jokers=tuple(self.jokers[i] for i in order))

    # ------------------------------------------------------------------ 算分

    def score(
        self,
        hand_cards: Sequence[Card],
        indices: Iterable[int],
        *,
        hand_name: str | None = None,
    ) -> ScoreBreakdown:
        return score_from_cards(
            hand_cards,
            indices,
            hand_base=self.base_fn,
            jokers=self.jokers,
            hand_played=self.hand_played,
            discards_left=self.discards_left,
            hands_left=self.hands_left,
            deck_size=self.deck_size,
            money=self.money,
            deck_composition=self.deck_composition,
            hand_name=hand_name,
            hands_played_this_round=self.hands_played_this_round,
            hand_plays_run=dict(self.hand_plays_run),
            base_scale=self.base_scale,
        )

    # ------------------------------------------------------------------ 搜索

    def enumerate(
        self,
        hand_cards: Sequence[Card],
        *,
        min_cards: int = 1,
        max_cards: int = MAX_PLAY_CARDS,
    ) -> list[Candidate]:
        """枚举所有合法出牌组合，按得分从高到低返回。

        手牌最多 8 张时是 218 种组合，直接全枚举。不做启发式剪枝，
        因为剪枝会漏掉"少打几张、把 K 留给 Baron"这类反直觉的最优解。
        """
        size = len(hand_cards)
        if size == 0:
            return []

        results: list[Candidate] = []
        for count in range(min_cards, min(max_cards, size) + 1):
            for combo in combinations(range(size), count):
                breakdown = self.score(hand_cards, combo)
                if breakdown.hand_name == "":
                    continue
                if self.allowed_hands is not None and breakdown.hand_name not in self.allowed_hands:
                    continue
                results.append(Candidate(indices=combo, score=breakdown))
        results.sort(key=lambda c: c.total, reverse=True)
        return results

    def best(self, hand_cards: Sequence[Card], **kwargs) -> Candidate | None:
        plays = self.enumerate(hand_cards, **kwargs)
        return plays[0] if plays else None

    def best_score(self, hand_cards: Sequence[Card], **kwargs) -> float:
        """只要分数，省掉排序开销；蒙特卡洛里会调用很多次。"""
        best = 0.0
        size = len(hand_cards)
        if size == 0:
            return 0.0
        max_cards = kwargs.get("max_cards", MAX_PLAY_CARDS)
        min_cards = kwargs.get("min_cards", 1)
        for count in range(min_cards, min(max_cards, size) + 1):
            for combo in combinations(range(size), count):
                breakdown = self.score(hand_cards, combo)
                if self.allowed_hands is not None and breakdown.hand_name not in self.allowed_hands:
                    continue
                if breakdown.total > best:
                    best = breakdown.total
        return best

    # ------------------------------------------------------------ 槽位优化

    def best_joker_order(
        self,
        hand_cards: Sequence[Card],
        *,
        min_gain: float = 1.02,
        max_jokers: int = 5,
        exhaustive_limit: int = 4,
    ) -> list[int] | None:
        """搜索能让得分最高的小丑牌槽位顺序。

        为什么顺序有影响：游戏按槽位从左到右逐个结算，加法和乘法交替进行，
        所以 (基础 + 加法) × 乘区 大于 (基础 × 乘区) + 加法。把加法牌放左边、
        乘区牌放右边是**完全免费的分数提升**，只需要调换位置。

        不需要穷举 5! 种排列：把所有加法牌稳定地排到乘区牌前面就是理论最优，
        所以先只评估这一个候选（外加复制类小丑牌存在时的小规模穷举，
        因为 Blueprint 复制右边那张，位置本身就是策略）。

        返回最优排列（下标数组），当前顺序已经够好时返回 None。
        """
        count = len(self.jokers)
        if count < 2 or count > max_jokers:
            return None

        current = tuple(range(count))
        has_copy = any(j.key in COPY_JOKERS for j in self.jokers)
        partitioned = self.additive_first_order()

        # 没有复制类小丑牌时，"加法在前、乘区在后"是**可证明不劣**的
        # （加法之间、乘法之间都可交换），所以直接应用，不需要比较分数。
        #
        # 这一点很关键：条件型乘区牌（例如 Card Sharp 只在本回合重复牌型时 x3）
        # 在回合**第一手**并不触发，此时重排的即时收益是 0——如果拿分数当门槛，
        # 就永远不会重排，白白丢掉后面几手的加分。
        if not has_copy:
            return list(partitioned) if partitioned != current else None

        # 复制类小丑牌（Blueprint 复制右边那张）的位置本身就是策略，只能比较
        candidates = [partitioned] if partitioned != current else []
        if count <= exhaustive_limit:
            candidates.extend(
                order for order in permutations(current)
                if order != current and order not in candidates
            )

        current_value = self.best_score(hand_cards)
        best_order = current
        best_value = current_value
        for order in candidates:
            value = self.reordered(order).best_score(hand_cards)
            if value > best_value:
                best_value = value
                best_order = order

        if best_order == current:
            return None
        if best_value < current_value * min_gain:
            return None
        return list(best_order)

    def additive_first_order(self) -> tuple[int, ...]:
        """把所有加法类小丑牌稳定地排到乘区类前面（保持各自的相对顺序）。"""
        additive = [i for i, j in enumerate(self.jokers) if not is_multiplicative(j.key)]
        multiplicative = [i for i, j in enumerate(self.jokers) if is_multiplicative(j.key)]
        return tuple(additive + multiplicative)
