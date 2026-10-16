"""小丑牌模型的测试。

这里钉住两类东西：

1. **key 必须真实存在。** key 拼错不会报错，只会静默按 0 收益处理，
   机器人于是持续低估自己的分数，而且从日志里完全看不出来。
   权威列表是从 Balatro.exe 里提取的（tools/extract_jokers.py）。

2. **数值不能取错。** 成长类小丑牌的描述模板里既有"每单位增量"又有"当前总值"，
   例如 Green Joker 的 "+#1# Mult per hand played ... (Currently +#3# Mult)"。
   如果取到前一个数字，Green Joker 会从 +11 变成 +1；对乘区类更是灾难
   （Constellation 会从 X1.8 变成 X0.1，等于把分数砍掉 95%）。
"""

from __future__ import annotations

import pytest

from balatro_bot.cards import Card
from balatro_bot.joker_data import JOKER_DATA
from balatro_bot.jokers import (
    ADDITIVE_MULT_JOKERS,
    DEFAULT_JOKER_VALUE,
    HANDLED_ELSEWHERE,
    JOKER_MODELS,
    JOKER_VALUE,
    KNOWN_UNSUPPORTED,
    MULTIPLICATIVE_JOKERS,
    NO_SCORE_EFFECT,
    PlayContext,
    current_segment,
    joker_value,
    model_joker,
    parse_chips,
    parse_mult,
    parse_suit,
    parse_xmult,
    raises_multiplier,
    unknown_jokers,
)

from conftest import card, joker, make_state
from balatro_bot.state import GameState


def make_joker(key: str, effect: str = "") -> Card:
    return Card.from_dict(joker(key, effect=effect))


# --------------------------------------------------------- key 必须真实存在


def test_all_model_keys_exist_in_game_data() -> None:
    """模型表里的每个 key 都必须在游戏里真实存在。"""
    unknown_keys = sorted(set(JOKER_MODELS) - set(JOKER_DATA))
    assert not unknown_keys, (
        f"这些 key 在 Balatro 里不存在（拼错了就永远不会触发）：{unknown_keys}"
    )


def test_all_value_keys_exist_in_game_data() -> None:
    unknown_keys = sorted(set(JOKER_VALUE) - set(JOKER_DATA))
    assert not unknown_keys, f"估值表里有不存在的 key：{unknown_keys}"


@pytest.mark.parametrize("table_name", [
    "HANDLED_ELSEWHERE", "KNOWN_UNSUPPORTED", "NO_SCORE_EFFECT",
])
def test_auxiliary_key_sets_exist(table_name: str) -> None:
    table = {
        "HANDLED_ELSEWHERE": HANDLED_ELSEWHERE,
        "KNOWN_UNSUPPORTED": KNOWN_UNSUPPORTED,
        "NO_SCORE_EFFECT": NO_SCORE_EFFECT,
    }[table_name]
    unknown_keys = sorted(set(table) - set(JOKER_DATA))
    assert not unknown_keys, f"{table_name} 里有不存在的 key：{unknown_keys}"


def test_game_data_has_expected_size() -> None:
    """游戏里应该是 150 张小丑牌；数量骤变说明提取脚本坏了。"""
    assert len(JOKER_DATA) == 150


def test_models_cover_most_jokers() -> None:
    """建模覆盖率。掉落会说明有 key 拼错或被误删。"""
    modeled = (
        set(JOKER_MODELS) | HANDLED_ELSEWHERE | KNOWN_UNSUPPORTED | NO_SCORE_EFFECT
    )
    covered = len(modeled & set(JOKER_DATA))
    assert covered >= 120, f"只覆盖了 {covered}/150 张"


def test_unknown_jokers_excludes_modelled_and_known() -> None:
    keys = ["j_greedy_joker", "j_hack", "j_four_fingers", "j_egg",
            "j_totally_made_up"]
    assert unknown_jokers(keys) == ["j_totally_made_up"]


# ------------------------------------------------------- 数值解析（关键）


def test_current_segment_prefers_currently_clause() -> None:
    """取到"当前值"段之后，前面那段"每单位增量"就不该再出现。"""
    text = "+1 Mult per hand played -1 Mult per discard (Currently +11 Mult)"
    segment = current_segment(text)
    assert "Currently" in segment
    assert "per hand played" not in segment
    assert "per discard" not in segment


def test_current_segment_returns_whole_text_when_absent() -> None:
    text = "+100 Chips -5 Chips for every hand played"
    assert current_segment(text) == text


def test_current_segment_handles_chinese() -> None:
    """描述文本跟随游戏语言，中文也要能识别"当前"段。"""
    text = "每打出一手 +1 倍率，每次弃牌 -1 倍率（当前 +11 倍率）"
    segment = current_segment(text)
    assert "当前" in segment
    assert "每打出一手" not in segment


def test_green_joker_reads_current_value_not_increment() -> None:
    """Green Joker：必须取到括号里的当前值 11，而不是"每手 +1"。"""
    card_obj = make_joker(
        "j_green_joker",
        "Played +1 Mult per hand played -1 Mult per discard (Currently +11 Mult)",
    )
    assert parse_mult(card_obj, 0.0) == 11.0


def test_constellation_reads_current_multiplier_not_increment() -> None:
    """Constellation：必须取 Currently 的 X1.8，而不是每张星球牌的 X0.1。

    取错的话倍率会从 1.8 掉到 0.1，等于把这一手的分数砍掉 95%。
    """
    card_obj = make_joker(
        "j_constellation",
        "This Joker gains X0.1 Mult every time a Planet card is used "
        "(Currently X1.8 Mult)",
    )
    assert parse_xmult(card_obj, 1.0) == 1.8


def test_campfire_reads_current_multiplier() -> None:
    card_obj = make_joker(
        "j_campfire",
        "This Joker gains X0.5 Mult for each card sold, resets when Boss Blind "
        "is defeated (Currently X2.5 Mult)",
    )
    assert parse_xmult(card_obj, 1.0) == 2.5


def test_bull_reads_current_chips() -> None:
    card_obj = make_joker(
        "j_bull",
        "+2 Chips for each $1 you have (Currently +56 Chips)",
    )
    assert parse_chips(card_obj, 0.0) == 56.0


def test_parse_falls_back_when_description_is_empty() -> None:
    card_obj = make_joker("j_constellation", "")
    assert parse_xmult(card_obj, 1.0) == 1.0
    assert parse_mult(card_obj, 7.0) == 7.0
    assert parse_chips(card_obj, 3.0) == 3.0


def test_ancient_joker_reads_suit_from_description() -> None:
    """Ancient Joker 的生效花色每回合轮换，但直接写在描述里。"""
    assert parse_suit(make_joker("j_ancient", "Each played card with Hearts suit gives X1.5 Mult")) == "H"
    assert parse_suit(make_joker("j_ancient", "Each played card with Spades suit gives X1.5 Mult")) == "S"
    assert parse_suit(make_joker("j_ancient", "no suit here")) is None


def test_ancient_joker_multiplies_per_matching_card() -> None:
    ctx = PlayContext(
        played=tuple(Card.from_dict(card(k)) for k in ("H_2", "H_3", "H_4")),
        held=(),
        hand_name="High Card",
    )
    effect = model_joker(
        "j_ancient",
        ctx,
        make_joker("j_ancient", "with Hearts suit gives X1.5 Mult (Currently X1.5 Mult)"),
    )
    assert effect.xmult == pytest.approx(1.5 ** 3)


# ------------------------------------------------------- 模型行为


def test_unknown_joker_returns_zero_effect() -> None:
    ctx = PlayContext(played=(), held=(), hand_name="High Card")
    effect = model_joker("j_not_real", ctx, make_joker("j_joker"))
    assert (effect.chips, effect.mult, effect.xmult) == (0.0, 0.0, 1.0)


def test_ramen_uses_current_multiplier() -> None:
    ctx = PlayContext(played=(), held=(), hand_name="High Card")
    effect = model_joker(
        "j_ramen", ctx, make_joker("j_ramen", "X2 Mult loses X0.01 Mult per card discarded")
    )
    assert effect.xmult == 2.0


def test_card_sharp_needs_repeat_hand() -> None:
    """Card Sharp 只在这一手牌型本回合已经打过时才触发。"""
    card_obj = make_joker("j_card_sharp", "X3 Mult if played poker hand has already been played")
    first = PlayContext(played=(), held=(), hand_name="Pair", hands_played_this_round=frozenset())
    repeat = PlayContext(played=(), held=(), hand_name="Pair",
                         hands_played_this_round=frozenset({"Pair"}))
    assert model_joker("j_card_sharp", first, card_obj).xmult == 1.0
    assert model_joker("j_card_sharp", repeat, card_obj).xmult == 3.0


def test_acrobat_only_on_final_hand() -> None:
    card_obj = make_joker("j_acrobat", "X3 Mult on final hand of round")
    normal = PlayContext(played=(), held=(), hand_name="Flush", is_final_hand=False)
    final = PlayContext(played=(), held=(), hand_name="Flush", is_final_hand=True)
    assert model_joker("j_acrobat", normal, card_obj).xmult == 1.0
    assert model_joker("j_acrobat", final, card_obj).xmult == 3.0


def test_blackboard_requires_all_black_held_cards() -> None:
    card_obj = make_joker("j_blackboard", "X3 Mult if all cards held in hand are Spades or Clubs")
    black = PlayContext(played=(), held=tuple(Card.from_dict(card(k)) for k in ("S_2", "C_3")),
                        hand_name="High Card")
    mixed = PlayContext(played=(), held=tuple(Card.from_dict(card(k)) for k in ("S_2", "H_3")),
                        hand_name="High Card")
    assert model_joker("j_blackboard", black, card_obj).xmult == 3.0
    assert model_joker("j_blackboard", mixed, card_obj).xmult == 1.0


def test_baron_scales_with_held_kings() -> None:
    card_obj = make_joker("j_baron")
    ctx = PlayContext(played=(), held=tuple(Card.from_dict(card(k)) for k in ("H_K", "D_K")),
                      hand_name="High Card")
    assert model_joker("j_baron", ctx, card_obj).xmult == pytest.approx(2.25)


def test_suit_joker_counts_only_scoring_cards() -> None:
    """花色类小丑牌数的是算分牌（ctx.played），不是打出的全部牌。"""
    card_obj = make_joker("j_greedy_joker")
    ctx = PlayContext(
        played=tuple(Card.from_dict(card(k)) for k in ("D_2", "D_3")),
        held=(), hand_name="Pair",
    )
    assert model_joker("j_greedy_joker", ctx, card_obj).mult == 6.0


def test_retrigger_bonus_stacks() -> None:
    from balatro_bot.jokers import retrigger_bonus

    ctx = PlayContext(played=(), held=(), hand_name="Pair",
                      joker_keys=frozenset({"j_hack", "j_sock_and_buskin"}))
    two = Card.from_dict(card("H_2"))
    face = Card.from_dict(card("H_K"))
    assert retrigger_bonus(ctx, two, 0) == 1          # Hack 对 2 生效
    assert retrigger_bonus(ctx, face, 0) == 1         # Sock and Buskin 对人头牌生效


def test_red_seal_adds_a_retrigger() -> None:
    from balatro_bot.jokers import retrigger_bonus

    ctx = PlayContext(played=(), held=(), hand_name="Pair")
    sealed = Card.from_dict(card("H_9", seal="RED"))
    assert retrigger_bonus(ctx, sealed, 1) == 1


def test_joker_value_default_for_unknown() -> None:
    assert joker_value("j_nonexistent") == DEFAULT_JOKER_VALUE
    assert joker_value("j_blueprint") > joker_value("j_8_ball")


# ------------------------------------------- "能提升倍率"的分类（商店用）


def test_multiplier_joker_keys_are_all_real() -> None:
    """ADDITIVE_MULT_JOKERS 里每个 key 都必须在权威表里。

    这是手写名单，最容易出的错就是把 key 拼错——拼错的后果是静默失效
    （那张牌永远拿不到经济充足的加权），从日志里看不出来。
    """
    unknown = sorted(k for k in ADDITIVE_MULT_JOKERS if k not in JOKER_DATA)
    assert not unknown, f"这些 key 在权威表里不存在：{unknown}"


def test_raises_multiplier_covers_both_categories() -> None:
    assert raises_multiplier("j_duo")          # 乘区（x2，有对子时）
    assert raises_multiplier("j_jolly")        # 加区（+8 倍率）
    assert raises_multiplier("j_blueprint")    # 复制邻居，间接提倍率


def test_raises_multiplier_excludes_non_multiplier_cards() -> None:
    """纯筹码丑、纯经济丑、纯功能丑都不算"提升倍率"。"""
    for key in ("j_8_ball", "j_sly", "j_stone", "j_rocket", "j_gift",
                "j_egg", "j_credit_card", "j_chaos"):
        assert not raises_multiplier(key), f"{key} 不该被当成提倍率牌"


def test_multiplier_categories_do_not_overlap() -> None:
    """加区和乘区不该重叠——重叠说明有一边写错了。"""
    assert not (ADDITIVE_MULT_JOKERS & MULTIPLICATIVE_JOKERS)


# ------------------------------------------- 全部小丑牌都必须有归属


def test_no_unmodelled_jokers_remain() -> None:
    """150 张权威小丑牌必须全部有归属：建模 / 别处处理 / 无数值影响。

    留着这个断言当哨兵：游戏更新加了新小丑牌时会失败，提醒去补建模，
    而不是静默地按 0 收益处理。
    """
    assert unknown_jokers(list(JOKER_DATA)) == []


def test_pareidolia_is_registered_as_handled_elsewhere() -> None:
    """Pareidolia 不是自己算分，而是改变上下文（所有牌都算人头牌）。

    效果早已由 PlayContext.all_faces 实现，只是没登记进表，一直误报"未建模"。
    """
    assert "j_pareidolia" in HANDLED_ELSEWHERE
    ctx = PlayContext(played=(), held=(), hand_name="Pair",
                      joker_keys=frozenset({"j_pareidolia"}))
    assert ctx.all_faces


# ------------------------------------------------------------ The Idol


@pytest.mark.parametrize("effect,expected", [
    ("Each played 7 of Hearts gives X2 Mult when scored", ("7", "H")),
    ("每张打出的红桃7在计分时给予 X2 倍率 每回合卡牌都会变动", ("7", "H")),
    ("Each played 10 of Spades gives X2 Mult when scored", ("T", "S")),
    ("每张打出的黑桃10在计分时给予 X2 倍率", ("T", "S")),
    ("Each played King of Diamonds gives X2 Mult when scored", ("K", "D")),
    ("每张打出的方块K在计分时给予 X2 倍率", ("K", "D")),
    ("Each played Ace of Clubs gives X2 Mult when scored", ("A", "C")),
])
def test_idol_target_parsed_in_both_languages(effect: str,
                                             expected: tuple[str, str]) -> None:
    """The Idol 的目标牌只在描述文本里，而且中英文语序相反。

    英文 "7 of Hearts"、中文 "红桃7"——所以按"离花色词最近的点数"取，
    不能靠固定语序（文本里另一个数字是倍率 X2，离花色远得多）。
    """
    from balatro_bot.jokers import parse_idol_target

    assert parse_idol_target(make_joker("j_idol", effect)) == expected


def test_idol_target_unreadable_returns_none() -> None:
    """读不到目标时宁可当它不提分，也不要瞎猜点数花色。"""
    from balatro_bot.jokers import parse_idol_target

    assert parse_idol_target(make_joker("j_idol", "")) == (None, None)
    assert parse_idol_target(make_joker("j_idol", "乱码没有目标牌")) == (None, None)


def test_idol_model_multiplies_per_matching_scoring_card() -> None:
    """每张匹配的算分牌给一次 X2（两张就是 X4）。"""
    idol = make_joker("j_idol", "Each played 7 of Hearts gives X2 Mult when scored")
    seven = Card.from_dict(card("H_7"))
    nine = Card.from_dict(card("H_9"))

    two_hits = PlayContext(played=(seven, seven), held=(), hand_name="Pair")
    assert model_joker("j_idol", two_hits, idol).xmult == pytest.approx(4.0)

    one_hit = PlayContext(played=(seven, nine), held=(), hand_name="Pair")
    assert model_joker("j_idol", one_hit, idol).xmult == pytest.approx(2.0)

    no_hit = PlayContext(played=(nine, nine), held=(), hand_name="Pair")
    assert model_joker("j_idol", no_hit, idol).xmult == pytest.approx(1.0)


def test_idol_only_counts_the_right_suit() -> None:
    """点数对但花色不对不算命中。"""
    idol = make_joker("j_idol", "Each played 7 of Hearts gives X2 Mult when scored")
    wrong_suit = Card.from_dict(card("S_7"))
    ctx = PlayContext(played=(wrong_suit,), held=(), hand_name="High Card")
    assert model_joker("j_idol", ctx, idol).xmult == pytest.approx(1.0)


# ------------------------------------------- 成长类的当前值（中英文都要能读）


def test_yorick_reads_current_xmult() -> None:
    ctx = PlayContext(played=(), held=(), hand_name="Pair")
    english = make_joker(
        "j_yorick",
        "This Joker gains X1 Mult every 23 [5] cards discarded "
        "(Currently X4 Mult)",
    )
    assert model_joker("j_yorick", ctx, english).xmult == pytest.approx(4.0)

    chinese = make_joker(
        "j_yorick",
        "每弃掉23[5]张牌 这张小丑牌获得 X1 倍率（当前为 X4 倍率）",
    )
    assert model_joker("j_yorick", ctx, chinese).xmult == pytest.approx(4.0)


def test_erosion_reads_current_mult() -> None:
    ctx = PlayContext(played=(), held=(), hand_name="Pair")
    english = make_joker(
        "j_erosion",
        "+4 Mult for each card below 52 in your full deck (Currently +12 Mult)",
    )
    assert model_joker("j_erosion", ctx, english).mult == pytest.approx(12.0)

    chinese = make_joker(
        "j_erosion",
        "在你的牌组中 每比完整的52张牌少一张 就获得+4倍率（当前为+12倍率）",
    )
    assert model_joker("j_erosion", ctx, chinese).mult == pytest.approx(12.0)


def test_stuntman_adds_chips() -> None:
    ctx = PlayContext(played=(), held=(), hand_name="Pair")
    english = make_joker("j_stuntman", "+250 Chips -2 hand size")
    assert model_joker("j_stuntman", ctx, english).chips == pytest.approx(250.0)

    chinese = make_joker("j_stuntman", "+250筹码 手牌上限-2")
    assert model_joker("j_stuntman", ctx, chinese).chips == pytest.approx(250.0)
