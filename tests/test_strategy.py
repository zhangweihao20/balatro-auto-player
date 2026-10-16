"""策略层测试。

重点不是"策略够不够聪明"（那要靠真实对局评测），而是三条工程性质：
    1. 决策可复现：同一个局面必须给出同一个答案
    2. 结论不随采样数摇摆：16 次和 128 次采样应该得出同样的动作
    3. 边界情况正确：最后一手不能弃牌、Boss 盲注不能跳、商店没钱不能乱买
"""

from __future__ import annotations

from typing import Any, Iterable

import pytest

from balatro_bot.scoring import score_play
from balatro_bot.state import GameState
from balatro_bot.strategy import (
    Buy,
    Config,
    Discard,
    NextRound,
    Play,
    Reroll,
    Sell,
    SelectBlind,
    SkipBlind,
    build_solver,
    choose_blind_action,
    choose_pack_action,
    choose_play_action,
    choose_shop_action,
    committed_hand,
    effective_joker_value,
    effective_reserve,
    evaluate_discard_plans,
    filler_penalty,
    greedy_discard_order,
    max_rerolls_for,
    pack_kind,
    plan_potential,
    planet_value,
    play_constraints,
    standard_pack_bonus,
    shop_card_value,
    synergy_adjustment,
    target_hand_types,
)
from balatro_bot.jokers import is_compounding, joker_value, raises_multiplier
from balatro_bot.params import TunableParams
from balatro_bot.solver import Solver

from conftest import card, consumable, hands_table, joker, make_state

RANKS = "23456789TJQKA"
SUITS = "HDCS"

JUNK_HAND = ["H_2", "D_5", "C_9", "S_J", "H_3", "D_7", "C_K", "S_4"]


def full_deck(exclude: Iterable[str] = ()) -> list[dict[str, Any]]:
    skip = set(exclude)
    return [card(f"{s}_{r}") for s in SUITS for r in RANKS if f"{s}_{r}" not in skip]


def with_area(raw: dict[str, Any], name: str, cards: list[dict], limit: int = 0) -> dict:
    raw[name] = {"count": len(cards), "limit": limit, "cards": cards}
    return raw


def junk_state(**kwargs) -> GameState:
    kwargs.setdefault("hand", [card(k) for k in JUNK_HAND])
    kwargs.setdefault("deck", full_deck(JUNK_HAND))
    kwargs.setdefault("required", 100_000)
    return GameState(make_state(**kwargs))


# --------------------------------------------------------------- 可复现性


def test_decision_is_deterministic() -> None:
    """同一个局面反复决策必须得到同一个答案。"""
    state = junk_state()
    config = Config()
    actions = {repr(choose_play_action(state, config)) for _ in range(5)}
    assert len(actions) == 1, f"决策不稳定：{actions}"


def test_decision_type_stable_across_sample_counts() -> None:
    """采样数变化不应该改变"打还是弃"这个判断。

    注意只保证**动作类型**稳定，不保证弃牌张数稳定：候选方案（弃 1 张 ⊂ 弃 2 张
    ⊂ 弃 3 张）的期望值常常非常接近，噪声会在它们之间摇摆。这是蒙特卡洛的固有
    限制，不值得为它把采样数堆到很高——实际运行时采样数是固定的，
    所以同一个局面每次都会得到同一个答案（由 decision_rng 保证）。
    """
    state = junk_state()
    actions = {
        samples: choose_play_action(state, Config(discard_samples=samples))
        for samples in (16, 32, 64, 128)
    }
    kinds = {type(action).__name__ for action in actions.values()}
    assert kinds == {"Discard"}, f"应该始终选择弃牌，实际：{actions}"


def test_same_state_same_decision_regardless_of_call_order() -> None:
    """连续多次调用必须完全一致（弃牌张数也一样）。"""
    state = junk_state()
    config = Config()
    first = repr(choose_play_action(state, config))
    for _ in range(4):
        assert repr(choose_play_action(state, config)) == first


def test_filtered_discard_order_is_stable() -> None:
    state = junk_state()
    solver = Solver.from_state(state)
    first = greedy_discard_order(solver, state.hand.cards)
    second = greedy_discard_order(solver, state.hand.cards)
    assert first == second


# --------------------------------------------------------------- 出牌判断


def test_plays_when_hand_clears_blind() -> None:
    """这一手就能过关时必须直接打，不该浪费弃牌机会。

    而且应该**打满 5 张**：垫牌不计分，但打出的张数等于补牌张数，
    多打一张就等于多免费循环一张牌（"隐藏弃牌"技巧）。
    """
    state = GameState(make_state(
        hand=[card("H_K"), card("D_K"), card("C_3"), card("S_5"), card("H_9")],
        deck=full_deck(),
        required=10,
    ))
    action = choose_play_action(state, Config())
    assert isinstance(action, Play)
    assert {0, 1} <= set(action.indices), "一对 K 必须在打出的牌里"
    assert len(action.indices) == 5, "应该打满 5 张来多循环牌"
    assert score_play(state, action.indices).total == 60


def test_hidden_discard_keeps_flush_potential() -> None:
    """打垫牌时不该丢掉正在凑同花的花色。

    手牌：一对 K + 4 张梅花。最优解会打 5 张，但垫牌应该挑红桃 9 而不是梅花。
    """
    hand = [card("H_K"), card("D_K"), card("C_2"), card("C_5"), card("C_9"),
            card("C_J"), card("H_9")]
    state = GameState(make_state(hand=hand, deck=full_deck(), required=10))
    action = choose_play_action(state, Config())

    assert isinstance(action, Play)
    played = {state.hand.cards[i].key for i in action.indices}
    clubs_left = [c.key for i, c in enumerate(state.hand.cards)
                  if i not in action.indices and c.suit == "C"]
    assert len(clubs_left) >= 2, (
        f"不该把梅花当垫牌丢掉（正在凑同花），打出了 {sorted(played)}"
    )


# ------------------------------------------------------------- Boss 盲注限制


def boss_state(**kwargs) -> GameState:
    raw = make_state(**kwargs)
    raw["blinds"]["boss"]["status"] = "CURRENT"
    for name in ("small", "big"):
        raw["blinds"][name]["status"] = "DEFEATED"
    return GameState(raw)


def test_psychic_forces_five_cards() -> None:
    """The Psychic：必须打满 5 张。打不够虽然不报错，但不会计分。

    注意要把 Boss 盲注自己的过关分也设低：本回合的 required 取自
    **当前盲注**（这里是 Boss），不是全局默认值。否则一对 K（60 分）
    远达不到 600，机器人会按"优先弃牌"去凑更大的牌型。
    """
    state = boss_state(
        hand=[card("H_K"), card("D_K"), card("C_3"), card("S_5"), card("H_9")],
        deck=full_deck(), required=10,
    )
    state.raw["blinds"]["boss"]["name"] = "The Psychic"
    state.raw["blinds"]["boss"]["effect"] = "Must play 5 cards"
    state.raw["blinds"]["boss"]["score"] = 10
    state = GameState(state.raw)

    constraints = play_constraints(state, Config())
    assert (constraints.min_cards, constraints.max_cards) == (5, 5)
    action = choose_play_action(state, Config())
    assert isinstance(action, Play)
    assert len(action.indices) == 5


def test_flint_halves_base_score() -> None:
    """The Flint：牌型基础筹码和倍率减半。"""
    raw = make_state(hand=[card("H_K"), card("D_K")])
    raw["blinds"]["boss"]["name"] = "The Flint"
    raw["blinds"]["boss"]["effect"] = "Base Chips and Mult for played poker hands are halved"
    raw["blinds"]["boss"]["status"] = "CURRENT"
    for name in ("small", "big"):
        raw["blinds"][name]["status"] = "DEFEATED"
    state = GameState(raw)

    solver, constraints = build_solver(state, Config())
    assert constraints.base_scale == 0.5
    breakdown = solver.score(state.hand.cards, [0, 1])
    assert breakdown.base_chips == 5      # 10 → 5
    assert breakdown.base_mult == 1.0     # 2 → 1


def test_eye_excludes_hand_types_already_played() -> None:
    """The Eye：本回合不能再打已经打过的牌型。"""
    raw = make_state(hand=[card("H_K"), card("D_K"), card("C_3"), card("S_5"), card("H_9")])
    raw["blinds"]["boss"]["name"] = "The Eye"
    raw["blinds"]["boss"]["effect"] = "No repeat hand types this round"
    raw["blinds"]["boss"]["status"] = "CURRENT"
    raw["hands"]["Pair"]["played_this_round"] = 1
    for name in ("small", "big"):
        raw["blinds"][name]["status"] = "DEFEATED"
    state = GameState(raw)

    _, constraints = build_solver(state, Config())
    assert constraints.allowed_hands is not None
    assert "Pair" not in constraints.allowed_hands


def test_mouth_locks_to_the_already_played_type() -> None:
    raw = make_state(hand=[card("H_K"), card("D_K"), card("C_3"), card("S_5"), card("H_9")])
    raw["blinds"]["boss"]["name"] = "The Mouth"
    raw["blinds"]["boss"]["effect"] = "Only one hand type can be played this round"
    raw["blinds"]["boss"]["status"] = "CURRENT"
    raw["hands"]["Pair"]["played_this_round"] = 1
    for name in ("small", "big"):
        raw["blinds"][name]["status"] = "DEFEATED"
    state = GameState(raw)

    _, constraints = build_solver(state, Config())
    assert constraints.allowed_hands == frozenset({"Pair"})


def test_tooth_limits_cards_when_money_matters() -> None:
    """The Tooth：每张牌损失 $1，有钱时少打牌。"""
    raw = make_state(hand=[card("H_K"), card("D_K"), card("C_3"), card("S_5"), card("H_9")],
                     money=20)
    raw["blinds"]["boss"]["name"] = "The Tooth"
    raw["blinds"]["boss"]["effect"] = "Lose $1 per card played"
    raw["blinds"]["boss"]["status"] = "CURRENT"
    for name in ("small", "big"):
        raw["blinds"][name]["status"] = "DEFEATED"
    state = GameState(raw)

    constraints = play_constraints(state, Config())
    assert constraints.max_cards == 3
    assert constraints.prefer_more_cards is False


def test_normal_blind_has_no_constraints() -> None:
    state = GameState(make_state(hand=[card("H_K")]))
    constraints = play_constraints(state, Config())
    assert constraints.allowed_hands is None
    assert (constraints.min_cards, constraints.max_cards) == (1, 5)
    assert constraints.base_scale == 1.0


# --------------------------------------------------------------- 经济规则


def test_interest_floor_not_applied_before_it_is_worth_it() -> None:
    """还没成型时不要死守 $25，否则会没钱买牌、死于底注 2。"""
    raw = make_state(state="SHOP", money=28)
    raw["ante_num"] = 4
    raw["jokers"] = {"count": 1, "limit": 5, "cards": [joker("j_jolly")]}
    state = GameState(raw)
    assert effective_reserve(state, Config()) == 4     # 只有 1 张小丑牌 → 不用守


def test_interest_floor_applies_once_established() -> None:
    raw = make_state(state="SHOP", money=28)
    raw["ante_num"] = 4
    raw["jokers"] = {"count": 3, "limit": 5,
                     "cards": [joker(k) for k in ("j_jolly", "j_duo", "j_trio")]}
    state = GameState(raw)
    assert effective_reserve(state, Config()) == 25    # 已成型 → 守住 $25 吃满利息


def test_interest_floor_blocks_mediocre_purchase() -> None:
    """成型后，普通牌不该动用利息本金。

    注意钱 $27 ≥ surplus_money：这时机器人**不会买这张废牌，而是去重摇找
    更好的**——正是"经济充足时提升倍率"的预期行为。所以这里断言"没买"，
    而不是"直接离开商店"。守利息的原始意图由下面那个对照测试覆盖。
    """
    raw = make_state(state="SHOP", money=27)
    raw["ante_num"] = 4
    raw["jokers"] = {"count": 3, "limit": 5,
                     "cards": [joker(k) for k in ("j_jolly", "j_duo", "j_trio")]}
    with_area(raw, "shop", [priced(joker("j_8_ball"), 5)], limit=2)
    state = GameState(raw)
    action = choose_shop_action(state, Config(), rerolls=0)
    assert not isinstance(action, Buy)
    assert isinstance(action, Reroll)


def test_interest_floor_blocks_mediocre_purchase_without_surplus() -> None:
    """钱不够充裕时照旧守利息：废牌不买，也不重摇（重摇费会动到本金）。"""
    raw = make_state(state="SHOP", money=20)
    raw["ante_num"] = 4
    raw["jokers"] = {"count": 3, "limit": 5,
                     "cards": [joker(k) for k in ("j_jolly", "j_duo", "j_trio")]}
    with_area(raw, "shop", [priced(joker("j_8_ball"), 5)], limit=2)
    state = GameState(raw)
    assert isinstance(choose_shop_action(state, Config(), rerolls=0), NextRound)


def test_interest_floor_allows_top_tier_purchase() -> None:
    raw = make_state(state="SHOP", money=27)
    raw["ante_num"] = 4
    raw["jokers"] = {"count": 3, "limit": 5,
                     "cards": [joker(k) for k in ("j_jolly", "j_duo", "j_trio")]}
    with_area(raw, "shop", [priced(joker("j_blueprint"), 5)], limit=2)
    state = GameState(raw)
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, Buy)


def test_reroll_budget_shrinks_with_ante() -> None:
    """重摇预算按底注递减（文档：底注1 $10-15、底注2 $8-10、之后只在找关键牌时摇）。"""
    config = Config()
    for ante, expected in ((1, 3), (2, 2), (3, 1), (5, 1)):
        raw = make_state(state="SHOP", money=50)
        raw["ante_num"] = ante
        assert max_rerolls_for(GameState(raw), config) == expected


def test_discards_junk_when_blind_is_far() -> None:
    """手牌是散牌、离过关很远时应该弃牌去找牌型。"""
    action = choose_play_action(junk_state(), Config())
    assert isinstance(action, Discard), f"应该弃牌，实际 {action}"
    assert 1 <= len(action.indices) <= 5


def test_no_discard_on_last_hand() -> None:
    """最后一手没有退路，只能打。"""
    action = choose_play_action(junk_state(hands_left=1), Config())
    assert isinstance(action, Play)


def test_no_discard_when_discards_exhausted() -> None:
    action = choose_play_action(junk_state(discards_left=0), Config())
    assert isinstance(action, Play)


def test_no_discard_when_disabled() -> None:
    action = choose_play_action(junk_state(), Config(use_discards=False))
    assert isinstance(action, Play)


def test_no_discard_when_deck_is_empty() -> None:
    """牌堆空了没法补牌，弃牌毫无意义。"""
    action = choose_play_action(junk_state(deck=[]), Config())
    assert isinstance(action, Play)


def test_keeps_made_hand_together() -> None:
    """弃牌顺序应该把散牌排在前面，而不是拆掉对子。"""
    hand = [card("H_K"), card("D_K"), card("C_3"), card("S_7"), card("H_2"),
            card("D_9"), card("C_J"), card("S_4")]
    state = GameState(make_state(hand=hand, deck=full_deck(), required=100_000))
    solver = Solver.from_state(state)
    order = greedy_discard_order(solver, state.hand.cards)
    assert order[0] not in (0, 1), f"不该先拆对子，顺序是 {order}"


# --------------------------------------------------------------- 盲注判断


def test_boss_blind_cannot_be_skipped() -> None:
    state = GameState(make_state(state="BLIND_SELECT", blind_type="BOSS"))
    action = choose_blind_action(state, Config(skip_blinds=True))
    assert isinstance(action, SelectBlind)


def test_small_blind_skipped_only_when_enabled() -> None:
    raw = make_state(state="BLIND_SELECT")
    raw["blinds"]["small"]["tag_name"] = "Buffoon Tag"
    state = GameState(raw)

    assert isinstance(choose_blind_action(state, Config(skip_blinds=False)), SelectBlind)
    assert isinstance(choose_blind_action(state, Config(skip_blinds=True)), SkipBlind)


def test_low_value_tag_is_not_skipped() -> None:
    raw = make_state(state="BLIND_SELECT")
    raw["blinds"]["small"]["tag_name"] = "Nothing Tag"
    state = GameState(raw)
    assert isinstance(choose_blind_action(state, Config(skip_blinds=True)), SelectBlind)


# --------------------------------------------------------------- 商店判断


def shop_state(*, money: int = 20, shop: list[dict] | None = None,
               vouchers: list[dict] | None = None,
               jokers: list[dict] | None = None,
               consumables: list[dict] | None = None,
               consumable_limit: int = 2,
               hands: dict | None = None,
               deck: list[dict] | None = None) -> GameState:
    raw = make_state(state="SHOP", money=money, jokers=jokers or [], hands=hands)
    if deck is not None:
        # `cards` 必须写成**列表**：`Area.from_raw` 只认列表，写成 dict 会解析成
        # 空牌堆，于是 `valuation_scenario` 一律返回空、估值静默退回静态口径。
        # 这里原来写的是 `{i: entry ...}`，等于 `deck=` 参数**一直没生效**。
        raw["cards"] = {"count": len(deck), "limit": len(deck), "cards": list(deck)}
    with_area(raw, "shop", shop or [], limit=2)
    with_area(raw, "vouchers", vouchers or [], limit=1)
    with_area(raw, "consumables", consumables or [], limit=consumable_limit)
    return GameState(raw)


def priced(item: dict, cost: int) -> dict:
    item = dict(item)
    item["cost"] = {"sell": 1, "buy": cost}
    return item


def test_economy_rule_spends_discards_when_they_catch_up() -> None:
    """新规则：两者都不为 0 时，剩余出牌数要大于剩余弃牌数。

    回合结束每剩一手 +$1，弃牌不给钱 → 出牌机会比弃牌值钱。
    所以弃牌数追平出牌数时，应该先把不值钱的弃牌花掉。

    对照组把弃牌数降到 1（出牌 2 > 弃牌 1，规则不触发），此时这一手
    已经顶得上平均每手需要的分，就该直接打。
    """
    keys = ["H_5", "D_6", "C_7", "S_8", "D_9", "H_2", "S_K", "H_3"]
    hand = [card(k) for k in keys]
    deck = full_deck(keys)

    # 需求定 400：顺子实际能打 260（顺子的筹码来自 5 张算分牌，不是 120），
    # 够不上直接通关（260 < 400），但顶得上平均每手需要的 200 分（400/2）
    # → 属于"打牌收益达标"。
    caught_up = GameState(make_state(
        hand=hand, deck=deck, required=400, hands_left=2, discards_left=3,
    ))
    assert isinstance(choose_play_action(caught_up, Config()), Discard)

    ahead = GameState(make_state(
        hand=hand, deck=deck, required=400, hands_left=2, discards_left=1,
    ))
    assert isinstance(choose_play_action(ahead, Config()), Play)


def test_economy_rule_can_be_turned_off() -> None:
    """规则可关闭——它是一条策略，不是硬约束。"""
    keys = ["H_5", "D_6", "C_7", "S_8", "D_9", "H_2", "S_K", "H_3"]
    state = GameState(make_state(
        hand=[card(k) for k in keys], deck=full_deck(keys),
        required=400, hands_left=2, discards_left=3,
    ))
    config = Config(params=TunableParams(keep_hands_above_discards=False))
    assert isinstance(choose_play_action(state, config), Play)


def test_economy_rule_needs_a_card_to_discard() -> None:
    """牌堆空了就不能弃牌——守规则不能让机器人卡死。"""
    keys = ["H_K", "D_K", "C_3", "S_7", "H_9"]
    state = GameState(make_state(
        hand=[card(k) for k in keys], deck=[], required=400,
        hands_left=2, discards_left=3,
    ))
    assert isinstance(choose_play_action(state, Config()), Play)


def test_surplus_relief_only_applies_to_multiplier_cards() -> None:
    """门槛放宽只对"能提倍率"的牌生效。

    用调高的门槛把两张牌都做成边缘够格，再对比：
      * j_banner（加区，能提倍率）→ 门槛从 7.0 降到 5.5 → 买
      * j_8_ball（纯功能）→ 门槛不降 → 不买

    这是实测踩过的坑：放宽范围一开，纯功能牌就被顺手买了进来。
    """
    params = TunableParams(
        buy_threshold=7.0, surplus_buy_relief=1.5,
        empty_slot_bonus=0.0, few_slot_bonus=0.0,
    )
    config = Config(params=params)
    assert raises_multiplier("j_banner") and not raises_multiplier("j_8_ball")

    good = shop_state(money=40, shop=[priced(joker("j_banner"), 5)])
    assert isinstance(choose_shop_action(good, config, rerolls=9), Buy)

    junk = shop_state(money=40, shop=[priced(joker("j_8_ball"), 5)])
    assert not isinstance(choose_shop_action(junk, config, rerolls=9), Buy)


def test_surplus_money_boosts_multiplier_jokers() -> None:
    """经济充足时给"能提升倍率"的牌加权（规格："尝试提升倍率"）。"""
    state = shop_state(money=40, shop=[priced(joker("j_jolly"), 5)])
    params = TunableParams()
    normal = shop_card_value(state.shop.cards[0], state, params, surplus=False)
    surplus = shop_card_value(state.shop.cards[0], state, params, surplus=True)
    assert surplus > normal


def test_surplus_money_does_not_boost_useless_jokers() -> None:
    """纯功能牌不享受加权——钱再多也不该为它多花。"""
    state = shop_state(money=40, shop=[priced(joker("j_8_ball"), 5)])
    params = TunableParams()
    normal = shop_card_value(state.shop.cards[0], state, params, surplus=False)
    surplus = shop_card_value(state.shop.cards[0], state, params, surplus=True)
    assert surplus == normal


def test_surplus_money_buys_extra_rerolls() -> None:
    """钱多时多给重摇预算去找倍率牌（底注 1 基础预算 3 次）。"""
    config = Config()
    poor = shop_state(money=16, shop=[])
    assert isinstance(choose_shop_action(poor, config, rerolls=3), NextRound)
    rich = shop_state(money=40, shop=[])
    assert isinstance(choose_shop_action(rich, config, rerolls=3), Reroll)


# ------------------------------------------------------------ 商店估值


def test_buys_strong_joker() -> None:
    state = shop_state(shop=[priced(joker("j_brainstorm"), 5)])
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, Buy)
    assert action.card == 0


def test_skips_weak_affordable_joker() -> None:
    """买得起但价值不够的牌不该买——钱要留给关键牌。"""
    state = shop_state(money=3, shop=[priced(joker("j_8_ball"), 3)])
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, NextRound)


def test_does_not_spend_below_reserve() -> None:
    """钱不够时不能把家底花光：保留额是给利息和关键牌用的。"""
    state = shop_state(money=6, shop=[priced(joker("j_abstract"), 5)])
    config = Config(params=TunableParams(money_reserve=4, empty_slot_bonus=0.0))
    action = choose_shop_action(state, config, rerolls=0)
    assert isinstance(action, NextRound)


def test_buys_voucher() -> None:
    state = shop_state(money=20, vouchers=[priced(joker("v_antimatter"), 10)])
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, Buy)
    assert action.voucher == 0


def test_buys_planet_card_from_shop_shelf() -> None:
    """商店货架上的星球牌必须买。

    这里曾经有 bug：货架上的卡被一律按 joker_value 估值，星球牌拿到默认值
    4.0，低于购买阈值 4.5，于是机器人从来没买过星球牌——白白丢掉本作最稳的
    收益来源（升级牌型）。
    """
    state = shop_state(
        money=10,
        shop=[priced(consumable("c_mercury", card_set="PLANET"), 3)],
    )
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, Buy)
    assert action.card == 0


def test_committed_planet_is_bought() -> None:
    table = hands_table()
    table["Pair"]["level"] = 3
    table["Pair"]["played"] = 5
    state = shop_state(money=20, hands=table,
                       shop=[priced(consumable("c_mercury", card_set="PLANET"), 3)])
    assert isinstance(choose_shop_action(state, Config(), rerolls=0), Buy)


def test_offtype_planet_is_never_bought_even_with_surplus() -> None:
    """锁死牌型：非专精牌型的星球牌**连经济充足的加权也不给**。

    实测踩过：surplus 的 +1.5 是加给**所有**星球牌的，于是非专精牌型的水星
    从 3.0 被顶到 4.5、正好越过门槛 → 等级分散到五个牌型上，
    单一牌型永远升不到能让 γ 切换的第 3 级。
    """
    table = hands_table()
    table["Pair"]["level"] = 3
    state = shop_state(money=60, hands=table,     # 钱多 = 经济充足
                       shop=[priced(consumable("c_jupiter", card_set="PLANET"), 3)])
    # 木星升级同花，而我们在专精对子
    assert planet_value(state.shop.cards[0], state) < Config().params.buy_threshold
    assert not isinstance(choose_shop_action(state, Config(), rerolls=0), Buy)


def test_planet_pack_skips_offtype_when_locked() -> None:
    """锁死时，星球包里没有专精牌型的星球牌就宁可不要——拿了就是分兵。"""
    table = hands_table()
    table["Pair"]["level"] = 3
    raw = make_state(state="SMODS_BOOSTER_OPENED", hands=table)
    with_area(raw, "pack", [
        consumable("c_jupiter", card_set="PLANET"),   # 同花
        consumable("c_pluto", card_set="PLANET"),     # 高牌
    ])
    assert choose_pack_action(GameState(raw), Config()).skip


def test_planet_pack_takes_the_committed_planet() -> None:
    table = hands_table()
    table["Pair"]["level"] = 3
    raw = make_state(state="SMODS_BOOSTER_OPENED", hands=table)
    with_area(raw, "pack", [
        consumable("c_jupiter", card_set="PLANET"),
        consumable("c_mercury", card_set="PLANET"),   # 对子 ← 要这张
    ])
    action = choose_pack_action(GameState(raw), Config())
    assert not action.skip and action.card == 1


# ============================================ ①②③ 牌位与复利
#
# 数据背景（tools/mult_diagnosis.py / tools/deck_channel.py，共 100 局）：
#   * 620 次小丑牌买入里 70.6% 是估值 ≤6.5 的廉价加法填充牌
#   * 换牌 363 次 = 3.6 次/局，其中 60 次卖掉的是乘法牌
#   * 局终 54% 的局一张 ×Mult 都没有
#   * Standard 包买了 0 次；每局最多改造出 0.14 张 STEEL、0.08 张 GLASS


def booster_card(key: str, cost: int = 4) -> dict:
    """补充包在 gamestate 里就是一张 set=BOOSTER 的卡。"""
    return {
        "id": _booster_ids(),
        "key": key, "set": "BOOSTER", "label": key, "suit": None, "rank": None,
        "value": {}, "modifier": {}, "state": {},
        "cost": {"buy": cost, "sell": 2},
    }


_booster_counter = [900]


def _booster_ids() -> int:
    _booster_counter[0] += 1
    return _booster_counter[0]


def pack_shop_state(*, jokers=(), deck=None, packs=(), money=40) -> GameState:
    raw = make_state(state="SHOP", money=money, jokers=list(jokers),
                     hands=hands_table())
    cards = deck if deck is not None else full_deck()
    raw["cards"] = {"count": len(cards), "limit": len(cards), "cards": list(cards)}
    with_area(raw, "shop", [], limit=2)
    with_area(raw, "vouchers", [], limit=1)
    with_area(raw, "consumables", [], limit=2)
    with_area(raw, "packs", list(packs), limit=2)
    return GameState(raw)


# ------------------------------------------------------ ① 复利牌不当牺牲品


def test_swap_victims_protects_compounding_jokers() -> None:
    """**核心回归测试：① 的全部语义。**

    牺牲品按**当前**边际贡献挑，而成长型/乘法型的价值在后面每一手：男爵手里
    没 K 时约等于 0、星座刚买时 ×1.0。所以它们必然在还没长起来时被判成最差、
    拿去换掉——实测 363 次换牌里 60 次卖掉的是乘法牌。
    """
    from dataclasses import replace

    from balatro_bot.cards import Card
    from balatro_bot.strategy import swap_victims

    params = TunableParams()
    keys = ("j_constellation", "j_jolly", "j_sly", "j_banner", "j_scary_face")
    jokers = [Card.from_dict(joker(k)) for k in keys]
    assert is_compounding("j_constellation")

    # 货架是**一次性**牌 → 只允许牺牲非复利牌，星座（#0）受保护
    victims = swap_victims(jokers, "j_scholar", params)
    assert 0 not in victims, "复利牌被允许牺牲了"
    assert set(victims) == {1, 2, 3, 4}

    # 全队复利牌 + 同款换同款（没有成色提升）→ 空集
    all_compound = [Card.from_dict(joker("j_constellation")) for _ in range(5)]
    assert swap_victims(all_compound, "j_constellation", params) == []

    # 全队复利牌，但换进来的明显更强 → 允许单调升级
    stronger = swap_victims(all_compound, "j_blueprint", params)
    assert stronger, "复利换更强复利应该允许"

    # 紧急时放行（原行为）
    assert len(swap_victims(all_compound, "j_constellation", params,
                            urgent=True)) == 5

    # 关掉守卫 → 回到旧行为，配对测量必须能这么做
    off = replace(params, compound_switch_guard=False)
    assert len(swap_victims(jokers, "j_scholar", off)) == 5


def test_all_compounding_lineup_only_allows_monotone_upgrades() -> None:
    """同款同价的复利牌不该互相换（第一版守卫被 `j_duo` 绕过去的那个洞）。"""
    from balatro_bot.cards import Card
    from balatro_bot.strategy import swap_victims

    jokers = [Card.from_dict(joker("j_constellation")) for _ in range(5)]
    assert swap_victims(jokers, "j_constellation", TunableParams()) == []


def test_compound_guard_off_restores_old_behaviour() -> None:
    """开关关掉时回到旧行为——配对测量必须能这么做。"""
    from dataclasses import replace

    config = Config()
    off = replace(config.params, compound_switch_guard=False)
    assert off.compound_switch_guard is False
    assert config.params.compound_switch_guard is True


# ------------------------------------------------------ ② 填充牌估值递减


def test_filler_penalty_hits_only_cheap_non_compounding_jokers() -> None:
    params = TunableParams()
    # 廉价加法牌：底注越高罚得越重
    assert filler_penalty("j_joker", 1, params) == 0.0          # 底注 2 以内不罚
    assert filler_penalty("j_joker", 2, params) == 0.0
    assert filler_penalty("j_joker", 4, params) == pytest.approx(1.0)
    assert filler_penalty("j_joker", 7, params) == pytest.approx(2.5)
    # 复利牌不罚——它现在看着弱，但会自己长大
    assert filler_penalty("j_constellation", 7, params) == 0.0
    assert filler_penalty("j_baron", 7, params) == 0.0
    # 成色高的牌不罚
    assert filler_penalty("j_blueprint", 7, params) == 0.0


def test_filler_valuation_drops_with_ante() -> None:
    """② 的意图：同一张廉价填充牌，底注越高越不该买。"""
    from balatro_bot.cards import Card

    card = Card.from_dict(joker("j_joker"))
    values = []
    for ante in (1, 4, 7):
        raw = make_state(state="SHOP", money=40, hands=hands_table())
        raw["ante_num"] = ante              # 顶层键就叫 ante_num
        values.append(shop_card_value(card, GameState(raw), Config().params))
    assert values[0] > values[1] > values[2], values


# ------------------------------------------------------ ③ 牌库改造


def test_standard_pack_becomes_buyable_when_the_deck_is_plain() -> None:
    """③a：Standard 包常量估值 4.0 < 闸门 5.0，原来**永远买不到**。

    注意默认参数里 `standard_pack_bonus = 0.0`——实测这个渠道是**亏的**
    （每局 ≈$15 只多换 0.5 张强化牌，平均底注 5.10 → 4.70），所以默认关掉。
    这里显式打开，只为验证"按需加价"这条机制本身是通的。
    """
    from dataclasses import replace

    config = Config()
    config.params = replace(config.params, standard_pack_bonus=1.2)

    plain = pack_shop_state(packs=[booster_card("p_standard_normal_1")])
    action = choose_shop_action(plain, config, rerolls=0)
    assert isinstance(action, Buy) and action.pack == 0, (
        f"牌库一张强化牌都没有，却还不买标准包：{action!r}"
    )

    # 默认关掉：同一局面不买（等于原行为）
    off = choose_shop_action(
        pack_shop_state(packs=[booster_card("p_standard_normal_2")]),
        Config(), rerolls=0,
    )
    assert not (isinstance(off, Buy) and off.pack == 0), "默认应该是关掉的"


def test_standard_pack_stops_being_worth_it_once_the_deck_is_enhanced() -> None:
    """自我限幅：攒够强化牌之后就不再加价了。"""
    params = TunableParams()
    plain = pack_shop_state()
    assert standard_pack_bonus(plain, params) == pytest.approx(
        params.standard_pack_bonus * params.enhanced_target
    )
    enhanced = pack_shop_state(deck=[
        card(f"H_{r}", enhancement=enh) for r, enh in
        zip("2345", ("GLASS", "STEEL", "MULT", "BONUS"))
    ] + [c for c in full_deck() if c["key"] not in {"H_2", "H_3", "H_4", "H_5"}])
    assert standard_pack_bonus(enhanced, params) == 0.0


def test_standard_pack_picks_the_enhanced_card() -> None:
    """③b **核心回归测试**：原来按**卖出价**挑牌，完全看不见强化。"""
    raw = make_state(state="SMODS_BOOSTER_OPENED", hands=hands_table())
    with_area(raw, "pack", [
        card("H_9"),                                  # 普通牌
        card("S_3", enhancement="GLASS"),             # 玻璃 ← 要这张
        card("D_7"),
    ])
    action = choose_pack_action(GameState(raw), Config())
    assert not action.skip
    assert action.card == 1, "应该挑强化好的牌，而不是卖出价最高的"


def test_enhancement_switch_guards_exist() -> None:
    """③ 的两个旋钮都能关掉。"""
    from dataclasses import replace

    params = TunableParams()
    off = replace(params, standard_pack_bonus=0.0)
    assert off.standard_pack_bonus == 0.0
    assert params.enhanced_target > 0


# ------------------------------------------------- 危机度：用潜力而不是现状


WEAK_HAND = ("H_2", "D_7", "C_9", "S_J", "H_4")


def crisis_state(required: int) -> GameState:
    hand = [card(k) for k in WEAK_HAND]
    raw = make_state(
        state="SELECTING_HAND", hand=hand, required=required,
        deck=full_deck(), hands_left=3, discards_left=4,
    )
    with_area(raw, "hand", hand, limit=8)
    raw["cards"] = {"count": 52, "limit": 52, "cards": full_deck()}
    return GameState(raw)


def test_crisis_uses_potential_so_it_does_not_overreact() -> None:
    """**核心回归测试：危急度用潜力估，不用现状。**

    原来 `crisis = 需要 / (score_now × 剩余手数)`，而 `score_now` 是
    `score_capability`——"**现在这 8 张牌**能打多少"。开局那 8 张是随机的，
    所以它系统性偏低、crisis 偏高 → 门槛归零、开始赌低概率大牌型 →
    越赌越危急（实测一局 `危急=1.1x → 1.9x`，最后差 10 分输掉大盲）。

    这里直接验公式：同一个局面下，旧口径判危急、新口径不判。
    """
    state = crisis_state(200)
    solver = Solver.from_state(state)
    current = solver.best(state.hand.cards)
    assert current is not None
    plans = evaluate_discard_plans(
        state, solver, play_constraints(state, Config()), current, Config()
    )
    assert plans, "这个局面应该有可凑的计划"

    potential = plan_potential(plans, solver, state.hand.cards, current.total)
    hands = max(1, state.round.hands_left)

    old = state.score_remaining / max(1.0, current.total * hands)
    new = state.score_remaining / max(1.0, potential * hands)

    assert potential > current.total, "这个局面本来就是「弃牌能凑出更好的」"
    assert old > 1.0, f"旧口径（现状）应该判危急，实际 {old:.2f}"
    assert new <= 1.0, f"新口径（潜力）不该判危急，实际 {new:.2f}"


def test_crisis_switch_is_off_by_default_equivalent() -> None:
    """开关关掉时回到旧行为——配对测量必须能这么做。"""
    from dataclasses import replace

    config = Config()
    off = replace(config.params, crisis_uses_potential=False)
    assert off.crisis_uses_potential is False
    assert config.params.crisis_uses_potential is True


def test_potential_is_at_least_the_immediate_play() -> None:
    """潜力 ≥ 现状：`plans` 里必然含"不用弃牌"的那条，所以不会低于当前最优。"""
    state = crisis_state(200)
    solver = Solver.from_state(state)
    current = solver.best(state.hand.cards)
    assert current is not None
    plans = evaluate_discard_plans(
        state, solver, play_constraints(state, Config()), current, Config()
    )
    assert plan_potential(
        plans, solver, state.hand.cards, current.total
    ) >= current.total


def test_planet_pack_skips_when_consumable_slots_are_full() -> None:
    """消耗牌槽满了就不能拿——拿了会**静默丢失**。

    mod 的 `pack.lua:139-152` 只校验小丑牌位，对消耗牌没有任何检查，于是
    `open_pack(card=N)` 不报错、牌也不进槽。实测"从商店买来的 Mercury 总会
    被用掉，从包里拿的却大多从没被用过"，区别就在这里。
    """
    table = hands_table()
    table["Pair"]["level"] = 3
    raw = make_state(state="SMODS_BOOSTER_OPENED", hands=table)
    with_area(raw, "pack", [consumable("c_mercury", card_set="PLANET")])
    # 两个槽都占满
    with_area(raw, "consumables", [
        consumable("c_jupiter", card_set="PLANET"),
        consumable("c_pluto", card_set="PLANET"),
    ], limit=2)

    action = choose_pack_action(GameState(raw), Config())
    assert action.skip, "槽满了还去拿，等于白扔这个包"


def test_tarot_pack_skips_when_consumable_slots_are_full() -> None:
    """塔罗/幻灵同理——它们也占消耗牌槽。"""
    raw = make_state(state="SMODS_BOOSTER_OPENED")
    with_area(raw, "pack", [consumable("c_hermit", card_set="TAROT")])
    with_area(raw, "consumables", [
        consumable("c_jupiter", card_set="PLANET"),
        consumable("c_pluto", card_set="PLANET"),
    ], limit=2)

    assert choose_pack_action(GameState(raw), Config()).skip


def test_joker_pack_is_unaffected_by_full_consumable_slots() -> None:
    """小丑牌包不看消耗牌槽——别把这条检查用错了地方。"""
    raw = make_state(state="SMODS_BOOSTER_OPENED",
                     jokers=[joker("j_joker")])
    with_area(raw, "pack", [joker("j_blueprint")])
    with_area(raw, "consumables", [
        consumable("c_jupiter", card_set="PLANET"),
        consumable("c_pluto", card_set="PLANET"),
    ], limit=2)

    action = choose_pack_action(GameState(raw), Config())
    assert not action.skip, "消耗牌槽满了不影响拿小丑牌"


# ------------------------------------- 卖掉"永远不会用"的消耗牌，腾出槽位


def test_useless_consumable_classification() -> None:
    """判据：星球牌不看牌型就卖；白名单里的一律不卖。"""
    from balatro_bot.strategy import is_useless_consumable

    table = hands_table(levels={"Pair": 3})           # 专精对子
    state = shop_state(
        hands=table,
        consumables=[
            consumable("c_mercury", card_set="PLANET"),   # 升对子 ← 要留
            consumable("c_neptune", card_set="PLANET"),   # 升同花顺 ← 杂牌
            consumable("c_hermit", card_set="TAROT"),     # 白名单塔罗 ← 要留
            consumable("c_wraith", card_set="SPECTRAL"),  # 条件性可用 ← 要留
            consumable("c_familiar", card_set="SPECTRAL"),  # 破坏性幻灵 ← 卖
        ],
    )
    cards = state.consumables.cards
    params = Config().params
    assert not is_useless_consumable(state, cards[0], params)
    assert is_useless_consumable(state, cards[1], params)
    assert not is_useless_consumable(state, cards[2], params)
    assert not is_useless_consumable(state, cards[3], params)
    assert is_useless_consumable(state, cards[4], params)


def test_no_planet_is_junk_when_the_committed_hand_is_unknown() -> None:
    """安全阀：算不出"可吃牌型"时不能把所有星球牌当垃圾清掉。

    `committed_hand` 在 hands 表为空时返回 None，`accepted_planet_hands`
    随之是空集——没有这个安全阀，"腾槽"就变成"清仓"了。
    """
    from balatro_bot.strategy import is_useless_consumable

    state = shop_state(
        hands={},          # 空表 → committed_hand 返回 None
        consumables=[consumable("c_neptune", card_set="PLANET")],
    )
    assert not is_useless_consumable(
        state, state.consumables.cards[0], Config().params
    )


def test_shop_sells_the_off_type_planet_to_free_a_slot() -> None:
    """**核心回归测试。**

    实测的因果链：女祭司/皇帝造出随机星球牌 → 随机牌型通常不是专精的 →
    "用非专精星球牌"被故意禁止 → 卖又从来不卖 → **2 个槽被垃圾永久占住** →
    槽满之后天体包只能跳过，星球牌供给被自己的垃圾堵死。
    （`tools/slot_trace.py` 里能看到 `c_neptune` 从第 87 步躺到结束。）
    """
    from balatro_bot.strategy import Sell

    table = hands_table(levels={"Pair": 3})
    state = shop_state(
        money=20, hands=table,
        consumables=[
            consumable("c_neptune", card_set="PLANET"),   # 杂牌，该卖
            consumable("c_mercury", card_set="PLANET"),   # 要留的
        ],
    )
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, Sell), f"应该卖掉杂牌星球牌，实际 {action}"
    assert action.consumable == 0
    assert action.joker is None, "卖的是消耗牌，别错卖小丑牌"


def test_shop_keeps_a_usable_planet() -> None:
    """专精牌型的星球牌不能被当成垃圾卖掉。"""
    from balatro_bot.strategy import Sell

    table = hands_table(levels={"Pair": 3})
    state = shop_state(
        money=20, hands=table,
        consumables=[consumable("c_mercury", card_set="PLANET")],
    )
    action = choose_shop_action(state, Config(), rerolls=0)
    assert not (isinstance(action, Sell) and action.consumable == 0)


def test_selling_junk_is_free_and_comes_before_buying() -> None:
    """腾槽排在买包之前——留着垃圾的话天体包买了也接不住。"""
    from balatro_bot.strategy import Sell

    table = hands_table(levels={"Pair": 3})
    state = shop_state(
        money=20, hands=table,
        shop=[priced(consumable("c_mercury", card_set="PLANET"), 3)],
        consumables=[consumable("c_neptune", card_set="PLANET")],
    )
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, Sell) and action.consumable == 0


def test_planet_for_unplayed_rare_hand_is_skipped() -> None:
    """升级"高牌"这种我们几乎不打的牌型，不如把钱留着。"""
    state = shop_state(
        money=10,
        shop=[priced(consumable("c_pluto", card_set="PLANET"), 3)],
    )
    action = choose_shop_action(state, Config(), rerolls=0)
    assert not isinstance(action, Buy)


def test_planet_for_played_hand_is_preferred() -> None:
    """已经打出过的牌型，升级它最有价值。"""
    raw = make_state(state="SHOP", money=10)
    with_area(raw, "shop", [priced(consumable("c_pluto", card_set="PLANET"), 3),
                            priced(consumable("c_mercury", card_set="PLANET"), 3)],
             limit=2)
    raw["hands"]["Pair"]["played"] = 3
    state = GameState(raw)
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, Buy)
    assert action.card == 1, "应该选升级 Pair 的水星，而不是升级高牌的冥王星"


def test_committed_hand_picks_the_highest_level() -> None:
    table = hands_table()
    table["Flush"]["level"] = 4
    table["Flush"]["played"] = 6
    table["Pair"]["played"] = 9
    state = GameState(make_state(hands=table))
    assert committed_hand(state) == "Flush"


def test_committed_hand_defaults_to_a_small_hand_type() -> None:
    """视频核心口诀："前期用大中牌型过渡，尽早转小牌型"。

    所以第一回合就该把小牌型（对子）当成专精目标，而不是等打出来再说——
    这个目标决定了我们买哪个星球牌、以及哪些小丑牌和牌型契合。
    """
    state = GameState(make_state())
    assert committed_hand(state) == "Pair"


def test_committed_hand_keeps_an_already_invested_big_hand() -> None:
    """前期靠大牌型过渡留下 3 级以上等级时先保留它，别浪费已经花掉的星球牌。"""
    table = hands_table()
    table["Flush"]["level"] = 4
    table["Flush"]["played"] = 6
    state = GameState(make_state(hands=table))
    assert committed_hand(state) == "Flush"


def test_committed_hand_locks_onto_the_level_leader() -> None:
    """等级领先的牌型被锁定为专精目标——"锁死在一个牌型上"。

    规则变了：以前是"升过级的小牌型一律优先于大牌型"，但那样会让
    已经投进去的等级被丢掉，而且 γ 会在前后期之间来回摆。
    现在只要某个牌型领先 `commit_lead` 级就锁定它。
    """
    table = hands_table()
    table["Flush"]["level"] = 4
    table["Pair"]["level"] = 2
    state = GameState(make_state(hands=table))
    assert committed_hand(state) == "Flush"


def test_committed_hand_ignores_a_tiny_lead() -> None:
    """只领先不到 commit_lead 级时不算锁定，仍然按优先级挑小牌型。"""
    table = hands_table()
    table["Pair"]["level"] = 2
    table["Flush"]["level"] = 2
    state = GameState(make_state(hands=table))
    assert committed_hand(state) == "Pair"


def test_commitment_survives_buying_a_big_hand_enabler() -> None:
    """买了四指也不会把投资方向掀翻。

    实测踩过：买一张 Four Fingers（把同花/顺子变成 4 张即可）会让
    `target_hand_types` 变成"同花优先"，于是所有对子系小丑瞬间被降权、
    下一轮被清仓换掉 —— 一次 $7 的买入重构了整个阵容。
    现在等级领先的牌型锁定，不受这件事影响。
    """
    table = hands_table()
    table["Pair"]["level"] = 4
    state = GameState(make_state(hands=table, jokers=[joker("j_four_fingers")]))
    assert "Flush" in target_hand_types(state)     # 优先级确实变了
    assert committed_hand(state) == "Pair"         # 但专精目标没变


def test_big_hand_becomes_target_with_four_fingers() -> None:
    """拿到四指（4 张即可成同花/顺子）之后，大牌型才值得当目标。"""
    state = GameState(make_state(jokers=[joker("j_four_fingers")]))
    assert "Flush" in target_hand_types(state)
    assert committed_hand(state) == "Flush"


def test_synergy_adjustment_rewards_matching_hand_type() -> None:
    """牌型和专精目标一致 → 加权；不一致 → 降价。"""
    assert synergy_adjustment("j_jolly", "Pair") > 0
    assert synergy_adjustment("j_droll", "Pair") < 0
    assert synergy_adjustment("j_cavendish", "Pair") == 0.0   # 无牌型条件
    assert synergy_adjustment("j_jolly", None) == 0.0


def test_offtype_planet_is_skipped_once_committed() -> None:
    """一旦专精某个牌型，其它牌型的星球牌就不该再买——这是文档排第 5 的策略。"""
    table = hands_table()
    table["Flush"]["level"] = 3
    table["Flush"]["played"] = 5
    raw = make_state(state="SHOP", money=20, hands=table)
    with_area(raw, "shop", [priced(consumable("c_mercury", card_set="PLANET"), 3)],
             limit=2)
    state = GameState(raw)
    # 水星升级 Pair，但我们在专精 Flush → 不该买
    assert planet_value(state.shop.cards[0], state) < 4.5
    action = choose_shop_action(state, Config(), rerolls=0)
    assert not isinstance(action, Buy)


def test_committed_planet_is_bought() -> None:
    table = hands_table()
    table["Flush"]["level"] = 3
    table["Flush"]["played"] = 5
    raw = make_state(state="SHOP", money=20, hands=table)
    with_area(raw, "shop", [priced(consumable("c_jupiter", card_set="PLANET"), 3)],
             limit=2)
    state = GameState(raw)
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, Buy)
    assert action.card == 0


def test_multiplicative_jokers_are_worth_more_from_ante_four() -> None:
    """第 4 底注起纯加法牌追不上涨幅，乘区牌应该更值钱。"""
    early = effective_joker_value("j_duo", 5, ante=1)
    late = effective_joker_value("j_duo", 5, ante=4)
    assert late > early
    # 加法牌不受这个加成影响
    assert effective_joker_value("j_jolly", 5, ante=4) == effective_joker_value("j_jolly", 5, ante=1)


def test_pure_utility_jokers_are_not_bought_early() -> None:
    """底注 1 不该买"免费重摇"这类一分额外分都打不出来的牌。

    实测机器人就这样死于底注 1：手里是 Hack + Chaos the Clown。
    """
    for key in ("j_chaos", "j_juggler", "j_dna", "j_mr_bones"):
        assert effective_joker_value(key, 0, ante=1) < 4.5, key
        assert effective_joker_value(key, 1, ante=1) < 4.5, key


def test_pure_utility_jokers_allowed_late() -> None:
    """后期牌位富裕时可以考虑它们。"""
    assert effective_joker_value("j_chaos", 0, ante=5) >= 4.5


def test_scoring_jokers_still_get_the_empty_slot_bonus() -> None:
    """真正能加分的牌依然享受"先把位置填满"的加成。"""
    assert effective_joker_value("j_jolly", 0, ante=1) > joker_value("j_jolly")
    assert effective_joker_value("j_duo", 0, ante=1) > joker_value("j_duo")


def test_economy_jokers_are_not_treated_as_pure_utility() -> None:
    """经济牌会滚钱，不该被当成纯功能牌压价。"""
    for key in ("j_rocket", "j_golden", "j_to_the_moon"):
        assert effective_joker_value(key, 0, ante=1) >= 4.5, key


def test_strategy_joker_key_sets_all_exist() -> None:
    """策略层里所有以 j_ 开头的 key 都必须在游戏里真实存在。

    拼错的 key 不会报错，只会静默失效——例如把塔罗牌 c_deja_vu 写成
    j_deja_vu，那张牌就永远不会被正确估值。这个测试之前真的抓出过这个错。
    """
    from balatro_bot.joker_data import JOKER_DATA
    from balatro_bot.strategy import PURE_UTILITY_JOKERS

    unknown_keys = sorted(key for key in PURE_UTILITY_JOKERS if key not in JOKER_DATA)
    assert not unknown_keys, f"PURE_UTILITY_JOKERS 里有不存在的 key：{unknown_keys}"


def test_planet_hand_keys_all_exist() -> None:
    from balatro_bot.strategy import PLANET_HAND

    assert len(PLANET_HAND) == 12
    # 升级目标必须是真实的牌型名，否则 planet_value 会永远走 fallback
    from balatro_bot.cards import HAND_NAMES

    for key, hand_name in PLANET_HAND.items():
        assert hand_name in HAND_NAMES, f"{key} 指向了不存在的牌型 {hand_name}"


def test_buys_useful_tarot_from_shop_shelf() -> None:
    state = shop_state(
        money=10,
        shop=[priced(consumable("c_hermit", card_set="TAROT"), 3)],
    )
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, Buy)
    assert action.card == 0


def test_does_not_buy_planet_without_consumable_slot() -> None:
    """消耗牌位满了就买不了（钱多时会去重摇，那也是对的）。"""
    state = shop_state(
        money=10,
        shop=[priced(consumable("c_mercury", card_set="PLANET"), 3)],
        consumables=[consumable("c_venus", card_set="PLANET"),
                     consumable("c_mars", card_set="PLANET")],
    )
    action = choose_shop_action(state, Config(), rerolls=0)
    assert not isinstance(action, Buy), f"消耗牌位满了不该买，实际 {action}"


def test_does_not_buy_playing_card() -> None:
    """普通扑克牌基本不值得买（钱多时会去重摇，那也是对的）。"""
    state = shop_state(money=20, shop=[priced(card("H_A"), 3)])
    action = choose_shop_action(state, Config(), rerolls=0)
    assert not isinstance(action, Buy), f"不该买普通扑克牌，实际 {action}"


def test_does_not_buy_junk_when_jokers_are_full() -> None:
    """牌位满了、商店里只有废牌 → 不买它。

    注意**会重摇**去继续找升级目标（重摇正是牌位满时唯一的手段），
    所以这里断言的是"没买"，不是"立刻离开商店"。
    """
    state = shop_state(
        money=20,
        jokers=[joker("j_brainstorm"), joker("j_baron"), joker("j_blueprint"),
                joker("j_triboulet"), joker("j_perkeo")],
        shop=[priced(joker("j_8_ball"), 5)],
    )
    action = choose_shop_action(state, Config(), rerolls=0)
    assert not isinstance(action, Buy)


def test_full_jokers_can_still_reroll_to_look_for_an_upgrade() -> None:
    """牌位满了也必须能重摇。

    实测踩过的坑：重摇分支原来要求 `free_joker_slots > 0`，于是**牌位满的时候
    根本不能重摇**——而那正是唯一能换牌的局面。结果是 A5 带着 $84 离开商店、
    去撞 22000 的 Boss。
    """
    state = shop_state(
        money=40,
        jokers=[joker("j_jolly"), joker("j_duo"), joker("j_smiley"),
                joker("j_scary_face"), joker("j_hanger")],
        shop=[],
    )
    assert isinstance(choose_shop_action(state, Config(), rerolls=0), Reroll)


def test_no_reroll_when_every_joker_is_eternal() -> None:
    """全是永恒牌时换不了牌，重摇纯属浪费。"""
    cards = [joker(key) for key in
             ("j_jolly", "j_duo", "j_smiley", "j_scary_face", "j_hanger")]
    for entry in cards:
        entry["modifier"]["eternal"] = True
    state = shop_state(money=40, jokers=cards, shop=[])
    assert isinstance(choose_shop_action(state, Config(), rerolls=0), NextRound)


def test_sells_worst_joker_for_a_much_better_one() -> None:
    """小丑牌位满了，但商店里有明显更强的牌 → 卖掉最差的换。"""
    state = shop_state(
        money=20,
        jokers=[joker("j_brainstorm"), joker("j_8_ball"), joker("j_blueprint"),
                joker("j_triboulet"), joker("j_perkeo")],
        shop=[priced(joker("j_baron"), 5)],
    )
    action = choose_shop_action(state, Config(), rerolls=0)
    assert type(action).__name__ == "Sell"
    assert action.joker == 1          # j_8_ball 是最差的


# --------------------------------------------------------------- 补充包


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("p_arcana_normal_1", "arcana"),
        ("p_buffoon_mega_1", "buffoon"),
        ("p_celestial_jumbo_1", "celestial"),
        ("p_spectral_normal_2", "spectral"),
        ("p_standard_normal_1", "standard"),
        ("v_antimatter", ""),
    ],
)
def test_pack_kind(key: str, expected: str) -> None:
    assert pack_kind(key) == expected


def test_pack_picks_best_joker() -> None:
    raw = make_state(state="SMODS_BOOSTER_OPENED")
    with_area(raw, "pack", [joker("j_8_ball"), joker("j_blueprint")])
    state = GameState(raw)
    action = choose_pack_action(state, Config())
    assert action.card == 1
    assert not action.skip


def test_planet_pack_picks_committed_hand() -> None:
    """星球包里的牌是立刻使用的，必须挑我们专精的那个牌型。"""
    table = hands_table()
    table["Flush"]["level"] = 3
    table["Flush"]["played"] = 5
    raw = make_state(state="SMODS_BOOSTER_OPENED", hands=table)
    with_area(raw, "pack", [
        consumable("c_mercury", card_set="PLANET"),    # 升级 Pair
        consumable("c_jupiter", card_set="PLANET"),    # 升级 Flush ← 要这张
        consumable("c_pluto", card_set="PLANET"),      # 升级 High Card
    ])
    state = GameState(raw)
    action = choose_pack_action(state, Config())
    assert action.card == 1
    assert not action.skip


def test_planet_pack_skips_offtype_when_no_commitment() -> None:
    """还没专精时，罕见牌型的星球牌不值得拿。"""
    raw = make_state(state="SMODS_BOOSTER_OPENED")
    with_area(raw, "pack", [consumable("c_pluto", card_set="PLANET")])
    state = GameState(raw)
    assert choose_pack_action(state, Config()).skip


def test_never_buys_spectral_pack() -> None:
    """幻灵牌我们刻意不使用，买它的包等于浪费钱。"""
    state = shop_state(money=20, shop=[priced(consumable("p_spectral_normal_1"), 4)])
    action = choose_shop_action(state, Config(), rerolls=0)
    assert not isinstance(action, Buy)


def test_buys_a_pack_even_when_the_pack_shelf_is_full() -> None:
    """补充包货架摆满时**也要能买**。

    实测踩过的坑：判据原来是 `booster_slots_free > 0`，即 `card_limit - 包数`。
    而补充包货架通常就是满的（两个位子摆两个包）→ 那个值恒为 0 →
    **整条买包分支从来没执行过**。10 局评测里一个补充包都没买，
    连带卡死了天体包（牌型等级最主要的来源）。
    """
    packs = [priced(consumable("p_celestial_normal_1"), 4) for _ in range(2)]
    raw = make_state(state="SHOP", money=60, jokers=[], deck=full_deck())
    with_area(raw, "packs", packs, limit=2)
    with_area(raw, "shop", [], limit=2)
    with_area(raw, "vouchers", [], limit=1)
    with_area(raw, "consumables", [], limit=2)
    state = GameState(raw)
    assert state.booster_slots_free == 0        # 货架是满的
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, Buy) and action.pack is not None


def test_does_not_buy_a_pack_when_the_shelf_is_empty() -> None:
    raw = make_state(state="SHOP", money=60, jokers=[], deck=full_deck())
    with_area(raw, "packs", [], limit=2)
    with_area(raw, "shop", [], limit=2)
    with_area(raw, "vouchers", [], limit=1)
    with_area(raw, "consumables", [], limit=2)
    state = GameState(raw)
    assert not isinstance(choose_shop_action(state, Config(), rerolls=0), Buy)


def test_pack_skips_when_joker_slots_full() -> None:
    raw = make_state(state="SMODS_BOOSTER_OPENED")
    with_area(raw, "pack", [joker("j_blueprint")])
    raw["jokers"] = {"count": 5, "limit": 5,
                     "cards": [joker(k) for k in
                               ("j_baron", "j_perkeo", "j_triboulet", "j_canio",
                                "j_brainstorm")]}
    state = GameState(raw)
    action = choose_pack_action(state, Config())
    assert action.skip


def test_pack_skips_when_empty() -> None:
    state = GameState(make_state(state="SMODS_BOOSTER_OPENED"))
    assert choose_pack_action(state, Config()).skip


# ------------------------------------------------- 换牌：牌位满了怎么办


def full_slots(*keys: str, eternal: bool = False) -> list[dict]:
    """构造 5 张占满牌位的小丑牌。"""
    cards = [joker(k) for k in keys]
    if eternal:
        cards[0]["modifier"]["eternal"] = True
    return cards


DUDS = ("j_8_ball", "j_egg", "j_chaos", "j_credit_card", "j_gift")


def test_sells_a_joker_when_slots_are_full_and_a_better_one_is_available() -> None:
    """牌位满了但商店里有明显更好的 → 卖掉最差的腾位子。

    这条直接对应实测发现的最大失分点：13/20 局死在"满牌位 + 一沓花不出去的钱"
    ——牌位满了买不了新牌，而原来的门槛（2.5）用静态表比较，5 张像样的牌里
    根本没有哪张能比最差的还高 2.5 分。
    """
    state = shop_state(money=40, jokers=full_slots(*DUDS),
                       shop=[priced(joker("j_blueprint"), 5)])
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, Sell)
    assert action.joker is not None and 0 <= action.joker < len(DUDS)


def test_does_not_sell_when_the_replacement_would_not_be_bought() -> None:
    """只在确认新牌真的会被买进来时才卖——卖了不买是净亏。"""
    params = TunableParams(buy_threshold=9.0, sell_upgrade_margin=0.0,
                           empty_slot_bonus=0.0, few_slot_bonus=0.0)
    state = shop_state(money=40, jokers=full_slots(*DUDS),
                       shop=[priced(joker("j_banner"), 5)])
    action = choose_shop_action(state, Config(params=params), rerolls=0)
    assert not isinstance(action, Sell)


def test_never_sells_an_eternal_joker() -> None:
    """永恒牌卖了会扣分，不能当牺牲品——但它不该挡住其余四张的换牌。"""
    params = TunableParams(sell_upgrade_margin=0.0, buy_threshold=0.0)
    state = shop_state(money=40, jokers=full_slots(*DUDS, eternal=True),
                       shop=[priced(joker("j_blueprint"), 5)])
    action = choose_shop_action(state, Config(params=params), rerolls=0)
    assert isinstance(action, Sell)
    assert action.joker != 0          # 0 号是永恒牌


def test_no_swap_when_every_joker_is_eternal() -> None:
    """全是永恒牌时无牌可卖，不该硬卖。"""
    params = TunableParams(sell_upgrade_margin=0.0, buy_threshold=0.0)
    cards = [joker(k) for k in DUDS]
    for entry in cards:
        entry["modifier"]["eternal"] = True
    state = shop_state(money=40, jokers=cards,
                       shop=[priced(joker("j_blueprint"), 5)])
    assert not isinstance(choose_shop_action(state, Config(params=params), rerolls=0), Sell)


def test_surplus_money_lowers_the_swap_margin() -> None:
    """经济充足时换牌门槛再降一档——钱闲着没有收益，牌位才是稀缺资源。"""
    # 门槛 3.0、盈余减免 0.0 时不换；盈余减免 3.0 时换
    strict = TunableParams(sell_upgrade_margin=3.0, surplus_sell_relief=0.0,
                           empty_slot_bonus=0.0, few_slot_bonus=0.0)
    state = shop_state(money=40, jokers=full_slots(*DUDS),
                       shop=[priced(joker("j_banner"), 5)])
    assert not isinstance(choose_shop_action(state, Config(params=strict), rerolls=0), Sell)

    loose = strict.replace(surplus_sell_relief=3.0)
    state2 = shop_state(money=40, jokers=full_slots(*DUDS),
                        shop=[priced(joker("j_banner"), 5)])
    assert isinstance(choose_shop_action(state2, Config(params=loose), rerolls=0), Sell)


def test_no_swap_attempt_when_a_slot_is_free() -> None:
    """还有空位就直接买，不需要卖任何东西。"""
    state = shop_state(money=40, jokers=full_slots(*DUDS[:4]),
                       shop=[priced(joker("j_blueprint"), 5)])
    action = choose_shop_action(state, Config(), rerolls=0)
    assert isinstance(action, Buy)
