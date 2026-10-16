"""Boss / 精英盲注**约束建模**的测试。

这里钉住的是"机器人对局面的理解是否和游戏一致"，不是"应对得好不好"。
分成四组：

1. **覆盖度**：游戏里的盲注，约束表一条都不能漏。漏了不会报错，
   只会静默地把一条约束当成"没有约束"。
2. **算分正确性**：The Arm（降级发生在算分之前）、Crimson Heart
   （禁用的小丑牌要排除）、Verdant Leaf（全体卡牌压制）。
   这三条算错，后面所有策略都是在错的地基上。
3. **合法动作**：Psychic / Eye / Mouth / Ox / Flint / Tooth / Hook。
4. **健壮性**：认不出来的盲注要降级成"无特殊约束"，而不是崩掉。
"""

from __future__ import annotations

import pytest

from balatro_bot.bosses import (
    BOSS_EFFECTS,
    boss_effect,
    is_elite,
    unmodelled_effects,
)
from balatro_bot.cards import (
    BASE_HAND_SCORE,
    HAND_LEVEL_STEP,
    hand_base_one_level_down,
)
from balatro_bot.solver import Solver
from balatro_bot.state import GameState
from balatro_bot.strategy import (
    Config,
    Discard,
    Play,
    Sell,
    build_solver,
    play_constraints,
)

from conftest import card, joker, make_state

# 游戏里除小盲/大盲以外的全部盲注名（tools/list_bosses.py 导出）。
GAME_BLINDS = [
    "Amber Acorn", "Cerulean Bell", "Crimson Heart", "Verdant Leaf", "Violet Vessel",
    "The Arm", "The Club", "The Eye", "The Fish", "The Flint", "The Goad", "The Head",
    "The Hook", "The House", "The Manacle", "The Mark", "The Mouth", "The Needle",
    "The Ox", "The Pillar", "The Plant", "The Psychic", "The Serpent", "The Tooth",
    "The Wall", "The Water", "The Wheel", "The Window",
]

RANKS = "23456789TJQKA"
SUITS = "HDCS"
HAND = ["H_K", "D_K", "C_3", "S_7", "H_9"]


def deck_dicts(exclude=()) -> list[dict]:
    skip = set(exclude)
    return [card(f"{s}_{r}") for s in SUITS for r in RANKS if f"{s}_{r}" not in skip]


def boss(name: str, effect: str = "", **kwargs) -> GameState:
    """构造一个"当前盲注就是这个 Boss"的状态。

    必须把小盲的状态改掉：`GameState.current_blind` 按 CURRENT → SELECT 的顺序
    遍历 blinds，而 make_state 默认把小盲设成 CURRENT，不改就会一直取到小盲。
    """
    hand = kwargs.pop("hand", [card(k) for k in HAND])
    raw = make_state(hand=hand, deck=deck_dicts([c["key"] for c in hand]), **kwargs)
    raw["blinds"]["small"]["status"] = "DEFEATED"
    raw["blinds"]["big"]["status"] = "DEFEATED"
    raw["blinds"]["boss"].update(status="CURRENT", name=name, effect=effect)
    return GameState(raw)


# ------------------------------------------------------------ ① 覆盖度


def test_every_game_blind_is_modelled() -> None:
    """游戏里的 28 个盲注，约束表必须全覆盖。

    漏一条不会报错——只会静默地把它当成"没有约束"，然后在 Ante 8 打错。
    历史上正是这样：精英盲注 5 条全漏，而它们全在 Ante 8。
    """
    missing = unmodelled_effects(GAME_BLINDS)
    assert not missing, f"这些盲注没有建模：{missing}"


def test_constraint_table_has_no_stray_entries() -> None:
    """约束表里不能有游戏里不存在的条目（拼错名字会静默失效）。"""
    known = {name.strip().lower() for name in GAME_BLINDS}
    stray = sorted(key for key in BOSS_EFFECTS if key not in known)
    assert not stray, f"约束表里有不存在的盲注：{stray}"


def test_elite_blinds_are_flagged() -> None:
    elites = {name for name in GAME_BLINDS if is_elite(name)}
    assert elites == {"Amber Acorn", "Cerulean Bell", "Crimson Heart",
                      "Verdant Leaf", "Violet Vessel"}


# ------------------------------------------------- ② 算分正确性


def test_hand_level_steps_match_the_base_table() -> None:
    """等级增量表要覆盖所有牌型，且与 1 级基础分表一致。

    数据从 game.lua 的 G.GAME.hands 提取（tools/extract_hand_levels.py），
    这里只做一致性自检：两张表不能有谁缺了谁。
    """
    assert set(HAND_LEVEL_STEP) == set(BASE_HAND_SCORE)
    for name, (step_chips, step_mult) in HAND_LEVEL_STEP.items():
        assert step_chips > 0 and step_mult > 0, name


def test_one_level_down_subtracts_the_step() -> None:
    # 对子 5 级：10+15*4 = 70 筹码，2+1*4 = 6 倍率 → 4 级 55/5
    assert hand_base_one_level_down("Pair", 70, 6) == (55, 5)
    # 同花 3 级：35+15*2 = 65 / 4+2*2 = 8 → 2 级 50/6
    assert hand_base_one_level_down("Flush", 65, 8) == (50, 6)


def test_one_level_down_has_floors() -> None:
    """下限：筹码不低于 0、倍率不低于 1（对应游戏里的 math.max）。"""
    chips, mult = hand_base_one_level_down("Pair", 1, 1)
    assert chips == 0 and mult == 1


def test_the_arm_downgrades_only_levelled_hands() -> None:
    """The Arm 只在**等级 > 1** 时降级（游戏的 `if level > 1 then`）。

    无条件减一级会把 1 级的同花（35/4）错算成（20/2）——这是实测踩到的坑。
    """
    state = boss("The Arm", "Decrease level of played poker hand")
    state.raw["hands"]["Pair"].update(level=5, chips=70, mult=6)
    state = GameState(state.raw)

    constraints = play_constraints(state, Config())
    assert constraints.level_penalty
    solver, _ = build_solver(state, Config())
    assert solver.base_fn("Pair") == (55, 5)      # 5 级 → 4 级
    assert solver.base_fn("Flush") == (35, 4)     # 1 级不动


def test_without_the_arm_levels_are_untouched() -> None:
    state = boss("The Hook", "Discards 2 random cards per hand played")
    state.raw["hands"]["Pair"].update(level=5, chips=70, mult=6)
    state = GameState(state.raw)
    solver, _ = build_solver(state, Config())
    assert solver.base_fn("Pair") == (70, 6)


def test_crimson_heart_excludes_the_disabled_joker() -> None:
    """被禁用的小丑牌不结算，也不提供关键字型能力。

    游戏会把 debuff 写进状态（blind.lua:647 专门处理 card.area == G.jokers），
    以前算分完全没看这个标记，每手都高估自己的分数。
    """
    state = boss("Crimson Heart", "One random Joker disabled every hand",
                 jokers=[joker("j_jolly")])
    solver = Solver.from_state(state)
    active = solver.best(state.hand.cards).total

    raw = boss("Crimson Heart", "One random Joker disabled every hand",
               jokers=[joker("j_jolly")])
    raw.raw["jokers"]["cards"][0]["state"]["debuff"] = True
    disabled = GameState(raw.raw)
    solver_off = Solver.from_state(disabled)

    assert active > 0
    assert solver_off.best(disabled.hand.cards).total < active


def test_verdant_leaf_debuffed_cards_do_not_score() -> None:
    """Verdant Leaf：全体卡牌被压制时，卡牌点数与强化都不结算。"""
    state = boss("Verdant Leaf", "All cards debuffed until 1 Joker sold")
    solver = Solver.from_state(state)
    normal = solver.best(state.hand.cards).total

    raw = boss("Verdant Leaf", "All cards debuffed until 1 Joker sold")
    for entry in raw.raw["hand"]["cards"]:
        entry["state"]["debuff"] = True
    debuffed = GameState(raw.raw)
    assert Solver.from_state(debuffed).best(debuffed.hand.cards).total < normal


# --------------------------------------------------------------- ③ 合法动作


def test_psychic_forces_exactly_five_cards() -> None:
    constraints = play_constraints(boss("The Psychic", "Must play 5 cards"), Config())
    assert (constraints.min_cards, constraints.max_cards) == (5, 5)


def test_flint_halves_the_base_score() -> None:
    constraints = play_constraints(
        boss("The Flint", "Base Chips and Mult for played poker hands are halved"),
        Config(),
    )
    assert constraints.base_scale == 0.5


def test_tooth_limits_cards_when_money_matters() -> None:
    rich = play_constraints(boss("The Tooth", "Lose $1 per card played", money=20),
                            Config())
    assert rich.max_cards == 3 and not rich.prefer_more_cards
    poor = play_constraints(boss("The Tooth", "Lose $1 per card played", money=3),
                            Config())
    assert poor.max_cards == 5


def test_hook_stops_preferring_more_cards() -> None:
    constraints = play_constraints(
        boss("The Hook", "Discards 2 random cards per hand played"), Config()
    )
    assert not constraints.prefer_more_cards


def test_eye_bans_repeated_hand_types() -> None:
    state = boss("The Eye", "No repeat hand types this round")
    state.raw["hands"]["Pair"]["played_this_round"] = 1
    state = GameState(state.raw)
    allowed = play_constraints(state, Config()).allowed_hands
    assert allowed is not None and "Pair" not in allowed


def test_mouth_locks_to_the_first_hand_type() -> None:
    state = boss("The Mouth", "Only one hand type can be played this round")
    state.raw["hands"]["Flush"]["played_this_round"] = 1
    state = GameState(state.raw)
    assert play_constraints(state, Config()).allowed_hands == frozenset({"Flush"})


def test_ox_bans_the_most_played_hand() -> None:
    state = boss("The Ox", "Playing your most played hand sets your money to $0")
    state.raw["hands"]["Pair"]["played"] = 9
    state = GameState(state.raw)
    allowed = play_constraints(state, Config()).allowed_hands
    assert allowed is not None and "Pair" not in allowed


def test_elite_blinds_expose_their_constraints() -> None:
    """精英盲注必须至少被识别出来并留痕，不能再静默当成"没有约束"。"""
    for name, effect in [
        ("Crimson Heart", "One random Joker disabled every hand"),
        ("Verdant Leaf", "All cards debuffed until 1 Joker sold"),
        ("Cerulean Bell", "Forces 1 card to always be selected"),
        ("Amber Acorn", "Flips and shuffles all Joker cards"),
    ]:
        notes = play_constraints(boss(name, effect), Config()).notes
        assert notes, f"{name} 没有留下任何约束记录"
        assert any(name in note for note in notes)

    crimson = play_constraints(
        boss("Crimson Heart", "One random Joker disabled every hand"), Config()
    )
    assert crimson.effect is not None
    assert crimson.effect.disables_one_joker_per_hand


def test_violet_vessel_needs_no_special_handling() -> None:
    """纯加倍的盲注靠 required_score 就够，不该产生多余约束。"""
    constraints = play_constraints(boss("Violet Vessel", "Very large blind"), Config())
    assert constraints.notes == ()
    assert constraints.effect is not None and constraints.effect.score_multiplier == 6.0


def test_debuff_suits_need_no_action_from_the_bot() -> None:
    """花色压制由游戏写进 card.debuff，机器人不需要额外约束。"""
    for name, suit in [("The Club", "Clubs"), ("The Goad", "Spades"),
                       ("The Head", "Hearts"), ("The Window", "Diamonds")]:
        constraints = play_constraints(boss(name, f"All {suit} cards are debuffed"),
                                       Config())
        assert constraints.notes == ()
        assert constraints.effect is not None
        assert constraints.effect.debuffs_suit == suit


# --------------------------------------------------------------- ④ 健壮性


def test_unknown_boss_degrades_gracefully() -> None:
    """认不出来的盲注不能崩——降级成"无特殊约束"继续跑。"""
    state = boss("Some Future Blind", "does something unknown")
    constraints = play_constraints(state, Config())
    assert constraints.notes == ()
    assert constraints.effect is None
    assert (constraints.min_cards, constraints.max_cards) == (1, 5)


def test_non_boss_rounds_have_no_constraints() -> None:
    raw = make_state(hand=[card(k) for k in HAND])
    constraints = play_constraints(GameState(raw), Config())
    assert constraints.notes == () and constraints.effect is None


def test_fallback_matches_by_effect_text() -> None:
    """名字对不上时按效果文本兜底（游戏改名或换语言时的保险）。"""
    state = boss("Unknown Name", "Must play 5 cards")
    constraints = play_constraints(state, Config())
    assert constraints.effect is not None
    assert (constraints.min_cards, constraints.max_cards) == (5, 5)


# ------------------------------------------------- ⑤ 应对措施：Verdant Leaf


def verdant_leaf(jokers: list[dict], *, debuff_cards: bool = True) -> GameState:
    state = boss("Verdant Leaf", "All cards debuffed until 1 Joker sold",
                 jokers=jokers)
    if debuff_cards:
        for entry in state.raw["hand"]["cards"]:
            entry["state"]["debuff"] = True
        state = GameState(state.raw)
    return state


def test_verdant_leaf_sells_the_least_useful_joker() -> None:
    """全体卡牌被压制时卖掉最不心疼的那张小丑牌。

    机制（card.lua 的卖牌处理）：卖掉**任意一张**小丑牌，盲注就被
    `disable()`，压制随之解除。不卖的话整回合只能拿牌型基础筹码打分。
    这里 Jolly 在有对子时 +8 倍率（手牌里有 KK），8 球完全不提分，
    所以应该卖 8 球。
    """
    from balatro_bot.strategy import choose_sell_action

    state = verdant_leaf([joker("j_jolly"), joker("j_8_ball")])
    action = choose_sell_action(state, Config())
    assert isinstance(action, Sell)
    assert action.joker == 1


def test_no_sell_when_the_debuff_is_already_lifted() -> None:
    """状态里没有压制标记时不该卖——卖过之后游戏会解除标记。"""
    from balatro_bot.strategy import choose_sell_action

    state = verdant_leaf([joker("j_jolly")], debuff_cards=False)
    assert choose_sell_action(state, Config()) is None


def test_no_sell_without_jokers() -> None:
    from balatro_bot.strategy import choose_sell_action

    assert choose_sell_action(verdant_leaf([]), Config()) is None


def test_no_sell_under_other_blinds() -> None:
    """只有"卖牌能解除"的盲注才卖，别的时候卖牌是纯亏。"""
    from balatro_bot.strategy import choose_sell_action

    state = boss("Crimson Heart", "One random Joker disabled every hand",
                 jokers=[joker("j_jolly")])
    for entry in state.raw["hand"]["cards"]:
        entry["state"]["debuff"] = True
    state = GameState(state.raw)
    assert choose_sell_action(state, Config()) is None


def test_sell_can_be_disabled_by_parameter() -> None:
    from balatro_bot.params import TunableParams
    from balatro_bot.strategy import choose_sell_action

    state = verdant_leaf([joker("j_jolly"), joker("j_8_ball")])
    config = Config(params=TunableParams(sell_to_lift_debuff=False))
    assert choose_sell_action(state, config) is None


def test_bot_dispatch_actually_sends_the_sell() -> None:
    """接线测试：光有决策函数不够，_dispatch 必须真的把 Sell 发出去。

    这条覆盖的是一类很容易漏的 bug——决策写对了，但主循环根本没调用它。
    """
    from balatro_bot.bot import Bot

    state = verdant_leaf([joker("j_jolly"), joker("j_8_ball")])
    bot = Bot(None, Config(), verbose=False)      # _dispatch 不用 client
    action = bot._dispatch(state, 0)
    assert isinstance(action, Sell)
    assert action.joker == 1


def test_bot_dispatch_plays_normally_under_other_blinds() -> None:
    """没有"卖牌解压制"的盲注下，不能平白卖牌。"""
    from balatro_bot.bot import Bot

    state = boss("The Club", "All Club cards are debuffed")
    bot = Bot(None, Config(), verbose=False)
    assert isinstance(bot._dispatch(state, 0), (Play, Discard))


# ------------------------------------------------------- ⑥ 应对措施：The Arm


def test_arm_guard_prefers_avoiding_levelled_hand_types() -> None:
    """The Arm 下把"会被降级的牌型"记成软偏好，而不是直接禁牌。

    禁牌的后果可能比降一级严重得多：实测专精到 6 级的对子被禁后，
    机器人只能打高牌 15 分，而照打对子按 5 级算是 595 分。
    真正的取舍要在出牌决策里比分数，所以这里只标记偏好。
    """
    state = boss("The Arm", "Decrease level of played poker hand")
    state.raw["hands"]["Pair"].update(level=5, chips=70, mult=6)
    state = GameState(state.raw)

    constraints = play_constraints(state, Config())
    assert "Pair" in constraints.avoid_hands
    assert constraints.allowed_hands is None       # 不限制合法动作
    assert constraints.level_penalty               # 降级照样算进分数


def test_arm_guard_does_not_ban_when_nothing_else_can_clear() -> None:
    """换个牌型也过不了关时，照样打原来那一手（不能为省一级而输掉回合）。"""
    from balatro_bot.strategy import choose_play_action

    state = boss("The Arm", "Decrease level of played poker hand", required=10)
    state.raw["hands"]["Pair"].update(level=5, chips=70, mult=6)
    state = GameState(state.raw)
    action = choose_play_action(state, Config())
    assert isinstance(action, Play)
    # 手里是 KK397，能过关的只有对子；对子被标为"想避开"也必须打
    assert len(action.indices) >= 2


def test_arm_guard_leaves_level_one_hands_alone() -> None:
    """全是 1 级时没有东西可避开，不该产生偏好。"""
    state = boss("The Arm", "Decrease level of played poker hand")
    assert play_constraints(state, Config()).avoid_hands == frozenset()


def test_arm_guard_can_be_disabled_by_parameter() -> None:
    from balatro_bot.params import TunableParams

    state = boss("The Arm", "Decrease level of played poker hand")
    state.raw["hands"]["Pair"].update(level=5, chips=70, mult=6)
    state = GameState(state.raw)
    config = Config(params=TunableParams(arm_level_guard=False))
    assert play_constraints(state, config).avoid_hands == frozenset()


# --------------------------------------------------- ⑦ 应对措施：Amber Acorn


def test_amber_acorn_needs_no_extra_action() -> None:
    """Amber Acorn 在**盲注开局**打乱一次顺序，而机器人本来就在本回合第一手
    重排小丑牌——顺序上已经覆盖，不需要额外应对措施。

    这条测试钉住"知道它是什么、且不需要动手"这个结论；如果哪天改成
    每手都打乱，这里会提醒需要重新评估。
    """
    constraints = play_constraints(
        boss("Amber Acorn", "Flips and shuffles all Joker cards"), Config()
    )
    assert constraints.effect is not None
    assert constraints.effect.shuffles_jokers
    assert constraints.allowed_hands is None
    assert constraints.min_cards == 1 and constraints.max_cards == 5
