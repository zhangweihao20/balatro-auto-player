"""算分引擎的测试。

算分错一个数字，策略层就会做出错误的决定，而且很难从日志里看出来，
所以这里对每一段（基础分 / 牌面筹码 / 强化 / 版本 / 小丑牌 / 留在手里的牌）
都用可以手算的例子钉住。
"""

from __future__ import annotations

import pytest

from balatro_bot.scoring import score_play
from balatro_bot.search import best_play, enumerate_plays
from balatro_bot.state import GameState

from conftest import card, hands_table, joker, make_state


def state_of(**kwargs) -> GameState:
    return GameState(make_state(**kwargs))


def test_pair_of_kings() -> None:
    """对 K：基础 10 筹码 / 2 倍率，两张 K 各贡献 10 筹码 → (10+20)*2 = 60。"""
    state = state_of(hand=[card("H_K"), card("D_K"), card("C_3"), card("S_5"), card("H_9")])
    result = score_play(state, [0, 1])
    assert result.hand_name == "Pair"
    assert result.base_chips == 10
    assert result.card_chips == 20
    assert result.chips == 30
    assert result.mult == 2
    assert result.total == 60


def test_flush_adds_all_card_chips() -> None:
    """同花：基础 35 筹码 / 4 倍率，5 张牌点数全部计入。2+5+9+10+3 = 29。"""
    hand = [card("H_2"), card("H_5"), card("H_9"), card("H_J"), card("H_3")]
    state = state_of(hand=hand)
    result = score_play(state, [0, 1, 2, 3, 4])
    assert result.hand_name == "Flush"
    assert result.base_chips == 35
    assert result.card_chips == 29
    assert result.mult == 4
    assert result.total == (35 + 29) * 4


def test_edition_foil_adds_chips() -> None:
    """闪箔版本 +50 筹码。"""
    hand = [card("H_K", edition="FOIL"), card("D_K"), card("C_3"), card("S_5"), card("H_9")]
    state = state_of(hand=hand)
    result = score_play(state, [0, 1])
    assert result.card_chips == 10 + 10 + 50
    assert result.total == (10 + 70) * 2


def test_edition_polychrome_multiplies() -> None:
    """多彩版本 x1.5 倍率。"""
    hand = [card("H_K", edition="POLYCHROME"), card("D_K"), card("C_3"), card("S_5"), card("H_9")]
    state = state_of(hand=hand)
    result = score_play(state, [0, 1])
    assert result.xmult == pytest.approx(1.5)
    assert result.total == pytest.approx(30 * 2 * 1.5)


def test_enhancement_bonus_and_mult() -> None:
    """奖励牌 +30 筹码，倍率牌 +4 倍率。"""
    hand = [card("H_K", enhancement="BONUS"), card("D_K", enhancement="MULT"),
            card("C_3"), card("S_5"), card("H_9")]
    state = state_of(hand=hand)
    result = score_play(state, [0, 1])
    assert result.card_chips == 10 + 30 + 10
    assert result.card_mult == 4
    assert result.total == (10 + 50) * (2 + 4)


def test_junk_cards_do_not_add_chips() -> None:
    """垫牌不算分：一对 K 配三张散牌，和只打这两张 K 得分完全一样。

    这是本作最容易搞错的地方。游戏里算分循环遍历的是 scoring_hand
    （由 evaluate_poker_hand 按牌型给出），不是打出的全部牌。
    """
    hand = [card("H_K"), card("D_K"), card("C_3"), card("S_5"), card("H_9")]
    state = state_of(hand=hand)
    pair_only = score_play(state, [0, 1])
    pair_plus_junk = score_play(state, [0, 1, 2, 3, 4])

    assert pair_only.hand_name == pair_plus_junk.hand_name == "Pair"
    assert pair_only.total == pair_plus_junk.total
    assert pair_only.cards_scoring == pair_plus_junk.cards_scoring == 2
    assert pair_plus_junk.cards_played == 5


def test_high_card_scores_only_the_highest_card() -> None:
    """高牌只算最高（按 nominal）的那一张。"""
    hand = [card("H_K"), card("D_5"), card("C_3")]
    state = state_of(hand=hand)
    result = score_play(state, [0, 1, 2])
    assert result.hand_name == "High Card"
    assert result.cards_scoring == 1
    assert result.card_chips == 10          # 只有 K
    assert result.chips == 5 + 10
    assert result.total == 15


def test_ace_is_the_highest_for_high_card() -> None:
    """A 的 nominal 是 11，高于 K 的 10.3，所以高牌算 A。"""
    hand = [card("H_A"), card("D_K"), card("C_3")]
    state = state_of(hand=hand)
    result = score_play(state, [0, 1, 2])
    assert result.card_chips == 11


def test_two_pair_scores_four_cards() -> None:
    hand = [card("H_K"), card("D_K"), card("C_2"), card("S_2"), card("H_3")]
    state = state_of(hand=hand)
    result = score_play(state, [0, 1, 2, 3, 4])
    assert result.hand_name == "Two Pair"
    assert result.cards_scoring == 4
    assert result.card_chips == 10 + 10 + 2 + 2
    assert result.chips == 20 + 24
    assert result.total == (20 + 24) * 2


def test_three_of_a_kind_scores_three_cards() -> None:
    hand = [card("H_K"), card("D_K"), card("C_K"), card("S_2"), card("H_3")]
    state = state_of(hand=hand)
    result = score_play(state, [0, 1, 2, 3, 4])
    assert result.hand_name == "Three of a Kind"
    assert result.cards_scoring == 3
    assert result.card_chips == 30


def test_splash_makes_every_played_card_score() -> None:
    """Splash 让打出的每张牌都算分——这是垫牌规则的唯一例外。"""
    hand = [card("H_K"), card("D_K"), card("C_3"), card("S_5"), card("H_9")]
    state = state_of(hand=hand, jokers=[joker("j_splash")])
    result = score_play(state, [0, 1, 2, 3, 4])
    assert result.hand_name == "Pair"
    assert result.cards_scoring == 5
    assert result.card_chips == 10 + 10 + 3 + 5 + 9


def test_suit_joker_counts_only_scoring_cards() -> None:
    """花色类小丑牌数的是算分牌：打一手方块同花，5 张都算。"""
    hand = [card("D_2"), card("D_5"), card("D_9"), card("D_J"), card("D_3"),
            card("H_7")]
    state = state_of(hand=hand, jokers=[joker("j_greedy_joker")])
    result = score_play(state, [0, 1, 2, 3, 4])
    assert result.hand_name == "Flush"
    assert result.cards_scoring == 5
    assert result.joker_mult == 15          # 5 张方块 × +3
    assert result.chips == 35 + 2 + 5 + 9 + 10 + 3
    assert result.total == (35 + 29) * (4 + 15)


def test_baron_multiplies_held_kings() -> None:
    """Baron：每张留在手里的 K 提供 x1.5 倍率。留 3 张 → 1.5^3。

    打的是 S_2 和 H_3 两张，牌型是高牌，只算 nominal 较高的 3 那一张。
    """
    hand = [card("H_K"), card("D_K"), card("C_K"), card("S_2"), card("H_3")]
    state = state_of(hand=hand, jokers=[joker("j_baron")])
    result = score_play(state, [3, 4])
    assert result.hand_name == "High Card"
    assert result.cards_scoring == 1
    assert result.card_chips == 3
    assert result.joker_xmult == pytest.approx(1.5 ** 3)
    assert result.total == pytest.approx((5 + 3) * 1 * 1.5 ** 3)


def test_duo_doubles_on_pair() -> None:
    hand = [card("H_K"), card("D_K"), card("C_3"), card("S_5"), card("H_9")]
    state = state_of(hand=hand, jokers=[joker("j_duo")])
    result = score_play(state, [0, 1])
    assert result.joker_xmult == 2
    assert result.total == 30 * 2 * 2


def test_duo_does_not_trigger_on_high_card() -> None:
    """没有对子时 The Duo 不该触发——这是 contains 关系是否正确的关键用例。"""
    hand = [card("H_K"), card("D_Q"), card("C_3"), card("S_5"), card("H_9")]
    state = state_of(hand=hand, jokers=[joker("j_duo")])
    result = score_play(state, [0, 1])
    assert result.hand_name == "High Card"
    assert result.joker_xmult == 1


def test_five_of_a_kind_is_not_a_flush() -> None:
    """五条不包含同花：The Tribe（含同花时 x2）不该触发。"""
    hand = [card("H_K"), card("D_K"), card("C_K"), card("S_K"), card("H_K")]
    state = state_of(hand=hand, jokers=[joker("j_tribe")])
    result = score_play(state, [0, 1, 2, 3, 4])
    assert result.hand_name == "Five of a Kind"
    assert result.joker_xmult == 1


def test_held_steel_multiplies() -> None:
    """钢牌留在手里 x1.5。"""
    hand = [card("H_K"), card("D_K"), card("C_3"), card("S_5", enhancement="STEEL"),
            card("H_9")]
    state = state_of(hand=hand)
    result = score_play(state, [0, 1])
    assert result.card_xmult == pytest.approx(1.5)


def test_debuffed_card_does_not_score() -> None:
    """被 Boss 盲注压制的牌不贡献筹码，但仍然构成牌型。"""
    hand = [card("H_K", debuff=True), card("D_K"), card("C_3"), card("S_5"), card("H_9")]
    state = state_of(hand=hand)
    result = score_play(state, [0, 1])
    assert result.hand_name == "Pair"
    assert result.card_chips == 10          # 只有未被压制的 K
    assert result.total == (10 + 10) * 2


def test_unknown_joker_is_reported() -> None:
    hand = [card("H_K"), card("D_K"), card("C_3"), card("S_5"), card("H_9")]
    state = state_of(hand=hand, jokers=[joker("j_some_future_joker")])
    result = score_play(state, [0, 1])
    assert result.unknown == ("j_some_future_joker",)
    assert result.joker_mult == 0


def test_hands_table_fallback_when_missing() -> None:
    """状态里读不到 hands 表时退回一级基础值，而不是崩掉。"""
    state = state_of(hand=[card("H_K"), card("D_K")], hands={})
    result = score_play(state, [0, 1])
    assert result.base_chips == 10
    assert result.base_mult == 2


def test_leveled_hand_uses_table_value() -> None:
    """牌型升级后应使用表里的绝对值，而不是一级基础值。"""
    table = hands_table(overrides={"Pair": (40, 6)})
    state = state_of(hand=[card("H_K"), card("D_K")], hands=table)
    result = score_play(state, [0, 1])
    assert (result.base_chips, result.base_mult) == (40, 6)


def test_red_seal_retriggers_card() -> None:
    """红蜡封让牌多触发一次，点数和强化都翻倍。"""
    hand = [card("H_K", seal="RED"), card("D_K"), card("C_3"), card("S_5"), card("H_9")]
    state = state_of(hand=hand)
    result = score_play(state, [0, 1])
    assert result.card_chips == 10 * 2 + 10


def test_invalid_indices_raise() -> None:
    state = state_of(hand=[card("H_K")])
    with pytest.raises(IndexError):
        score_play(state, [5])
    with pytest.raises(ValueError):
        score_play(state, [0, 0])


# ------------------------------------------------------------------ 搜索


def test_best_play_prefers_higher_score() -> None:
    """同时有一对 K 和一手红桃同花时，应该选分更高的同花。

    对 K = (10+20)*2 = 60；同花 = (35+36)*4 = 284。
    """
    hand = [card("H_K"), card("D_K"), card("H_Q"), card("H_2"), card("H_5"), card("H_9")]
    state = state_of(hand=hand)
    best = best_play(state)
    assert best is not None
    assert best.score.hand_name == "Flush"
    assert best.total == (35 + 10 + 10 + 2 + 5 + 9) * 4


def test_enumerate_covers_all_subsets() -> None:
    """8 张手牌、可打 1~5 张 → 218 种组合。"""
    hand = [card(k) for k in
            ("H_2", "H_3", "H_4", "H_5", "H_6", "D_9", "C_T", "S_J")]
    state = state_of(hand=hand)
    plays = enumerate_plays(state)
    assert len(plays) == 218
    assert plays == sorted(plays, key=lambda c: c.total, reverse=True)


def test_baron_rewards_holding_kings() -> None:
    """Baron 会让"留住 K"的收益变高，但不该压过打出三条 K。

    这是搜索正确性的一个好例子：xmult 更大不等于总分更高。
    """
    hand = [card("H_K"), card("D_K"), card("C_K"), card("S_2"), card("H_3")]
    with_baron = state_of(hand=hand, jokers=[joker("j_baron")])
    without_baron = state_of(hand=hand)

    # 打 S_2 + H_3（高牌只算 3 那一张），留住三张 K
    held_score = score_play(with_baron, [3, 4]).total
    baseline = score_play(without_baron, [3, 4]).total
    assert held_score == pytest.approx(baseline * 1.5 ** 3)

    # 三条 K 得 (30 + 30) * 3 = 180，依然优于留 K 的 27 分
    best = best_play(with_baron)
    assert best is not None
    assert best.score.hand_name == "Three of a Kind"
    assert best.total == 180
