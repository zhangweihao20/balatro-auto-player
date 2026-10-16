"""主 build 流派识别（阶段一）的测试。

阶段一只做"盘点 + 识别"，输出全部进决策日志、**不改变任何决策**。所以这里的
测试重点是"识别得对不对、稳不稳"，而不是对局结果。
"""

from __future__ import annotations

import pytest

from balatro_bot.build import (
    ARCHETYPES,
    ArchetypeScore,
    BuildSnapshot,
    DeckProfile,
    archetype_scores,
    build_snapshot,
    commit_archetype,
    hand_lead,
)
from balatro_bot.params import TunableParams
from balatro_bot.state import GameState
from balatro_bot.strategy import Config

from conftest import card, hands_table, joker, make_state
from test_strategy import full_deck, priced, shop_state, with_area


def build_state(*, jokers=(), deck=None, hands=None, discard=()) -> GameState:
    raw = make_state(state="SHOP", jokers=list(jokers), hands=hands)
    cards = deck if deck is not None else full_deck()
    # 必须是列表：`Area.from_raw` 不认 dict（写成 dict 会静默变成空牌堆）
    raw["cards"] = {"count": len(cards), "limit": len(cards), "cards": list(cards)}
    with_area(raw, "discard", list(discard), limit=500)
    with_area(raw, "play", [], limit=5)
    with_area(raw, "consumables", [], limit=2)
    with_area(raw, "shop", [], limit=2)
    with_area(raw, "vouchers", [], limit=1)
    return GameState(raw)


# ---------------------------------------------------------------- 牌库构成


def test_deck_profile_counts_faces_and_enhancements() -> None:
    cards = [
        card("H_K"), card("S_Q"), card("D_J"), card("C_9"),
        card("H_A", enhancement="STEEL"), card("S_2", enhancement="STEEL"),
        card("C_3", enhancement="GLASS"), card("D_4", seal="RED"),
        card("H_Q", enhancement="BONUS"),          # 被改造过的人头牌
    ]
    from balatro_bot.cards import Card

    deck = DeckProfile.from_cards([Card.from_dict(c) for c in cards])
    assert deck.size == 9
    assert deck.faces == 4
    assert deck.count_of("STEEL") == 2
    assert deck.count_of("GLASS") == 1
    assert deck.count_of("invested_face") == 1
    assert deck.seals == 1
    assert deck.share_of("invested_face") == pytest.approx(1 / 9)


def test_empty_deck_shares_are_zero() -> None:
    deck = DeckProfile.from_cards([])
    assert deck.share_of("invested_face") == 0.0
    assert deck.share_of("STEEL") == 0.0
    assert deck.share_of(None) == 0.0


def test_full_deck_includes_the_discard_pile() -> None:
    """牌库构成必须含弃牌堆，否则 STEEL / K 会被系统性低估（这是加 gamestate
    补丁的原因）。"""
    state = build_state(
        deck=[card("H_2"), card("H_3")],
        discard=[card("S_K"), card("C_K", enhancement="STEEL")],
    )
    assert state.discard_pile.count == 2
    assert len(state.full_deck) == 4
    assert DeckProfile.from_cards(state.full_deck).count_of("STEEL") == 1


# ---------------------------------------------------------------- 流派识别


def test_archetype_needs_its_core_joker_to_exist() -> None:
    """硬前提：没有核心牌就谈不上走这个流派。"""
    state = build_state(jokers=[joker("j_jolly")])
    keys = {score.key for score in archetype_scores(state)}
    assert keys == {"hand"}


def test_face_archetype_reflects_investment_in_faces() -> None:
    """人头流的支持度要反映"为它投入了多少"。

    注意信号是**被改造过的人头牌**（强化/蜡封/版本），不是人头牌总数——
    整副牌恒为 52 张、人头恒为 12 张，那个数字永远不变（实测确认）。
    """
    plain = [card("H_K"), card("S_Q"), card("D_J"), card("C_2")]
    invested = [
        card("H_K", enhancement="BONUS"), card("S_Q", seal="RED"),
        card("D_J", enhancement="MULT"), card("C_2"),
    ]
    params = TunableParams()

    low = archetype_scores(build_state(jokers=[joker("j_baron")], deck=plain),
                           params=params)[0]
    high = archetype_scores(build_state(jokers=[joker("j_baron")], deck=invested),
                            params=params)[0]
    assert high.key == "face" and low.key == "face"
    assert low.deck_invest == 0.0
    assert high.deck_invest > 0.0
    assert high.support > low.support


def test_lineup_share_is_scale_free() -> None:
    """阵容证据是**占比**，所以不随静态表的整体尺度变化。"""
    only_baron = archetype_scores(build_state(jokers=[joker("j_baron")]))[0]
    assert only_baron.key == "face"
    assert only_baron.lineup_share == pytest.approx(1.0)

    # 男爵 + 一张牌型丑 → 两边按成色分占比
    both = archetype_scores(
        build_state(jokers=[joker("j_baron"), joker("j_jolly")])
    )
    shares = {s.key: s.lineup_share for s in both}
    assert shares["face"] > 0 and shares["hand"] > 0
    assert sum(shares.values()) == pytest.approx(1.0)


def test_scores_are_sorted_by_support() -> None:
    state = build_state(jokers=[joker("j_jolly"), joker("j_baron")])
    scores = archetype_scores(state)
    assert [s.support for s in scores] == sorted(
        (s.support for s in scores), reverse=True
    )


# ---------------------------------------------------------------- 承诺（迟滞）


def _score(key: str, support: float) -> ArchetypeScore:
    return ArchetypeScore(
        key=key, label=key, support=support,
        lineup_share=0.0, deck_invest=0.0, lineup_jokers=(),
    )


def test_no_commit_below_the_support_floor() -> None:
    params = TunableParams()
    assert commit_archetype([_score("hand", 0.01)], None, params) is None


def test_first_commit_takes_the_best() -> None:
    assert commit_archetype([_score("hand", 0.9), _score("face", 0.2)],
                            None) == "hand"


def test_hysteresis_keeps_the_incumbent() -> None:
    """新方向只**略好**时不切换——投资方向来回摆会把等级和牌位都掀翻。"""
    params = TunableParams()
    scores = [_score("face", 0.60), _score("hand", 0.55)]
    assert commit_archetype(scores, "hand", params) == "hand"


def test_a_clearly_better_archetype_wins() -> None:
    params = TunableParams()
    scores = [_score("face", 1.00), _score("hand", 0.55)]
    assert commit_archetype(scores, "hand", params) == "face"


def test_committed_archetype_is_a_plan_not_a_description() -> None:
    """**核心回归测试。**

    主流派是**计划**，不该因为阵容临时变化就作废——实测第一版 11 局最终全部
    退回"未承诺"（轨迹 `— → 人头流@16 → 牌型流@38 → —`），因为核心牌一被
    卖掉、support 掉到阈值以下，承诺就没了。
    """
    params = TunableParams()

    # ① 承诺的流派暂时没有核心牌了（support 掉到阈值下）→ 守住计划
    weak = [_score("hand", 0.30)]
    assert commit_archetype(weak, "face", params) == "face"

    # ② 阵容暂时空了 → 也守住计划
    assert commit_archetype([], "face", params) == "face"

    # ③ 但新方向自己有足够成色时，改计划是允许的
    strong = [_score("hand", 0.80)]
    assert commit_archetype(strong, "face", params) == "hand"


def test_committed_archetype_switches_when_both_are_visible() -> None:
    """两边都在候选里时，新方向要**明显**压过才换。"""
    params = TunableParams()
    scores = [_score("face", 0.60), _score("hand", 0.55)]
    assert commit_archetype(scores, "hand", params) == "hand"
    scores = [_score("face", 1.00), _score("hand", 0.55)]
    assert commit_archetype(scores, "hand", params) == "face"


# ---------------------------------------------------------------- 快照


def test_hand_lead_reads_the_level_gap() -> None:
    table = hands_table(levels={"Pair": 4, "Flush": 2})
    assert hand_lead(build_state(hands=table)) == 2
    assert hand_lead(build_state(hands=hands_table())) == 0


def test_snapshot_is_self_consistent_and_loggable() -> None:
    table = hands_table(levels={"Pair": 3})
    state = build_state(
        jokers=[joker("j_baron"), joker("j_jolly")],
        deck=[card("H_K"), card("S_Q"), card("D_J"), card("C_2"),
              card("H_3"), card("S_4")],
        hands=table,
    )
    snapshot = build_snapshot(state, previous=None)
    assert isinstance(snapshot, BuildSnapshot)
    assert snapshot.hand_type == "Pair"
    assert snapshot.hand_lead >= 0
    assert snapshot.committed in {s.key for s in snapshot.scores} or snapshot.committed is None

    numbers = snapshot.numbers()
    assert numbers["牌型"] == "Pair"
    assert "牌库" in numbers and numbers["人头"] == 3
    # 每个候选流派都给一行可读的证据
    for score in snapshot.top():
        assert score.label in numbers


def test_archtypes_table_is_consistent() -> None:
    for key, archetype in ARCHETYPES.items():
        assert archetype.key == key
        assert archetype.core, f"{key} 没有核心牌就永远不会被识别"
        assert archetype.label


# ------------------------------------------------- 阶段二：流派接进决策


def test_archetype_bonus_makes_a_core_joker_win_the_shop() -> None:
    """阶段二第一件事：属于主流派核心的小丑牌要能在排序里胜出。

    原来抓牌是逐张近视的——按单张边际贡献排序，看不出"再抓一张人头牌，
    男爵就从 ×1.5 变 ×3"这种方向性收益。
    """
    from balatro_bot.strategy import Buy, choose_shop_action, shop_card_value

    # 男爵(9.5) 和 杂技演员(7.5) 摆在货架上，空一个牌位
    state = shop_state(
        money=40,
        jokers=[joker("j_smiley")],
        shop=[priced(joker("j_acrobat"), 5), priced(joker("j_baron"), 5)],
    )
    # 无流派时：静态分高的男爵本来就赢——换一对更接近的来验证加成有效生效
    plain = shop_card_value(state.shop.cards[1], state, Config().params)
    boosted = shop_card_value(state.shop.cards[1], state, Config().params,
                              archetype="face")
    assert boosted == pytest.approx(plain + Config().params.build_card_bonus)

    action = choose_shop_action(state, Config(), rerolls=0, archetype="face")
    assert isinstance(action, Buy) and action.card == 1


def test_archetype_target_order_puts_kings_first_for_face_builds() -> None:
    """人头流：男爵数的是**手里**的 K，所以改造目标要先挑 K。"""
    from balatro_bot.strategy import archetype_target_order

    hand = [card("H_5"), card("S_Q"), card("D_K"), card("C_J"), card("H_9")]
    state = build_state(jokers=[joker("j_baron")],
                        deck=[card("H_2")], hands=hands_table())
    order = archetype_target_order(state, "face", state.hand.cards)
    # 该测试里 hand 是空的，直接用显式手牌构造一张状态来验证
    assert order == () or all(isinstance(i, int) for i in order)

    from balatro_bot.cards import Card

    cards = [Card.from_dict(c) for c in hand]
    order = archetype_target_order(state, "face", cards)
    assert list(order)[:3] == [2, 1, 3], f"应该是 K → Q → J，实际 {order}"


def test_archetype_target_order_prefers_unconverted_cards_for_steel() -> None:
    """钢铁流：改造的意义是"多造出一张 STEEL"，所以要优先还没变的牌。"""
    from balatro_bot.cards import Card
    from balatro_bot.strategy import archetype_target_order

    hand = [
        card("H_2", enhancement="STEEL"), card("H_3"), card("H_4"),
        card("H_5", enhancement="STEEL"),
    ]
    state = build_state(jokers=[joker("j_steel_joker")], hands=hands_table())
    order = archetype_target_order(
        state, "steel", [Card.from_dict(c) for c in hand]
    )
    assert set(order) == {1, 2}, f"应该是那两张还不是 STEEL 的，实际 {order}"


def test_no_archetype_means_no_change() -> None:
    """没有承诺流派时，一切照旧——阶段二的改动必须是"关闭即等于原行为"。"""
    from balatro_bot.strategy import archetype_target_order, shop_card_value

    state = shop_state(money=40, jokers=[joker("j_baron")],
                       shop=[priced(joker("j_baron"), 5)])
    base = shop_card_value(state.shop.cards[0], state, Config().params)
    same = shop_card_value(state.shop.cards[0], state, Config().params,
                           archetype=None)
    assert base == pytest.approx(same)
    assert archetype_target_order(state, None, state.hand.cards) == ()
