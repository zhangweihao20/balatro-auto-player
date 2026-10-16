"""凑牌型规划的测试。

对应策略规格里的这一段：

    依据当前手牌、牌型倍率和牌库剩余卡牌策略性弃牌，以获取期望值
    （牌型综合倍率得分 - 惩罚项弃牌数量×系数）最高的牌型
    （凑出牌型的概率 + 需弃牌数量 + 牌型倍率，其中弃牌数量为惩罚项）。

这里钉住四件事：
    1. 超几何概率算得对（这是"凑出概率"的地基）
    2. 每个牌型的"还差几张 / 外面有几张"推导正确
    3. 评分函数的单调性：概率越高越好、目标牌型越强越好、弃牌越多越差
    4. 决策行为：散牌该弃牌，已成牌该直接打（不能被"0% 概率的高分牌型"骗走）
"""

from __future__ import annotations

import math

import pytest

from balatro_bot.cards import BASE_HAND_SCORE, Card
from balatro_bot.planning import (
    MAX_DISCARD,
    PlanWeights,
    hypergeometric_at_least,
    plan_expected_score,
    plan_targets,
    plan_value,
)
from balatro_bot.solver import Solver
from balatro_bot.state import GameState

from conftest import card, joker, make_state

RANKS = "23456789TJQKA"
SUITS = "HDCS"


def deck_dicts(exclude: tuple[str, ...] = ()) -> list[dict]:
    """整副牌（conftest 的 dict 形式，给 make_state 用）。"""
    skip = set(exclude)
    return [card(f"{s}_{r}") for s in SUITS for r in RANKS if f"{s}_{r}" not in skip]


def to_cards(dicts: list[dict]) -> list[Card]:
    return [Card.from_dict(d) for d in dicts]


def hand_cards(keys: list[str]) -> list[Card]:
    return [Card.from_dict(card(k)) for k in keys]


def pile_for(keys: list[str]) -> list[Card]:
    """去掉手牌之后的牌堆。"""
    return to_cards(deck_dicts(tuple(keys)))


def solver_for(jokers: tuple[Card, ...] = ()) -> Solver:
    return Solver(jokers=jokers, base_fn=lambda name: BASE_HAND_SCORE.get(name, (5, 1)))


# --------------------------------------------------------------- 概率


def test_probability_is_exact_for_a_single_out() -> None:
    """牌堆 47 张里有 3 张补牌，抽 1 张至少中 1 张 = 3/47。"""
    assert hypergeometric_at_least(47, 3, 1, 1) == pytest.approx(3 / 47)


def test_probability_matches_manual_combinatorics() -> None:
    """4 张补牌、抽 2 张、至少中 1 张：1 - C(44,2)/C(48,2)。"""
    expected = 1 - math.comb(44, 2) / math.comb(48, 2)
    assert hypergeometric_at_least(48, 4, 2, 1) == pytest.approx(expected)


def test_probability_bounds() -> None:
    assert hypergeometric_at_least(40, 5, 3, 0) == 1.0      # 不需要任何牌
    assert hypergeometric_at_least(40, 0, 3, 1) == 0.0      # 外面没有补牌
    assert hypergeometric_at_least(40, 5, 3, 5) == 0.0      # 需要 5 张但只抽 3 张
    assert hypergeometric_at_least(40, 40, 3, 3) == 1.0     # 全是补牌


def test_probability_increases_with_more_outs() -> None:
    few = hypergeometric_at_least(45, 2, 3, 1)
    many = hypergeometric_at_least(45, 9, 3, 1)
    assert many > few


# --------------------------------------------------------- 各牌型的推导


def find(plans, name):
    for plan in plans:
        if plan.hand_name == name:
            return plan
    return None


def test_pair_plan_keeps_the_pair() -> None:
    hand = hand_cards(["H_K", "D_K", "C_3", "S_7", "H_9", "D_2", "C_5", "S_J"])
    plans = plan_targets(solver_for(), hand, pile_for([c.key for c in hand]))
    pair = find(plans, "Pair")
    assert pair is not None
    assert pair.needed == 0          # 已经是成牌
    assert pair.probability == 1.0
    assert pair.discard == ()        # 成牌是"打出去"，不是"弃掉别的"
    assert set(pair.keep) == {0, 1}


def test_four_flush_needs_exactly_one_card() -> None:
    # 四张红桃 + 三张杂牌（注意别凑成 5 张红桃，那就已经是同花了）
    hand = hand_cards(["H_2", "H_5", "H_9", "H_J", "D_3", "C_7", "S_K", "D_8"])
    plans = plan_targets(solver_for(), hand, pile_for([c.key for c in hand]))
    flush = find(plans, "Flush")
    assert flush is not None
    assert flush.needed == 1
    assert flush.outs == 9           # 剩下 9 张红桃都在牌堆里
    assert flush.probability > 0.15


def test_open_ended_straight_has_many_outs() -> None:
    # 5-6-7-8 两头都能成顺（4 或 9），但手里不能已经有 9
    hand = hand_cards(["H_5", "D_6", "C_7", "S_8", "H_2", "D_Q", "S_K", "H_3"])
    plans = plan_targets(solver_for(), hand, pile_for([c.key for c in hand]))
    straight = find(plans, "Straight")
    assert straight is not None
    assert straight.needed == 1
    assert straight.outs >= 4


def test_made_straight_needs_nothing() -> None:
    hand = hand_cards(["H_5", "D_6", "C_7", "S_8", "D_9", "H_2", "S_K", "H_3"])
    plans = plan_targets(solver_for(), hand, pile_for([c.key for c in hand]))
    straight = find(plans, "Straight")
    assert straight.needed == 0
    assert straight.discard == ()


def test_plans_never_discard_more_than_allowed() -> None:
    hand = hand_cards(["H_2", "D_5", "C_9", "S_J", "H_3", "D_7", "C_K", "S_4"])
    plans = plan_targets(
        solver_for(), hand, pile_for([c.key for c in hand]), max_discard=3
    )
    assert plans
    for plan in plans:
        assert len(plan.discard) <= 3


# --------------------------------------------------------- 评分函数


def test_value_rewards_higher_probability() -> None:
    low = plan_value(0.1, 200, 100, 2, PlanWeights())
    high = plan_value(0.6, 200, 100, 2, PlanWeights())
    assert high > low


def test_value_rewards_stronger_target_hand() -> None:
    weak = plan_value(0.4, 40, 100, 2, PlanWeights())
    strong = plan_value(0.4, 400, 100, 2, PlanWeights())
    assert strong > weak


def test_value_rewards_more_discards() -> None:
    """规格："弃牌数量不再是惩罚项"，改成奖励项。

    Balatro 里补牌张数 = 弃牌张数，多弃一张就等于多循环一张、
    更早看到需要的牌。所以同样条件下弃得多的评分更高。
    """
    few = plan_value(0.4, 200, 100, 1, PlanWeights())
    many = plan_value(0.4, 200, 100, 4, PlanWeights())
    assert many > few


def test_value_without_discard_bonus_is_neutral() -> None:
    """把系数设为 0 时，弃牌张数不影响评分。"""
    weights = PlanWeights(discard_bonus=0.0)
    assert plan_value(0.4, 200, 100, 1, weights) == plan_value(0.4, 200, 100, 4, weights)


def test_zero_probability_cannot_win() -> None:
    """0% 概率的牌型只能靠弃牌系数拿分，不能压过高概率的实在牌型。

    这是实测踩过的坑：纯加法评分下"0% 概率的五条（基础分 1440）"
    会压过"55% 概率的对子"，机器人于是为 6% 的同花顺弃掉成型的顺子。
    """
    lottery = plan_value(0.0, 1440, 5, 3, PlanWeights())
    solid = plan_value(0.55, 20, 5, 1, PlanWeights())
    assert lottery < solid
    assert lottery == pytest.approx(PlanWeights().discard_bonus * 3)


def test_gamma_shifts_preference_between_early_and_late() -> None:
    """γ 就是规格里"前期偏大牌型、中后期偏易凑牌型"那个参数。

    注意 P<1 时 P^γ 随 γ 单调递减，所以**不能说"安全的牌型在后期绝对评分更高"**。
    真正说明 γ 起作用的是**差距**：γ 越大，"稳妥的牌型"相对"高风险的大牌型"
    的领先幅度越大。这才是"中后期更倾向易凑齐的低难度牌型"的量化含义。
    """
    early = PlanWeights(gamma=0.6)
    late = PlanWeights(gamma=1.4)
    risky = lambda w: plan_value(0.12, 800, 20, 3, w)     # 低概率、高倍率
    safe = lambda w: plan_value(0.75, 20, 20, 1, w)       # 高概率、低倍率

    assert safe(late) - risky(late) > safe(early) - risky(early)
    # 前期甚至可能反过来去追大牌型
    assert risky(early) > risky(late)


# --------------------------------------------------------- 期望得分换算


def test_expected_score_is_probability_weighted() -> None:
    """期望得分 = P × 凑成分 + (1−P) × 当前分。"""
    solver = solver_for()
    plans = plan_targets(
        solver,
        hand_cards(["H_K", "D_K", "C_3", "S_7", "H_9", "D_2", "C_5", "S_J"]),
        pile_for([]),
        current_score=60.0,
    )
    flush = find(plans, "Flush")
    assert flush is not None
    assert plan_expected_score(flush, 60.0) == pytest.approx(
        flush.probability * flush.completed_score + (1 - flush.probability) * 60.0
    )


def test_completed_score_includes_joker_effects() -> None:
    """规格要求评分含**当前小丑牌效果**——换小丑牌，估计得分必须跟着变。

    Jolly：有对子时 +8 倍率。对子基础分是 10×2，加上它倍率应该显著变大。
    """
    keys = ["H_K", "D_K", "C_3", "S_7", "H_9", "D_2", "C_5", "S_J"]
    plain = plan_targets(solver_for(), hand_cards(keys), pile_for([]), current_score=60.0)
    boosted = plan_targets(
        solver_for((Card.from_dict(joker("j_jolly")),)),
        hand_cards(keys), pile_for([]), current_score=60.0,
    )
    plain_pair = find(plain, "Pair")
    boosted_pair = find(boosted, "Pair")
    assert plain_pair is not None and boosted_pair is not None
    assert boosted_pair.completed_score > plain_pair.completed_score


# --------------------------------------------------- 端到端：决策行为


def choose(keys: list[str], required: int = 8000):
    from balatro_bot.strategy import Config, choose_play_action

    state = GameState(make_state(
        hand=[card(k) for k in keys], deck=deck_dicts(tuple(keys)), required=required
    ))
    return choose_play_action(state, Config())


def test_junk_hand_discards() -> None:
    """散牌必须弃牌去凑——打一手高牌毫无意义。"""
    from balatro_bot.strategy import Discard

    action = choose(["H_2", "D_5", "C_9", "S_J", "H_3", "D_7", "C_K", "S_4"])
    assert isinstance(action, Discard)
    assert 1 <= len(action.indices) <= MAX_DISCARD


def test_made_straight_is_played_when_it_pulls_its_weight() -> None:
    """顺子这一手如果顶得上"平均每手需要的分"，就直接打，不去赌同花顺。

    新规格："优先选择通过弃牌的方式来过牌，**除非打牌过牌的收益达标**"。
    所以能不能打由 play_bar 这条门槛决定：
      * 过关需求 300、还剩 4 手 → 平均每手 75 分，顺子能打 120 → 达标 → 打
    相对地，test_hard_requirement_keeps_discarding 覆盖达不到门槛的情形。
    """
    from balatro_bot.strategy import Play

    action = choose(["H_5", "D_6", "C_7", "S_8", "D_9", "H_2", "S_K", "H_3"], required=300)
    assert isinstance(action, Play)


def test_hard_requirement_does_not_gamble_a_made_hand() -> None:
    """过关需求远超当前牌力时，也不会为了赌同花顺把成型的顺子弃掉。

    这是"期望得分"兜底写错时会犯的错：拿"现在就打的分数"当没凑成的兜底，
    于是 0.26×960 + 0.74×260 = 442 看起来远超 260，机器人就去赌了。
    正确的兜底是"弃牌后剩下的 3 张能打多少"（十几分），期望只有 260 上下，
    够不上 27.5% 的门槛 → 打。

    （旧版"优先弃牌"规则下这里会弃牌；那条规则已按要求回滚。）
    """
    from balatro_bot.strategy import Play

    action = choose(["H_5", "D_6", "C_7", "S_8", "D_9", "H_2", "S_K", "H_3"], required=8000)
    assert isinstance(action, Play)


def test_clearing_hand_is_played_immediately() -> None:
    from balatro_bot.strategy import Play

    action = choose(["H_K", "D_K", "C_3", "S_7", "H_9"], required=10)
    assert isinstance(action, Play)


def test_play_now_beats_a_hopeless_chase() -> None:
    """最后一个弃牌机会用完之后只能打。"""
    from balatro_bot.strategy import Config, Play, choose_play_action

    state = GameState(make_state(
        hand=[card(k) for k in ["H_2", "D_5", "C_9", "S_J", "H_3"]],
        deck=deck_dicts(),
        required=8000,
        discards_left=0,
    ))
    assert isinstance(choose_play_action(state, Config()), Play)


def test_gamma_switches_on_hand_value_not_ante() -> None:
    """规格："当牌型的期望值超出一般高难度牌型时可切换出牌策略"。

    切换依据**不是底注**，而是专精牌型的得分有没有追平一般高难度牌型
    （参考值 big_hand_reference，默认同花 140）。所以两个局面底注相同，
    只是对子被星球牌喂到 10 级，γ 就应该从"前期"跳到"中后期"。
    """
    from conftest import hands_table

    from balatro_bot.strategy import Config, phase_weights

    config = Config()

    early = GameState(make_state(hands=hands_table()))                 # 对子 1 级：10×2
    late = GameState(make_state(hands=_leveled_pair(10)))              # 对子 10 级：145×11

    gamma_early = phase_weights(Solver.from_state(early), early, config.params).gamma
    gamma_late = phase_weights(Solver.from_state(late), late, config.params).gamma

    assert gamma_early < 1.0 < gamma_late
    assert gamma_early == pytest.approx(config.params.gamma_early, abs=0.15)
    assert gamma_late == pytest.approx(config.params.gamma_late, abs=0.15)


def _leveled_pair(level: int) -> dict:
    """把对子升到 level 级（每级 +15 筹码 / +1 倍率，和游戏一致）。"""
    from conftest import hands_table

    table = hands_table()
    table["Pair"]["level"] = level
    table["Pair"]["chips"] = 10 + 15 * (level - 1)
    table["Pair"]["mult"] = 2 + 1 * (level - 1)
    return table
