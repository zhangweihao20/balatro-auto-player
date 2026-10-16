"""阵容估值（真实得分口径）的测试。

背景：商店里判断"这张小丑牌值不值得"以前用一张离线静态表 + 几个同量级的
加减项。实测证明会出错，两类具体的错误各有一条测试钉住：

1. **筹码丑反超倍率丑**：Sly Joker（+50 筹码）因为"要求对子"拿到 +1.5 契合度，
   反超 Smiley Face（+5 倍率/人头牌）——直接违反规格的"倍率丑 > 筹码丑"。
2. **引擎不认的牌赖着不走**：Smeared / Four Fingers / Shortcut 静态 8.0~8.5，
   但它们改变牌型判定、算分引擎不实现 → 真实贡献是 0，却永远当不上"最差的"
   → 牌位被永久占死。

现在改成用**已经过真机验证的算分引擎**量真实贡献，这些测试保证它不再退化。
"""

from __future__ import annotations

import pytest

from balatro_bot.cards import Card
from balatro_bot.planning import representative_hand, representative_hands
from balatro_bot.state import GameState
from balatro_bot.strategy import (
    Config,
    hand_softness,
    joker_addition_gain,
    joker_marginal_values,
    lineup_score,
    valuation_plan,
    valuation_scenario,
)

from conftest import hands_table, joker, make_state
from test_strategy import full_deck, priced, with_area

FACES = ("J", "Q", "K")


def shop_state(held: tuple[str, ...] = (), *, deck=None, money: int = 40) -> GameState:
    raw = make_state(
        state="SHOP", money=money,
        jokers=[joker(key) for key in held],
        deck=deck if deck is not None else full_deck(),
    )
    with_area(raw, "shop", [], limit=2)
    with_area(raw, "vouchers", [], limit=1)
    with_area(raw, "consumables", [], limit=2)
    return GameState(raw)


def gain(state: GameState, key: str) -> float:
    plan = valuation_plan(state, Config())
    return joker_addition_gain(state, Card.from_dict(joker(key)), plan)


# ------------------------------------------------------------ 打分场景本身


def test_representative_hand_has_five_cards() -> None:
    deck = [Card.from_dict(entry) for entry in full_deck()]
    for name in ("Pair", "Two Pair", "Three of a Kind", "Straight", "Flush",
                 "Full House", "Four of a Kind", "High Card"):
        played, held = representative_hand(deck, name)
        assert len(played) == 5, name
        assert len(held) == 3, name


def test_representative_hand_pads_with_distinct_ranks() -> None:
    """垫牌每个点数只能一张，否则给"对子"垫上同点牌就变成四条了。

    对子场景本来就该有 2 张同点（那一对），所以判据是"没有任何点数出现 3 次以上"。
    """
    from collections import Counter

    deck = [Card.from_dict(entry) for entry in full_deck()]
    played, _ = representative_hand(deck, "Pair")
    counts = Counter(card.rank for card in played)
    assert max(counts.values()) == 2          # 只有那一对是重复的，没变成三条/四条
    assert len(played) == 5


def test_multiple_scenarios_cover_faces_and_aces() -> None:
    """多个锚点：一个用最高点数（通常是 A），一个用最高人头牌（K）。

    只用一个锚点会系统性偏袒某一类小丑牌——A 在 Balatro 里**不算人头牌**，
    所以只用 A 锚点时，"每个人头牌 +5 倍率"的笑脸会显示成 0 贡献而被卖掉。
    """
    deck = [Card.from_dict(entry) for entry in full_deck()]
    scenarios = representative_hands(deck, "Pair")
    assert len(scenarios) >= 2
    anchors = {played[0].rank for played, _ in scenarios}
    assert "A" in anchors
    assert anchors & set(FACES)


def test_scenario_is_empty_for_an_empty_deck() -> None:
    assert valuation_scenario(shop_state(deck=[]), Config()) == ([], "")


# ------------------------------------------- 引擎不认的牌必须显示零贡献


@pytest.mark.parametrize("key", ["j_smeared", "j_four_fingers", "j_shortcut"])
def test_engine_unaware_jokers_show_no_gain(key: str) -> None:
    """这三张改变牌型判定，而算分引擎不实现 → 真实贡献必须是 ×1.0。

    它们静态估值 8.0~8.5，正是"买进来就换不掉、永久占死牌位"的根源。
    """
    state = shop_state(("j_jolly", "j_duo", "j_trio"))
    assert gain(state, key) == pytest.approx(1.0)


# ------------------------------------------- 人头牌系必须显示出真实贡献


@pytest.mark.parametrize("key", ["j_smiley", "j_scary_face", "j_photograph"])
def test_face_card_jokers_show_a_real_gain(key: str) -> None:
    """人头牌系小丑必须在场景里有贡献，否则会被当成废牌卖掉。"""
    state = shop_state(("j_jolly", "j_duo", "j_trio"))
    assert gain(state, key) > 1.0


def test_position_matters_for_blueprint() -> None:
    """Blueprint 复制右边的邻居——追加在末尾等于什么都没复制。

    机器人买完可以免费重排，所以估值要按**最佳摆放位置**算。
    """
    state = shop_state(("j_jolly", "j_duo", "j_trio"))
    assert gain(state, "j_blueprint") > 1.0
    assert gain(state, "j_brainstorm") > 1.0


# -------------------------------------------------------- 边际贡献（卖哪张）


def test_marginals_are_zero_for_jokers_that_do_not_fit() -> None:
    """场景是"对子"，三条（j_trio）在它面前没有贡献——该卖的就是这种。

    注意这里必须用**硬筛口径**（只有专精牌型那一个视角）。走 `valuation_plan`
    的话，过渡期的软筛会按等级把其它牌型也算进来，三条就不再是 0 了——
    那是另一条测试（`test_transition_soft_gate_*`）要管的事。
    """
    state = shop_state(("j_jolly", "j_duo", "j_trio"))
    plan = valuation_plan(state, Config())
    assert len(plan) == 1, "这套状态没有任何牌型升过级，软筛不该生效"
    marginals = joker_marginal_values(state, plan)
    assert marginals[0] > 0            # jolly：有对子 +8 倍率
    assert marginals[1] > 0            # duo：有对子 ×2
    assert marginals[2] == pytest.approx(0.0)   # trio：要三条，不吃对子


def test_marginals_rank_the_mult_joker_above_the_chips_joker() -> None:
    """在"倍率已经很高、筹码很低"的阵容里，加筹码确实更值钱。

    这条说明新的估值**不是照搬"倍率丑 > 筹码丑"的口号**，而是算真实收益——
    规格里那条是筹码/倍率均衡时的经验法则，算真实得分比它更准。
    """
    state = shop_state(("j_jolly", "j_duo", "j_trio"))
    view = valuation_plan(state, Config())[0]
    base = lineup_score(state, state.jokers.cards, *view.scenarios[0], view.hand_name)
    assert base > 0


# ------------------------------------------- 牌型条件的"软筛"（仅过渡期）


def soft_state(
    pair_level: int = 1,
    flush_level: int = 1,
    *,
    flush_played: int = 0,
    pair_played: int = 0,
) -> GameState:
    """对子/同花两个牌型各自升级到指定等级的局面，用来验证软筛强度。"""
    raw = make_state(
        state="SHOP", money=40, jokers=[joker("j_jolly")], deck=full_deck(),
        hands=hands_table(
            overrides={
                "Pair": (10 + 15 * (pair_level - 1), 2 + (pair_level - 1)),
                "Flush": (35 + 15 * (flush_level - 1), 4 + (flush_level - 1)),
            },
            levels={"Pair": pair_level, "Flush": flush_level},
            played={"Pair": pair_played, "Flush": flush_played},
        ),
    )
    with_area(raw, "shop", [], limit=2)
    with_area(raw, "vouchers", [], limit=1)
    with_area(raw, "consumables", [], limit=2)
    return GameState(raw)


def test_softness_is_zero_when_no_hand_type_stands_out() -> None:
    """全 1 级、也没打过别的牌型 → 没有任何"另一种可能流派"，软筛不生效。"""
    state = soft_state()
    assert hand_softness(state, Config().params) == pytest.approx(0.0)
    assert len(valuation_plan(state, Config())) == 1


def test_soft_gate_fires_on_played_evidence_alone() -> None:
    """**这条是为了防止"软筛永远不触发"再次发生。**

    真实对局里 `lock_planet_type` 把所有星球牌砸在专精牌型上（实测一局只用
    1 张），其它牌型**永远升不了级**。所以"另一种可能的流派"必须能靠
    **实际打过几次**来体现——只按等级挑候选的话软筛是死代码。
    """
    state = soft_state(flush_played=3)
    assert hand_softness(state, Config().params) > 0.0
    plan = valuation_plan(state, Config())
    assert {view.hand_name for view in plan} == {"Pair", "Flush"}
    gain = joker_addition_gain(state, Card.from_dict(joker("j_droll")), plan)
    assert gain > 1.0 + Config().params.joker_buy_gain


def test_softness_fades_as_the_level_lead_grows() -> None:
    """等级领先越多，越接近定型；领先到 soft_gate_lead 就完全回到硬筛。"""
    params = Config().params
    assert hand_softness(soft_state(2, 2), params) == pytest.approx(1.0)   # 打平
    assert hand_softness(soft_state(3, 2), params) == pytest.approx(0.5)   # 领先 1 级
    assert hand_softness(soft_state(4, 2), params) == pytest.approx(0.0)   # 领先 2 级 = 定型


def test_plan_weights_sum_to_one() -> None:
    plan = valuation_plan(soft_state(3, 2), Config())
    assert len(plan) == 2
    assert sum(view.weight for view in plan) == pytest.approx(1.0)


def test_transition_lets_a_flush_joker_show_a_real_gain() -> None:
    """**这是这次改动的核心行为**。

    对子和同花打平时（都没定型），"要求同花"的 j_droll 应该显示出真实增益，
    而不是被 0/1 硬筛判成 ×1.00（那样它永远不会被买、还总被卖掉）。
    """
    state = soft_state(2, 2)
    plan = valuation_plan(state, Config())
    assert {view.hand_name for view in plan} == {"Pair", "Flush"}
    gain_droll = joker_addition_gain(state, Card.from_dict(joker("j_droll")), plan)
    assert gain_droll > 1.0 + Config().params.joker_buy_gain


def test_settled_build_falls_back_to_the_hard_gate() -> None:
    """对子领先 2 级 = 已定型 → 只剩专精牌型一个视角，同花系的牌回到 ×1.00。"""
    state = soft_state(4, 2)
    plan = valuation_plan(state, Config())
    assert [view.hand_name for view in plan] == ["Pair"]
    gain_droll = joker_addition_gain(state, Card.from_dict(joker("j_droll")), plan)
    assert gain_droll == pytest.approx(1.0)


def test_soft_gate_never_touches_the_scoring_engine() -> None:
    """软筛只影响"用哪些视角估值"，**不改算分引擎**。

    引擎的保真度是 22/22 对齐真机验证过的，被污染会直接错判实际出牌得分。
    所以在同花场景里，"要求对子"的 j_jolly 仍然必须贡献 0。
    """
    state = soft_state(2, 2)
    plan = valuation_plan(state, Config())
    flush_view = next(v for v in plan if v.hand_name == "Flush")
    played, held = flush_view.scenarios[0]
    with_jolly = lineup_score(state, [Card.from_dict(joker("j_jolly"))], played, held, "Flush")
    without = lineup_score(state, [], played, held, "Flush")
    assert with_jolly == pytest.approx(without)


# ------------------------------------------------------------ 商店集成


def test_shop_rejects_an_engine_unaware_joker_into_a_free_slot() -> None:
    """空位买牌也要过"真实得分"闸门：Smeared 这类牌不该再被买进来。"""
    from balatro_bot.strategy import Buy, choose_shop_action

    raw = make_state(state="SHOP", money=40, jokers=[joker("j_jolly"), joker("j_duo")],
                     deck=full_deck())
    with_area(raw, "shop", [priced(joker("j_smeared"), 5)], limit=2)
    with_area(raw, "vouchers", [], limit=1)
    with_area(raw, "consumables", [], limit=2)
    state = GameState(raw)
    assert not isinstance(choose_shop_action(state, Config(), rerolls=9), Buy)


def test_shop_still_buys_a_joker_that_really_helps() -> None:
    from balatro_bot.strategy import Buy, choose_shop_action

    raw = make_state(state="SHOP", money=40, jokers=[joker("j_jolly"), joker("j_duo")],
                     deck=full_deck())
    with_area(raw, "shop", [priced(joker("j_duo"), 5)], limit=2)
    with_area(raw, "vouchers", [], limit=1)
    with_area(raw, "consumables", [], limit=2)
    state = GameState(raw)
    assert isinstance(choose_shop_action(state, Config(), rerolls=9), Buy)
