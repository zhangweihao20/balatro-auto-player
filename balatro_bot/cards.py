"""牌对象与牌型判定。

牌型名字必须和游戏里 G.GAME.hands 的 key 完全一致，否则读不到基础分。
这里用 Balatro 的原始英文名（本项目的 score 表来自状态里的 hands 字段，
所以名字对不上会立刻被测试发现）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

RANKS = "23456789TJQKA"
SUITS = "HDCS"

SUIT_NAMES = {"H": "Hearts", "D": "Diamonds", "C": "Clubs", "S": "Spades"}
RANK_ORDER = {rank: index for index, rank in enumerate(RANKS)}
FACE_RANKS = frozenset("JQK")

# 每张牌贡献的筹码（A 记 11 点）
RANK_CHIPS: dict[str, int] = {
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "T": 10, "J": 10, "Q": 10, "K": 10, "A": 11,
}

# 牌型从强到弱。顺序同时用于"哪个牌型更好"的兜底比较。
HAND_NAMES: tuple[str, ...] = (
    "Flush Five",
    "Flush House",
    "Five of a Kind",
    "Straight Flush",
    "Four of a Kind",
    "Full House",
    "Flush",
    "Straight",
    "Three of a Kind",
    "Two Pair",
    "Pair",
    "High Card",
)

# Balatro 的 Card:get_nominal() 用来排序（例如判断"高牌"里哪一张最高）。
# 点数之外还有花色和人头的微小权重，数值取自 card.lua：
#     点数 2..10，J/Q/K = 10，A = 11
#     花色 方块 0.01 < 梅花 0.02 < 红桃 0.03 < 黑桃 0.04
#     人头 J 0.1 < Q 0.2 < K 0.3 < A 0.4
SUIT_NOMINAL: dict[str, float] = {"D": 0.01, "C": 0.02, "H": 0.03, "S": 0.04}
FACE_NOMINAL: dict[str, float] = {"J": 0.1, "Q": 0.2, "K": 0.3, "A": 0.4}

# 兜底的 1 级基础分（当状态里读不到 hands 表时使用）。
# 游戏内 hands[name].chips / .mult 是含等级加成的绝对值，正常情况下优先用那个。
BASE_HAND_SCORE: dict[str, tuple[int, int]] = {
    "High Card": (5, 1),
    "Pair": (10, 2),
    "Two Pair": (20, 2),
    "Three of a Kind": (30, 3),
    "Straight": (30, 4),
    "Flush": (35, 4),
    "Full House": (40, 4),
    "Four of a Kind": (60, 7),
    "Straight Flush": (100, 8),
    "Five of a Kind": (120, 12),
    "Flush House": (140, 14),
    "Flush Five": (160, 16),
}

# 每个牌型**每升一级**的增量 (筹码, 倍率)。
# 来源：game.lua 的 G.GAME.hands 定义里的 l_chips / l_mult
#       （用 tools/extract_hand_levels.py 可重新导出并校验）。
#
# 游戏里的等级换算公式是（functions_common_events.lua 的 level_up_hand）：
#     chips = max(s_chips + l_chips * (level - 1), 0)
#     mult  = max(s_mult  + l_mult  * (level - 1), 1)
#
# 机器人需要它来建模 **The Arm**（打出的牌型等级 -1，且降级发生在算分之前）：
# 状态里给的是当前等级的绝对值，减一级就是各自减去这里的增量。
HAND_LEVEL_STEP: dict[str, tuple[int, int]] = {
    "High Card": (10, 1),
    "Pair": (15, 1),
    "Two Pair": (20, 1),
    "Three of a Kind": (20, 2),
    "Straight": (30, 3),
    "Flush": (15, 2),
    "Full House": (25, 2),
    "Four of a Kind": (30, 3),
    "Straight Flush": (40, 4),
    "Five of a Kind": (35, 3),
    "Flush House": (40, 4),
    "Flush Five": (50, 3),
}


def hand_base_one_level_down(
    name: str, chips: int, mult: int
) -> tuple[int, int]:
    """把牌型基础分降到**低一级**的值（The Arm 用）。

    公式来自游戏的 `level_up_hand(..., -1)`：等级降一级后筹码/倍率按增量线性
    回退，下限分别是 0 和 1。The Arm 只在原等级 > 1 时触发，所以不会退到 0 级。
    """
    step_chips, step_mult = HAND_LEVEL_STEP.get(name, (0, 0))
    return max(0, chips - step_chips), max(1, mult - step_mult)


def hand_base_level_up(name: str, chips: int, mult: int) -> tuple[int, int]:
    """把牌型基础分提到**高一级**的值（星球牌估值用）。

    和 `hand_base_one_level_down` 对称：游戏里 `level_up_hand(..., +1)` 就是按
    增量线性加上去。"这张星球牌值多少分"必须用真实算分引擎量，而这个函数
    提供"升一级之后的基础分"。
    """
    step_chips, step_mult = HAND_LEVEL_STEP.get(name, (0, 0))
    return max(0, chips + step_chips), max(1, mult + step_mult)


@dataclass(frozen=True, slots=True)
class Card:
    """一张牌。字段直接对应 API 返回的 Card 对象。"""

    id: int
    key: str
    set: str
    label: str
    suit: str | None
    rank: str | None
    enhancement: str | None
    edition: str | None
    seal: str | None
    debuff: bool
    hidden: bool
    buy_cost: int
    sell_cost: int
    effect: str
    eternal: bool = False
    perishable: int | None = None
    rental: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Card":
        value = data.get("value") or {}
        modifier = data.get("modifier") or {}
        card_state = data.get("state") or {}
        cost = data.get("cost") or {}
        return cls(
            id=int(data.get("id", -1)),
            key=str(data.get("key", "")),
            set=str(data.get("set", "")),
            label=str(data.get("label", "")),
            suit=value.get("suit"),
            rank=value.get("rank"),
            enhancement=modifier.get("enhancement"),
            edition=modifier.get("edition"),
            seal=modifier.get("seal"),
            debuff=bool(card_state.get("debuff", False)),
            hidden=bool(card_state.get("hidden", False)),
            buy_cost=int(cost.get("buy", 0) or 0),
            sell_cost=int(cost.get("sell", 0) or 0),
            effect=str(value.get("effect", "")),
            eternal=bool(modifier.get("eternal", False)),
            perishable=modifier.get("perishable"),
            rental=bool(modifier.get("rental", False)),
        )

    # ------------------------------------------------------------ 基本属性

    @property
    def is_playing_card(self) -> bool:
        return self.set in ("DEFAULT", "ENHANCED")

    @property
    def is_joker(self) -> bool:
        return self.set == "JOKER"

    @property
    def is_stone(self) -> bool:
        """石头牌没有花色和点数，但有 +50 筹码。"""
        return self.enhancement == "STONE"

    @property
    def is_wild(self) -> bool:
        """万能牌花色视作任意。"""
        return self.enhancement == "WILD"

    @property
    def is_face(self) -> bool:
        return self.rank in FACE_RANKS

    @property
    def base_chips(self) -> int:
        """这张牌本身贡献的点数筹码（不含强化）。"""
        if self.is_stone or self.rank is None:
            return 0
        return RANK_CHIPS.get(self.rank, 0)

    @property
    def scores(self) -> bool:
        """被 Boss 盲注压制（debuff）的牌不参与算分。"""
        return not self.debuff

    @property
    def nominal(self) -> float:
        """摊开排序用的权重（对应游戏的 Card:get_nominal）。

        注意这不是筹码值：人头牌的点数是 10，但 nominal 带上 0.1/0.2/0.3 的
        人头权重，用来决定"高牌里哪一张算分"、以及同点时的先后。
        石头牌没有点数，记 0。
        """
        if self.rank is None or self.is_stone:
            return 0.0
        return (
            RANK_CHIPS.get(self.rank, 0)
            + SUIT_NOMINAL.get(self.suit or "", 0.0)
            + FACE_NOMINAL.get(self.rank, 0.0)
        )

    @property
    def has_red_seal(self) -> bool:
        return self.seal == "RED"

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        bits = [self.label or self.key]
        if self.enhancement:
            bits.append(f"[{self.enhancement}]")
        if self.edition:
            bits.append(f"<{self.edition}>")
        if self.seal:
            bits.append(f"({self.seal} seal)")
        if self.debuff:
            bits.append("(debuffed)")
        return "".join(bits)


# ------------------------------------------------------------------ 牌型判定


def _rank_counter(cards: Iterable[Card]) -> Counter[str]:
    """统计点数；石头牌没有点数，不参与。"""
    return Counter(c.rank for c in cards if c.rank is not None and not c.is_stone)


def is_flush(cards: Sequence[Card]) -> bool:
    """是否同花。石头牌破坏同花；万能牌可以配合任何花色。"""
    if len(cards) != 5:
        return False
    if any(c.is_stone for c in cards):
        return False
    fixed = {c.suit for c in cards if not c.is_wild}
    return len(fixed) <= 1


def is_straight(cards: Sequence[Card]) -> bool:
    """是否顺子。必须正好 5 张、点数互不相同且连续（A 可当 1 或 14）。"""
    if len(cards) != 5:
        return False
    ranks = [c.rank for c in cards]
    if any(r is None or c.is_stone for r, c in zip(ranks, cards)):
        return False
    if len(set(ranks)) != 5:
        return False
    indexes = sorted(RANK_ORDER[r] for r in ranks)  # type: ignore[index]
    if indexes == list(range(indexes[0], indexes[0] + 5)):
        return True
    # A-2-3-4-5：把 A 当成 -1
    low_indexes = sorted(-1 if r == "A" else RANK_ORDER[r] for r in ranks)  # type: ignore[index]
    return low_indexes == list(range(low_indexes[0], low_indexes[0] + 5))


def classify(cards: Sequence[Card]) -> str | None:
    """判断这组牌构成什么牌型，返回 Balatro 的牌型名。

    返回 None 表示不是合法的出牌组合（张数为 0 或超过 5）。
    张数不足 5 时不可能构成顺子/同花，只能是比较点数的牌型。
    """
    count = len(cards)
    if count == 0 or count > 5:
        return None

    ranks = _rank_counter(cards)
    counts = sorted(ranks.values(), reverse=True)

    if count == 5:
        flush = is_flush(cards)
        straight = is_straight(cards)

        if counts == [5]:
            return "Flush Five" if flush else "Five of a Kind"
        if counts == [3, 2] and flush:
            return "Flush House"
        if straight and flush:
            return "Straight Flush"
        if counts == [4, 1]:
            return "Four of a Kind"
        if counts == [3, 2]:
            return "Full House"
        if flush:
            return "Flush"
        if straight:
            return "Straight"
        if counts == [3, 1, 1]:
            return "Three of a Kind"
        if counts == [2, 2, 1]:
            return "Two Pair"
        if counts == [2, 1, 1, 1]:
            return "Pair"
        return "High Card"

    # 少于 5 张
    if counts and counts[0] == count:
        return {4: "Four of a Kind", 3: "Three of a Kind", 2: "Pair", 1: "High Card"}[count]
    if counts[:2] == [2, 2]:
        return "Two Pair"
    if counts and counts[0] == 2:
        return "Pair"
    return "High Card"


def same_rank_cards(cards: Sequence[Card], count: int) -> list[Card]:
    """挑出"恰好 count 张同点数"的那些牌，按点数从高到低。

    对应游戏里的 get_X_same()：只有**恰好** count 张的组才算，
    所以 3 张 K 在 count=2 时不算对子（游戏里三条的"对子"是另行推导出来的）。
    """
    by_rank: dict[str, list[Card]] = {}
    for card in cards:
        if card.rank is None or card.is_stone:
            continue
        by_rank.setdefault(card.rank, []).append(card)

    groups = [
        group for group in by_rank.values() if len(group) == count
    ]
    groups.sort(key=lambda group: RANK_ORDER[group[0].rank], reverse=True)  # type: ignore[index]
    return [card for group in groups for card in group]


def scoring_cards(cards: Sequence[Card]) -> list[Card]:
    """打出的这些牌里，**真正参与算分**的是哪几张。

    这是本作最容易搞错的地方：垫牌不算分。打一对 K 再加三张散牌，
    只有两张 K 贡献点数筹码；打高牌时更是只有最高的那一张算分。
    对应的游戏实现是 evaluate_poker_hand()，它按牌型取 poker_hands[牌型][1]，
    而算分循环遍历的是 scoring_hand 而不是打出的全部牌。

    Splash（"打出的每张牌都算分"）会改变这个行为，由调用方处理。
    """
    name = classify(cards)
    if name is None:
        return []

    if name in ("Flush", "Straight", "Straight Flush",
                "Flush Five", "Flush House", "Five of a Kind", "Full House"):
        # 这些牌型的算分牌就是打出的全部（5 张）
        return list(cards)

    if name == "Four of a Kind":
        return same_rank_cards(cards, 4)
    if name == "Three of a Kind":
        return same_rank_cards(cards, 3)
    if name == "Two Pair":
        return same_rank_cards(cards, 2)      # 恰好两组，共 4 张
    if name == "Pair":
        return same_rank_cards(cards, 2)

    # 高牌：只算 get_nominal() 最高的那一张
    return [max(cards, key=lambda card: card.nominal)] if cards else []


def hand_contains(cards: Sequence[Card], target: str) -> bool:
    """这张牌型是否"包含"某个更简单的牌型。

    用于小丑牌的条件判断（例如 The Duo："打出的牌包含对子时 x2 倍率"）。
    """
    name = classify(cards)
    if name is None:
        return False
    return contains_hand(name, target)


# 牌型包含关系：X -> X 一定同时满足的那些牌型。
#
# 注意这里用的是"语义包含"（打出 X 是否也构成 Y），不是按强度排序。
# 两者经常被混淆，但按强度排序会得出"五条包含同花"这种错误结论
# （五条要求 5 张同点数，不可能同时是同花，除非是"同花五条"）。
HAND_CONTAINS: dict[str, frozenset[str]] = {
    "Flush Five": frozenset({"Flush Five", "Five of a Kind", "Four of a Kind",
                             "Three of a Kind", "Pair", "Flush"}),
    "Flush House": frozenset({"Flush House", "Full House", "Three of a Kind",
                              "Two Pair", "Pair", "Flush"}),
    "Five of a Kind": frozenset({"Five of a Kind", "Four of a Kind",
                                 "Three of a Kind", "Pair"}),
    "Straight Flush": frozenset({"Straight Flush", "Straight", "Flush"}),
    "Four of a Kind": frozenset({"Four of a Kind", "Three of a Kind", "Pair"}),
    "Full House": frozenset({"Full House", "Three of a Kind", "Two Pair", "Pair"}),
    "Flush": frozenset({"Flush"}),
    "Straight": frozenset({"Straight"}),
    "Three of a Kind": frozenset({"Three of a Kind", "Pair"}),
    "Two Pair": frozenset({"Two Pair", "Pair"}),
    "Pair": frozenset({"Pair"}),
    "High Card": frozenset({"High Card"}),
}


def contains_hand(have: str, want: str) -> bool:
    """打出的牌型 have 是否满足"包含 want"的条件。"""
    return want in HAND_CONTAINS.get(have, frozenset({have}))


def is_better_hand(candidate: str, incumbent: str) -> bool:
    return _hand_rank(candidate) < _hand_rank(incumbent)


def suit_of(card: Card) -> str | None:
    """万能牌没有确定花色，返回 None 表示"任意"。"""
    if card.is_wild or card.is_stone:
        return None
    return card.suit
