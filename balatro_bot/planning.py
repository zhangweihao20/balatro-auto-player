"""凑牌型规划：决定"该弃哪几张、去凑哪个牌型"。

对应策略规格里的这一段：

    若最优牌型无法直接通关，则依据当前手牌、牌型倍率和牌库剩余卡牌策略性弃牌，
    以获取期望值（牌型综合倍率得分 - 惩罚项弃牌数量×系数）最高的牌型
    （凑出牌型的概率 + 需弃牌数量 + 牌型倍率，其中弃牌数量为惩罚项）。

实现拆成三块：

1. **凑出概率**：把"还需要几张、牌堆里还有几张能补上"算清楚，用超几何分布
   求"抽 d 张里至少出现 k 张所需牌"的概率。这是精确公式，不是蒙特卡洛估计，
   因此没有采样噪声（旧的蒙特卡洛实现保留在 strategy.py，可切换对比）。

2. **保留/弃掉哪些牌**：按目标牌型逐类推导（对子看点数、同花看花色、顺子看
   5 张连续窗口……），并遵守"一次最多弃 5 张"的限制。

3. **综合评分**：`value = w_prob·P + w_score·(牌型倍率归一化) − w_cost·弃牌数`。
   其中两项正项权重随底注变化，实现规格里的
   "前期更倾向高难度牌型、中后期更倾向易凑齐的低难度牌型"。
"""

from __future__ import annotations

import itertools
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Sequence

from .cards import RANKS, RANK_CHIPS, RANK_ORDER, SUITS, Card
from .solver import Solver

# 一次最多能弃几张（Balatro 默认上限）
MAX_DISCARD = 5

# 能凑出牌型的那些牌型，按"基础分从低到高"排列
PLANNABLE_HANDS: tuple[str, ...] = (
    "High Card", "Pair", "Two Pair", "Three of a Kind", "Straight", "Flush",
    "Full House", "Four of a Kind", "Straight Flush", "Five of a Kind",
)


@dataclass(frozen=True, slots=True)
class PlanWeights:
    """牌型排名用的权重。

    对应规格："利用特征：凑出牌型的概率 + 需弃牌数量 + 牌型倍率 + 当前小丑牌效果"。

    gamma 是"前期更倾向高难度牌型、中后期更倾向易凑齐的低难度牌型"
    那个参数：值越小越宽容低概率的大牌型，越大越要求稳定。

    discard_bonus 是**弃牌数量的系数**。规格明确说"弃牌数量不再是惩罚项"，
    所以这里是**正号**：弃掉几张就等于循环几张，弃得越多越早看到新牌。
    （Balatro 里"打出的张数 = 补牌张数"，弃牌同理，所以多弃是纯收益。）
    """

    gamma: float = 1.0
    discard_bonus: float = 0.05


@dataclass(slots=True)
class TargetPlan:
    """一个"去凑某个牌型"的计划。"""

    hand_name: str
    keep: tuple[int, ...]
    discard: tuple[int, ...]
    needed: int            # 还需要几张才能凑成
    outs: int              # 牌堆里能补上的张数
    probability: float     # 这一轮能凑成的概率
    base_score: float      # 牌型本身的基础分（筹码 × 倍率，不含小丑牌）
    completed_score: float  # 凑成之后那一手的**实际得分（含当前小丑牌效果）**
    value: float           # 综合评分

    @property
    def made(self) -> bool:
        return self.needed == 0

    def describe(self) -> str:  # pragma: no cover - 仅用于日志
        return (
            f"{self.hand_name}: P={self.probability:.2f} 还需{self.needed}张"
            f"(外面{self.outs}张) 弃{len(self.discard)}张 "
            f"基础分{self.base_score:.0f} 实际{self.completed_score:.0f} "
            f"→ 评分 {self.value:.3f}"
        )


# --------------------------------------------------------------- 概率


def hypergeometric_at_least(
    population: int, successes: int, draws: int, needed: int
) -> float:
    """从 population 张牌里抽 draws 张，至少抽到 needed 张"补牌"的概率。

    精确的超几何分布尾概率。补牌（successes）就是牌堆里能填上缺口的那些张。
    """
    if needed <= 0:
        return 1.0
    if successes <= 0 or draws <= 0 or needed > draws or needed > successes:
        return 0.0
    draws = min(draws, population)
    successes = min(successes, population)
    total = math.comb(population, draws)
    if total == 0:
        return 0.0
    miss = 0
    for hit in range(needed, min(successes, draws) + 1):
        rest = draws - hit
        if rest > population - successes:
            continue
        miss += math.comb(successes, hit) * math.comb(population - successes, rest)
    return min(1.0, miss / total)


# --------------------------------------------------------------- 手牌结构


def _rank_groups(hand: Sequence[Card]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for index, card in enumerate(hand):
        if card.rank is not None and not card.is_stone:
            groups[card.rank].append(index)
    return groups


def _suit_groups(hand: Sequence[Card]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for index, card in enumerate(hand):
        if card.suit and not card.is_stone and not card.is_wild:
            groups[card.suit].append(index)
    return groups


def _pile_rank_counts(pile: Sequence[Card]) -> Counter[str]:
    return Counter(c.rank for c in pile if c.rank and not c.is_stone)


def _pile_suit_counts(pile: Sequence[Card]) -> Counter[str]:
    return Counter(c.suit for c in pile if c.suit and not c.is_stone and not c.is_wild)


# --------------------------------------------------------------- 各牌型的计划


@dataclass(slots=True)
class _Sketch:
    """某个目标的草图：保留哪几张、还差几张、补牌从哪来。"""

    keep: list[int]
    needed: int
    out_ranks: frozenset[str] = frozenset()
    out_suit: str | None = None


def _sketch_pair(hand, ranks, pile_ranks) -> _Sketch:
    if not ranks:
        return _Sketch([], 2)
    # 优先手里已经多的；一样多时选牌堆里更容易补到的
    best = max(ranks, key=lambda r: (len(ranks[r]), pile_ranks.get(r, 0), RANK_ORDER[r]))
    held = ranks[best]
    if len(held) >= 2:
        return _Sketch(held[:2], 0)
    return _Sketch(held, 1, frozenset({best}))


def _sketch_same_rank(hand, ranks, pile_ranks, target: int) -> _Sketch:
    if not ranks:
        return _Sketch([], target)
    best = max(ranks, key=lambda r: (len(ranks[r]), pile_ranks.get(r, 0), RANK_ORDER[r]))
    held = ranks[best]
    if len(held) >= target:
        return _Sketch(held[:target], 0)
    return _Sketch(held, target - len(held), frozenset({best}))


def _sketch_two_pair(hand, ranks, pile_ranks) -> _Sketch:
    order = sorted(ranks, key=lambda r: (len(ranks[r]), pile_ranks.get(r, 0), RANK_ORDER[r]),
                   reverse=True)
    if not order:
        return _Sketch([], 4)
    first = order[0]
    held_first = ranks[first]

    if len(held_first) >= 2:
        # 已经有一对，再找第二个点数（手里有单张的优先）
        rest = [r for r in order[1:] if r != first]
        if not rest:
            return _Sketch(held_first[:2], 2, frozenset({first}), None)
        # 优先手里已经有一张的（只要再补一张）
        singles = [r for r in rest if len(ranks[r]) == 1]
        if singles:
            second = max(singles, key=lambda r: (pile_ranks.get(r, 0), RANK_ORDER[r]))
            keep = held_first[:2] + ranks[second]
            return _Sketch(keep, 1, frozenset({second}))
        second = max(rest, key=lambda r: (pile_ranks.get(r, 0), RANK_ORDER[r]))
        return _Sketch(held_first[:2], 2, frozenset({second}))

    # 还没有对子：挑两个最容易补到的点数
    pool = sorted(ranks, key=lambda r: (pile_ranks.get(r, 0), RANK_ORDER[r]), reverse=True)
    if len(pool) < 2:
        return _Sketch(ranks.get(first, []), 3, frozenset({first}))
    a, b = pool[0], pool[1]
    keep = ranks[a] + ranks[b]
    return _Sketch(keep, 2, frozenset({a, b}))


def _sketch_full_house(hand, ranks, pile_ranks) -> _Sketch:
    order = sorted(ranks, key=lambda r: (len(ranks[r]), pile_ranks.get(r, 0), RANK_ORDER[r]),
                   reverse=True)
    if len(order) < 2:
        if not order:
            return _Sketch([], 5)
        only = order[0]
        return _Sketch(ranks[only], 3, frozenset({only}))

    best: _Sketch | None = None
    best_cost = 99
    # 尝试前几个点数两两组合，取"还差张数最少"的
    for trip in order[:4]:
        for pair in order[:4]:
            if pair == trip:
                continue
            need = max(0, 3 - len(ranks[trip])) + max(0, 2 - len(ranks[pair]))
            if need < best_cost:
                best_cost = need
                keep = ranks[trip][:3] + ranks[pair][:2]
                outs = frozenset()
                if len(ranks[trip]) < 3:
                    outs |= {trip}
                if len(ranks[pair]) < 2:
                    outs |= {pair}
                best = _Sketch(keep, need, outs)
    return best or _Sketch([], 5)


def _sketch_flush(hand, suits, pile_suits) -> _Sketch:
    if not suits:
        return _Sketch([], 5)
    best = max(suits, key=lambda s: (len(suits[s]), pile_suits.get(s, 0)))
    held = suits[best]
    if len(held) >= 5:
        return _Sketch(held[:5], 0)
    return _Sketch(held, 5 - len(held), frozenset(), best)


def _straight_windows() -> list[tuple[str, ...]]:
    """所有 5 张连续点数的窗口，包含 A2345 这种"A 当 1"的情况。"""
    windows: list[tuple[str, ...]] = []
    for start in range(0, len(RANKS) - 4):
        windows.append(tuple(RANKS[start:start + 5]))
    windows.append(("A", "2", "3", "4", "5"))
    return windows


def _sketch_straight(hand, ranks, pile_ranks) -> _Sketch:
    best: _Sketch | None = None
    best_need = 99
    best_outs = -1
    for window in _straight_windows():
        held: list[int] = []
        missing: set[str] = set()
        for rank in window:
            if rank in ranks and ranks[rank]:
                held.append(ranks[rank][0])
            else:
                missing.add(rank)
        need = len(missing)
        if need > MAX_DISCARD:
            continue
        # 外面能补上的张数越多越好，作为同分时的次要标准
        outs = sum(pile_ranks.get(r, 0) for r in missing)
        if need < best_need or (need == best_need and outs > best_outs):
            best_need = need
            best_outs = outs
            best = _Sketch(held, need, frozenset(missing))
    return best or _Sketch([], 5)


def _sketch_straight_flush(hand, suits, ranks, pile) -> _Sketch:
    """同花顺：在同一花色里找顺子窗口。很少能成，概率自然很低。"""
    if not suits:
        return _Sketch([], 5)
    pile_by_suit: dict[str, Counter[str]] = defaultdict(Counter)
    for card in pile:
        if card.suit and card.rank and not card.is_stone:
            pile_by_suit[card.suit][card.rank] += 1

    best: _Sketch | None = None
    best_need = 99
    best_outs = -1
    for suit, indices in suits.items():
        held_ranks = {hand[i].rank: i for i in indices}
        for window in _straight_windows():
            held = [held_ranks[r] for r in window if r in held_ranks]
            missing = {r for r in window if r not in held_ranks}
            need = len(missing)
            if need > MAX_DISCARD:
                continue
            outs = sum(pile_by_suit[suit].get(r, 0) for r in missing)
            if need < best_need or (need == best_need and outs > best_outs):
                best_need = need
                best_outs = outs
                best = _Sketch(held, need, frozenset(missing), suit)
    return best or _Sketch([], 5)


def _sketch_high_card(hand) -> _Sketch:
    if not hand:
        return _Sketch([], 0)
    best = max(range(len(hand)), key=lambda i: hand[i].nominal)
    return _Sketch([best], 0)


# --------------------------------------------------------------- 组装计划


def _build_sketch(hand_name: str, hand, pile) -> _Sketch:
    ranks = _rank_groups(hand)
    suits = _suit_groups(hand)
    pile_ranks = _pile_rank_counts(pile)
    pile_suits = _pile_suit_counts(pile)

    if hand_name == "High Card":
        return _sketch_high_card(hand)
    if hand_name == "Pair":
        return _sketch_pair(hand, ranks, pile_ranks)
    if hand_name == "Two Pair":
        return _sketch_two_pair(hand, ranks, pile_ranks)
    if hand_name == "Three of a Kind":
        return _sketch_same_rank(hand, ranks, pile_ranks, 3)
    if hand_name == "Four of a Kind":
        return _sketch_same_rank(hand, ranks, pile_ranks, 4)
    if hand_name == "Five of a Kind":
        return _sketch_same_rank(hand, ranks, pile_ranks, 5)
    if hand_name == "Full House":
        return _sketch_full_house(hand, ranks, pile_ranks)
    if hand_name == "Flush":
        return _sketch_flush(hand, suits, pile_suits)
    if hand_name == "Straight":
        return _sketch_straight(hand, ranks, pile_ranks)
    if hand_name == "Straight Flush":
        return _sketch_straight_flush(hand, suits, ranks, pile)
    return _Sketch([], 5)


def _order_discards(sketch: _Sketch, hand, max_discard: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """把"不保留的牌"排个序，挑出最该弃掉的 max_discard 张。

    如果非保留牌比能弃的上限还多，就要在里面再挑——优先留下和目标的
    "补牌特征"一致的（同花色 / 同点数），因为它们还有可能变成补牌。
    """
    keep_set = set(sketch.keep)
    candidates = [i for i in range(len(hand)) if i not in keep_set]

    def affinity(index: int) -> tuple:
        card = hand[index]
        same_suit = 1 if (sketch.out_suit and card.suit == sketch.out_suit) else 0
        same_rank = 1 if (card.rank in sketch.out_ranks) else 0
        return (same_rank + same_suit, card.nominal)

    # affinity 高 = 更值得留 = 排后面
    candidates.sort(key=affinity)
    discard = tuple(sorted(candidates[:max_discard]))
    keep = tuple(sorted(set(range(len(hand))) - set(discard)))
    return keep, discard


def top_base_score(solver: Solver) -> float:
    """所有可凑牌型里最高的基础分。用于"难度参考"和归一化。"""
    best = 1.0
    for name in PLANNABLE_HANDS:
        chips, mult = solver.base_fn(name)
        best = max(best, float(chips * mult))
    return best


def hand_base_score(solver: Solver, hand_name: str) -> float:
    """某个牌型的基础分（筹码 × 倍率，含等级加成，不含小丑牌）。"""
    chips, mult = solver.base_fn(hand_name)
    return float(chips * mult)


def plan_value(
    probability: float,
    completed_score: float,
    current_score: float,
    discards: int,
    weights: PlanWeights,
) -> float:
    """一个凑牌计划的评分（越大越值得去凑）。

    ```
    评分 = P^γ × log₂(1 + 实际得分/当前得分) + β × 弃牌张数
    ```

    四项特征对应规格里的四样东西：

    | 规格 | 实现 |
    | --- | --- |
    | 凑出牌型的概率 | `P`，精确超几何概率 |
    | 牌型倍率 + **当前小丑牌效果** | `completed_score`（用真实算分引擎算出的实际得分） |
    | 需弃牌数量 | `+ β × discards`，**正号**（规格："不再是惩罚项"） |

    为什么概率要取幂而不是并列相加：纯加法形式下"0% 概率的五条
    （基础分 1440）"会和"55% 概率的对子"打平甚至胜出——实测的后果是
    机器人为了 6% 的同花顺**弃掉已经成型的顺子**。概率以幂次作用于得分
    才是"期望值"该有的形状（规格里用的正是"期望值"这个词）。

    为什么对得分取 `log₂`：牌型基础分是重尾分布（高牌 5、五条 1440），
    线性比值会让罕见牌型一枝独秀。

    β 取正号的含义：**弃牌本身是收益**——Balatro 里补牌张数等于弃牌张数，
    多弃一张就等于多循环一张，更早看到需要的牌。这同时也解释了
    "优先通过弃牌来过渡"这条策略。
    """
    if current_score <= 0:
        ratio = 1.0
    else:
        ratio = max(1.0, completed_score / current_score)
    scaled = math.log2(1.0 + ratio)
    powered = probability ** max(0.05, weights.gamma) if probability > 0 else 0.0
    return powered * scaled + weights.discard_bonus * discards


# ------------------------------------------- 凑成之后的**实际**得分
#
# 规格要求评分把"当前小丑牌效果"算进去。做法不是在这里重写一遍小丑牌逻辑，
# 而是：**把"凑成之后的那手牌"造出来，交给已经过真机逐手验证的算分引擎**
# （Solver.score → scoring.score_from_cards）。这样小丑牌效果、版本、强化、
# 以及留在手里的牌触发的效果（Baron / 钢牌）全都自动算进去，
# 而且和真实得分是同一个来源——不会出现"规划层算一套、出牌时算另一套"。
#
# 举例：手里有 Jolly（有对子时 +8 倍率）和 The Duo（有对子时 x2）时，
# 造出一副"对子"算出来的得分会显著高于它的基础分（10×2），
# 于是专精对子变得明显更值钱——这正是"当前小丑牌效果"该起的作用。

_FABRICATED_ID = itertools.count(10_000_000)


def _fabricated_card(rank: str, suit: str) -> Card:
    """造一张用于估算的牌（不进入真实对局，只用来问算分引擎"这样能得多少分"）。"""
    key = f"{suit}_{rank}"
    return Card(
        id=next(_FABRICATED_ID), key=key, set="DEFAULT", label=key,
        suit=suit, rank=rank,
        enhancement=None, edition=None, seal=None,
        debuff=False, hidden=False, buy_cost=0, sell_cost=0, effect="",
    )


def _spare_suit(hand: Sequence[Card], keep: Sequence[int]) -> str:
    """挑一个手里最少的花色来造补牌，避免把牌型意外变成同花。"""
    kept = set(keep)
    counts = Counter(c.suit for i, c in enumerate(hand) if i in kept and c.suit)
    return min(SUITS, key=lambda suit: counts.get(suit, 0))


def _fabricate_missing(hand: Sequence[Card], sketch: _Sketch) -> list[Card]:
    """按缺口造出"还差的那几张"。"""
    if sketch.needed <= 0:
        return []
    made: list[Card] = []
    if sketch.out_ranks:
        ranks = sorted(sketch.out_ranks, key=lambda r: RANK_ORDER[r], reverse=True)
        for index in range(sketch.needed):
            made.append(_fabricated_card(
                ranks[index % len(ranks)],
                sketch.out_suit or _spare_suit(hand, sketch.keep),
            ))
    elif sketch.out_suit:
        used = {hand[i].rank for i in sketch.keep}
        pool = [r for r in reversed(RANKS) if r not in used] or list(reversed(RANKS))
        for index in range(sketch.needed):
            made.append(_fabricated_card(pool[index % len(pool)], sketch.out_suit))
    else:
        for index in range(sketch.needed):
            made.append(_fabricated_card(RANKS[-(index % 5) - 2], SUITS[index % 4]))
    return made


def completion_layout(
    hand: Sequence[Card],
    sketch: _Sketch,
    discard: Sequence[int],
) -> tuple[list[Card], list[Card]]:
    """造出"弃牌补牌之后、打出这一手"的完整局面。

    返回 (打出去的那几张, 留在手里的那几张)。

    新手牌 = 原来的手牌去掉弃掉的 + 补进来的牌。补进来的牌里，
    一部分是凑成牌型必需的（made），其余是多抽但不计分的填充牌（filler）。
    """
    kept = [hand[i] for i in sketch.keep]
    made = _fabricate_missing(hand, sketch)
    played = kept + made

    discard_set = set(discard)
    keep_set = set(sketch.keep)
    held = [c for i, c in enumerate(hand) if i not in discard_set and i not in keep_set]

    fillers = max(0, len(discard) - len(made))
    for index in range(fillers):
        held.append(_fabricated_card(RANKS[index % 5], SUITS[(index + 1) % 4]))
    return played, held


def completed_score(
    solver: Solver,
    hand: Sequence[Card],
    sketch: _Sketch,
    plan_name: str,
    discard: Sequence[int],
) -> float:
    """凑成这个牌型之后，这一手**实际能得多少分**（含当前小丑牌效果）。

    直接调 `Solver.score`，并把牌型名强制成目标牌型——这样基础筹码/倍率
    取的是该牌型当前等级的数值，而小丑牌、版本、强化、留手牌效果由算分引擎
    照常结算。返回 0 表示这个局面算不出分（不该发生，兜底而已）。
    """
    played, held = completion_layout(hand, sketch, discard)
    if not played:
        return 0.0
    new_hand = list(played) + list(held)
    indices = tuple(range(len(played)))
    breakdown = solver.score(new_hand, indices, hand_name=plan_name)
    return breakdown.total


def plan_miss_score(solver: Solver, hand: Sequence[Card], plan: TargetPlan) -> float:
    """弃牌但**没凑成**时，手里剩下的牌最好能打多少分。

    不能拿"现在就打"的分数当兜底——牌已经弃掉了，原来那手成牌也就没了。
    实测这个过于乐观的兜底会让机器人为赌 26% 的同花顺**弃掉已经成型的顺子**
    （260 分的顺子弃掉后只剩 3 张，实际只能打十几分）。

    所以这里老老实实算：拿保留下来的那几张牌，求它们自己能打出的最高分。
    """
    kept = [hand[i] for i in plan.keep if i < len(hand)]
    if not kept:
        return 0.0
    best = solver.best(kept)
    return best.total if best is not None else 0.0


def plan_expected_score(plan: TargetPlan, miss_score: float) -> float:
    """弃牌去凑这个牌型之后的**期望得分**。

    `P × 凑成后的实际得分 + (1−P) × 弃牌后残余牌力的得分`

    `miss_score` 必须由 `plan_miss_score` 算出来传入，不能用"现在就打"的分数
    （原因见那个函数的说明）。这个量用于"弃牌还是出牌"的最终判断：
    只比较规划评分是不够的，因为"打一手高牌"的评分不差，但它根本过不了关。
    """
    return plan.probability * plan.completed_score + (1.0 - plan.probability) * miss_score


def plan_targets(
    solver: Solver,
    hand: Sequence[Card],
    pile: Sequence[Card],
    *,
    max_discard: int = MAX_DISCARD,
    weights: PlanWeights | None = None,
    allowed_hands: frozenset[str] | None = None,
    current_score: float = 0.0,
) -> list[TargetPlan]:
    """为每个可凑的牌型生成计划，并按评分从高到低返回。

    current_score 是"现在就打"那一手的实际得分（含小丑牌效果）。
    它出现在评分公式的比值里：去凑一个比现在强多少倍的牌型。

    评分用到的 `completed_score` 是**造出凑成后的牌、交给真实算分引擎**得到的
    实际得分，所以"当前小丑牌效果"是直接算进去的，而不是靠静态表近似。
    """
    weights = weights or PlanWeights()
    if not hand:
        return []

    sketches: dict[str, _Sketch] = {}
    for name in PLANNABLE_HANDS:
        if allowed_hands is not None and name not in allowed_hands:
            continue
        sketch = _build_sketch(name, hand, pile)
        if sketch.needed > max_discard:
            continue
        sketches[name] = sketch

    if not sketches:
        return []

    plans: list[TargetPlan] = []
    for name, sketch in sketches.items():
        chips, mult = solver.base_fn(name)
        base_score = float(chips * mult)

        if sketch.needed == 0:
            # 已经是成牌：直接打出去，不弃牌，概率记 1
            actual = completed_score(solver, hand, sketch, name, ())
            plans.append(TargetPlan(
                hand_name=name, keep=tuple(sketch.keep), discard=(),
                needed=0, outs=0, probability=1.0,
                base_score=base_score, completed_score=actual,
                value=plan_value(1.0, actual, current_score, 0, weights),
            ))
            continue

        keep, discard = _order_discards(sketch, hand, max_discard)
        drawn = len(discard)
        outs = _count_outs(pile, sketch)
        probability = (
            0.0 if sketch.needed > drawn
            else hypergeometric_at_least(len(pile), outs, drawn, sketch.needed)
        )
        actual = completed_score(solver, hand, sketch, name, discard)
        plans.append(TargetPlan(
            hand_name=name, keep=keep, discard=discard,
            needed=sketch.needed, outs=outs, probability=probability,
            base_score=base_score, completed_score=actual,
            value=plan_value(probability, actual, current_score, drawn, weights),
        ))

    plans.sort(key=lambda plan: plan.value, reverse=True)
    return plans


def _count_outs(pile: Sequence[Card], sketch: _Sketch) -> int:
    """牌堆里能补上缺口的张数。"""
    if sketch.needed <= 0:
        return 0
    total = 0
    for card in pile:
        if card.is_stone:
            continue
        if sketch.out_suit and card.suit == sketch.out_suit and not card.is_wild:
            total += 1
        elif sketch.out_ranks and card.rank in sketch.out_ranks:
            total += 1
    return total


# --------------------------------------------------- 自动生成初始参数


def _synthetic_deck() -> list[str]:
    return [f"{suit}_{rank}" for suit in SUITS for rank in RANKS]


def _make_card(key: str, index: int) -> Card:
    return Card(
        id=index, key=key, set="DEFAULT", label=key,
        suit=key[0], rank=key[2:],
        enhancement=None, edition=None, seal=None,
        debuff=False, hidden=False, buy_cost=0, sell_cost=0, effect="",
    )


def _distinct_rank_indices(
    deck: Sequence[Card], used: set[int], needed: int
) -> list[int]:
    """从没用过的牌里挑 needed 张，**每个点数最多一张**。

    每个点数只用一张，是因为给"对子"垫上另外两张同点牌，整手就变成四条了，
    代表性手牌也就失去意义（会误导按点数/牌型计数的小丑牌估值）。
    """
    if needed <= 0:
        return []
    chosen: list[int] = []
    taken_ranks = {deck[index].rank for index in used}
    for rank in sorted(
        (r for r in RANKS if r not in taken_ranks),
        key=lambda r: RANK_ORDER.get(r, 0),
        reverse=True,
    ):
        for index in range(len(deck)):
            if index in used or deck[index].rank != rank:
                continue
            chosen.append(index)
            break
        if len(chosen) >= needed:
            break
    return chosen


def representative_hand(
    deck: Sequence[Card],
    hand_name: str,
    *,
    play_size: int = 5,
    hold_size: int = 3,
    prefer_rank: str | None = None,
) -> tuple[list[Card], list[Card]]:
    """从一副牌里挑出"该牌型最有代表性的一手"，返回 (打出的牌, 留手的牌)。

    用途：**在商店里给阵容估值**。商店阶段 `state.hand` 是空的，但 mod 会把
    整副牌报在 `cards` 区（`G.deck`），所以可以直接从里面挑一副像样的手牌，
    当作"换了这张小丑牌之后，我大概能打多少分"的打分场景。

    有了场景，就能用**已经过真机验证的算分引擎**去量每张小丑牌的真实贡献，
    而不是靠离线写死的静态表——静态表不知道我们打什么牌型、牌组是什么，
    这正是"筹码丑反超倍率丑"和"引擎不认的牌赖着不走"的根源。

    `prefer_rank` 用来指定锚点点数（例如强制用 K 而不是 A），配合
    `representative_hands` 做多锚点覆盖。
    """
    if not deck or not hand_name:
        return [], []

    # 用"偏好点数的牌"当假想补牌堆，把草图的平局判定引到想要的锚点上
    # （草图内部按 (手里张数, 外面能补几张, 点数高低) 排序）。
    pile = [card for card in deck if prefer_rank and card.rank == prefer_rank]
    sketch = _build_sketch(hand_name, deck, pile)
    used = set(sketch.keep)

    # 纯同花场景要挑**点数最高**的同花牌，而不是草图给的任意几张。
    # 草图在"已经成同花"时只返回前 5 张（花色记在 out_suit 上的只有"还差几张"
    # 的情况），拿到的往往是 2-3-4-5-6 这种一张人头牌都没有的同花，
    # 会让"人头牌相关"的小丑牌（照片、笑脸、吓人脸、男爵……）在估值里
    # 一律显示成 0 贡献，于是被错误地卖掉。
    if hand_name == "Flush":
        suits: dict[str, list[int]] = {}
        for index, card in enumerate(deck):
            if card.suit and not card.is_stone and not card.is_wild:
                suits.setdefault(card.suit, []).append(index)
        if suits:
            best_suit = max(
                suits.values(),
                key=lambda idxs: (len(idxs), sum(deck[i].nominal for i in idxs)),
            )
            by_nominal = sorted(best_suit, key=lambda i: deck[i].nominal, reverse=True)
            used = set(by_nominal[:max(len(used), play_size)])

    # 补齐牌型缺口。草图是按"手上 8 张、其余靠抽"设计的，整副牌摆在那里时
    # 会低估该留几张（例如每个点数都有 4 张时，"两对"只会留下其中一对）。
    # 商店里我们有整副牌，直接把缺口补上。
    if sketch.needed > 0:
        target = len(used) + sketch.needed
        for index in range(len(deck)):
            if len(used) >= target:
                break
            if index in used:
                continue
            card = deck[index]
            if sketch.out_ranks and card.rank in sketch.out_ranks:
                used.add(index)
            elif sketch.out_suit and card.suit == sketch.out_suit:
                used.add(index)

    # 顺子同理：草图会取最靠前的窗口（2-3-4-5-6），一张人头牌都没有。
    # 改成点数最高的窗口（T-J-Q-K-A），更贴近真实会去打的那一手。
    if hand_name in ("Straight", "Straight Flush"):
        best_window = max(
            _straight_windows(),
            key=lambda window: sum(RANK_ORDER.get(rank, 0) for rank in window),
        )
        if hand_name == "Straight Flush":
            suits: dict[str, list[int]] = {}
            for index, card in enumerate(deck):
                if card.suit and not card.is_stone and not card.is_wild:
                    suits.setdefault(card.suit, []).append(index)
            suit = max(suits, key=lambda s: len(suits[s])) if suits else None
        else:
            suit = None
        picked: list[int] = []
        for rank in best_window:
            for index, card in enumerate(deck):
                if card.rank != rank or index in picked:
                    continue
                if suit is not None and card.suit != suit:
                    continue
                picked.append(index)
                break
        if len(picked) == len(best_window):
            used = set(picked)

    played = [deck[index] for index in sorted(used)]

    filler = _distinct_rank_indices(deck, used, play_size - len(played))
    played += [deck[index] for index in filler]
    used |= set(filler)

    held = [deck[index] for index in _distinct_rank_indices(deck, used, hold_size)]
    return played, held


def representative_hands(
    deck: Sequence[Card], hand_name: str, **kwargs
) -> list[tuple[list[Card], list[Card]]]:
    """多个打分场景（不同锚点），避免系统性偏袒某一类小丑牌。

    只用一个锚点会出错：对子锚点默认落在 **A** 上，而 Balatro 里 A 不算人头牌
    → 场景里一张人头牌都没有 → "每个人头牌 +5 倍率"的笑脸、照片、男爵
    全都显示成 0 贡献，于是被当成废牌卖掉（实测就是这样）。

    所以用两个锚点：「最高点数」和「最高人头牌」，估值时取两者的**平均倍数**。
    """
    scenarios: list[tuple[list[Card], list[Card]]] = []
    for prefer in (None, "K"):
        played, held = representative_hand(deck, hand_name, prefer_rank=prefer, **kwargs)
        if played and (played, held) not in scenarios:
            scenarios.append((played, held))
    return scenarios


def strategy_switch(
    solver: Solver,
    committed_hand: str | None,
    reference_base: float,
    *,
    switch_point: float = 0.8,
    sharpness: float = 0.25,
) -> float:
    """前后期策略的混合比例（0=完全前期，1=完全中后期）。

    规格：

    > 前期因为低难度牌型倍率较低所以才会更倾向于打出高难度牌型来快速通关；
    > 在过渡期需拟定最终 build 打牌方向，通过各种手段提高牌型的期望值；
    > **当牌型的期望值超出一般高难度牌型时可切换出牌策略**。

    所以切换依据**不是底注**（原来的 phase_pivot 是拍脑袋的），而是
    "专精牌型的得分有没有追平一般高难度牌型"。参考值 `reference_base`
    取 Flush / Straight / Full House 这类"一般高难度牌型"的基础分。

    举例（参考值取同花 140）：
      * 对子 1 级  → 10×2 = 20      → 20/140 = 0.14 → 混合比例≈0（前期，继续追大牌）
      * 对子 10 级 → 约 145×11 ≈ 1600 → 11.4    → 混合比例≈1（已切换，专精小牌型）
    也就是说：**星球牌和对应小丑牌把对子喂起来之后，策略自动切换**，
    不需要人工指定"第几底注开始换打法"。
    """
    if not committed_hand or reference_base <= 0:
        return 0.0
    progress = hand_base_score(solver, committed_hand) / reference_base
    x = (progress - switch_point) / max(sharpness, 1e-6)
    return 1.0 / (1.0 + math.exp(-x))


def derive_initial_discard_bonus(
    samples: int = 120, seed: int = 20240612
) -> float:
    """自动生成"弃牌数量系数"的初始值（规格里它从惩罚项改成了奖励项）。

    含义必须说清楚：**多弃一张牌值多少分**。多弃一张就多抽一张，凑成机会变大，
    所以系数应该是"多抽一张带来的完成概率增量"，换算到评分公式的同一尺度
    （评分里的得分项是 `log₂(1 + 凑成分/当前分)`）：

        β ≈ 0.5 × median[ (P(d+1) − P(d)) × log₂(1 + 凑成分/当前分) ]

    那个 0.5 是**阻尼**：上面算的是"多抽一张"的边际价值，而公式按张线性累加。
    真实收益是递减的（第一张补牌比第五张值钱），所以取边际值的一半当作
    每张的平均贡献。这样 β 和评分公式同源：公式换了，β 自动跟着变。
    （早先的版本在测量时把系数本身设成 0，测到的其实是"不同计划之间的强弱差"，
    和弃牌张数无关——那是个概念错误，已改正。）
    """
    import random

    from .cards import BASE_HAND_SCORE

    rng = random.Random(seed)
    deck = _synthetic_deck()
    solver = Solver(jokers=(), base_fn=lambda name: BASE_HAND_SCORE.get(name, (5, 1)))
    weights = PlanWeights(gamma=1.0, discard_bonus=0.0)

    margins: list[float] = []
    for _ in range(samples):
        rng.shuffle(deck)
        hand = [_make_card(key, i) for i, key in enumerate(deck[:8])]
        pile = [_make_card(key, 100 + i) for i, key in enumerate(deck[8:])]
        if not pile:
            continue

        current = solver.best(hand)
        if current is None or current.total <= 0:
            continue

        plans = plan_targets(
            solver, hand, pile, max_discard=MAX_DISCARD,
            weights=weights, current_score=current.total,
        )
        chase = [p for p in plans if p.discard]
        if not chase:
            continue

        plan = chase[0]
        drawn = len(plan.discard)
        if drawn >= MAX_DISCARD:
            continue                       # 已经弃满，没有"再多弃一张"可言
        extra = hypergeometric_at_least(
            len(pile), plan.outs, drawn + 1, plan.needed
        )
        ratio = max(1.0, plan.completed_score / current.total)
        margins.append((extra - plan.probability) * math.log2(1.0 + ratio))

    if not margins:
        return 0.0
    margins.sort()
    median = margins[len(margins) // 2]
    return round(min(0.5, max(0.0, median * 0.5)), 4)


def derive_initial_discard_aversion(samples: int = 120, seed: int = 20240612) -> float:
    """自动生成"弃牌门槛"的初始值。

    做法（不是拍脑袋的常数）：
      1. 用标准 52 张牌随机发 samples 副 8 张手牌（不需要游戏在跑）；
      2. 对每副手牌算"弃牌去凑最佳牌型"相对于"现在就打"的**相对期望提升**：
         `(P·凑成分 + (1−P)·当前分) / 当前分 − 1`
      3. 取这些提升的**第 10 百分位 × 0.25**，夹在 [0.1, 0.8] 内。

    为什么不用中位数：相对期望提升是重尾分布——实测第 10 百分位就有 1.10、
    中位数 5.34（散牌当前分太低，随便凑个牌型都是好几倍）。取中位数的任何
    比例都会顶到上限，门槛就失去意义了。取偏下分位再打对折，含义是
    "**只有连较差的机会都比不上时才不弃牌**"，初值自然落在 0.2~0.3。

    初始值随评分公式和权重自动变化，不需要人工重调；
    之后由 tools/tune.py 用真机对局数据继续修正。
    """
    import random

    from .cards import BASE_HAND_SCORE

    rng = random.Random(seed)
    deck = _synthetic_deck()
    solver = Solver(jokers=(), base_fn=lambda name: BASE_HAND_SCORE.get(name, (5, 1)))
    weights = PlanWeights(gamma=1.0, discard_bonus=0.0)

    gains: list[float] = []
    for _ in range(samples):
        rng.shuffle(deck)
        hand = [_make_card(key, i) for i, key in enumerate(deck[:8])]
        pile = [_make_card(key, 100 + i) for i, key in enumerate(deck[8:])]

        current = solver.best(hand)
        if current is None or current.total <= 0 or not pile:
            continue

        plans = plan_targets(
            solver, hand, pile, max_discard=MAX_DISCARD,
            weights=weights, current_score=current.total,
        )
        chase = [p for p in plans if p.discard]
        if not chase:
            continue

        plan = chase[0]
        expected = plan_expected_score(plan, plan_miss_score(solver, hand, plan))
        gains.append(expected / current.total - 1.0)

    if not gains:
        return 0.25
    gains.sort()
    low = gains[int(len(gains) * 0.10)]
    return round(min(0.8, max(0.1, low * 0.25)), 4)
