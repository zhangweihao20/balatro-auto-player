"""小丑牌槽位顺序的测试。

游戏按槽位从左到右逐个结算（源码 functions_state_events.lua 的 joker 循环）：

    mult = mod_mult(mult + effect.mult_mod)     -- 加法
    mult = mod_mult(mult * effect.Xmult_mod)    -- 乘法

加法和乘法交替进行，所以 **(基础 + 加法) × 乘区** 大于 **(基础 × 乘区) + 加法**。
把加法牌放左边、乘区牌放右边是**完全免费的分数提升**，只需要调换位置
（接口提供 rearrange）。这里钉住这个性质和对应的搜索结果。
"""

from __future__ import annotations

import pytest

from balatro_bot.cards import Card
from balatro_bot.jokers import JokerEffect, PlayContext, model_joker
from balatro_bot.scoring import score_play
from balatro_bot.solver import Solver
from balatro_bot.state import GameState

from conftest import card, joker, make_state

PAIR_HAND = ["H_K", "D_K", "C_3", "S_5", "H_9"]


def state_with(joker_keys: list[str], hand: list[str] | None = None) -> GameState:
    keys = hand or PAIR_HAND
    return GameState(make_state(
        hand=[card(k) for k in keys],
        jokers=[joker(k) for k in joker_keys],
    ))


# --------------------------------------------------------- 顺序确实影响得分


def test_additive_before_multiplicative_scores_higher() -> None:
    """Jolly(+8 Mult 有对子时) 放在 The Duo(x2 有对子时) 左边，得分更高。

    计算过程（对 K：基础 10 筹码 / 2 倍率，两张 K 共 20 筹码）：
        加法在前：(2 + 8) × 2 = 20 倍率 → 600 分
        乘法在前：2 × 2 + 8 = 12 倍率 → 360 分
    位置不同，**同样的牌差了 67% 的分数**。
    """
    good = state_with(["j_jolly", "j_duo"])
    bad = state_with(["j_duo", "j_jolly"])

    good_score = score_play(good, [0, 1])
    bad_score = score_play(bad, [0, 1])

    assert good_score.final_mult == pytest.approx(20.0)
    assert bad_score.final_mult == pytest.approx(12.0)
    assert good_score.total == 600
    assert bad_score.total == 360
    assert good_score.total > bad_score.total


def test_chips_are_additive_regardless_of_order() -> None:
    """筹码是纯加法，顺序不影响它。"""
    first = state_with(["j_blue_joker", "j_jolly"])
    breakdown = score_play(first, [0, 1])
    assert breakdown.base_chips == 10
    assert breakdown.card_chips == 20


# ----------------------------------------------------------- 顺序搜索


def test_best_joker_order_finds_additive_first() -> None:
    """求解器应该发现"把自己排成 [Jolly, Duo]"更优，并给出重排建议。"""
    state = state_with(["j_duo", "j_jolly"])   # 当前顺序是"乘法在前"，是差的那种
    solver = Solver.from_state(state)

    order = solver.best_joker_order(state.hand.cards)
    assert order == [1, 0], f"应该建议把 Jolly 换到前面，实际 {order}"

    # 换过去之后得分确实变高
    improved = solver.reordered(order).best_score(state.hand.cards)
    assert improved == 600
    assert improved > solver.best_score(state.hand.cards)


def test_best_joker_order_returns_none_when_already_optimal() -> None:
    state = state_with(["j_jolly", "j_duo"])
    solver = Solver.from_state(state)
    assert solver.best_joker_order(state.hand.cards) is None


def test_best_joker_order_returns_none_for_single_joker() -> None:
    state = state_with(["j_jolly"])
    assert Solver.from_state(state).best_joker_order(state.hand.cards) is None


def test_best_joker_order_applies_partition_without_measuring() -> None:
    """没有复制类小丑牌时直接应用"加法在前"，不依赖当前这一手的分数比较。

    这条很关键：条件型乘区牌在回合第一手不触发，如果拿分数当门槛就永远不重排。
    """
    # Card Sharp 是条件型乘区（本回合重复牌型才 x3），在回合第一手不触发。
    # 它现在排在加法牌 Scholar 的左边，应该被换到右边。
    state = state_with(["j_card_sharp", "j_scholar"])
    solver = Solver.from_state(state)

    # 先确认这一手它确实不触发（否则这个测试就没有意义了）
    assert score_play(state, [0, 1]).joker_xmult == 1.0

    order = solver.best_joker_order(state.hand.cards)
    assert order == [1, 0], f"Card Sharp 应该被排到右边，实际 {order}"


def test_best_joker_order_preserves_relative_order_within_groups() -> None:
    """同组内保持相对顺序（稳定分组），不做无谓的乱序。"""
    state = state_with(["j_scholar", "j_duo", "j_jolly", "j_ramen"])
    solver = Solver.from_state(state)
    assert solver.additive_first_order() == (0, 2, 1, 3)


def test_best_joker_order_returns_none_when_already_partitioned() -> None:
    state = state_with(["j_jolly", "j_duo"])
    solver = Solver.from_state(state)
    assert solver.best_joker_order(state.hand.cards) is None


# ----------------------------------------------------------- Blueprint 复制


def test_blueprint_copies_the_joker_to_its_right() -> None:
    """Blueprint 复制右边那张的收益——所以它的位置本身就是一种策略选择。"""
    hand = [Card.from_dict(card(k)) for k in PAIR_HAND]
    ctx = PlayContext(played=hand[:2], held=tuple(hand[2:]), hand_name="Pair")

    jolly = Card.from_dict(joker("j_jolly"))
    blueprint = Card.from_dict(joker("j_blueprint"))
    duo = Card.from_dict(joker("j_duo"))

    # [Blueprint, Jolly]：Blueprint 复制 Jolly → 各 +8
    effect = model_joker("j_blueprint", ctx, blueprint,
                         joker_index=0, jokers=(blueprint, jolly))
    assert effect.mult == 8.0
    assert effect.xmult == 1.0

    # [Blueprint, Duo]：Blueprint 复制 Duo → 各 x2
    effect = model_joker("j_blueprint", ctx, blueprint,
                         joker_index=0, jokers=(blueprint, duo))
    assert effect.xmult == 2.0

    # [Jolly, Blueprint]：Blueprint 右边没有牌 → 收益 0
    effect = model_joker("j_blueprint", ctx, blueprint,
                         joker_index=1, jokers=(jolly, blueprint))
    assert (effect.chips, effect.mult, effect.xmult) == (0.0, 0.0, 1.0)


def test_brainstorm_copies_the_leftmost_joker() -> None:
    hand = [Card.from_dict(card(k)) for k in PAIR_HAND]
    ctx = PlayContext(played=hand[:2], held=tuple(hand[2:]), hand_name="Pair")

    jolly = Card.from_dict(joker("j_jolly"))
    storm = Card.from_dict(joker("j_brainstorm"))
    effect = model_joker("j_brainstorm", ctx, storm,
                         joker_index=1, jokers=(jolly, storm))
    assert effect.mult == 8.0


def test_blueprint_does_not_recurse_forever() -> None:
    """两张 Blueprint 互相复制时不能无限递归。"""
    hand = [Card.from_dict(card(k)) for k in PAIR_HAND]
    ctx = PlayContext(played=hand[:2], held=tuple(hand[2:]), hand_name="Pair")
    one = Card.from_dict(joker("j_blueprint"))
    two = Card.from_dict(joker("j_blueprint"))
    effect = model_joker("j_blueprint", ctx, one, joker_index=0, jokers=(one, two))
    assert isinstance(effect, JokerEffect)


def test_blueprint_position_changes_total_score() -> None:
    """同一个牌组，Blueprint 放左边还是右边，总分不同。"""
    left = state_with(["j_blueprint", "j_jolly", "j_duo"])
    right = state_with(["j_jolly", "j_duo", "j_blueprint"])
    assert score_play(left, [0, 1]).total != score_play(right, [0, 1]).total


def test_blueprint_triggers_measured_search() -> None:
    """有 Blueprint 时必须真的比较分数（它复制右边那张，位置决定收益）。"""
    state = state_with(["j_jolly", "j_duo", "j_blueprint"])
    solver = Solver.from_state(state)
    order = solver.best_joker_order(state.hand.cards)
    # Blueprint 在最后会复制到空 → 收益 0，应该被挪走
    assert order is not None
    assert order[-1] != 2, "Blueprint 不该留在最后（右边没有可复制的牌）"
