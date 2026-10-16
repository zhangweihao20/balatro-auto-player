"""全局局面判断（时间维度 + 危急度）的测试。

背景：以前所有决策都是**单步局部最优**——买牌比较"换了这张当前阵容能多打
多少分"，出牌比较"这一手的期望"。没有任何地方问过两个全局问题：

* **时间维度**：按这个速度，8 个底注之后到得了吗？
* **危急度**：这一关我是不是已经过不了了？

后果很具体：实测 10 局里有 7 局是**带着 $47~84 的余额死掉的**，而当时面对的
是 11000~35000 的需求；而且越接近失败，保留额还被抬得越高、重摇预算砍得越少
（方向是反的）。
"""

from __future__ import annotations

import pytest

from balatro_bot.strategy import (
    ANTE_GROWTH,
    ANTE_REQUIREMENT,
    Config,
    Situation,
    assess,
    boss_requirement,
    effective_reserve,
    max_rerolls_for,
    score_capability,
)
from balatro_bot.state import GameState

from conftest import consumable, joker, make_state
from test_strategy import full_deck, priced, with_area

PAIR_ENGINE = ("j_jolly", "j_duo", "j_smiley", "j_scary_face", "j_hanger")


def shop(ante: int = 5, money: int = 84, jokers: tuple[str, ...] = PAIR_ENGINE,
         *, deck=None) -> GameState:
    raw = make_state(
        state="SHOP", money=money,
        jokers=[joker(key) for key in jokers],
        deck=full_deck() if deck is None else deck,
    )
    raw["ante_num"] = ante
    for area in ("shop", "vouchers", "consumables", "packs"):
        with_area(raw, area, [], limit=2)
    return GameState(raw)


# --------------------------------------------------------------- 判据本身


def test_unknown_capability_is_not_treated_as_crisis() -> None:
    """估不出得分能力时不该当成危急——那只是信息缺失，不是局面糟糕。

    否则所有简化测试状态（只有钱和小丑牌、没有牌组）都会被判成紧急。
    """
    state = shop(deck=[])
    situation = assess(state, Config())
    assert not situation.known
    assert situation.crisis == 0.0
    assert not situation.urgent


def test_crisis_detects_an_unclearable_blind() -> None:
    """得分力 × 剩余手数 撑不住本关需求时，危急度 > 1。"""
    situation = Situation(ante=5, score_now=100.0, required_now=22000.0,
                          hands_left=4, money=0)
    assert situation.crisis == pytest.approx(22000 / 400)
    assert situation.urgent


def test_crisis_is_satisfied_when_capacity_covers_the_requirement() -> None:
    situation = Situation(ante=5, score_now=6000.0, required_now=22000.0,
                          hands_left=4, money=0)
    assert situation.crisis < 1.0


def test_pace_follows_the_ante_curve() -> None:
    """同一个得分能力，早期不落后、后期落后。

    每过一个底注需求涨 ANTE_GROWTH 倍，所以"现在该达到的水平"随底注上升。
    """
    early = Situation(ante=1, score_now=3600.0, required_now=600, hands_left=4, money=0)
    late = Situation(ante=7, score_now=3600.0, required_now=70000, hands_left=4, money=0)
    assert early.pace > 1.0
    assert late.pace < 1.0


def test_ante_growth_matches_the_requirement_curve() -> None:
    """增长率常量要和需求曲线自洽（A1 到 A8 正好是它的 7 次方）。"""
    assert ANTE_REQUIREMENT[8] / ANTE_REQUIREMENT[1] == pytest.approx(ANTE_GROWTH ** 7)


def test_boss_requirement_is_twice_the_base() -> None:
    assert boss_requirement(5) == ANTE_REQUIREMENT[5] * 2


def test_real_state_is_assessed_end_to_end() -> None:
    """真实状态上端到端跑一遍：低底注不紧急，高底注紧急。"""
    early = assess(shop(ante=1), Config())
    late = assess(shop(ante=7), Config())
    assert early.score_now > 0
    assert not early.urgent
    assert late.urgent


# --------------------------------------------------------- 能力估计要正确


def test_capability_uses_the_best_joker_order() -> None:
    """顺序对复制类小丑牌是决定性的：Blueprint 与 Brainstorm 相邻成环时
    会互相复制、双双归零。机器人可以免费重排，所以能力估计要按最佳摆放算。
    """
    plain = assess(shop(ante=5, jokers=("j_jolly", "j_duo", "j_smiley",
                                        "j_scary_face", "j_hanger")), Config())
    # Blueprint 放在 Duo 左边 → 复制 Duo → 第二个 x2
    boosted = assess(shop(ante=5, jokers=("j_blueprint", "j_jolly", "j_duo",
                                          "j_smiley", "j_scary_face")), Config())
    assert boosted.score_now > plain.score_now


def test_capability_matches_the_pair_scenario() -> None:
    """能力的口径是"专精牌型能打多少分"，所以对子引擎要有明显得分。"""
    assert score_capability(shop(ante=5), Config()) > 1000


# ------------------------------------------------------- 紧急时解除保守


def test_urgent_drops_the_interest_floor() -> None:
    """紧急时保留额降到 $4：钱留着吃 $5 利息救不了这一关。"""
    state = shop(ante=5)
    situation = assess(state, Config())
    assert situation.urgent
    assert effective_reserve(state, Config(), situation) == Config().params.money_reserve


def test_not_urgent_keeps_the_interest_floor() -> None:
    """不紧急时照旧守 $25 利息本金。"""
    state = shop(ante=3)
    situation = assess(state, Config())
    assert not situation.urgent
    assert effective_reserve(state, Config(), situation) == Config().params.interest_floor


def test_urgent_unlocks_rerolls() -> None:
    """紧急时重摇预算改成"花得起就一直摇"（$84 至少能摇十几次）。"""
    state = shop(ante=5, money=84)
    situation = assess(state, Config())
    assert max_rerolls_for(state, Config(), situation) >= 10
    calm = assess(shop(ante=3, money=84), Config())
    assert max_rerolls_for(state, Config(), calm) == 1


def test_urgent_rerolls_still_respect_the_floor() -> None:
    """钱很少时不该借着重摇把家底掏空。"""
    state = shop(ante=6, money=6)
    situation = assess(state, Config())
    assert max_rerolls_for(state, Config(), situation) <= 1


# --------------------------------------------------- 紧急时买什么变了


def test_celestial_pack_is_bought_when_urgent() -> None:
    """紧急时天体包一律买——那时钱留着没有意义。

    注意：**按需估值不再是"给天体包加常数分"**。平时靠算期望决定，
    紧急才是无条件买。
    """
    from balatro_bot.strategy import Buy, choose_shop_action

    state = shop(ante=5)          # A5 + 这套阵容 → 落后/危急
    assert assess(state, Config()).urgent
    raw = make_state(state="SHOP", money=60, jokers=[joker(k) for k in PAIR_ENGINE],
                     deck=full_deck())
    raw["ante_num"] = 5
    with_area(raw, "packs", [priced(consumable("p_celestial_normal_1"), 4)], limit=2)
    with_area(raw, "shop", [], limit=2)
    with_area(raw, "vouchers", [], limit=1)
    with_area(raw, "consumables", [], limit=2)
    action = choose_shop_action(GameState(raw), Config(), rerolls=0)
    assert isinstance(action, Buy) and action.pack is not None


def test_celestial_valuation_scales_with_pack_size() -> None:
    """命中期望随包型变化：普通包 25%，巨型/超级包 41.7%。

    锁定在**一个**牌型时（wanted=1），超级包的"能拿两张"用不上，
    所以它和巨型包期望相同却贵 $2 —— 单位价格更差。
    """
    from balatro_bot.strategy import celestial_expected_levels, celestial_pack_shape

    normal = celestial_pack_shape("p_celestial_normal_1")
    jumbo = celestial_pack_shape("p_celestial_jumbo_1")
    mega = celestial_pack_shape("p_celestial_mega_1")

    locked = {
        "normal": celestial_expected_levels(12, *normal, 1),
        "jumbo": celestial_expected_levels(12, *jumbo, 1),
        "mega": celestial_expected_levels(12, *mega, 1),
    }
    assert locked["normal"] == pytest.approx(0.25)
    assert locked["jumbo"] == pytest.approx(5 / 12)
    assert locked["mega"] == pytest.approx(5 / 12)      # 拿两张用不上

    # 单位价格：巨型最划算，超级最差
    per_dollar = {"normal": locked["normal"] / 4, "jumbo": locked["jumbo"] / 6,
                  "mega": locked["mega"] / 8}
    assert per_dollar["jumbo"] > per_dollar["normal"] > per_dollar["mega"]


def test_spare_hand_roughly_doubles_the_hit_chance() -> None:
    """允许"主 + 备"之后，池子里有两张想要的，命中率大致翻倍。"""
    from balatro_bot.strategy import celestial_expected_levels, celestial_pack_shape

    jumbo = celestial_pack_shape("p_celestial_jumbo_1")
    one = celestial_expected_levels(12, *jumbo, 1)
    two = celestial_expected_levels(12, *jumbo, 2)
    assert two > one * 1.5
    assert two == pytest.approx(1 - 252 / 792)


def test_urgency_planet_bonus_only_applies_to_the_committed_hand() -> None:
    """紧急加权**只给专精牌型的星球牌**。

    给全部星球牌统一加分是错的：会把非专精牌型的星球牌从 3.0 顶过 4.5 的门槛，
    实测就是这样把等级摊薄到五个不同牌型上、单一牌型最高只到 +2 级。
    """
    from balatro_bot.cards import Card
    from balatro_bot.strategy import PLANET_HAND, committed_hand, shop_card_value

    from conftest import consumable

    state = shop(ante=5)
    situation = assess(state, Config())
    assert situation.urgent
    committed = committed_hand(state, Config().params)

    values = {}
    for key in ("c_mercury", "c_pluto", "c_uranus", "c_saturn"):
        card = Card.from_dict(consumable(key, card_set="PLANET"))
        values[PLANET_HAND[key]] = shop_card_value(
            card, state, Config().params, situation=situation
        )

    # 专精牌型的星球牌拿到加权，其它保持基础估值
    assert values[committed] >= Config().params.buy_threshold
    for hand, value in values.items():
        if hand != committed:
            assert value < Config().params.buy_threshold, hand


# --------------------------------------------- 消耗牌的目标张数必须合法


def test_consumable_targets_are_clipped_to_the_game_limit() -> None:
    """选少了和选多了都会被游戏拒绝，两种都实测踩过：

        "Consumable 'The Tower' requires exactly 1 card (provided: 5)"
        "Card 'c_sun' requires 1-3 target card(s). Provided: 0"

    所以目标张数必须裁到这张牌允许的上限。
    """
    from balatro_bot.strategy import consumable_targets

    state = shop(ante=1)
    five = (0, 1, 2, 3, 4)
    assert consumable_targets(state, "c_tower", five) == (0,)
    assert consumable_targets(state, "c_lovers", five) == (0,)
    assert consumable_targets(state, "c_death", five) == (0, 1)
    assert consumable_targets(state, "c_sun", five) == (0, 1, 2)
    assert len(consumable_targets(state, "c_sun", five)) == 3


# --------------------------------------------- 主 + 备：备选只吃已投入的牌型


def test_spare_hand_must_already_be_invested() -> None:
    """备选必须是**已经在投入的**（等级 > 1）；一级的牌型不算备选。"""
    from conftest import hands_table

    from balatro_bot.strategy import spare_planet_hand

    table = hands_table()
    table["Pair"]["level"] = 4
    table["Straight"]["level"] = 1          # 没投过
    assert spare_planet_hand(GameState(make_state(hands=table)), Config().params) is None

    table["Straight"]["level"] = 2          # 投过一级 → 可以当备选
    assert spare_planet_hand(GameState(make_state(hands=table)), Config().params) == "Straight"


def test_spare_hand_does_not_change_the_committed_type() -> None:
    """备选**不影响 γ**：γ 只看主牌型，前后期切换不会被备选带偏。"""
    from conftest import hands_table

    from balatro_bot.strategy import accepted_planet_hands, committed_hand

    table = hands_table()
    table["Pair"]["level"] = 5
    table["Straight"]["level"] = 2
    state = GameState(make_state(hands=table))
    params = Config().params

    assert committed_hand(state, params) == "Pair"
    assert accepted_planet_hands(state, params) == ("Pair", "Straight")


def test_spare_can_be_disabled() -> None:
    from conftest import hands_table

    from balatro_bot.params import TunableParams
    from balatro_bot.strategy import accepted_planet_hands

    table = hands_table()
    table["Pair"]["level"] = 5
    table["Straight"]["level"] = 2
    state = GameState(make_state(hands=table))
    params = TunableParams(allow_spare_planet_hand=False)
    assert accepted_planet_hands(state, params) == ("Pair",)


def test_planet_for_the_spare_hand_is_accepted() -> None:
    """备选牌型的星球牌可以从包里吃掉——它已经在投入，不算分兵。"""
    from conftest import consumable, hands_table

    from balatro_bot.strategy import choose_pack_action

    table = hands_table()
    table["Pair"]["level"] = 5
    table["Straight"]["level"] = 2
    raw = make_state(state="SMODS_BOOSTER_OPENED", hands=table)
    with_area(raw, "pack", [
        consumable("c_jupiter", card_set="PLANET"),   # 同花：没投过
        consumable("c_saturn", card_set="PLANET"),    # 顺子：备选 ← 要这张
    ])
    action = choose_pack_action(GameState(raw), Config())
    assert not action.skip and action.card == 1


def test_planet_for_an_uninvested_hand_is_still_rejected() -> None:
    from conftest import consumable, hands_table

    from balatro_bot.strategy import choose_pack_action

    table = hands_table()
    table["Pair"]["level"] = 5
    table["Straight"]["level"] = 2
    raw = make_state(state="SMODS_BOOSTER_OPENED", hands=table)
    with_area(raw, "pack", [consumable("c_jupiter", card_set="PLANET")])   # 同花
    assert choose_pack_action(GameState(raw), Config()).skip


# ------------------------------------- 星球牌供给类优惠券（Telescope / Merchant）


def voucher_card(key: str):
    from balatro_bot.cards import Card

    return Card.from_dict({
        "id": 1, "key": key, "set": "VOUCHER", "label": key,
        "suit": None, "rank": None, "value": {}, "modifier": {}, "state": {},
        "cost": {"buy": 10, "sell": 5},
    })


def voucher_state(*owned: str, played: dict | None = None):
    from conftest import hands_table

    table = hands_table()
    table["Pair"].update(level=4, played=30)
    table["Flush"].update(played=5)
    if played:
        for name, count in played.items():
            table[name]["played"] = count
    raw = make_state(state="SHOP", money=40, jokers=[joker("j_jolly")],
                     deck=full_deck(), hands=table)
    if owned:
        raw["used_vouchers"] = {key: "" for key in owned}
    for area in ("shop", "vouchers", "consumables", "packs"):
        with_area(raw, area, [], limit=2)
    return GameState(raw)


def test_used_vouchers_are_read_from_the_state() -> None:
    from balatro_bot.strategy import current_planet_rate

    params = Config().params
    assert current_planet_rate(voucher_state(), params) == params.planet_rate_base
    assert current_planet_rate(
        voucher_state("v_planet_merchant"), params
    ) == params.planet_rate_merchant
    assert current_planet_rate(
        voucher_state("v_planet_merchant", "v_planet_tycoon"), params
    ) == params.planet_rate_tycoon


def test_telescope_guarantees_the_first_pack_card() -> None:
    """Telescope 让天体包第一张**保底**是想要的（card.lua:1737）。

    普通包命中期望 0.25 → 1.0，即 4 倍。
    """
    from balatro_bot.strategy import celestial_expected_levels

    free = celestial_expected_levels(12, 3, 1, 1)
    with_telescope = celestial_expected_levels(12, 3, 1, 1, guaranteed=True)
    assert free == pytest.approx(0.25)
    assert with_telescope == pytest.approx(1.0)
    assert with_telescope / free == pytest.approx(4.0)


def test_telescope_multiplier_is_the_hit_chance_ratio() -> None:
    from balatro_bot.strategy import voucher_supply_multiplier

    assert voucher_supply_multiplier(
        voucher_state(), "v_telescope"
    ) == pytest.approx(4.0)


def test_telescope_is_worthless_when_the_guaranteed_hand_is_not_acceptable() -> None:
    """保底落在"打得最多"的牌型上——如果那个牌型不是我们能吃的，买了也白买。

    这是实测价值最高的一条：我们可能一直在用别的牌型过渡，
    保底就落不到专精的地方。
    """
    from balatro_bot.strategy import voucher_supply_multiplier

    state = voucher_state(played={"High Card": 99})       # 打得最多的是高牌
    assert voucher_supply_multiplier(state, "v_telescope") == 1.0


def test_planet_merchant_multiplier_is_the_rate_gain() -> None:
    """Planet Merchant: planet_rate 4 → 9.6，即 2.4 倍。

    注意必须是**增量**——货架上那张还没买，写错成"当前值"就会得到 1.0。
    """
    from balatro_bot.strategy import voucher_supply_multiplier

    assert voucher_supply_multiplier(
        voucher_state(), "v_planet_merchant"
    ) == pytest.approx(9.6 / 4)
    # 已经持有 → 没有增量
    assert voucher_supply_multiplier(
        voucher_state("v_planet_merchant"), "v_planet_merchant"
    ) == pytest.approx(1.0)


def test_planet_tycoon_upgrades_from_merchant() -> None:
    """Planet Tycoon 需要先有 Planet Merchant，此时是 32/9.6 ≈ 3.33 倍。"""
    from balatro_bot.strategy import voucher_supply_multiplier

    assert voucher_supply_multiplier(
        voucher_state("v_planet_merchant"), "v_planet_tycoon"
    ) == pytest.approx(32 / 9.6)


def test_supply_multiplier_is_capped() -> None:
    """×8 那一档要被封顶，免得把整个评分尺度顶爆。"""
    from balatro_bot.strategy import voucher_value

    params = Config().params
    value = voucher_value(voucher_card("v_planet_tycoon"), voucher_state(), params)
    base = 6.0
    assert value == pytest.approx(base * params.voucher_multiplier_cap)


def test_supply_vouchers_are_prioritised() -> None:
    """供给类优惠券的估值要越过 `exception_value`，也就是**允许动用利息本金**。

    这是"提高优先级"的实际落点：门槛（5.0）它们本来就过，
    真正卡住的是"要不要为了它花掉那 25 块利息本金"（例外线 7.5）。
    """
    from balatro_bot.strategy import voucher_value

    params = Config().params
    for key in ("v_telescope", "v_planet_merchant"):
        value = voucher_value(voucher_card(key), voucher_state(), params)
        assert value >= params.exception_value, key
        assert value > 6.5, f"{key} 的估值没有提高"


def test_plain_vouchers_keep_their_base_value() -> None:
    from balatro_bot.strategy import VOUCHER_VALUE, voucher_value

    params = Config().params
    for key in ("v_overstock", "v_grabber"):
        assert voucher_value(voucher_card(key), voucher_state(), params) == \
            VOUCHER_VALUE[key]


def test_telescope_makes_celestial_packs_more_valuable() -> None:
    """Telescope 之后同一张天体包的每元增益应显著提高（供给改善）。"""
    from balatro_bot.cards import Card
    from balatro_bot.strategy import celestial_pack_gain_per_dollar, valuation_scenario

    plain = voucher_state()
    with_tel = voucher_state("v_telescope")
    card = Card.from_dict({
        "id": 2, "key": "p_celestial_normal_1", "set": "BOOSTER",
        "label": "天体包", "suit": None, "rank": None,
        "value": {}, "modifier": {}, "state": {}, "cost": {"buy": 4, "sell": 2},
    })

    rates = []
    for state in (plain, with_tel):
        scenarios, hand_name = valuation_scenario(state, Config())
        rates.append(celestial_pack_gain_per_dollar(
            state, Config(), card, scenarios, hand_name
        ))
    assert rates[0] > 0
    assert rates[1] == pytest.approx(rates[0] * 4.0, rel=1e-6)


def test_consumable_targets_never_return_empty_for_a_targeted_card() -> None:
    """算分牌不够时要从手里补，不能给 0 个目标（那也会被拒）。"""
    from conftest import card

    from balatro_bot.cards import Card
    from balatro_bot.strategy import consumable_targets

    state = shop(ante=1)
    hand = [Card.from_dict(card("H_A")), Card.from_dict(card("D_K"))]
    targets = consumable_targets(state, "c_sun", (), hand=hand)
    assert targets == (0, 1)


def test_unknown_consumable_gets_no_targets() -> None:
    from balatro_bot.strategy import consumable_targets

    state = shop(ante=1)
    assert consumable_targets(state, "c_hermit", (0, 1, 2)) == ()


def test_pack_selection_passes_targets_for_targeted_tarots() -> None:
    """秘术包里挑到需要目标的塔罗牌时，必须带上合法的目标张数。

    原来一律不带目标，于是 The Sun / The Moon / The Star / The World 会被
    游戏拒掉（"requires 1-3 target card(s). Provided: 0"），包等于白买。
    """
    from balatro_bot.cards import Card
    from balatro_bot.strategy import choose_pack_action

    from conftest import card, consumable, hands_table

    raw = make_state(state="SMODS_BOOSTER_OPENED", money=40, hands=hands_table())
    with_area(raw, "pack", [consumable("c_sun", card_set="TAROT")], limit=3)
    with_area(raw, "hand", [card("H_A"), card("D_A"), card("H_K")], limit=8)
    action = choose_pack_action(GameState(raw), Config())
    assert not action.skip
    assert 1 <= len(action.targets) <= 3


def test_pack_selection_falls_back_when_a_targeted_tarot_cannot_be_aimed() -> None:
    """手里没有牌可指定时，需要目标的塔罗牌只能放弃，不能带着 0 目标去撞错误。"""
    from balatro_bot.strategy import choose_pack_action

    from conftest import consumable, hands_table

    raw = make_state(state="SMODS_BOOSTER_OPENED", money=40, hands=hands_table())
    with_area(raw, "pack", [consumable("c_sun", card_set="TAROT")], limit=3)
    with_area(raw, "hand", [], limit=8)
    assert choose_pack_action(GameState(raw), Config()).skip
