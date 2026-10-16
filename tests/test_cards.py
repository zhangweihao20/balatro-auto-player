"""牌型判定与包含关系的测试。

这些是最基础的正确性保证：牌型名字一旦和游戏里的 key 不一致，
机器人就会去读兜底表，算出来的分数全是错的。
"""

from __future__ import annotations

import pytest

from balatro_bot.cards import Card, classify, contains_hand, is_flush, is_straight

from conftest import card


def build(*keys: str) -> list[Card]:
    return [Card.from_dict(card(k)) for k in keys]


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (("H_A", "H_K", "H_Q", "H_J", "H_T"), "Straight Flush"),
        (("H_2", "H_5", "H_9", "H_J", "H_3"), "Flush"),
        (("H_A", "D_2", "C_3", "S_4", "H_5"), "Straight"),   # A 当 1 的顺子
        (("H_T", "D_J", "C_Q", "S_K", "H_A"), "Straight"),   # A 当 14 的顺子
        (("H_K", "D_K", "C_K", "S_2", "H_3"), "Three of a Kind"),
        (("H_K", "D_K", "C_2", "S_2", "H_3"), "Two Pair"),
        (("H_K", "D_K", "C_2", "S_5", "H_3"), "Pair"),
        (("H_K", "D_Q", "C_2", "S_5", "H_3"), "High Card"),
        (("H_K", "D_K", "C_K", "S_K", "H_3"), "Four of a Kind"),
        (("H_K", "D_K", "C_K", "S_2", "H_2"), "Full House"),
        (("H_K", "H_Q", "H_J", "H_T", "H_9"), "Straight Flush"),
        (("H_K", "D_K"), "Pair"),
        (("H_K",), "High Card"),
        (("H_K", "D_K", "C_K", "S_K"), "Four of a Kind"),
        (("H_K", "D_K", "C_2", "S_2"), "Two Pair"),
    ],
)
def test_classify(keys: tuple[str, ...], expected: str) -> None:
    assert classify(build(*keys)) == expected


def test_flush_five_and_flush_house() -> None:
    # 同花五条：5 张同点数同花色
    five = [Card.from_dict(card("H_K")) for _ in range(5)]
    assert classify(five) == "Flush Five"

    # 同花葫芦：3+2 且同花色
    house = build("H_K", "H_K", "H_K", "H_2", "H_2")
    assert classify(house) == "Flush House"

    # 非同花的五条
    mixed = build("H_K", "D_K", "C_K", "S_K", "H_K")
    assert classify(mixed) == "Five of a Kind"


def test_classify_rejects_invalid_sizes() -> None:
    assert classify([]) is None
    assert classify(build("H_K", "D_K", "C_K", "S_K", "H_2", "D_2")) is None


def test_stone_card_breaks_flush_and_straight() -> None:
    """石头牌没有花色和点数，不能用来凑同花或顺子。"""
    stone = Card.from_dict(card("H_2", enhancement="STONE"))
    cards = [Card.from_dict(card("H_K")), Card.from_dict(card("H_Q")),
             Card.from_dict(card("H_J")), Card.from_dict(card("H_T")), stone]
    assert not is_flush(cards)
    assert not is_straight(cards)
    assert classify(cards) == "High Card"


def test_wild_card_matches_any_suit() -> None:
    """万能牌只改花色，不改点数：H_9 配黑桃 KQJT 可以凑成同花顺。"""
    wild = Card.from_dict(card("H_9", enhancement="WILD"))
    cards = [Card.from_dict(card("S_K")), Card.from_dict(card("S_Q")),
             Card.from_dict(card("S_J")), Card.from_dict(card("S_T")), wild]
    assert is_flush(cards)
    assert is_straight(cards)
    assert classify(cards) == "Straight Flush"


def test_wild_card_does_not_change_rank() -> None:
    """反例：万能牌的 2 仍然算 2，凑不出 KQJT 的顺子。"""
    wild = Card.from_dict(card("H_2", enhancement="WILD"))
    cards = [Card.from_dict(card("S_K")), Card.from_dict(card("S_Q")),
             Card.from_dict(card("S_J")), Card.from_dict(card("S_T")), wild]
    assert is_flush(cards)
    assert not is_straight(cards)
    assert classify(cards) == "Flush"


@pytest.mark.parametrize(
    ("have", "want", "expected"),
    [
        ("Full House", "Pair", True),
        ("Full House", "Three of a Kind", True),
        ("Full House", "Two Pair", True),
        ("Four of a Kind", "Pair", True),
        ("Two Pair", "Three of a Kind", False),
        ("Three of a Kind", "Two Pair", False),
        ("Straight Flush", "Flush", True),
        ("Straight Flush", "Straight", True),
        # 关键反例：五条要求 5 张同点数，不可能同时是同花
        ("Five of a Kind", "Flush", False),
        ("Five of a Kind", "Two Pair", False),
        ("Flush", "Straight", False),
        ("Pair", "Pair", True),
    ],
)
def test_contains_hand(have: str, want: str, expected: bool) -> None:
    assert contains_hand(have, want) is expected
