"""一次出牌的算分。

Balatro 的算分顺序是：
    1. 取牌型基础 chips / mult（含等级加成）
    2. 每张打出的牌结算自身点数筹码、强化、版本；红蜡封等可以重复触发
    3. 结算每张小丑牌（含"留在手里"的牌触发的效果，例如 Baron 数手里的 K）
    4. 全部加完后，逐个应用倍率乘区
    5. 得分 = chips × mult

本模块按这个顺序算，并把各段分别暴露出来，方便看日志定位"分数为什么不对"。

乘区内部用连乘近似（Balatro 是按小丑牌从左到右依次乘），因为乘法可交换，
结果一致；真正重要的是"加区在乘区之前"，这一点是严格保证的。

核心函数 :func:`score_from_cards` 刻意不接收 GameState，只接收算分需要的几个
字段——弃牌决策要做上千次蒙特卡洛采样，每次都重建状态对象太慢。
按整个状态算分的入口是 :func:`score_play`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

from .cards import Card, classify, scoring_cards
from .jokers import JokerEffect, PlayContext, model_joker, retrigger_bonus, unknown_jokers
from .state import GameState

# 强化效果的数值。随机类取期望值（Lucky 1/5 概率 +20 Mult）。
ENHANCEMENT_CHIPS = {"BONUS": 30.0, "STONE": 50.0}
ENHANCEMENT_MULT = {"MULT": 4.0}
ENHANCEMENT_XMULT = {"GLASS": 2.0}
LUCKY_EXPECTED_MULT = 20.0 / 5.0       # 1/5 概率 +20 Mult
LUCKY_EXPECTED_MONEY = 20.0 / 15.0     # 1/15 概率 +$20

EDITION_CHIPS = {"FOIL": 50.0}
EDITION_MULT = {"HOLO": 10.0}
EDITION_XMULT = {"POLYCHROME": 1.5}

STEEL_HELD_XMULT = 1.5
GOLD_HELD_MONEY = 3.0
GOLD_SEAL_PLAYED_MONEY = 3.0


@dataclass(slots=True)
class ScoreBreakdown:
    """一次出牌的完整算分结果。

    chips / mult 是**按游戏真实顺序累加后的最终值**，不是各段简单相加：
    小丑牌按槽位顺序逐个结算（+加减、x乘区），所以顺序会影响结果
    （游戏源码 functions_state_events.lua 的 joker 循环）。
    """

    hand_name: str
    cards_played: int          # 打出几张（决定 Half Joker 这类条件）
    cards_scoring: int = 0     # 其中真正参与算分的几张
    base_chips: float = 0.0
    base_mult: float = 0.0
    card_chips: float = 0.0
    card_mult: float = 0.0
    card_xmult: float = 1.0
    joker_chips: float = 0.0
    joker_mult: float = 0.0
    joker_xmult: float = 1.0
    money: float = 0.0
    unknown: tuple[str, ...] = ()
    # 按真实顺序累加的最终值
    final_chips: float = 0.0
    final_mult: float = 0.0
    # 每张小丑牌在这一手里的收益（按槽位顺序），用于排序优化和排错
    joker_effects: tuple[JokerEffect, ...] = ()

    @property
    def chips(self) -> float:
        return self.final_chips

    @property
    def mult(self) -> float:
        return self.final_mult

    @property
    def xmult(self) -> float:
        """乘区总量，仅用于展示与断言。"""
        return self.card_xmult * self.joker_xmult

    @property
    def total(self) -> float:
        return self.final_chips * self.final_mult

    def describe(self) -> str:
        cards = (
            f"{self.cards_scoring}/{self.cards_played}张"
            if self.cards_scoring != self.cards_played
            else f"{self.cards_played}张"
        )
        return (
            f"{self.hand_name}({cards}) "
            f"chips={self.final_chips:.0f} mult={self.final_mult:.1f} "
            f"x={self.xmult:.3f} → {self.total:.0f}"
        )


def _card_effect_once(card: Card) -> JokerEffect:
    """一张牌单次触发时的贡献（不含点数筹码）。"""
    effect = JokerEffect()
    enhancement = card.enhancement
    if enhancement in ("BONUS", "STONE"):
        effect.chips += ENHANCEMENT_CHIPS[enhancement]
    elif enhancement == "MULT":
        effect.mult += ENHANCEMENT_MULT["MULT"]
    elif enhancement == "GLASS":
        effect.xmult *= ENHANCEMENT_XMULT["GLASS"]
    elif enhancement == "LUCKY":
        effect.mult += LUCKY_EXPECTED_MULT
        effect.money += LUCKY_EXPECTED_MONEY
    # 金色强化是"留在手里到回合结束"才给钱，不是打出时给，故此处不处理

    edition = card.edition
    if edition in ("FOIL", "HOLO"):
        if edition == "FOIL":
            effect.chips += EDITION_CHIPS[edition]
        else:
            effect.mult += EDITION_MULT[edition]
    elif edition == "POLYCHROME":
        effect.xmult *= EDITION_XMULT[edition]

    if card.seal == "GOLD":
        effect.money += GOLD_SEAL_PLAYED_MONEY
    return effect


def score_from_cards(
    hand_cards: Sequence[Card],
    played_indices: Iterable[int],
    *,
    hand_base: Callable[[str], tuple[int, int]],
    jokers: Sequence[Card] = (),
    hand_played: int = 0,
    discards_left: int = 0,
    hands_left: int = 1,
    deck_size: int = 0,
    money: int = 0,
    deck_composition: Sequence[Card] = (),
    hand_name: str | None = None,
    hands_played_this_round: frozenset[str] = frozenset(),
    hand_plays_run: Mapping[str, int] | None = None,
    base_scale: float = 1.0,
) -> ScoreBreakdown:
    """算一次出牌的期望得分。

    played_indices 是手牌的 0 基下标，和 ``play()`` 的参数一致。
    传空集合或非法牌型时 total 为 0。

    base_scale 用于 Boss 盲注 The Flint（"打出的牌型基础筹码和倍率减半"）。
    """
    indices = list(played_indices)
    total_cards = len(hand_cards)
    if len(set(indices)) != len(indices):
        raise ValueError(f"手牌下标重复：{indices}")
    if any(i < 0 or i >= total_cards for i in indices):
        raise IndexError(f"手牌下标越界：{indices}，当前手牌 {total_cards} 张")

    played_full = [hand_cards[i] for i in indices]
    index_set = set(indices)
    held = [c for i, c in enumerate(hand_cards) if i not in index_set]

    name = hand_name or classify(played_full)
    if name is None:
        return ScoreBreakdown(hand_name="", cards_played=len(played_full))

    # 被 Boss 禁用的小丑牌既不结算，也不该提供"关键字"型效果
    # （Splash 让所有牌算分、Mime 让留手牌重触发、四指改牌型判定……）——
    # 它已经被禁用了，那些能力当然也一起失效。
    joker_keys = frozenset(c.key for c in jokers if not c.debuff)

    # 关键：只有构成牌型的那几张牌算分。Splash 例外——它让打出的每张牌都算分。
    scoring = (
        list(played_full)
        if "j_splash" in joker_keys
        else scoring_cards(played_full)
    )

    base_chips, base_mult = hand_base(name)
    breakdown = ScoreBreakdown(
        hand_name=name,
        cards_played=len(played_full),
        cards_scoring=len(scoring),
        base_chips=float(base_chips) * base_scale,
        base_mult=float(base_mult) * base_scale,
    )

    ctx = PlayContext(
        played=tuple(scoring),          # 花色/点数类小丑牌数的是"算分牌"
        held=tuple(held),
        hand_name=name,
        joker_keys=joker_keys,
        joker_count=len(jokers),
        hand_played=hand_played,
        discards_left=discards_left,
        deck_size=deck_size,
        money=money,
        cards_played=len(played_full),  # Half Joker 看的是打出的总张数
        is_final_hand=hands_left <= 1,
        deck_composition=tuple(deck_composition),
        hands_played_this_round=hands_played_this_round,
        hand_plays_run=dict(hand_plays_run or {}),
    )

    # ---- 打出的牌：只有算分牌结算点数与强化
    for position, card in enumerate(scoring):
        if not card.scores:
            continue
        for _ in range(1 + retrigger_bonus(ctx, card, position)):
            breakdown.card_chips += card.base_chips
            effect = _card_effect_once(card)
            breakdown.card_chips += effect.chips
            breakdown.card_mult += effect.mult
            breakdown.card_xmult *= effect.xmult
            breakdown.money += effect.money

    # ---- 留在手里的牌（钢牌 / 金强化）
    # 这些在"打出的牌"之后、"小丑牌"之前结算，因此归入卡牌乘区。
    mime_triggers = 2 if "j_mime" in ctx.joker_keys else 1
    for card in held:
        if card.enhancement == "STEEL":
            breakdown.card_xmult *= STEEL_HELD_XMULT ** mime_triggers
        elif card.enhancement == "GOLD":
            breakdown.money += GOLD_HELD_MONEY * mime_triggers
    # ---- 小丑牌：严格按槽位顺序结算
    #
    # 游戏的顺序（functions_state_events.lua）：
    #   1. 版本提供的 +筹码 / +倍率（闪箔 +50 筹码、全息 +10 倍率）
    #   2. 小丑牌自身效果
    #   3. 版本提供的乘区（多彩 x1.5）—— 在自身效果之后
    # 因为加法和乘法交替进行，**槽位顺序会改变最终得分**：
    # 加法牌放左边、乘区牌放右边才能拿满。Solver 会用这个性质搜索最优顺序。
    chips = breakdown.base_chips + breakdown.card_chips
    mult = (breakdown.base_mult + breakdown.card_mult) * breakdown.card_xmult

    collected: list[JokerEffect] = []
    for index, joker in enumerate(jokers):
        # 被 Boss 禁用的小丑牌不结算。Crimson Heart（Ante 8 精英盲注）
        # 每手禁用它其中一张，游戏会把 debuff 写进状态（blind.lua:647 专门
        # 处理 card.area == G.jokers）。这里不跳过的话，每手都会高估自己的
        # 分数——实测过：Jolly 被禁用后预测仍然是 300，和没禁用一样。
        if joker.debuff:
            continue

        if joker.edition == "FOIL":
            chips += EDITION_CHIPS["FOIL"]
            breakdown.joker_chips += EDITION_CHIPS["FOIL"]
        elif joker.edition == "HOLO":
            mult += EDITION_MULT["HOLO"]
            breakdown.joker_mult += EDITION_MULT["HOLO"]

        effect = model_joker(joker.key, ctx, joker, joker_index=index, jokers=tuple(jokers))
        chips += effect.chips
        mult += effect.mult
        mult *= effect.xmult
        breakdown.joker_chips += effect.chips
        breakdown.joker_mult += effect.mult
        breakdown.joker_xmult *= effect.xmult
        breakdown.money += effect.money

        if joker.edition == "POLYCHROME":
            mult *= EDITION_XMULT["POLYCHROME"]
            breakdown.joker_xmult *= EDITION_XMULT["POLYCHROME"]
            effect = JokerEffect(chips=effect.chips, mult=effect.mult,
                                 xmult=effect.xmult * EDITION_XMULT["POLYCHROME"],
                                 money=effect.money)
        collected.append(effect)

    breakdown.joker_effects = tuple(collected)
    breakdown.final_chips = chips
    breakdown.final_mult = mult
    breakdown.unknown = tuple(unknown_jokers([c.key for c in jokers]))
    return breakdown


def score_play(
    state: GameState,
    played_indices: Iterable[int],
    *,
    hand_name: str | None = None,
) -> ScoreBreakdown:
    """按整个游戏状态算一次出牌的得分。"""
    return score_from_cards(
        state.hand.cards,
        played_indices,
        hand_base=state.hand_base,
        jokers=state.jokers.cards,
        hand_played=state.round.hands_played,
        discards_left=state.round.discards_left,
        hands_left=state.round.hands_left,
        deck_size=len(state.draw_pile.cards),
        money=state.money,
        deck_composition=tuple(state.hand.cards) + tuple(state.draw_pile.cards),
        hand_name=hand_name,
        hands_played_this_round=_played_this_round(state),
        hand_plays_run=_plays_run(state),
    )


def _played_this_round(state: GameState) -> frozenset[str]:
    """本回合已经打出过的牌型（Card Sharp 用）。"""
    return frozenset(
        name for name, info in state.hands.items() if info.played_this_round > 0
    )


def _plays_run(state: GameState) -> dict[str, int]:
    """本局每种牌型打过几次（Supernova 用）。"""
    return {name: info.played for name, info in state.hands.items()}


def describe_jokers(state: GameState) -> str:  # pragma: no cover - 仅用于日志
    """列出当前小丑牌，并标出哪些没建模。"""
    unknown = set(unknown_jokers([c.key for c in state.jokers.cards]))
    parts = []
    for card in state.jokers.cards:
        marker = " (未建模)" if card.key in unknown else ""
        parts.append(f"{card.label or card.key}{marker}")
    return ", ".join(parts) if parts else "无"
