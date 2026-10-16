"""策略层：把状态变成"下一步做什么"。

这一层不直接调用 RPC，只返回动作对象，因此可以脱离游戏单独测试和回放。
机器人主循环负责把动作翻译成接口调用。

决策分四块：
    * 盲注：选还是跳
    * 出牌：打哪几张 / 弃哪几张
    * 消耗牌：什么时候用
    * 商店：买什么、什么时候重摇、什么时候走
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from math import comb
from typing import Callable, Collection, Sequence

from .bosses import BossEffect, boss_effect
from .cards import Card, hand_base_level_up, hand_base_one_level_down
from .jokers import (
    NO_SCORE_EFFECT,
    is_compounding,
    is_multiplicative,
    joker_value,
    raises_multiplier,
)
from .journal import note
from .params import TunableParams
from .planning import (
    MAX_DISCARD,
    PlanWeights,
    hand_base_score,
    plan_expected_score,
    plan_miss_score,
    plan_targets,
    representative_hands,
    strategy_switch,
    top_base_score,
)
from .solver import Candidate, Solver
from .state import GameState

# 决策函数默认使用的参数集。生产路径会把 Config.params 传进来；
# 单元测试直接调函数时用这个默认值，行为和不参数化时一致。
DEFAULT_PARAMS = TunableParams()

# --------------------------------------------------------------------- 配置


@dataclass
class Config:
    """机器人行为参数。默认值偏向"稳"：不做激进赌博，优先保住过关。"""

    deck: str = "RED"
    stake: str = "WHITE"
    seed: str | None = None

    # 所有可调参数集中在这里（见 params.py）。修改策略不需要改决策代码。
    params: TunableParams = field(default_factory=TunableParams)

    # 弃牌决策用哪套算法：
    #   "plan"        —— 规格里的评分函数（概率 + 牌型倍率 - 弃牌惩罚），精确无噪声
    #   "monte-carlo" —— 旧的补牌采样估计（保留下来便于对比）
    discard_strategy: str = "plan"

    # 每次决策前睡多少秒（节流）。0 = 全速。决策本身只要 4~6 ms，
    # 整局墙钟的大头是游戏自己结算动作，这个值用来放慢节奏、减轻游戏负担。
    decision_delay: float = 0.0

    # 每回合基准出牌数（The Manacle / 部分牌组会改；状态里读到 0 时用这个兜底）
    hands_per_round: int = 4

    # --- 弃牌
    use_discards: bool = True
    # 只有走 "monte-carlo" 路径时才用到的两个参数
    # （"plan" 路径用精确的超几何概率，不需要采样）
    discard_samples: int = 32
    discard_min_gain: float = 1.25
    discard_max_cards: int = 5

    # --- 商店
    # 预算类参数留在 Config（不是可调策略，是规则约束）；
    # 估值阈值之类的可调项都在 params 里。
    max_rerolls_by_ante: dict[int, int] = field(
        default_factory=lambda: {1: 3, 2: 2, 3: 1}
    )

    # --- 盲注
    skip_blinds: bool = False

    # --- 消耗牌
    use_planet_cards: bool = True


# --------------------------------------------------------------------- 动作


@dataclass(slots=True)
class SelectBlind:
    pass


@dataclass(slots=True)
class SkipBlind:
    pass


@dataclass(slots=True)
class Play:
    indices: tuple[int, ...]


@dataclass(slots=True)
class Discard:
    indices: tuple[int, ...]


@dataclass(slots=True)
class CashOut:
    pass


@dataclass(slots=True)
class NextRound:
    pass


@dataclass(slots=True)
class Refresh:
    """什么都不做，只重新取一次状态。

    用于过渡状态（HAND_PLAYED / DRAW_TO_HAND / ...）——这些状态下游戏
    还在放动画，任何操作都会被拒绝，轮询才是正确做法。
    """

    pass


@dataclass(slots=True)
class Rearrange:
    """重排小丑牌槽位。

    游戏按槽位从左到右逐个结算，加法和乘法交替进行，所以把加法牌放左边、
    乘区牌放右边能拿到**完全免费**的分数提升。
    """

    jokers: tuple[int, ...]


@dataclass(slots=True)
class Buy:
    card: int | None = None
    voucher: int | None = None
    pack: int | None = None


@dataclass(slots=True)
class Sell:
    joker: int | None = None
    consumable: int | None = None


@dataclass(slots=True)
class Reroll:
    pass


@dataclass(slots=True)
class UseConsumable:
    index: int
    cards: tuple[int, ...] = ()


@dataclass(slots=True)
class OpenPack:
    card: int | None = None
    targets: tuple[int, ...] = ()
    skip: bool = False


Action = (
    SelectBlind | SkipBlind | Play | Discard | CashOut | NextRound | Refresh
    | Rearrange | Buy | Sell | Reroll | UseConsumable | OpenPack
)


# --------------------------------------------------------------- 商店估值表

VOUCHER_VALUE: dict[str, float] = {
    "v_antimatter": 10.0,       # 多一个小丑牌位
    "v_overstock": 8.0,         # 商店多一个商品位
    "v_overstock_plus": 8.0,
    "v_clearance_sale": 7.5,    # 全场七五折
    "v_liquidation": 7.5,
    "v_directors_cut": 7.0,     # 可以重摇 Boss 盲注
    "v_paint_brush": 7.0,       # 手牌上限 +1
    "v_palette": 7.0,
    "v_seed_money": 7.0,        # 利息上限提高
    "v_money_tree": 7.0,
    "v_telescope": 6.5,
    "v_observatory": 6.5,
    "v_grabber": 7.0,           # 每回合多一手（视频推荐的优惠券之一）
    "v_nacho_tong": 7.0,
    "v_tarot_merchant": 6.0,
    "v_planet_merchant": 6.0,
    "v_tarot_tycoon": 6.0,
    "v_planet_tycoon": 6.0,
    "v_reroll_surplus": 5.5,
    "v_reroll_glut": 5.5,
    "v_crystal_ball": 5.5,
    "v_omen_globe": 5.5,
    "v_magic_trick": 5.0,
    "v_illusion": 5.0,
    "v_hieroglyph": 5.0,
    "v_petroglyph": 5.0,
    "v_hone": 5.0,
    "v_gloss": 5.0,
    "v_wasteful": 5.0,
    "v_recyclomancy": 5.0,
    "v_blank": 3.0,
}
DEFAULT_VOUCHER_VALUE = 5.0

# 补充包优先级，取自视频："小丑包 > 幻灵包 > 秘术包 > 标准包 > 天体包"。
#
# 这里和我最初的判断相反：我原本把星球包排在第一位（"升级牌型最稳"），
# 但天体包给的是**随机牌型**的星球牌，常常不是我们专精的那个，买了可能
# 一张都用不上；小丑包则是直接补强战力。所以按视频的顺序。
# 阈值是 5.0，因此实际只会买前三种。
PACK_VALUE: dict[str, float] = {
    "buffoon": 8.0,     # 小丑牌：直接补强
    "spectral": 7.0,    # 幻灵牌：高风险高收益
    "arcana": 6.0,      # 塔罗牌：强化核心牌
    "standard": 4.0,    # 普通扑克牌
    "celestial": 4.5,   # 星球牌：内容随机，常常用不上
}
DEFAULT_PACK_VALUE = 4.0

# 值得为它跳过盲注的标签
VALUABLE_TAGS = frozenset({
    "Charm Tag", "Meteor Tag", "Buffoon Tag", "Ethereal Tag",
    "Investment Tag", "Voucher Tag", "Rare Tag", "Negative Tag",
    "Top-up Tag", "Speed Tag", "Handy Tag", "Garbage Tag",
})

# 星球牌：立刻使用，升级对应牌型。牌型等级是本作最稳的收益来源。
PLANET_KEYS = frozenset({
    "c_mercury", "c_venus", "c_earth", "c_mars", "c_jupiter", "c_saturn",
    "c_uranus", "c_neptune", "c_pluto", "c_planet_x", "c_ceres", "c_eris",
})

# 不需要指定手牌目标、收益确定为正的塔罗牌，看到就用。
SAFE_CONSUMABLE_KEYS = frozenset({
    "c_hermit",            # 钱翻倍（最多 +$20）
    "c_temperance",        # 每张小丑牌给 $1（最多 $50）
    "c_wheel_of_fortune",  # 1/4 概率给一张小丑牌加版本
    "c_emperor",           # 生成最多 2 张塔罗牌
    "c_high_priestess",    # 生成最多 2 张星球牌
    "c_judgement",         # 生成一张随机小丑牌（需要空位）
    "c_fool",              # 复制上一次使用的塔罗/星球牌
})

# 需要指定手牌目标的塔罗牌，目标取"当前最优出牌用到的牌"。
TARGETED_CONSUMABLE_KEYS = frozenset({
    "c_magician", "c_empress", "c_hierophant", "c_devil", "c_tower",
    "c_strength", "c_hanged_man", "c_death", "c_justice", "c_lovers",
    "c_chariot", "c_star", "c_moon", "c_sun", "c_world",
})

# 需要指定手牌目标的消耗牌**最多能选几张**（游戏里的 max_highlighted）。
#
# 选少了和选多了都会被游戏拒绝，实测两种都踩过：
#   "Consumable 'The Tower' requires exactly 1 card (provided: 5)"
#   "Card 'c_sun' requires 1-3 target card(s). Provided: 0"
# 规则：给到上限即可（游戏接受 1..上限）；"恰好 N 张"的牌，上限本身就是 N。
CONSUMABLE_TARGET_COUNT: dict[str, int] = {
    # 最多 2 张
    "c_magician": 2, "c_empress": 2, "c_hierophant": 2, "c_chariot": 2,
    "c_strength": 2, "c_hanged_man": 2, "c_devil": 2,
    "c_death": 2,                       # 恰好 2 张：把左边那张变成右边那张
    # 恰好 1 张
    "c_tower": 1, "c_justice": 1, "c_lovers": 1,
    # 最多 3 张（把选中的牌改成某种花色）
    "c_star": 3, "c_moon": 3, "c_sun": 3, "c_world": 3,
    # 幻灵牌加蜡封：都是恰好 1 张
    "c_deja_vu": 1, "c_trance": 1, "c_medium": 1, "c_talisman": 1,
}


def swap_victims(
    jokers: Sequence[Card],
    incoming_key: str,
    params: TunableParams = DEFAULT_PARAMS,
    *,
    urgent: bool = False,
    sellable: Sequence[int] | None = None,
    rank_value: "Callable[[str], float] | None" = None,
) -> list[int]:
    """① 换牌时**允许**牺牲的牌位。整个 ① 的语义都在这里，便于单独测试。

    问题是：牺牲品按**当前**边际贡献挑（`_worst`），而成长型/乘法型的价值在
    后面每一手——男爵手里没 K 时约等于 0、星座刚买时 ×1.0、绿色丑第一手只是
    个 +1。所以它们**必然**在还没长起来时被判成最差。实测每局换 3.6 次牌，
    363 次换牌里 60 次卖掉的是乘法牌，局终 54% 的局一张 ×Mult 都没有。

    ```text
    非紧急时：
      有非复利牌可牺牲    → 只在这类里挑（复利牌留下来长）
      全队都是复利牌      → 只允许"复利换复利、且静态成色更高"的单调升级
      换进来的不是复利牌  → 空集（用一次性牌换复利牌是净亏）
    紧急时 / 关掉守卫    → 原行为（全部可牺牲）
    ```

    注意 `j_duo`（×2 倍率）**本身就是复利牌**——第一版守卫写成"换进来的是
    复利牌就放行"，被它绕过去，测试抓到了。

    `rank_value` 是静态成色口径，由调用方注入（它要捕获 `state`/`params`）；
    不给就用最朴素的静态表。
    """
    rank = rank_value or joker_value
    pool = list(sellable) if sellable is not None else [
        i for i, card in enumerate(jokers) if not card.eternal
    ]
    if not params.compound_switch_guard or urgent:
        return pool
    plain = [i for i in pool if not is_compounding(jokers[i].key)]
    if plain:
        return plain
    if not is_compounding(incoming_key):
        return []
    incoming = rank(incoming_key)
    return [
        i for i in pool
        if is_compounding(jokers[i].key)
        and rank(jokers[i].key) < incoming - params.sell_upgrade_margin
    ]


def filler_penalty(key: str, ante: int, params: TunableParams = DEFAULT_PARAMS) -> float:
    """② 廉价填充牌的估值惩罚，随底注递增。

    牌位只有 5 个，而每个底注的过关分涨 2.08 倍。一张 +4 倍率在底注 2 是
    "+40%"，在底注 6 连零头都算不上，却照样占着一个位子——实测 620 次小丑牌
    买入里 **70.6% 是这种估值 ≤6.5 的牌**（`tools/mult_diagnosis.py`）。

    ```text
    penalty = filler_penalty_per_ante × max(0, 底注 − filler_free_antes)
    ```

    复利牌（`is_compounding`）**不罚**：它们现在看着弱，但会自己长大。
    成色高的牌也不罚（`filler_value_ceiling` 以上）。
    """
    if is_compounding(key) or joker_value(key) > params.filler_value_ceiling:
        return 0.0
    over = max(0, ante - params.filler_free_antes)
    return params.filler_penalty_per_ante * over


def standard_pack_bonus(
    state: GameState, params: TunableParams = DEFAULT_PARAMS
) -> float:
    """③ 牌库还缺多少张强化牌 → Standard 包该加多少估值。

    Standard 包是"拿到**已经强化好**的牌"的主渠道，而
    玻璃牌算分 ×2、钢铁牌留手 ×1.5，**和 ×Mult 小丑牌相乘**——
    这是第二条乘法通道（5 张玻璃 = ×32）。

    实测 100 局里 Standard 包买了 **0 次**：它的常量估值 4.0 永远够不到
    `pack_threshold = 5.0`（那条闸门比的是**绝对值**）。而每局最多只改造出
    0.14 张 STEEL、0.08 张 GLASS。

    设计成"缺得越多越值钱、攒够了自然不再买"，所以它是自我限幅的。
    """
    from .build import DeckProfile

    profile = DeckProfile.from_cards(state.full_deck)
    enhanced = sum(profile.by_enhancement.values())
    missing = max(0, params.enhanced_target - enhanced)
    return params.standard_pack_bonus * missing


# 强化的相对价值（标准包选牌用）。玻璃算分 ×2 最强，钢铁留手 ×1.5 次之。
ENHANCEMENT_VALUE: dict[str, float] = {
    "GLASS": 3.0, "STEEL": 2.5, "MULT": 2.0, "BONUS": 1.5,
    "WILD": 1.5, "LUCKY": 1.5, "GOLD": 1.0, "STONE": 0.5,
}


def _archetype_core(archetype: str | None) -> frozenset[str]:
    """主流派的核心小丑牌名单（流派不存在时返回空集）。"""
    if not archetype:
        return frozenset()
    from .build import ARCHETYPES

    spec = ARCHETYPES.get(archetype)
    return spec.core if spec is not None else frozenset()


def _merge_targets(preferred: Sequence[int], fallback: Sequence[int]) -> tuple[int, ...]:
    """流派偏好的目标排在前面，其余用算分牌补位（去重、保持顺序）。"""
    out: list[int] = []
    for index in list(preferred) + list(fallback):
        if index not in out:
            out.append(index)
    return tuple(out)


def consumable_targets(
    state: GameState,
    card_key: str,
    preferred: Sequence[int] = (),
    *,
    hand: Sequence[Card] | None = None,
) -> tuple[int, ...]:
    """把"想指定的手牌"裁到这张消耗牌允许的张数。

    目标优先取**真正算分的那几张**——改点数、改花色、加蜡封都只在乎它们。
    算分牌不够时再从手里补，避免出现 0 目标被游戏拒绝。
    """
    allowed = CONSUMABLE_TARGET_COUNT.get(card_key, 0)
    if allowed <= 0:
        return ()
    pool = list(preferred)
    if hand is not None:
        pool += [index for index in range(len(hand)) if index not in pool]
    return tuple(pool[:allowed])


# 幻灵牌：默认不用（不少是破坏性的），但这几张收益明确。
SAFE_SPECTRAL_KEYS = frozenset({
    "c_black_hole",   # 所有牌型等级 +1
    "c_soul",         # 生成一张传奇小丑牌
})
# 需要指定手牌目标的幻灵牌（给算分牌加蜡封）
TARGETED_SPECTRAL_KEYS = frozenset({
    "c_deja_vu",      # 红蜡封（重复触发）
    "c_trance",       # 蓝蜡封
    "c_medium",       # 紫蜡封
    "c_talisman",     # 金蜡封（+$3）
})
# Wraith 会生成一张稀有丑，但**把钱清零**——钱多时用它是亏的
WRAITH_KEY = "c_wraith"
WRAITH_MAX_MONEY = 8

# --------------------------------------------------- 牌型专精与契合度
#
# 视频策略的核心口诀："前期用大中牌型过渡，尽早转小牌型"。
# 小牌型（对子/高牌）基础分低，但容易凑出、能吃的小丑牌更多、
# 也更能利用蜡封/黄金/钢铁这类增强牌——所以长期收益远高于大牌型。

SMALL_HAND_PRIORITY: tuple[str, ...] = (
    "Pair", "Two Pair", "Three of a Kind", "High Card",
)

# 拿到这些"降低大牌型难度"的辅助丑之后，大牌型才值得当专精目标
BIG_HAND_ENABLERS: dict[str, tuple[str, ...]] = {
    "j_four_fingers": ("Flush", "Straight", "Straight Flush"),
    "j_smeared": ("Flush", "Flush House", "Flush Five"),
    "j_shortcut": ("Straight", "Straight Flush"),
}

# 牌型条件型小丑牌 -> 它要求的牌型。
#
# 视频原话："泛用性越好，抓取优先级越高。例如卡文迪许 > 老千小丑 > 杂技演员"。
# 说的是无条件的 x3（卡文迪许）比"要本回合重复牌型"的 x3（老千）更可靠。
# 更关键的是：这些牌只有在我们真打那个牌型时才有价值——静态估值表给
# 对子丑和同花丑一样的分数，但专精对子时同花丑就是废牌。
JOKER_HAND_REQUIREMENT: dict[str, str] = {
    "j_jolly": "Pair", "j_sly": "Pair", "j_duo": "Pair",
    "j_zany": "Three of a Kind", "j_wily": "Three of a Kind",
    "j_trio": "Three of a Kind",
    "j_mad": "Two Pair", "j_clever": "Two Pair",
    "j_crazy": "Straight", "j_devious": "Straight", "j_order": "Straight",
    "j_droll": "Flush", "j_crafty": "Flush", "j_tribe": "Flush",
    "j_family": "Four of a Kind",
}
SYNERGY_BONUS = 1.5      # 和专精牌型一致：这就是核心引擎
SYNERGY_PENALTY = 2.0    # 牌型不匹配：再强我们也吃不到


def pack_kind(key: str) -> str:
    """从补充包的 key 里解析出类别。"""
    for kind in PACK_VALUE:
        if key.startswith(f"p_{kind}"):
            return kind
    return ""


# 商店货架上的卡不只是小丑牌，还可能是塔罗牌、星球牌或普通扑克牌。
# 这些必须分类估值——曾经这里把所有货架卡都按 joker_value 打分，
# 结果星球牌拿到默认值 4.0（低于购买阈值），机器人从来没买过星球牌，
# 白白丢掉了本作最稳的收益来源（升级牌型）。
PLANET_SHOP_VALUE = 7.0        # 我们确实在打的牌型：升级它，长期收益最高
PLANET_COMMON_VALUE = 5.5      # 常见牌型，还没打过但大概率会打
PLANET_OFFTYPE_VALUE = 3.0     # 我们根本不打的牌型，升级了也白搭
TAROT_SHOP_VALUE = 6.5         # 会用到的塔罗牌
UNUSED_TAROT_VALUE = 2.0       # 不认识的塔罗牌（我们不主动用）
PLAYING_CARD_SHOP_VALUE = 3.0  # 普通扑克牌，基本不值得买

# 星球牌 -> 它升级的牌型。取自游戏本体 game.lua 的 config.hand_type。
PLANET_HAND: dict[str, str] = {
    "c_mercury": "Pair",
    "c_venus": "Three of a Kind",
    "c_earth": "Full House",
    "c_mars": "Four of a Kind",
    "c_jupiter": "Flush",
    "c_saturn": "Straight",
    "c_uranus": "Two Pair",
    "c_neptune": "Straight Flush",
    "c_pluto": "High Card",
    "c_planet_x": "Five of a Kind",
    "c_ceres": "Flush House",
    "c_eris": "Flush Five",
}

# 还没打过也值得先升的常见牌型（高牌、五条这些太罕见，不值得投）
COMMON_HANDS = frozenset({
    "Pair", "Two Pair", "Three of a Kind", "Straight", "Flush", "Full House",
})


def target_hand_types(state: GameState) -> tuple[str, ...]:
    """本局应该专精哪些牌型，按优先级。

    视频策略："前期用大中牌型过渡，尽早转小牌型"。小牌型（对子）容易凑、
    能吃的小丑牌更多、也更能利用增强牌。只有拿到"降低大牌型难度"的辅助丑
    （四指 / 涂抹 / 捷径）时，大牌型才值得当目标。
    """
    keys = {card.key for card in state.jokers.cards}
    enabled: list[str] = []
    for joker_key, hand_types in BIG_HAND_ENABLERS.items():
        if joker_key in keys:
            enabled.extend(hand_types)
    if enabled:
        # 有了辅助丑，大牌型的基础分高得多，值得优先
        return tuple(dict.fromkeys(enabled + list(SMALL_HAND_PRIORITY)))
    return SMALL_HAND_PRIORITY


def committed_hand(
    state: GameState,
    params: TunableParams = DEFAULT_PARAMS,
) -> str | None:
    """本局专精的牌型——**锁定**在一个牌型上。

    规则按顺序：

    1. **等级领先就锁定**：等级最高的那个牌型只要比第二名高出
       `commit_lead` 级，就锁定它。这一条是"锁死"的关键：
       * 已经投进去的星球牌不会被浪费；
       * γ（前后期切换）不会来回摆——否则好不容易把专精牌型升上去、
         γ 刚切到中后期，目标一换又掉回前期；
       * 买了一张"降低大牌型难度"的辅助丑（四指/涂抹/捷径）也不会把
         整个阵容的投资方向掀翻。
    2. 否则按优先级挑第一个升过级的（`target_hand_types`）。
    3. 非目标牌型已经投到 `keep_invested_level` 级以上，保留它。
    4. 都没有就返回优先级第一名（通常是"对子"）。
    """
    table = state.hands
    if not table:
        return None
    targets = target_hand_types(state)

    ranked = sorted(
        table.values(), key=lambda info: (info.level, info.played), reverse=True
    )
    if ranked:
        top = ranked[0]
        runner_up = ranked[1].level if len(ranked) > 1 else 1
        if top.level > 1 and top.level - runner_up >= params.commit_lead:
            return top.name

    for name in targets:
        info = table.get(name)
        if info is not None and info.level > 1:
            return name

    if ranked:
        best = ranked[0]
        if best.name not in targets and best.level >= params.keep_invested_level:
            return best.name

    return targets[0] if targets else None


def spare_planet_hand(
    state: GameState, params: TunableParams = DEFAULT_PARAMS
) -> str | None:
    """已经在投入的**备选**牌型：等级 > 1，且不是主牌型。

    用途：天体包里展示的星球牌可能不是主牌型的。只要那张牌对应的牌型
    **已经在投入**（等级 > 1），吃掉它就不算分兵。

    关键约束：备选**只影响"要不要这张星球牌"**，不参与 γ 的计算——
    前后期切换只看主牌型（`committed_hand`），所以备选不会把 γ 带偏。
    """
    if not params.allow_spare_planet_hand:
        return None
    primary = committed_hand(state, params)
    best = None
    for info in state.hands.values():
        if info.name == primary or info.level <= 1:
            continue
        if best is None or (info.level, info.played) > (best.level, best.played):
            best = info
    return best.name if best else None


def accepted_planet_hands(
    state: GameState, params: TunableParams = DEFAULT_PARAMS
) -> tuple[str, ...]:
    """可以吃星球牌的牌型：主牌型（+ 已在投入的备选）。"""
    primary = committed_hand(state, params)
    if not primary:
        return ()
    spare = spare_planet_hand(state, params)
    return (primary, spare) if spare and spare != primary else (primary,)


# 天体包的（展示张数, 可拿张数），数据来自 game.lua:673-680：
#   p_celestial_normal  extra=3 choose=1  $4
#   p_celestial_jumbo   extra=5 choose=1  $6
#   p_celestial_mega    extra=5 choose=2  $8
CELESTIAL_PACK_SHAPES: dict[str, tuple[int, int]] = {
    "mega": (5, 2),
    "jumbo": (5, 1),
    "normal": (3, 1),
}


def celestial_pack_shape(key: str) -> tuple[int, int]:
    """从包名解析 (展示张数, 可拿张数)。"""
    for shape, value in CELESTIAL_PACK_SHAPES.items():
        if shape in key:
            return value
    return 3, 1


def celestial_expected_levels(
    pool: int,
    shown: int,
    choose: int,
    wanted: int,
    *,
    guaranteed: bool = False,
) -> float:
    """期望能拿到几张想要的星球牌（超几何分布）。

    从 `pool` 张里展示 `shown` 张，其中有 `wanted` 张是我们想要的，最多能拿
    `choose` 张。`choose=2` 只在池子里有两张想要的时才有意义——硬锁之下
    （`wanted=1`）超级包的"能拿两张"用不上，所以它和巨型包期望相同却更贵。

    `guaranteed=True` 表示**第一张必然是想要的**：这是 Telescope 的效果
    （`card.lua:1737`，包里第一张被强制成"打得最多"的牌型的星球牌）。
    这时先拿走它，再看剩下 `shown−1` 张里还有没有。
    """
    if pool <= 0 or shown <= 0 or wanted <= 0 or choose <= 0:
        return 0.0
    shown = min(shown, pool)
    total = comb(pool, shown)
    if total <= 0:
        return 0.0

    if guaranteed:
        taken = min(1, choose)
        extra = celestial_expected_levels(
            pool - 1, shown - 1, choose - taken, wanted - 1
        )
        return taken + extra

    expected = 0.0
    for hits in range(0, min(shown, wanted) + 1):
        ways = comb(wanted, hits) * comb(pool - wanted, shown - hits)
        expected += (ways / total) * min(hits, choose)
    return expected


def guaranteed_planet_hand(state: GameState) -> str | None:
    """Telescope 保底给的那张星球牌对应哪个牌型。

    游戏逻辑（`card.lua:1737-1751`）：天体包的**第一张**被强制成
    **打得次数最多**的牌型（按 `played`，不是按等级）的星球牌。

    注意这个"打得最多"未必是我们的专精牌型——如果我们一直在打别的牌型过渡，
    Telescope 的保底就落不到我们要的地方。所以调用方必须再判一次它是否
    属于可吃牌型。

    状态里没有 `visible` 字段，用 `played > 0` 近似（没打过就不会显示）。
    """
    seen = [info for info in state.hands.values() if info.played > 0]
    if not seen:
        return None
    return max(seen, key=lambda info: info.played).name


def current_planet_rate(state: GameState, params: TunableParams = DEFAULT_PARAMS) -> float:
    """当前商店里星球牌的出现率。

    游戏里 `G.GAME.planet_rate` 默认 4（`game.lua:1903`），
    Planet Merchant 设成 `4 × 2.4 = 9.6`、Planet Tycoon 设成 `4 × 8 = 32`
    （`card.lua:1895-1899`）。
    """
    if state.owns_voucher("v_planet_tycoon"):
        return params.planet_rate_tycoon
    if state.owns_voucher("v_planet_merchant"):
        return params.planet_rate_merchant
    return params.planet_rate_base


def planet_rate_gain(
    state: GameState, key: str, params: TunableParams = DEFAULT_PARAMS
) -> float:
    """"买下 key 之后"商店星球牌出现率相对**现在**的倍数。

    注意是**增量**而不是当前值：商店货架上那张还没买，估值要算的是买了之后的
    变化。（写错成当前值的话，Planet Merchant 会被估成 1.0 倍、等于没加分。）
    """
    target = {
        "v_planet_merchant": params.planet_rate_merchant,
        "v_planet_tycoon": params.planet_rate_tycoon,
    }.get(key)
    if target is None:
        return 1.0
    current = current_planet_rate(state, params)
    if current <= 0 or target <= current:
        return 1.0
    return target / current


def voucher_supply_multiplier(state: GameState, key: str) -> float:
    """这张优惠券对"星球牌供给"的提升倍数（与星球牌无关的返回 1.0）。

    * **Telescope** —— 天体包第一张保底是"打得最多"的牌型的星球牌。
      如果那个牌型正好在可吃牌型里，命中期望按 `celestial_expected_levels`
      的 `guaranteed` 分支重算（普通包 0.25 → 1.0，即 **4 倍**）。
      如果保底落在我们不吃的地方，倍数就是 1 —— 那种情况下买了也白买。
    * **Planet Merchant / Planet Tycoon** —— 直接提高商店的 `planet_rate`，
      按"买入后的增量"折算。
    """
    params = DEFAULT_PARAMS
    if key in ("v_planet_merchant", "v_planet_tycoon"):
        return planet_rate_gain(state, key, params)

    if key != "v_telescope":
        return 1.0

    guaranteed_hand = guaranteed_planet_hand(state)
    if guaranteed_hand is None:
        return 1.0
    accepted = accepted_planet_hands(state, params)
    if not accepted or guaranteed_hand not in accepted:
        return 1.0

    wanted = len(accepted)
    shown, choose = CELESTIAL_PACK_SHAPES["normal"]
    base = celestial_expected_levels(
        params.celestial_pool_size, shown, choose, wanted
    )
    with_voucher = celestial_expected_levels(
        params.celestial_pool_size, shown, choose, wanted, guaranteed=True
    )
    if base <= 0:
        return 1.0
    return with_voucher / base


def synergy_adjustment(
    key: str,
    committed: str | None,
    params: TunableParams = DEFAULT_PARAMS,
) -> float:
    """这张小丑牌和我们专精牌型的契合度。

    视频强调"随机应变"——围绕已有的牌构筑。一张"+8 倍率（有对子时）"的
    小丑牌在专精对子时是核心引擎，在打同花时则完全是废牌，但静态估值表
    给它们一样的分数。这里把这个差别补上。
    """
    if committed is None:
        return 0.0
    requirement = JOKER_HAND_REQUIREMENT.get(key)
    if requirement is None:
        return 0.0
    return params.synergy_bonus if requirement == committed else -params.synergy_penalty


def planet_value(card: Card, state: GameState) -> float:
    """星球牌的购买价值。

    规则：**只买我们真在打的牌型的星球牌**。
    - 专精目标且已经打过 → 放心投（这是我们的成长主线）
    - 专精目标但还没打过 → 常见牌型价位，先攒着
    - 不是目标但已经打过 2 次以上 → 过渡期确实在用它，也投
    - 其它 → 不买（曾经这里一视同仁，钱花在了从来不打的牌型上）
    """
    hand = PLANET_HAND.get(card.key)
    if hand is None:
        return PLANET_SHOP_VALUE

    info = state.hands.get(hand)
    played = info.played if info is not None else 0
    target = committed_hand(state)

    if hand == target:
        # 专精目标：打过就可以放心投；还没打过先按常见价位攒
        return PLANET_SHOP_VALUE if played >= 1 else PLANET_COMMON_VALUE
    if played >= 2:
        # 过渡期确实在打这个牌型，投它不算浪费
        return PLANET_SHOP_VALUE
    if target is None and hand in COMMON_HANDS:
        return PLANET_COMMON_VALUE
    return PLANET_OFFTYPE_VALUE


# 对**当前这一手**完全没有帮助的"纯功能牌"：免费重摇、加手牌上限、生成卡牌之类。
#
# 开局阶段买它们等于白占一个牌位——实测机器人就这样死于底注 1：
# 手里是 Hack + Chaos the Clown，一分额外分都打不出来。
# 注意这个集合刻意不含经济牌（Rocket / Golden / To the Moon 会滚钱）、
# 也不含"改变牌型判定"的牌（Four Fingers / Smeared / Shortcut 能提高稳定性）。
PURE_UTILITY_JOKERS = frozenset({
    "j_chaos", "j_dna", "j_certificate", "j_luchador", "j_mr_bones",
    "j_sixth_sense", "j_superposition", "j_ring_master", "j_oops",
    "j_diet_cola", "j_drunkard", "j_juggler", "j_troubadour", "j_burglar",
    "j_marble", "j_hallucination", "j_cartomancer", "j_astronomer",
    "j_seance", "j_todo_list", "j_vagabond", "j_turtle_bean", "j_merry_andy",
    "j_invisible", "j_faceless", "j_credit_card", "j_trading",
    "j_hiker", "j_selzer", "j_chicot",
})

# 纯功能牌在成型之前的估值上限（低于购买阈值 4.5，也就是不买）
PURE_UTILITY_CAP = 3.5
PURE_UTILITY_FREE_ANTE = 5     # 到这个底注之后才允许考虑它们


def effective_joker_value(
    key: str,
    joker_count: int,
    ante: int = 1,
    params: TunableParams = DEFAULT_PARAMS,
) -> float:
    """按"当前有几个小丑牌位被占"和底注调整后的估值。

    空着的牌位不产生任何价值，所以早期"先把位置填满"比"只等最好的那张"
    更划算——**但只对真正能加分的牌成立**。纯功能牌享受不到这个加成，
    否则机器人会在底注 1 用它们把牌位占满，然后打不动盲注。

    从底注 multi_ante 起，纯加法牌已经追不上盲注的涨幅，必须转向乘区
    （视频："中后期倍乘丑能大幅拉高上限"）。
    """
    value = joker_value(key)

    if key in PURE_UTILITY_JOKERS and ante < params.pure_utility_free_ante:
        return min(value, params.pure_utility_cap)

    if joker_count < 2:
        value += params.empty_slot_bonus
    elif joker_count < params.few_slot_threshold:
        value += params.few_slot_bonus
    if ante >= params.multi_ante and is_multiplicative(key):
        value += params.multi_bonus
    return value


def voucher_value(
    card: Card, state: GameState, params: TunableParams = DEFAULT_PARAMS
) -> float:
    """优惠券的估值 = 基准值 × 星球牌供给倍数（封顶）。

    供给倍数只对"提高星球牌供给"的那几张非 1，见 `voucher_supply_multiplier`。
    封顶是为了不让 Planet Tycoon 的 ×8 把整个评分尺度顶爆——它只需要
    越过 `exception_value`（默认 7.5）就能动用利息本金，不需要一个天文数字。
    """
    base = VOUCHER_VALUE.get(card.key, DEFAULT_VOUCHER_VALUE)
    if base <= 0:
        return base
    multiplier = min(
        voucher_supply_multiplier(state, card.key), params.voucher_multiplier_cap
    )
    return base * max(1.0, multiplier)


def worth_breaking_reserve(card: Card, state: GameState) -> bool:
    """是否值得动用存款去买。

    排序用的估值带了"空位加成"，不能直接拿来判断要不要动存款——否则每个
    早期小丑牌都会轻松越过门槛，保留额就形同虚设。这里只看牌本身的成色。
    """
    if card.set == "JOKER":
        return joker_value(card.key) >= 8.0
    if card.set == "PLANET":
        return planet_value(card, state) >= PLANET_SHOP_VALUE
    if card.set in ("TAROT", "SPECTRAL"):
        return card.key in SAFE_CONSUMABLE_KEYS
    return False


def raw_shop_value(card: Card, state: GameState) -> float:
    """牌**本身**的价值，不含"空位加成"。

    判断"要不要动用存款"必须用这个，不能用排序用的估值——排序估值带了空位加成，
    会让每个早期小丑牌都轻松越过门槛，保留额就形同虚设。
    """
    if card.set == "JOKER":
        return joker_value(card.key)
    if card.set == "PLANET":
        return planet_value(card, state)
    if card.set in ("TAROT", "SPECTRAL"):
        if card.key in SAFE_CONSUMABLE_KEYS or card.key in TARGETED_CONSUMABLE_KEYS:
            return TAROT_SHOP_VALUE
        return UNUSED_TAROT_VALUE
    if card.set in ("DEFAULT", "ENHANCED"):
        return PLAYING_CARD_SHOP_VALUE
    return 3.0


def shop_card_value(
    card: Card,
    state: GameState,
    params: TunableParams = DEFAULT_PARAMS,
    *,
    surplus: bool = False,
    situation: "Situation | None" = None,
    archetype: str | None = None,
) -> float:
    """商店货架上某张牌的**排序**价值。

    * 小丑牌在静态估值和空位加成之外，加上"和专精牌型的契合度"——
      这是"随机应变、围绕已有牌构筑"的可计算版本。
    * `surplus=True`（经济充足）时，给"能提升倍率"的牌额外加权——
      规格："在经济充足的情况下尝试提升倍率"。钱攒着只吃 $5 利息，
      不如换成倍率，除非这张牌压根不提分。
    * `situation.urgent`（落后 / 本关过不了）时，星球牌再加一档——
      升牌型等级是唯一能跟上"每个底注涨 2.08 倍"的手段。
    * `archetype` 是已经承诺的**主流派**：属于它核心的小丑牌加权。

      这是阶段二的第一件事。原来抓牌是**逐张近视**的：按单张边际贡献排序，
      看不出"再抓一张人头牌，男爵就从 ×1.5 变 ×3"这种**方向性收益**。
      注意加权用的是静态表里那个"核心牌"名单，而不是边际贡献——后者在
      "锚点不含留手 K"时恰好会把男爵估成 0（详见 build.py 的模块文档）。
    """
    if card.set == "JOKER":
        base = effective_joker_value(
            card.key, state.joker_count, state.ante, params
        )
        base += synergy_adjustment(card.key, committed_hand(state, params), params)
        if surplus and raises_multiplier(card.key):
            base += params.surplus_mult_bonus
        if archetype and card.key in _archetype_core(archetype):
            base += params.build_card_bonus
        base -= filler_penalty(card.key, state.ante, params)
        return base
    if card.set == "PLANET":
        value = raw_shop_value(card, state)
        # 加权只给**可以吃的**星球牌（主牌型 + 已在投入的备选）。
        # 原来两条都是给全部星球牌的，后果：经济充足时非专精牌型的星球牌
        # 从 3.0 被顶到 4.5，正好越过门槛 → 等级分散在五个牌型上。
        accepted = accepted_planet_hands(state, params)
        is_target_planet = PLANET_HAND.get(card.key) in accepted
        if surplus and is_target_planet:
            value += params.surplus_mult_bonus
        if (situation is not None and situation.urgent and is_target_planet):
            value += params.urgency_planet_bonus
        return value
    return raw_shop_value(card, state)


def _is_consumable_slot_card(card: Card) -> bool:
    return card.set in ("PLANET", "TAROT", "SPECTRAL")


# ------------------------------------------------------------------ 盲注决策


@dataclass(slots=True)
class PlayConstraints:
    """Boss 盲注对本回合出牌的额外限制。

    不处理这些规则的后果不是"打得不优"，而是**发出非法操作**：
    The Psychic 下打 2 张牌会被游戏拒绝，机器人会反复重试直到卡死保护介入。

    另外，规则里说明"不满足限制的牌**仍然可以打出，只是不计分、也不触发效果**，
    等同于一次弃牌"——所以这些限制必须靠策略主动遵守，不能指望游戏报错兜底。
    """

    min_cards: int = 1
    max_cards: int = 5
    allowed_hands: frozenset[str] | None = None
    base_scale: float = 1.0
    # 是否倾向于"在得分接近最优时多打牌"（下面 best_play_choice 的隐藏弃牌技巧）
    prefer_more_cards: bool = True
    # The Arm：打出的牌型等级 -1，而且**降级发生在算分之前**，所以这一手要按
    # 低一级的筹码/倍率来算。build_solver 会据此把 base_fn 包一层。
    level_penalty: bool = False
    # 软偏好：尽量不打的牌型（目前只有 The Arm 的"会被降级的牌型"）。
    # 和 allowed_hands 不同，这个**不会**限制合法动作，只在"换个牌型也能过关"
    # 时才让位——所以不会出现"为省一级而打烂牌"的亏本情况。
    avoid_hands: frozenset[str] = frozenset()
    notes: tuple[str, ...] = ()
    # 完整的约束模型（bosses.BossEffect）。策略层要针对某条约束做特殊处理时
    # 从这里取，不必再去猜名称或效果文本。
    effect: "BossEffect | None" = None


# 名字匹配失败时按效果文本兜底。正常情况下用不到——mod 上报的 name 取自
# G.P_BLINDS[key].name，是游戏内部的英文原名，不随游戏语言变化；
# 而 effect 是本地化文本（中文环境下就是中文）。留着是为了游戏更新改名时不至于失效。
_TEXT_FALLBACK: tuple[tuple[tuple[str, ...], str], ...] = (
    (("must play 5 cards", "必须打出 5 张", "必须打出5张", "必須打出5張"), "The Psychic"),
    (("no repeat hand types", "不能重复", "不能重複"), "The Eye"),
    (("only one hand type", "只能打出一种", "只能打出一種"), "The Mouth"),
    (("sets money to $0", "金钱归零", "金錢歸零"), "The Ox"),
    (("base chips and mult", "基础筹码和倍率减半", "基礎籌碼和倍率減半"), "The Flint"),
    (("lose $1 per card", "每张牌损失", "每張牌損失"), "The Tooth"),
    (("discards 2 random", "随机弃掉 2 张", "隨機棄掉 2 張"), "The Hook"),
)


def _fallback_effect(text: str) -> "BossEffect | None":
    lowered = text.lower()
    for needles, name in _TEXT_FALLBACK:
        if any(needle.lower() in lowered for needle in needles):
            return boss_effect(name)
    return None


def play_constraints(state: GameState, config: Config) -> PlayConstraints:
    """根据当前盲注算出本回合的约束。

    约束模型在 `bosses.py`（28 条盲注的结构化定义）；这里只负责把它翻译成
    "求解器该怎么算分、哪些动作合法"。所有 Boss 都走同一条路径，
    不再靠逐条 if 拼——以前只认 7 条，精英盲注一条都不认。
    """
    blind = state.current_blind
    if blind.type != "BOSS":
        return PlayConstraints()

    effect = boss_effect(blind.name) or _fallback_effect(f"{blind.name} {blind.effect}")
    if effect is None:
        return PlayConstraints()

    constraints = PlayConstraints(effect=effect)
    notes: list[str] = []

    if effect.exact_cards is not None:
        constraints.min_cards = constraints.max_cards = effect.exact_cards
        notes.append(f"{effect.name}：必须打满 {effect.exact_cards} 张")

    if effect.halves_base_score:
        constraints.base_scale = 0.5
        notes.append(f"{effect.name}：牌型基础筹码/倍率减半")

    if effect.decreases_played_hand_level:
        constraints.level_penalty = True
        notes.append(f"{effect.name}：打出的牌型等级 -1（算分前生效）")

    if effect.takes_money_per_card:
        constraints.prefer_more_cards = False
        if state.money >= 10:
            constraints.max_cards = 3
        notes.append(f"{effect.name}：每张牌损失 $1，尽量少打")

    if effect.discards_after_play:
        # 每次出牌后随机弃牌：多打牌 = 留在手里的牌更少 = 损失更小
        constraints.prefer_more_cards = False
        notes.append(f"{effect.name}：出牌后随机弃 {effect.discards_after_play} 张手牌")

    if effect.debuff_lifted_by_selling_joker:
        notes.append(f"{effect.name}：全体卡牌被压制，卖一张小丑牌即可解除")

    if effect.disables_one_joker_per_hand:
        notes.append(f"{effect.name}：每手随机禁用一张小丑牌（已从算分中排除）")

    if effect.forces_one_card:
        # 强制选中一张牌是**合法动作**层面的约束，但 mod 的状态里没有这个字段
        # （整个 mod 源码搜不到 forced），所以机器人目前只能知道"有这回事"，
        # 无法预知是哪一张。记录下来，出牌后如果实际得分与预测不符，
        # 从日志里能立刻定位到它。
        notes.append(f"{effect.name}：强制选中一张牌，mod 未上报是哪张（无法预先建模）")

    if effect.shuffles_jokers:
        notes.append(f"{effect.name}：小丑牌顺序被打乱，需要重新确认摆放")

    table = state.hands
    if effect.locks_first_hand_type or effect.bans_repeat_hand_types:
        played = {name for name, info in table.items() if info.played_this_round > 0}
        if effect.locks_first_hand_type and played:
            # 本回合只能打同一种牌型
            constraints.allowed_hands = frozenset(played)
            notes.append(f"{effect.name}：只能继续打 {sorted(played)}")
        elif effect.bans_repeat_hand_types and played:
            remaining = frozenset(name for name in table if name not in played)
            if remaining:
                constraints.allowed_hands = remaining
                notes.append(f"{effect.name}：不能重复已打过的牌型")

    if effect.bans_most_played_hand and table:
        # 打出"本局打得最多的牌型"会把钱清零，尽量避开
        most = max(table.values(), key=lambda info: info.played)
        if most.played > 0:
            banned = frozenset({most.name})
            current = constraints.allowed_hands
            allowed = (current - banned) if current else frozenset(table) - banned
            if allowed:
                constraints.allowed_hands = allowed
                notes.append(f"{effect.name}：避开本局最常用的 {most.name}")

    if effect.decreases_played_hand_level and config.params.arm_level_guard:
        # The Arm：打出的牌型等级 -1（永久）。等级 1 的牌型不受影响，
        # 所以想避开的是"等级 > 1"的那些——那正是我们投资过的牌型。
        #
        # 注意这里只记成**软偏好**（avoid_hands），不直接禁牌：禁掉的后果可能
        # 比降一级严重得多。实测：专精到 6 级的对子若被禁，机器人只能打高牌
        # 15 分，而照打对子按 5 级算是 595 分——为了省一级亏 40 倍。
        # 真正该避开的时机是"换个牌型也能过关"，那要在出牌决策里比分数才知道。
        levelled = frozenset(
            name for name, info in table.items() if info.level > 1
        )
        if levelled:
            constraints.avoid_hands = levelled
            notes.append(f"{effect.name}：尽量避开会被降级的 {sorted(levelled)}")

    constraints.notes = tuple(notes)
    return constraints


def _cheapest_joker_to_sell(state: GameState) -> int:
    """卖哪张小丑牌损失最小——用真实算分引擎量出来。

    逐个"假设少了它"重算一次最优出牌得分，取掉分最少的那张。
    比静态估值表准：静态表看不出它和当前牌型、当前阵容的配合，
    也看不出它是不是已经被算分引擎忽略的牌（比如未建模的那些）。
    """
    jokers = state.jokers.cards
    if len(jokers) <= 1:
        return 0
    hand = state.hand.cards
    if not hand:
        return 0

    full = Solver.from_state(state)
    baseline = full.best(hand).total
    best_index, best_loss = 0, None
    for index in range(len(jokers)):
        without = replace(full, jokers=tuple(
            card for i, card in enumerate(jokers) if i != index
        ))
        loss = baseline - without.best(hand).total
        if best_loss is None or loss < best_loss:
            best_loss, best_index = loss, index
    return best_index


def choose_sell_action(state: GameState, config: Config) -> Sell | None:
    """回合内主动卖小丑牌。目前唯一用途：解除 Verdant Leaf 的全体压制。

    机制（`card.lua` 的卖牌处理里）：

        if self.ability.set == 'Joker' and G.GAME.blind.name == 'Verdant Leaf' then
            G.GAME.blind:disable()
        end

    卖掉**任意一张**小丑牌，盲注即被禁用，`debuff_card` 随之不再压制。
    不卖的话整个 Ante 8 精英回合都只能用牌型基础筹码打分——卡牌点数、
    强化、蜡封全都不算，几乎必然失败。

    判断"压制还在不在"直接看状态里的 `card.debuff`：卖过之后游戏会解除标记，
    所以这个条件天然只触发一次，不需要额外记"已经卖过"。
    """
    if not config.params.sell_to_lift_debuff:
        return None
    blind = state.current_blind
    if blind.type != "BOSS":
        return None
    effect = boss_effect(blind.name) or _fallback_effect(f"{blind.name} {blind.effect}")
    if effect is None or not effect.debuff_lifted_by_selling_joker:
        return None
    if not state.jokers.cards:
        return None
    if not any(card.debuff for card in state.hand.cards):
        return None                      # 压制已解除，或本来就没生效
    victim = _cheapest_joker_to_sell(state)
    note(f"卖小丑牌 #{victim} 以解除 {blind.name} 的全体压制",
         kind="sell", 卖=f"#{victim}",
         原因="卖任意一张小丑牌即解除盲注压制（card.lua:1616）",
         被压制的手牌=sum(1 for c in state.hand.cards if c.debuff))
    return Sell(joker=victim)


def choose_blind_action(state: GameState, config: Config) -> Action:
    """Boss 盲注不能跳，其余的按配置决定。"""
    blind = state.current_blind
    if blind.type == "BOSS":
        note(f"选中 {blind.name}（Boss 不能跳）", kind="blind",
             需求=round(state.current_blind.score), 类型="BOSS")
        return SelectBlind()
    if config.skip_blinds and (blind.tag_name or "") in VALUABLE_TAGS:
        note(f"跳过 {blind.name} 换标签「{blind.tag_name}」", kind="blind",
             标签=blind.tag_name)
        return SkipBlind()
    note(f"选中 {blind.name}", kind="blind",
         需求=round(state.current_blind.score), 类型=blind.type)
    return SelectBlind()


# ------------------------------------------------------------------ 出牌决策


def greedy_discard_order(
    solver: Solver,
    hand: Sequence[Card],
    *,
    min_cards: int = 1,
    max_cards: int = 5,
) -> list[int]:
    """按"最不值得留"的顺序排列手牌下标。

    每次挑出"去掉它之后最优出牌得分仍然最高"的那张——也就是对当前手牌
    贡献最小的那张。复杂度 O(n²)，一手 8 张只要 36 次求解。
    """
    remaining = list(range(len(hand)))
    cards = list(hand)
    order: list[int] = []
    while len(remaining) > 1:
        best_index: int | None = None
        best_value = -1.0
        for index in remaining:
            trial = [cards[j] for j in remaining if j != index]
            value = solver.best_score(trial, min_cards=min_cards, max_cards=max_cards)
            if value > best_value:
                best_value = value
                best_index = index
        if best_index is None:
            break
        order.append(best_index)
        remaining.remove(best_index)
    return order


def decision_rng(state: GameState) -> random.Random:
    """按局面确定性地播种。

    弃牌决策靠蒙特卡洛估计，天生带噪声。如果直接用一条全局随机流，
    同一个种子跑两次会得到不同的决策，评测结果就没法复现、也没法比较。
    这里用"局面指纹"当种子：只要局面一样，抽样序列就一样。
    """
    fingerprint = (
        state.seed, state.ante, state.round_num,
        state.round.hands_left, state.round.discards_left, state.round.chips,
        state.money, tuple(c.id for c in state.hand.cards),
    )
    return random.Random(repr(fingerprint))


def sample_draws(
    rng: random.Random,
    pool: Sequence[Card],
    count: int,
    samples: int,
) -> list[tuple[Card, ...]]:
    """预先生成一批补牌结果，供所有候选弃牌方案共用。

    这就是"公共随机数"技巧：候选 A 和候选 B 面对完全相同的补牌样本，
    于是两者期望值之差的方差远小于各自独立抽样——而我们要判断的正是
    "哪个方案更好"，所以这个方差才是关键的那个。
    """
    if count <= 0 or not pool:
        return [() for _ in range(samples)]
    count = min(count, len(pool))
    return [tuple(rng.sample(pool, count)) for _ in range(samples)]


def expected_after_discard(
    solver: Solver,
    hand: Sequence[Card],
    discard_indices: Sequence[int],
    draws: Sequence[tuple[Card, ...]],
    *,
    min_cards: int = 1,
    max_cards: int = 5,
) -> float:
    """用预先抽好的补牌样本，估计弃牌后最优出牌的期望得分。

    牌堆组成由状态给出（``cards`` 区域），因此这里是对真实补牌分布采样，
    不是凭空假设。
    """
    discard_set = set(discard_indices)
    keep = [c for i, c in enumerate(hand) if i not in discard_set]
    count = len(discard_set)
    if not draws or count == 0:
        return solver.best_score(keep, min_cards=min_cards, max_cards=max_cards)

    total = 0.0
    for drawn in draws:
        total += solver.best_score(
            keep + list(drawn[:count]), min_cards=min_cards, max_cards=max_cards
        )
    return total / len(draws)


def _level_downgraded_base(state: GameState):
    """The Arm 专用：返回一个"低一级"的牌型基础分函数。

    游戏里的结算顺序是（`functions_state_events.lua:614`）：

        if not G.GAME.blind:debuff_hand(...) then
            mult = mod_mult(G.GAME.hands[text].mult)   -- 已经是降级后的值

    The Arm 在 `debuff_hand` 里先执行 `level_up_hand(..., -1)` 且**不返回 true**，
    所以**这一手按降级后的等级算分**。机器人若直接用状态里读到的等级，
    在等级 > 1 时会固定高估一个等级。
    """

    def base(name: str) -> tuple[int, int]:
        chips, mult = state.hand_base(name)
        info = state.hands.get(name)
        # 游戏的条件是 `if G.GAME.hands[handname].level > 1 then` ——
        # **等级 1 的牌型不会被降级**，直接按原值算。无条件减一级会把
        # 同花（35/4）错算成（20/2）。
        if info is None or info.level <= 1:
            return chips, mult
        return hand_base_one_level_down(name, chips, mult)

    return base


def build_solver(state: GameState, config: Config) -> tuple[Solver, PlayConstraints]:
    """按当前 Boss 盲注的限制构造求解器。"""
    constraints = play_constraints(state, config)
    overrides: dict = {
        "base_scale": constraints.base_scale,
        "allowed_hands": constraints.allowed_hands,
    }
    if constraints.level_penalty:
        overrides["base_fn"] = _level_downgraded_base(state)
    solver = Solver.from_state(state, **overrides)
    # 限制可能把能打的牌型全禁掉（例如 The Eye 遇上烂手牌），那就放开牌型限制
    if constraints.allowed_hands is not None:
        legal = solver.enumerate(
            state.hand.cards,
            min_cards=constraints.min_cards,
            max_cards=constraints.max_cards,
        )
        if not legal:
            solver = replace(solver, allowed_hands=None)
    return solver, constraints


def choose_rearrange_action(state: GameState) -> Rearrange | None:
    """调整小丑牌槽位顺序能显著加分时，返回重排动作。

    这是**完全免费**的收益：只调换位置，不消耗任何资源，也不影响其他决策。
    """
    solver = Solver.from_state(state)
    if len(solver.jokers) < 2 or not state.hand.cards:
        return None
    order = solver.best_joker_order(state.hand.cards)
    if not order:
        return None
    before = solver.best(state.hand.cards)
    after = solver.reordered(order).best(state.hand.cards)
    note("重排小丑牌槽位（免费加分：加法在左、乘区在右）", kind="rearrange",
         重排前=round(before.total) if before else 0,
         重排后=round(after.total) if after else 0,
         顺序=list(order))
    return Rearrange(tuple(order))


def kept_card_value(hand: Sequence[Card], played_indices: Sequence[int]) -> float:
    """估算"没被打出去的那些牌"留在手里的价值。

    用来在**得分相同**的候选打法之间挑垫牌：同样是打 5 张，应该优先把
    最没用的牌打出去，别把正在凑同花/对子的料当垫牌丢掉。
    （文档原话："we are not discarding Clubs as junk cards because we are
    setting the hand for playing a flush next time"）
    """
    played = set(played_indices)
    suit_counts: dict[str, int] = {}
    rank_counts: dict[str, int] = {}
    for card in hand:
        if card.is_stone:
            continue
        if card.suit:
            suit_counts[card.suit] = suit_counts.get(card.suit, 0) + 1
        if card.rank:
            rank_counts[card.rank] = rank_counts.get(card.rank, 0) + 1

    value = 0.0
    for index, card in enumerate(hand):
        if index in played or card.is_stone:
            continue
        # 同花色的人越多，这张越值得留（做同花）
        value += 0.5 * suit_counts.get(card.suit or "", 0)
        # 已经有同点数的牌，留着凑对子/三条
        if card.rank and rank_counts.get(card.rank, 0) > 1:
            value += 2.0
        # 钢牌/金色强化留在手里本来就有收益
        if card.enhancement in ("STEEL", "GOLD"):
            value += 3.0
    return value


def best_play_choice(
    solver: Solver,
    hand: Sequence[Card],
    constraints: PlayConstraints,
    *,
    plays: list[Candidate] | None = None,
    prefer_hand: str | None = None,
    prefer_tolerance: float = 0.12,
    tolerance: float = 0.02,
) -> Candidate | None:
    """选出这一手要打哪几张牌。

    两条偏好，按优先级：

    1. **专精牌型**（`prefer_hand`）：视频的核心口诀是"尽早转小牌型"。只在
       得分差距不超过 12% 时才切换，避免为了长期投资丢掉当前盲注。
       调用方只在"这一手足够过关"时才传这个参数。
    2. **隐藏弃牌**：打出的牌数 = 补牌数，所以多打一张垫牌等于多免费循环
       一张牌。垫牌本来就不计分，所以在同牌型、得分接近最优的候选里，
       优先选打出张数更多的那个。

    两条都只在**同一牌型**内比较张数，避免为了多打牌换成更差的牌型
    （那会影响 The Eye / The Mouth 这类限制，也会让牌型等级投资白费）。
    """
    if plays is None:
        plays = solver.enumerate(
            hand, min_cards=constraints.min_cards, max_cards=constraints.max_cards
        )
    if not plays:
        return None

    best = plays[0]

    # 1) 专精牌型优先（长期投资）
    if prefer_hand is not None and prefer_hand != best.hand_name:
        floor = best.total * (1.0 - prefer_tolerance)
        preferred = [
            play for play in plays
            if play.hand_name == prefer_hand and play.total >= floor
        ]
        if preferred:
            return max(
                preferred,
                key=lambda play: (len(play.indices), kept_card_value(hand, play.indices)),
            )

    if not constraints.prefer_more_cards:
        return best

    threshold = best.total * (1.0 - tolerance)
    same_type = [
        play for play in plays
        if play.hand_name == best.hand_name and play.total >= threshold
    ]
    if len(same_type) <= 1:
        return best

    # 张数优先，其次把最没用的牌当垫牌
    return max(
        same_type,
        key=lambda play: (len(play.indices), kept_card_value(hand, play.indices)),
    )


def phase_weights(
    solver: Solver,
    state: GameState,
    params: TunableParams = DEFAULT_PARAMS,
) -> PlanWeights:
    """把"出牌策略参数"算出来。

    规格：

    > 前期因为低难度牌型倍率较低所以才会更倾向于打出高难度牌型来快速通关；
    > 在过渡期需拟定最终 build 打牌方向，通过各种手段提高牌型的期望值；
    > **当牌型的期望值超出一般高难度牌型时可切换出牌策略**。

    所以切换依据**不是底注**（按底注切是拍脑袋的），而是
    "专精牌型的期望值有没有追平一般高难度牌型"——见
    `planning.strategy_switch`。星球牌和小丑牌把专精牌型喂起来之后，
    策略会自动从"追大牌型"切到"稳定做小牌型"，不需要人工指定第几底注。
    """
    share = strategy_switch(
        solver,
        committed_hand(state, params),
        params.big_hand_reference,
        switch_point=params.switch_point,
        sharpness=params.switch_sharpness,
    )
    gamma = params.gamma_early + (params.gamma_late - params.gamma_early) * share
    return PlanWeights(gamma=gamma, discard_bonus=params.discard_bonus)


def evaluate_discard_plans(
    state: GameState,
    solver: Solver,
    constraints: PlayConstraints,
    current: Candidate,
    config: Config,
) -> list:
    """为当前局面生成"去凑哪个牌型"的候选计划。

    `current_score` 传的是"现在就打"那一手的实际得分（含小丑牌效果），
    评分公式用它当分母："去凑的牌型比现在强多少倍"。
    每个计划的分子则是**造出凑成后的牌、交给真实算分引擎**算出的得分。
    """
    round_info = state.round
    pile = state.draw_pile.cards
    room = min(MAX_DISCARD, round_info.discards_left, len(state.hand.cards) - 1)
    if room <= 0 or not pile:
        return []
    return plan_targets(
        solver, state.hand.cards, pile,
        max_discard=room,
        weights=phase_weights(solver, state, config.params),
        allowed_hands=constraints.allowed_hands,
        current_score=current.total,
    )


def plan_potential(
    plans: Sequence["DiscardPlan"],
    solver: Solver,
    hand: Sequence[Card],
    fallback: float,
) -> float:
    """这手牌在剩余弃牌数内真正能打到的水平（最优凑牌计划的期望分）。

    ```text
    potential = max over plans [ P × 凑成分 + (1 − P) × 兜底分 ]
    ```

    **为什么要它**：危急度原来用 `score_capability`（"现在这 8 张牌能打多少"）来
    估。开局那 8 张是随机的，所以它系统性偏低、crisis 偏高——实测底注 1 第一手
    就判成危急 1.7x，于是门槛归零、开始赌 9% 的 Straight Flush，越赌越危急
    （`危急=1.1x → 1.9x`），最后差 10 分输掉大盲。
    用潜力就对了：那是"这手牌**加上弃牌**之后能到哪"，而不是"这一眼看到哪"。

    `plans` 里必然含"不用弃牌"的那条（如果当前已经是成牌），所以潜力不会
    低于 `fallback`——这也是它和 `score_capability` 的关系：潜力 ≥ 现状。
    """
    best = fallback
    for plan in plans:
        value = plan_expected_score(
            plan, plan_miss_score(solver, hand, plan)
        )
        if value > best:
            best = value
    return best


def choose_plan_discard(
    state: GameState,
    solver: Solver,
    constraints: PlayConstraints,
    current: Candidate,
    config: Config,
) -> "Play | Discard | None":
    """决定"继续弃牌凑牌型"还是"打出手牌过牌"。

    判据：**弃牌后的期望得分要超过现在这一手多少倍，才值得弃牌。**

        弃牌  ⟺  E_τ*  ≥  S_cur × (1 + 门槛)

    只比较规划评分是不够的——"打一手高牌"的评分不差，但它可能根本过不了关；
    所以用绝对期望得分 `E = P × 凑成分 + (1−P) × 当前分` 来拍板。

    门槛就是 `discard_aversion`。它是一条可调参数：调大更保守（多打牌、
    少赌凑牌），调小更激进。

    唯一会放宽门槛的是**经济规则**：回合结束每剩一手 +$1、弃牌不给钱，
    所以出牌机会更值钱。当弃牌数已经追平或超过出牌数时（手里攒着一堆
    不值钱的弃牌），门槛降到 0——"不亏就弃"。但**不允许亏着弃**。

    返回 None 表示不适用（没有牌堆 / 没有弃牌机会），交给调用方走别的分支。
    """
    plans = evaluate_discard_plans(state, solver, constraints, current, config)
    if not plans:
        note("没有可凑的牌型（没牌堆 / 没弃牌机会）→ 打当前最优",
             kind="play", 当前得分=round(current.total), 还差=state.score_remaining)
        return None

    best_plan = plans[0]
    if not best_plan.discard:
        # 这个计划不用弃牌（已经是成牌）→ 直接打**当前最优**那一手。
        #
        # 报的数必须是 `current`（真正会打出去的那一手）的数。原来报的是
        # `best_plan.completed_score`（**凑牌计划**的牌型分数），两者可以完全
        # 不同：实测有一局日志写"已成牌（High Card）预估得分=15"，而实际打出去
        # 那一手得了 280。那种日志会把人带偏——我为此追了一个并不存在的
        # "19 倍算分误差"。
        note(f"已成牌（{current.hand_name}）→ 直接打",
             kind="play", 牌型=current.hand_name,
             预估得分=round(current.total),
             还差=state.score_remaining,
             够过关=current.total >= state.score_remaining)
        return Play(current.indices)

    params = config.params
    round_info = state.round

    expected = plan_expected_score(
        best_plan,
        plan_miss_score(solver, state.hand.cards, best_plan),
    )

    if params.crisis_uses_potential:
        # 危急度用**潜力**估，而不是"现在这 8 张牌能打多少"。
        #
        # `plans` 已经算好了，所以这里几乎不要钱。潜力 = 最优凑牌计划的期望分，
        # 也就是"这手牌在剩余弃牌数内真正能打到的水平"。
        # 原口径（`score_capability`）只看当前的 8 张，开局随机手牌会让它
        # 系统性偏低 → crisis 偏高 → 过早进入赌博模式 → 越赌越危急。
        potential = plan_potential(plans, solver, state.hand.cards, current.total)
        crisis = state.score_remaining / max(
            1.0, potential * max(1, round_info.hands_left)
        )
    else:
        crisis = assess(state, config).crisis

    # 经济规则：保住"剩余出牌数 > 剩余弃牌数"。弃牌数追平或超过出牌数时，
    # 说明该先把不值钱的弃牌花掉，于是把门槛降到 0（仍要求不亏）。
    must_spend_discards = (
        params.keep_hands_above_discards
        and 0 < round_info.hands_left <= round_info.discards_left
    )
    margin = 0.0 if must_spend_discards else params.discard_aversion

    # 危急：就算每手都打最优也过不了这一关。这时"没凑成"等于输，
    # 所以不该再把"没凑成还能打多少分"算进期望里——那点分数救不了这一关。
    # 只看 P × 凑成分，门槛也随之归零（稳扎稳打已经没有意义）。
    if crisis > 1.0:
        expected = best_plan.probability * best_plan.completed_score
        margin = 0.0

    numbers = dict(
        凑的牌型=best_plan.hand_name,
        概率=f"{best_plan.probability * 100:.0f}%",
        凑成分=round(best_plan.completed_score),
        现在打=round(current.total),
        期望=round(expected),
        门槛=f"+{margin * 100:.0f}%",
    )
    if crisis > 1.0:
        numbers["危急"] = f"{crisis:.1f}x"
    elif must_spend_discards:
        numbers["规则"] = "弃牌数已追平出牌数"

    if expected >= current.total * (1.0 + margin):
        note(f"弃 {len(best_plan.discard)} 张去凑 {best_plan.hand_name}："
             f"期望 {expected:.0f} ≥ 现在 {current.total:.0f} × (1+{margin:.2f})",
             kind="discard", **numbers)
        return Discard(best_plan.discard)

    note(f"直接打：凑 {best_plan.hand_name} 的期望 {expected:.0f}，"
         f"达不到 {current.total * (1.0 + margin):.0f}",
         kind="play", **numbers)
    return Play(current.indices)


def choose_play_action(
    state: GameState,
    config: Config,
    rng: random.Random | None = None,
) -> Play | Discard:
    """决定这一手是打还是弃，以及用哪几张。

    rng 只用于非关键的随机性；弃牌估计用的是按局面播种的确定性随机流，
    以保证同一种子可复现。
    """
    solver, constraints = build_solver(state, config)
    hand = state.hand.cards
    min_cards = constraints.min_cards
    max_cards = constraints.max_cards

    plays = solver.enumerate(hand, min_cards=min_cards, max_cards=max_cards)
    if not plays:
        raise RuntimeError("手牌为空，无法决策——这通常意味着状态已经过期")

    needed = state.score_remaining
    round_info = state.round

    # 只有"这一手就能过关"时才做牌型投资：否则保命优先，打最高分。
    # 这也是视频里"前期用大中牌型过渡"的含义——能过就行，别硬凑。
    can_clear = plays[0].total >= needed
    prefer_hand = committed_hand(state, config.params) if can_clear else None

    # The Arm：打出的牌型等级会 -1。**只有在"换个牌型也能过关"时才值得避开**，
    # 否则为了省一级去打一手烂牌，亏得更多（实测：对子 595 分 → 高牌 15 分）。
    # 所以这里只在备选牌型确实也能过关时，才把它从候选里剔掉。
    if can_clear and constraints.avoid_hands:
        alternatives = [p for p in plays if p.hand_name not in constraints.avoid_hands]
        if alternatives and max(p.total for p in alternatives) >= needed:
            plays = alternatives
            if prefer_hand in constraints.avoid_hands:
                prefer_hand = None

    best = best_play_choice(
        solver, hand, constraints, plays=plays, prefer_hand=prefer_hand,
        prefer_tolerance=config.params.prefer_hand_tolerance,
    )
    if best is None:
        raise RuntimeError("手牌为空，无法决策——这通常意味着状态已经过期")

    # 这一手就能过：直接打，不要浪费弃牌
    if best.total >= needed:
        note(f"这一手就能过关（{best.hand_name} {best.total:.0f} ≥ 需要 {needed}）",
             kind="play", 牌型=best.hand_name, 预估得分=round(best.total),
             需要=round(needed), 剩余手数=round_info.hands_left)
        return Play(best.indices)

    # 没有弃牌机会 / 最后一手 / 关闭了弃牌：只能打
    if (
        not config.use_discards
        or round_info.discards_left <= 0
        or round_info.hands_left <= 1
    ):
        if not config.use_discards:
            why = "只能打：弃牌功能已关闭"
        elif round_info.discards_left <= 0:
            why = "只能打：没有弃牌机会了"
        else:
            why = "只能打：这是最后一手"
        note(f"{why}（{best.hand_name} {best.total:.0f}）", kind="play",
             牌型=best.hand_name, 预估得分=round(best.total), 需要=round(needed))
        return Play(best.indices)

    # 规格里的评分函数路径（精确概率，无采样噪声）
    if config.discard_strategy == "plan":
        decided = choose_plan_discard(state, solver, constraints, best, config)
        if decided is not None:
            return decided

    # ---- 旧路径：蒙特卡洛估计补牌后的期望得分（保留下来便于对比）
    room = min(
        config.discard_max_cards,
        round_info.discards_left,
        len(hand) - 1,
    )
    if room <= 0:
        return Play(best.indices)

    draw_pile = state.draw_pile.cards
    if not draw_pile:
        return Play(best.indices)

    order = greedy_discard_order(solver, hand, min_cards=min_cards, max_cards=max_cards)
    local_rng = decision_rng(state) if rng is None else rng
    draws = sample_draws(local_rng, draw_pile, room, config.discard_samples)

    best_discard: tuple[int, ...] | None = None
    best_expected = best.total
    for size in range(1, room + 1):
        candidate = tuple(sorted(order[:size]))
        expected = expected_after_discard(
            solver, hand, candidate, draws,
            min_cards=min_cards, max_cards=max_cards,
        )
        if expected > best_expected:
            best_expected = expected
            best_discard = candidate

    if best_discard is None:
        return Play(best.indices)
    if best_expected < best.total * config.discard_min_gain:
        return Play(best.indices)
    return Discard(best_discard)


# ---------------------------------------------------------------- 消耗牌决策


def archetype_target_order(
    state: GameState,
    archetype: str | None,
    hand: Sequence[Card],
    params: TunableParams = DEFAULT_PARAMS,
) -> tuple[int, ...]:
    """按**主流派**排出"最该改造哪几张牌"的优先顺序（手牌下标）。

    这是阶段二的第二件事：塔罗目标原来一律取"这一手真正算分的牌"，但那和
    流派无关——人头流该优先给 K/Q 加蜡封（男爵数的是**手里**的 K），钢铁流
    该优先改造**还不是 STEEL** 的牌。

    ```text
    人头流  K > Q > J > 其它          （男爵每张 K ×1.5、射月每张 Q +13 倍率）
    钢铁流  优先还不是 STEEL 的牌      （目标是造出更多 STEEL）
    玻璃流  优先还不是 GLASS 的牌
    其它    （返回空）调用方沿用"算分牌优先"
    """
    del params
    if not archetype or not hand:
        return ()
    from .build import ARCHETYPES

    spec = ARCHETYPES.get(archetype)
    if spec is None:
        return ()
    order: list[int] = []
    if archetype == "face":
        # 人头流：先把人头牌排好，K 最前（男爵是按 K 计的）
        for wanted in ("K", "Q", "J"):
            order += [i for i, c in enumerate(hand)
                      if c.rank == wanted and not c.is_stone]
    elif spec.feature in ("STEEL", "GLASS"):
        # 钢铁/玻璃流：改造的意义在于"多造出一张"，所以优先还没变的
        order += [i for i, c in enumerate(hand)
                  if c.enhancement != spec.feature and not c.is_stone]
    return tuple(order)


def choose_consumable_action(
    state: GameState,
    config: Config,
    exclude: Collection[str] = (),
    *,
    archetype: str | None = None,
) -> UseConsumable | None:
    """挑一张该用的消耗牌。

    优先级：星球牌 > 幻灵牌（收益明确的）> 无目标塔罗牌 > 有目标塔罗牌/幻灵牌。

    视频把小丑包排在补充包第一、幻灵包第二，所以幻灵牌不能再一律不用：
    Black Hole（全牌型 +1 级）和 The Soul（传奇丑）是无条件收益，
    加蜡封的几张给算分牌用也有明确收益。其余幻灵牌仍然不碰——不少是
    破坏性的（整副牌变同点数但手牌上限 -1、摧毁 5 张牌换钱）。

    ``exclude`` 里是不该再碰的牌（key）——主循环会把被游戏拒绝过的牌放进去，
    否则每次都会重新挑中同一张被拒的牌。
    """
    consumables = [
        (index, card) for index, card in enumerate(state.consumables.cards)
        if card.key not in exclude
    ]
    if not consumables:
        return None

    if config.use_planet_cards:
        # 只吃**可吃牌型**的星球牌（主 + 已在投入的备选）。
        # 原来是无条件用掉任何星球牌——从 The High Priestess 之类拿到的
        # 杂牌星球会顺手用掉，等级就分散了。
        accepted = accepted_planet_hands(state, config.params)
        for index, card in consumables:
            if card.key in PLANET_KEYS and PLANET_HAND.get(card.key) in accepted:
                hand_type = PLANET_HAND[card.key]
                note(f"用星球牌 {card.label or card.key}（升级 {hand_type}）",
                     kind="use", 牌型=hand_type,
                     当前等级=state.hand_level(hand_type))
                return UseConsumable(index)

    # 幻灵牌：无条件收益的那几张直接用
    for index, card in consumables:
        if card.key in SAFE_SPECTRAL_KEYS:
            note(f"用幻灵牌 {card.label or card.key}（无条件收益）", kind="use")
            return UseConsumable(index)

    # Wraith 把钱清零，只有钱不多、而且有小丑牌位时才划算
    for index, card in consumables:
        if (card.key == WRAITH_KEY
                and state.money <= WRAITH_MAX_MONEY
                and state.joker_slots_free > 0):
            note("用 Wraith 换传奇小丑（钱已清空到可接受范围）", kind="use",
                 余额=state.money, 空位=state.joker_slots_free)
            return UseConsumable(index)

    for index, card in consumables:
        if card.key in SAFE_CONSUMABLE_KEYS:
            note(f"用塔罗牌 {card.label or card.key}（无目标、收益确定）", kind="use")
            return UseConsumable(index)

    for index, card in consumables:
        if card.key in TARGETED_CONSUMABLE_KEYS:
            best = Solver.from_state(state).best(state.hand.cards)
            if best is None:
                continue
            # 流派偏好的牌排在最前面，算分牌跟在后面补位（见 archetype_target_order）
            preferred = _merge_targets(
                archetype_target_order(state, archetype, state.hand.cards, config.params),
                best.indices,
            )
            targets = consumable_targets(state, card.key, preferred,
                                         hand=state.hand.cards)
            if not targets:
                continue
            note(f"用塔罗牌 {card.label or card.key} 改造算分牌", kind="use",
                 目标=[state.hand.cards[i].label for i in targets if i < len(state.hand.cards)])
            return UseConsumable(index, targets)

    # 加蜡封的幻灵牌：流派偏好的牌优先，其次才是这一手真正算分的牌
    for index, card in consumables:
        if card.key in TARGETED_SPECTRAL_KEYS:
            best = Solver.from_state(state).best(state.hand.cards)
            if best is None or not best.score.cards_scoring:
                continue
            scoring = [i for i in best.indices if i in best.score.cards_scoring]
            preferred = _merge_targets(
                archetype_target_order(state, archetype, state.hand.cards, config.params),
                scoring,
            )
            targets = consumable_targets(state, card.key, preferred,
                                         hand=state.hand.cards)
            if not targets:
                continue
            note(f"用幻灵牌 {card.label or card.key} 给算分牌加蜡封", kind="use",
                 目标=[state.hand.cards[i].label for i in targets if i < len(state.hand.cards)])
            return UseConsumable(index, targets)

    return None


# ------------------------------------------------------------------ 商店决策


# ------------------------------------------------------ 全局局面判断
#
# 以前所有决策都是**单步局部最优**：买牌比较"换了这张当前阵容能多打多少分"，
# 出牌比较"这一手的期望"。没有任何地方问过两个全局问题：
#
#   * **时间维度**：按这个速度，8 个底注之后到得了吗？
#   * **危急度**：这一关我是不是已经过不了了？
#
# 后果很具体：实测 10 局里有 7 局是**带着 $47~84 的余额死掉的**，而当时面对的
# 是 11000~35000 的需求——那些钱连零头都算不上，却一直没花出去（留着吃 $5 利息）。
# 而且越接近失败，`effective_reserve` 还把保留额抬得越高、重摇预算砍得越少。

# 每个底注的过关基准分（game.lua 的 get_blind_amount，白注）。
# 同一底注内：小盲 = N，大盲 = 1.5N，Boss = 2N。
ANTE_REQUIREMENT: dict[int, float] = {
    1: 300, 2: 800, 3: 2000, 4: 5000,
    5: 11000, 6: 20000, 7: 35000, 8: 50000,
}
# 底注之间需求的平均涨幅（A1→A8 是 50000/300，开 7 次方）
ANTE_GROWTH = (50000 / 300) ** (1 / 7)      # ≈ 2.08
FINAL_ANTE = 8


@dataclass(frozen=True, slots=True)
class Situation:
    """当前局面的全局判断。

    `score_now` 是"我一手大概能打多少分"的估计：出牌阶段用真实手牌求最优，
    商店阶段（没有手牌）用代表性手牌 × 当前阵容算。两种口径都来自
    已经过真机验证的算分引擎。
    """

    ante: int
    score_now: float
    required_now: float
    hands_left: int
    money: int

    @property
    def known(self) -> bool:
        """得分能力是否估得出来。

        估不出来（既没有手牌、又没有牌组信息）时不要当成"危急"——
        那只是信息缺失，不是局面糟糕。否则所有简化测试状态都会被判成紧急。
        """
        return self.score_now > 0

    @property
    def crisis(self) -> float:
        """>1 表示**就算每手都打最优也过不了这一关**。

        这是"对失败敏感"的判据：以前只有"这一手能过关就打"这种局部规则，
        没有整体判断。
        """
        capacity = self.score_now * max(1, self.hands_left)
        if capacity <= 0:
            return 0.0
        return self.required_now / capacity

    @property
    def pace(self) -> float:
        """>=1 表示时间维度上没有落后。

        每过一个底注，得分能力要涨 ANTE_GROWTH 倍，所以"现在应该达到的水平"
        是 `终局需求 ÷ 增长率的剩余次方`；`score_now` 比它低就是落后。
        """
        if not self.known:
            return 99.0
        runway = max(0, FINAL_ANTE - self.ante)
        target = ANTE_REQUIREMENT[FINAL_ANTE] / (ANTE_GROWTH ** runway)
        if target <= 0:
            return 99.0
        return self.score_now / target

    @property
    def urgent(self) -> bool:
        """落后、或者这一关过不了——两种情况都该解除保守。"""
        return self.known and (self.pace < 1.0 or self.crisis > 1.0)

    def describe(self) -> str:  # pragma: no cover - 仅用于日志
        return (f"A{self.ante} 得分力 {self.score_now:.0f} "
                f"需求 {self.required_now:.0f} 速度 {self.pace:.2f}x "
                f"危急 {self.crisis:.2f}x" + ("（紧急）" if self.urgent else ""))


def boss_requirement(ante: int) -> float:
    """该底注 Boss 盲注的过关分（同一底注内 Boss 是基准的 2 倍）。"""
    base = ANTE_REQUIREMENT.get(max(1, min(FINAL_ANTE, ante)), ANTE_REQUIREMENT[FINAL_ANTE])
    return base * 2.0


def score_capability(state: GameState, config: Config) -> float:
    """当前阵容"一手能打多少分"的估计。

    * 出牌阶段：直接用真实手牌求最优出牌得分。
    * 商店阶段：没有手牌，用代表性手牌 × 当前阵容算（和买牌估值同一套机制）。
    """
    hand = state.hand.cards
    if hand:
        solver = Solver.from_state(state)
        order = solver.best_joker_order(hand) if len(solver.jokers) >= 2 else None
        if order:
            solver = solver.reordered(order)
        best = solver.best(hand)
        if best is not None:
            return best.total

    scenarios, hand_name = valuation_scenario(state, config)
    if not scenarios:
        return 0.0
    return max(
        lineup_score(state, state.jokers.cards, played, held, hand_name)
        for played, held in scenarios
    )


def assess(state: GameState, config: Config) -> Situation:
    """算出当前局面的全局判断。"""
    if state.hand.cards:
        required = state.score_remaining
    else:
        # 商店里下一个盲注还没定，按本底注 Boss 的量级估（保守）
        required = boss_requirement(state.ante)
    hands_left = state.round.hands_left
    if hands_left <= 0:
        hands_left = config.hands_per_round
    return Situation(
        ante=state.ante,
        score_now=score_capability(state, config),
        required_now=float(required),
        hands_left=int(hands_left),
        money=state.money,
    )


def effective_reserve(
    state: GameState, config: Config, situation: Situation | None = None
) -> int:
    """本回合商店里至少要留多少钱。

    利息是 `min(钱 // 5, 5)`，也就是 $25 吃满（每回合 +$5）。但一味攒钱会让
    机器人没钱买牌、死于底注 2——所以只有在**已经有牌成型**之后才启用这个下限。
    视频的说法是"中后期重置后尽量留 25 块吃利息"。

    **但紧急时这条必须让位**：实测 7/10 局是带着 $47~84 的余额死掉的，
    而当时面对 11000~35000 的需求——那些钱留着吃 $5 利息毫无意义。
    所以落后或本关过不了时，保留额直接降回底线。
    """
    params = config.params
    if situation is not None and situation.urgent:
        return params.money_reserve
    if state.ante < params.interest_floor_ante:
        return params.money_reserve
    if state.joker_count < params.interest_floor_min_jokers:
        return params.money_reserve
    return max(params.money_reserve, params.interest_floor)


def max_rerolls_for(
    state: GameState, config: Config, situation: Situation | None = None
) -> int:
    """本轮商店允许的重摇次数。

    平时按底注递减（视频：底注1 $10-15、底注2 $8-10，之后只在找关键牌时摇）。
    **紧急时改成"花得起就一直摇"**：钱在那个时候的唯一价值就是换到一张能救命的牌。
    """
    params = config.params
    base = config.max_rerolls_by_ante.get(state.ante, 1)
    base = max(1, base)
    if situation is None or not situation.urgent:
        return base
    cost = max(1, state.round.reroll_cost)
    affordable = max(0, (state.money - params.money_reserve) // cost)
    return max(base, int(affordable))


# ------------------------------------------------ 阵容估值（真实得分口径）
#
# 商店里判断"这张小丑牌值不值得"以前用的是离线静态表（JOKER_VALUE）加几个
# 同量级的加减项。实测证明这条路会出错：
#
#   * 卖 Smiley Face（+5 倍率/人头牌）换 Sly Joker（+50 筹码）：
#     Sly 因为"要求对子"拿到 +1.5 契合度 → 6.0，反超 Smiley 的 5.5。
#     这直接违反规格的"倍率丑 > 筹码丑"。
#   * 买 Smeared / Four Fingers / Shortcut（静态 8.0~8.5）：
#     它们改变牌型判定，而算分引擎不实现 → 真实贡献是 0，却因为静态分高
#     永远当不上"最差的那张" → 牌位被永久占死。
#
# 现在改成**用已经过真机验证的算分引擎量真实得分贡献**：从整副牌里构造一个
# "专精牌型的代表性手牌"当打分场景（商店里 state.hand 是空的，但 mod 把整副牌
# 报在 cards 区），逐张比较阵容变化前后的得分。


def valuation_scenario(
    state: GameState, config: Config
) -> tuple[list[tuple[list[Card], list[Card]]], str]:
    """商店估值用的打分场景列表 + 牌型名；构造不出来时返回 ([], "")。

    用**多个锚点**（见 planning.representative_hands）：单个锚点会系统性偏袒
    某一类小丑牌，例如只用 A 当对子锚点时，人头牌系小丑全都显示 0 贡献。
    """
    deck = state.draw_pile.cards
    if not deck:
        return [], ""
    hand_name = committed_hand(state, config.params) or "Pair"
    return representative_hands(deck, hand_name), hand_name


@dataclass(slots=True, frozen=True)
class ValuationView:
    """一个估值视角：在某个牌型下、用这组场景打分，占多少权重。

    软筛（见 `valuation_plan`）就是"多个视角按权重混合"——不动算分引擎，
    所以引擎的保真度（22/22 对齐真机）完全不受影响。
    """

    scenarios: tuple[tuple[list[Card], list[Card]], ...]
    hand_name: str
    weight: float


def _soft_candidates(
    state: GameState,
    committed: str,
    params: TunableParams = DEFAULT_PARAMS,
) -> list:
    """过渡期里"另一种可能的流派"候选。

    两条入选途径，满足其一即可：

    * `level >= soft_level_floor` —— 投过星球牌
    * `played >= soft_played_floor` —— **实际打过**几次

    第二条是必须的：`lock_planet_type = True` 把所有星球牌都砸在专精牌型上，
    所以其它牌型永远升不了级。只按等级挑候选的话，软筛永远不会生效
    （第一版就是这么写的，实测 31 次买入 0 次触发）。
    """
    out = [
        info
        for info in state.hands.values()
        if info.name != committed
        and (
            info.level >= params.soft_level_floor
            or info.played >= params.soft_played_floor
        )
    ]
    out.sort(key=lambda info: (info.level, info.played), reverse=True)
    return out[: max(0, params.soft_hand_types)]


def hand_softness(state: GameState, params: TunableParams = DEFAULT_PARAMS) -> float:
    """软筛强度：1 = 完全过渡期，0 = 已定型（用硬筛）。

    依据是**等级领先**：

        lead = 最高等级 − 第二高等级
        soft = 1 − clamp(lead / soft_gate_lead, 0, 1)

    没有候选牌型（没有任何"另一种可能的流派"）时直接返回 0——这里显式判定，
    而不是靠调用方找不到候选来兜底，否则日志里会出现一个"软筛=1.00"却其实
    什么都没发生的假信号。
    """
    table = state.hands
    if not table:
        return 0.0
    committed = committed_hand(state, params)
    if not committed:
        return 0.0
    if not _soft_candidates(state, committed, params):
        return 0.0

    levels = sorted((info.level for info in table.values()), reverse=True)
    lead = levels[0] - (levels[1] if len(levels) > 1 else 1)
    span = max(1, params.soft_gate_lead)
    return 1.0 - min(1.0, max(0.0, lead / span))


def valuation_plan(state: GameState, config: Config) -> list[ValuationView]:
    """小丑牌估值用的视角列表（按权重混合多个牌型）。

    过渡期（`hand_softness > 0`）把"已经投入过的"其它牌型也纳入估值，
    权重按等级；专精牌型额外拿到 `1 − soft` 的独占份额。于是：

    * `soft = 0`（已定型 / 没有任何牌型升过级）→ 只剩专精牌型 = 原来的硬筛
    * `soft = 1`（完全过渡期）→ 纯按等级分配

    **只用于小丑牌的买/换/卖估值**，不用于星球牌和天体包的估值——那两者的
    收益是绑定在"某个具体牌型"上的（升的就是那个牌型），混牌型没有意义。
    """
    params = config.params
    deck = state.draw_pile.cards
    if not deck:
        return []

    committed = committed_hand(state, params) or "Pair"
    base = representative_hands(deck, committed)
    if not base:
        return []
    views = [ValuationView(tuple((list(p), list(h)) for p, h in base), committed, 1.0)]

    soft = hand_softness(state, params)
    if soft <= 0.0:
        return views

    candidates = _soft_candidates(state, committed, params)
    if not candidates:
        return views

    # 权重：等级越高越可能真的去打它
    committed_level = state.hand_level(committed)
    pool = {committed: committed_level}
    for info in candidates:
        pool[info.name] = info.level
    total = sum(pool.values())
    if total <= 0:
        return views

    weights = {name: soft * level / total for name, level in pool.items()}
    weights[committed] += 1.0 - soft

    views = []
    for name, weight in weights.items():
        scenarios = (
            base if name == committed else representative_hands(deck, name)
        )
        if not scenarios:
            continue
        views.append(
            ValuationView(
                tuple((list(p), list(h)) for p, h in scenarios), name, weight
            )
        )
    return views


def _level_bumped_base(state: GameState, hand_name: str, levels: int = 1):
    """返回一个"指定牌型等级 +levels"的基础分函数（星球牌估值用）。

    这是 `_level_downgraded_base`（The Arm 降级）的对称版本：只有目标牌型
    被提升，其它牌型保持原样。
    """

    def base(name: str) -> tuple[int, int]:
        chips, mult = state.hand_base(name)
        if levels <= 0 or name != hand_name:
            return chips, mult
        for _ in range(levels):
            chips, mult = hand_base_level_up(name, chips, mult)
        return chips, mult

    return base


def lineup_score(
    state: GameState,
    jokers: Sequence[Card],
    played: Sequence[Card],
    held: Sequence[Card],
    hand_name: str,
    *,
    level_bonus: int = 0,
) -> float:
    """给定小丑牌阵容，用真实算分引擎算这一手打多少分。

    牌型强制成 `hand_name`：我们要比较的是"同一手牌换不同阵容"，
    不能让牌型判定随阵容漂移。`level_bonus` 用来算"升一级值多少分"。

    会先按**最佳槽位摆放**重排：机器人每回合可以免费重排，而且顺序对复制类
    小丑牌（Blueprint / Brainstorm）是决定性的——两张相邻成环时会互相复制、
    双双归零（实测一套阵容因此被估成 128 分，而换个位置就是几千分）。
    """
    solver = replace(Solver.from_state(state), jokers=tuple(jokers))
    if level_bonus:
        solver = replace(
            solver, base_fn=_level_bumped_base(state, hand_name, level_bonus)
        )
    if len(jokers) >= 2:
        order = solver.best_joker_order(played)
        if order:
            solver = solver.reordered(order)
    hand = list(played) + list(held)
    return solver.score(hand, tuple(range(len(played))), hand_name=hand_name).total


def planet_level_gain(
    state: GameState,
    hand_name: str,
    scenarios: Sequence[tuple[Sequence[Card], Sequence[Card]]],
) -> float:
    """**升一级**能多打多少分——星球牌的收益，用真实算分引擎算。

    这一步是整个按需估值的关键：它自动包含了"等级的价值随阵容变强而变大"。
    有 xMult 小丑时，一级加的筹码/倍率会被乘区放大，所以后期星球牌更值钱。
    常数值做不到这件事，而那正是"该买不买、不该买乱买"的根源。
    """
    if not scenarios:
        return 0.0
    before = max(
        lineup_score(state, state.jokers.cards, played, held, hand_name)
        for played, held in scenarios
    )
    after = max(
        lineup_score(state, state.jokers.cards, played, held, hand_name, level_bonus=1)
        for played, held in scenarios
    )
    return max(0.0, after - before)


def celestial_pack_gain_per_dollar(
    state: GameState,
    config: Config,
    card: Card,
    scenarios: Sequence[tuple[Sequence[Card], Sequence[Card]]] | None = None,
    hand_name: str | None = None,
) -> float:
    """这个天体包"每 $1 能买到当前得分能力的百分之几"。

    = 期望级数 × 单级得分增幅 ÷ 当前得分能力 ÷ 价格

    用"占当前得分能力的比例"而不是绝对分数，是为了让它跨越底注可比：
    第 1 底注的 300 分和第 8 底注的 50000 分不是一个尺度。
    """
    if scenarios is None or hand_name is None:
        scenarios, hand_name = valuation_scenario(state, config)
    if not scenarios or not hand_name or card.buy_cost <= 0:
        return 0.0

    wanted = len(accepted_planet_hands(state, config.params))
    shown, choose = celestial_pack_shape(card.key)
    # Telescope：包里第一张保底是"打得最多"牌型的星球牌。
    # 只有当保底落在**可吃牌型**里时才算数——否则买了也拿不到我们要的。
    guaranteed = (
        state.owns_voucher("v_telescope")
        and guaranteed_planet_hand(state) in accepted_planet_hands(state, config.params)
    )
    levels = celestial_expected_levels(
        config.params.celestial_pool_size, shown, choose, wanted,
        guaranteed=guaranteed,
    )
    if levels <= 0:
        return 0.0

    gain = planet_level_gain(state, hand_name, scenarios)
    current = max(
        lineup_score(state, state.jokers.cards, played, held, hand_name)
        for played, held in scenarios
    )
    if current <= 0:
        return 0.0
    return levels * gain / current / card.buy_cost


def joker_marginal_values(
    state: GameState,
    plan: Sequence[ValuationView],
) -> list[float]:
    """每张**持有**的小丑牌的边际贡献（去掉它之后少打多少分），多视角取加权平均。

    这是"哪张最该卖"的正确依据：静态表看不出它和当前牌型、当前阵容的配合，
    也看不出算分引擎压根不认它。

    每个视角先在自己的场景上取平均，再按视角权重合并——这样视角之间的
    权重关系才和 `_best_placement_gain` 里的口径一致。
    """
    jokers = state.jokers.cards
    totals = [0.0] * len(jokers)
    for view in plan:
        if not view.scenarios:
            continue
        subtotal = [0.0] * len(jokers)
        for played, held in view.scenarios:
            base = lineup_score(state, jokers, played, held, view.hand_name)
            for index in range(len(jokers)):
                without = [card for i, card in enumerate(jokers) if i != index]
                subtotal[index] += (
                    base - lineup_score(state, without, played, held, view.hand_name)
                )
        divisor = max(1, len(view.scenarios))
        for index in range(len(jokers)):
            totals[index] += view.weight * subtotal[index] / divisor
    return totals


def _best_placement_gain(
    state: GameState,
    build_lineup: Callable[[list[Card]], list[Card]],
    plan: Sequence[ValuationView],
) -> float:
    """在一组估值视角上求最佳摆放位置的得分倍数（按视角权重加权）。

    位置对小丑牌是**决定性**的：Blueprint 复制的是右边那张，追加在末尾等于
    什么都没复制（实测 Blueprint 因此被估成 ×1.0）。而机器人买完牌可以免费重排，
    所以估值应该按"放到最合适的位置"来算，而不是按追加算。
    """
    jokers = list(state.jokers.cards)
    candidates = build_lineup(jokers)
    total = 0.0
    for view in plan:
        view_best = 0.0
        for played, held in view.scenarios:
            base = lineup_score(state, jokers, played, held, view.hand_name)
            if base <= 0:
                continue
            ratio = max(
                lineup_score(state, lineup, played, held, view.hand_name) / base
                for lineup in candidates
            )
            view_best = max(view_best, ratio)
        total += view.weight * view_best
    return total if total > 0 else 1.0


def joker_addition_gain(
    state: GameState,
    card: Card,
    plan: Sequence[ValuationView],
) -> float:
    """往空牌位里加一张 card 之后的得分倍数（按最佳摆放位置算）。

    每个位置都试一遍：位置决定结算顺序，加法在前、乘区在后得分更高；
    Blueprint / Brainstorm 更是完全取决于邻居。
    """
    def placements(jokers: list[Card]) -> list[list[Card]]:
        return [jokers[:i] + [card] + jokers[i:] for i in range(len(jokers) + 1)]

    return _best_placement_gain(state, placements, plan)


def joker_swap_gain(
    state: GameState,
    card: Card,
    index: int,
    plan: Sequence[ValuationView],
) -> float:
    """把第 index 张小丑牌换成 card 之后的得分倍数（按最佳摆放位置算）。"""
    def placements(jokers: list[Card]) -> list[list[Card]]:
        rest = [other for i, other in enumerate(jokers) if i != index]
        return [rest[:i] + [card] + rest[i:] for i in range(len(rest) + 1)]

    return _best_placement_gain(state, placements, plan)


def _pack_value(
    kind: str, situation: "Situation | None", params: TunableParams
) -> float:
    """补充包估值（天体包不走这里，它按需算期望，见 celestial_pack_gain_per_dollar）。

    天体包原来在这里吃一个常数 +3.0 的紧急加成——那是"落后就无脑多买包"，
    正是 30 局买 75 个包、只升 8 级的原因。改成按需估值之后这个常数必须去掉，
    否则两套逻辑叠加又会过量购买。
    """
    return PACK_VALUE.get(kind, DEFAULT_PACK_VALUE)


def is_useless_consumable(
    state: GameState, card: Card, params: TunableParams = DEFAULT_PARAMS
) -> bool:
    """这张消耗牌我们**永远不会用**吗——那它占着槽位就是纯损失。

    实测的因果链（`tools/slot_trace.py` 逐帧可查）：

    1. `The High Priestess` / `The Emperor` 在 `SAFE_CONSUMABLE_KEYS` 里，
       所以机器人会主动用它们——但它们造出来的是**随机**星球牌 / 塔罗牌；
    2. 随机星球牌通常不是专精牌型，而"用非专精牌型的星球牌"是被**故意禁止**的
       （用了就是分兵，等级一散 γ 就切不过去）；
    3. 机器人**从不卖消耗牌**（所有 `Sell` 调用点都只传了 `joker=`）；
    4. → 杂牌星球牌永久占住 2 个消耗牌槽，实测 `c_neptune` 从第 87 步躺到结束；
    5. → 槽满之后天体包只能跳过，**星球牌供给被自己的垃圾堵死**。

    所以要把"永远不会用"的牌卖掉腾槽。卖价很小，但一个消耗牌槽的价值远大于它。
    """
    if card.key in PLANET_KEYS:
        accepted = accepted_planet_hands(state, params)
        # 安全阀：连"可吃牌型"都算不出来时（`committed_hand` 返回 None，
        # 只会在 hands 表为空这种异常局面出现）**不要**把所有星球牌当垃圾清掉。
        # 否则这个清理会从"腾槽"变成"清仓"。
        if not accepted:
            return False
        return PLANET_HAND.get(card.key) not in accepted
    # 白名单里的都是"我们会用"的，不能卖
    if card.key in SAFE_CONSUMABLE_KEYS or card.key in TARGETED_CONSUMABLE_KEYS:
        return False
    if card.key in SAFE_SPECTRAL_KEYS or card.key in TARGETED_SPECTRAL_KEYS:
        return False
    # Wraith 是**条件性**可用（钱少且有小丑牌位时用它换传奇丑），不能当垃圾卖
    if card.key == WRAITH_KEY:
        return False
    # 剩下的是我们本来就不碰的牌——留着只会占槽
    return True


def _useless_consumable_to_sell(state: GameState, params: TunableParams) -> int | None:
    """挑一张"永远不会用"的消耗牌卖掉，返回它的槽位下标；没有就返回 None。"""
    for index, card in enumerate(state.consumables.cards):
        if is_useless_consumable(state, card, params):
            return index
    return None


def choose_shop_action(
    state: GameState,
    config: Config,
    rerolls: int,
    *,
    archetype: str | None = None,
) -> Action:
    """返回商店里的下一个动作；没事可做就离开商店。

    rerolls 是本轮商店已经重摇的次数，由主循环维护。
    `archetype` 是已经承诺的**主流派**（阶段二接入，见 build.py）。
    """
    money = state.money
    params = config.params
    free_joker_slots = state.joker_slots_free
    free_consumable_slots = state.consumable_slots_free
    jokers = state.jokers.cards

    # 全局局面判断：落后（时间维度）或者本关过不了（危急）。
    # 实测 7/10 局带着 $47~84 的余额死掉，而那些钱在当时的尺度上毫无意义。
    # 必须在 reserve 之前算——保留额和重摇预算都要看它。
    situation = assess(state, config)
    urgent = situation.urgent
    accepted = accepted_planet_hands(state, params)

    # 0) 先清掉"永远不会用"的消耗牌，腾出槽位。
    #
    # 必须排在**买包之前**：消耗牌槽只有 2 个，被杂牌占住时天体包只能跳过
    # （mod 的 pack.lua 不校验消耗牌位，硬拿会静默丢失）。这是免费的——
    # 卖牌还给钱，而空出来的槽能接住真正要的星球牌。
    junk = _useless_consumable_to_sell(state, params)
    if junk is not None:
        card = state.consumables.cards[junk]
        hand_type = PLANET_HAND.get(card.key)
        note(f"卖掉用不上的 {card.label or card.key}（腾消耗牌槽）",
             kind="sell", 槽位=junk,
             **({"牌型": hand_type, "可吃牌型": list(accepted)} if hand_type else {}))
        return Sell(consumable=junk)

    # 紧急时保留额降回底线（钱不花掉就是浪费）
    reserve = effective_reserve(state, config, situation)

    # 经济是否充足。充裕时**主动花钱提升倍率**，而不是一味攒着吃 $5 利息。
    # 注意放宽只对"能提倍率的牌"生效（乘区/加区小丑牌、升级牌型的星球牌）：
    # 否则门槛一降，纯功能牌（8 球、鸡蛋之类）也会被顺手买进来，反而更差。
    surplus = money >= params.surplus_money

    def _worth_spending_surplus(card: Card) -> bool:
        if not surplus:
            return False
        if card.set == "PLANET":
            # 只认可吃牌型的星球牌（主 + 已在投入的备选）；
            # 给全部星球牌放宽会把等级摊薄到用不上的牌型上
            return PLANET_HAND.get(card.key) in accepted
        return card.set == "JOKER" and raises_multiplier(card.key)

    def _buy_threshold(card: Card) -> float:
        """这张牌要多少评分才值得买（经济充足且能提倍率时门槛降低）。"""
        relief = params.surplus_buy_relief if _worth_spending_surplus(card) else 0.0
        return params.buy_threshold - relief

    def _rank_value(key: str, count: int) -> float:
        """静态口径的估值：静态表 + 空位加成 + 与专精牌型的契合度。

        只作为**兜底**使用——打分场景构造不出来时（牌组为空等）才走这条路。
        实测证明静态口径会出错，见上面 valuation_scenario 的说明。
        """
        return effective_joker_value(key, count, state.ante, params) + \
            synergy_adjustment(key, committed_hand(state, params), params)

    # 星球牌 / 天体包的估值绑定在"专精牌型"上（升的就是那个牌型），用单牌型口径。
    scenarios, hand_name = valuation_scenario(state, config)
    # 小丑牌的估值用**多视角**：过渡期把其它"已投入"的牌型按等级加权纳入，
    # 于是"要求别家牌型"的条件牌不会被 0/1 硬筛整类误杀。已定型时只剩专精牌型，
    # 等价于原来的硬筛。
    plan = valuation_plan(state, config)
    soft = hand_softness(state, params) if plan else 0.0
    has_scenario = bool(plan)

    def _is_utility_joker(card: Card) -> bool:
        """只给钱/只改状态、本身不贡献得分的小丑牌。

        这类牌的真实得分贡献天然是 0，不能因为"得分没涨"就拒绝——
        但也不该享受提倍率牌的放宽。
        """
        return card.set == "JOKER" and (
            card.key in NO_SCORE_EFFECT or card.key in PURE_UTILITY_JOKERS
        )

    def _score_gain(card: Card, replace_index: int | None) -> float | None:
        """把 card 放进阵容之后的得分倍数；场景不可用时返回 None（走静态口径）。"""
        if not has_scenario:
            return None
        if replace_index is None:
            return joker_addition_gain(state, card, plan)
        return joker_swap_gain(state, card, replace_index, plan)

    # 1) 买商店货架上最值得买的那张（小丑牌 / 星球牌 / 塔罗牌分类估值）
    if state.shop.cards:
        candidates: list[tuple[float, int]] = []
        for index, card in enumerate(state.shop.cards):
            if card.buy_cost <= 0 or card.buy_cost > money:
                continue
            if card.set == "JOKER" and free_joker_slots <= 0:
                continue
            if _is_consumable_slot_card(card) and free_consumable_slots <= 0:
                continue
            # 锁死牌型：不吃别的牌型的星球牌（备选是"已经在投入的"，不算分兵）
            if (card.set == "PLANET" and params.lock_planet_type
                    and PLANET_HAND.get(card.key) not in accepted):
                continue
            candidates.append((shop_card_value(card, state, params, surplus=surplus,
                                               situation=situation,
                                               archetype=archetype), index))
        candidates.sort(reverse=True)

        for best_value, index in candidates:
            chosen = state.shop.cards[index]
            boosts = _worth_spending_surplus(chosen)
            # 静态门槛对谁都生效（它管的是"这张牌本身值不值这个价"）
            if best_value < _buy_threshold(chosen):
                continue
            # 小丑牌额外过一道"真实得分"闸门：阵容得分要涨够，
            # 或者它是只给钱/只改状态的牌（那类牌得分天然不涨）。
            score_gain: float | None = None
            if chosen.set == "JOKER" and has_scenario:
                score_gain = _score_gain(chosen, None)
                if score_gain is not None and score_gain < 1.0 + params.joker_buy_gain:
                    if not _is_utility_joker(chosen):
                        continue
            price = chosen.buy_cost
            # 顶级牌 / 我们确定会用的星球牌，允许动用利息本金。
            # 注意这里用 raw_shop_value，不含空位加成。
            # 经济充足且这张牌能提倍率时也不必再守保留额——
            # 钱超过利息上限之后再攒着没有收益。
            break_reserve = (
                money - price < reserve
                and not boosts
                and not worth_breaking_reserve(chosen, state)
                and raw_shop_value(chosen, state) < params.exception_value
            )
            if not break_reserve:
                numbers = dict(估值=round(best_value, 1),
                               门槛=round(_buy_threshold(chosen), 1),
                               价格=price)
                if score_gain is not None:
                    numbers["阵容得分"] = f"×{score_gain:.2f}"
                # 过渡期的软筛要看得见：日志里没这个数字就没法判断它有没有生效
                if soft > 0.0 and len(plan) > 1:
                    numbers["软筛"] = "{} ({})".format(
                        f"{soft:.2f}", "/".join(v.hand_name for v in plan)
                    )
                note(f"买 {chosen.label or chosen.key}", kind="buy", **numbers)
                return Buy(card=index)

    # 2) 小丑牌位满了、商店里有更好的 → 卖掉**真实贡献最小**的那张来腾位子
    #
    # 实测这一条原来是错的：用静态表挑"最差的那张"，于是
    #   * 筹码丑（Sly，+50 筹码）因为"要求对子"拿到 +1.5 契合度，
    #     反超倍率丑（Smiley Face，+5 倍率/人头牌）——违反规格的
    #     "倍率丑 > 筹码丑"；
    #   * 引擎不认的牌（Smeared / Four Fingers / Shortcut，静态 8.0~8.5）
    #     真实贡献是 0，却因为静态分高而永远当不上"最差的"，废掉一个牌位。
    #
    # 现在改成：卖哪张由**去掉它之后少打多少分**决定；换不换由
    # **换上之后的得分倍数**决定。两个量都来自算分引擎，不再靠静态表。
    if free_joker_slots == 0 and jokers and state.shop.cards:
        # 永恒牌卖了会扣分，直接从候选里排除（而不是"最差的是永恒牌就整条放弃"——
        # 那样只要有一张永恒牌，其余四张就永远换不掉了）
        sellable = [i for i, card in enumerate(jokers) if not card.eternal]
        if sellable:
            # 边际贡献只算一次（每张货架牌都重算是浪费）。
            # `marginals is None` 表示没有估值场景，退回静态成色比较。
            marginals = joker_marginal_values(state, plan) if has_scenario else None

            def _worst(pool: list[int]) -> int:
                if marginals is not None:
                    return min(pool, key=lambda i: marginals[i])
                return min(pool, key=lambda i: _rank_value(jokers[i].key, len(jokers)))

            for index, card in enumerate(state.shop.cards):
                if card.set != "JOKER" or card.buy_cost <= 0 or card.buy_cost > money:
                    continue
                # ① 复利牌不当换牌牺牲品。语义全在 `swap_victims` 里（可单测）。
                pool = swap_victims(
                    jokers, card.key, params, urgent=urgent, sellable=sellable,
                    rank_value=lambda key: _rank_value(key, len(jokers)),
                )
                if not pool:
                    continue
                worst_index = _worst(pool)
                marginal = marginals[worst_index] if marginals is not None else 0.0
                gain = None
                if has_scenario:
                    # 换牌还有买卖差价损耗，所以平时要求更高的提升幅度；
                    # 但紧急时门槛归零——能提升就换，留着钱没有意义。
                    gain = _score_gain(card, worst_index)
                    required_gain = 1.0 if urgent else 1.0 + params.joker_swap_gain
                    if gain is None or gain < required_gain:
                        continue
                else:
                    worst_value = _rank_value(jokers[worst_index].key, len(jokers))
                    margin = max(0.0, params.sell_upgrade_margin
                                 - (params.surplus_sell_relief if surplus else 0.0))
                    if _rank_value(card.key, len(jokers)) < worst_value + margin:
                        continue
                # 新牌自己也得够格被买进来，否则"卖了又不买"是净亏
                if shop_card_value(card, state, params, surplus=surplus) < _buy_threshold(card):
                    continue
                numbers = dict(卖=f"#{worst_index} {jokers[worst_index].label or jokers[worst_index].key}",
                               边际贡献=round(marginal),
                               买=card.label or card.key)
                if gain is not None:
                    numbers["阵容得分"] = f"×{gain:.2f}"
                if urgent:
                    numbers["注意"] = "紧急：门槛已归零"
                note(f"卖 {numbers['卖']} 换 {numbers['买']}", kind="sell", **numbers)
                return Sell(joker=worst_index)

    # 3) 买代金券：几乎总是划算
    #
    # 星球牌供给类的（Telescope / Planet Merchant / Planet Tycoon）按**供给倍数**
    # 加权：它们提高的不是单包效率，而是"能拿到多少星球牌"——那才是真正的瓶颈
    # （实测 30 局对子平均只升 1.2~1.4 级，第 3 级才能让 γ 越过切换点）。
    # 加权后它们的估值会越过 `exception_value`，也就是**允许动用利息本金**去买。
    if state.vouchers.cards:
        for index, card in enumerate(state.vouchers.cards):
            if card.buy_cost <= 0 or card.buy_cost > money:
                continue
            value = voucher_value(card, state, params)
            if value >= params.voucher_threshold:
                if money - card.buy_cost >= reserve or value >= params.exception_value:
                    numbers = dict(估值=round(value, 1), 门槛=params.voucher_threshold)
                    supply = voucher_supply_multiplier(state, card.key)
                    if supply > 1.0:
                        numbers["星球牌供给"] = f"×{supply:.2f}"
                    if money - card.buy_cost < reserve:
                        numbers["注意"] = "动用利息本金"
                    note(f"买券 {card.label or card.key}", kind="buy", **numbers)
                    return Buy(voucher=index)

    # 4) 买补充包
    #
    # 注意判据是"货架上有包"，**不是**"还有空的货架位"。`booster_slots_free`
    # 算的是 `card_limit - 已有的包数`，而补充包货架通常就是满的（两个位子摆两个包）
    # → 那个值恒为 0 → 整条分支从来没执行过（实测 10 局一个包都没买）。
    # 这一条卡掉的正是**天体包**——牌型等级最主要的来源，也是唯一能跟上
    # "每个底注涨 2.08 倍"的手段。
    if state.packs.cards:
        for index, card in enumerate(state.packs.cards):
            if card.buy_cost <= 0 or card.buy_cost > money:
                continue
            kind = pack_kind(card.key)

            # 天体包**按需估值**：算"命中概率 × 升一级的得分增幅 ÷ 价格"，
            # 达不到门槛就不买——12 种星球牌里只有 1~2 张是我们要的，
            # 普通包命中率只有 25%，无脑买等于把钱扔掉（实测 30 局买 75 个包、
            # 花 $874 只升了 8 级）。紧急时例外：那时钱留着没有意义。
            if kind == "celestial":
                # **按需估值**：命中概率 × 升一级的得分增幅 ÷ 价格。
                # 12 种星球牌里只有 1~2 张是我们要的，普通包命中率只有 25%，
                # 无脑买等于把钱扔掉（实测 30 局买 75 个包、花 $874 只升 8 级）。
                #
                # 紧急时**降低门槛**而不是无条件买：无条件买会让估值完全失效
                # （实测门槛翻倍、买包数量反而上升）。
                rate = celestial_pack_gain_per_dollar(
                    state, config, card, scenarios, hand_name
                )
                threshold = params.pack_gain_per_dollar
                if urgent:
                    threshold *= params.urgent_pack_relief
                if rate >= threshold and (money - card.buy_cost >= reserve or urgent):
                    note(f"买天体包 {card.label or card.key}",
                         kind="pack", 每元增益=f"{rate * 100:.2f}%",
                         门槛=f"{threshold * 100:.2f}%",
                         价格=card.buy_cost,
                         **({"注意": "紧急：门槛已降到 25%"} if urgent else {}))
                    return Buy(pack=index)
                continue

            value = _pack_value(kind, situation, params)
            if kind == "standard":
                value += standard_pack_bonus(state, params)
            if value >= params.pack_threshold:
                if money - card.buy_cost >= reserve or value >= params.exception_value:
                    note(f"买补充包 {card.label or card.key}",
                         kind="pack", 估值=round(value, 1),
                         门槛=params.pack_threshold, 价格=card.buy_cost)
                    return Buy(pack=index)

    # 5) 重摇（预算按底注递减；同样要守住保留额）
    #    视频："前期至少留 15 块以上再重置；中后期重置后尽量留 25 块吃利息"
    #    经济充足时多给几次预算去找倍率牌；**紧急时改成"花得起就一直摇"**。
    reroll_budget = max_rerolls_for(state, config, situation)
    if surplus:
        reroll_budget += params.surplus_rerolls
        # 钱多到超过利息上限之后，保留额本身也没意义了，允许花到只剩保留额的一半
        reserve = min(reserve, max(params.money_reserve, params.interest_floor // 2))

    # 能靠重摇改善局面吗？**牌位满了也要允许重摇**——那正是寻找换牌目标的
    # 唯一手段。实测就是这里出的问题：A5 时牌位满、余额 $84，原来这条要求
    # `free_joker_slots > 0`，于是整条重摇分支被跳过，机器人带着 $84 离开商店
    # 去撞 22000 的 Boss。换牌需要至少有一张非永恒牌可以牺牲。
    can_improve = free_joker_slots > 0 or any(not card.eternal for card in jokers)
    if (
        can_improve
        and rerolls < reroll_budget
        and money >= params.reroll_min_money
        and money - state.round.reroll_cost >= reserve
    ):
        if free_joker_slots > 0:
            why = "重摇找能填满空位的牌"
        else:
            why = "重摇找换牌目标（牌位已满，只能换）"
        numbers = dict(预算=reroll_budget, 已摇=rerolls, 花费=state.round.reroll_cost)
        if urgent:
            numbers["注意"] = "紧急：预算改为花得起就摇"
        note(why, kind="reroll", **numbers)
        return Reroll()

    if not can_improve:
        why = "离开商店：没有可牺牲的小丑牌（全是永恒），重摇也没用"
    elif rerolls >= reroll_budget:
        why = f"离开商店：重摇预算用完（{rerolls}/{reroll_budget}）"
    elif money < params.reroll_min_money:
        why = f"离开商店：钱不足重摇门槛（${money} < ${params.reroll_min_money}）"
    else:
        why = "离开商店：再摇会跌破保留额"
    note(why, kind="flow", 保留额=reserve, 余额=money)
    return NextRound()


# ------------------------------------------------------------------ 开包决策


def choose_pack_action(
    state: GameState, config: Config, *, archetype: str | None = None
) -> OpenPack:
    """从打开的补充包里选牌。

    选什么很关键：星球包里的牌是**立刻使用**的，所以必须挑我们专精的那个
    牌型；塔罗包里则要挑我们真的会用的牌，而不是拿一张用不上的占着位置。
    """
    cards = state.open_pack.cards
    if not cards:
        note("包是空的 → 跳过", kind="pack")
        return OpenPack(skip=True)

    def _pick(index: int, why: str, **numbers) -> OpenPack:
        note(why, kind="pack", 拿了=cards[index].label or cards[index].key, **numbers)
        return OpenPack(card=index)

    def _skip(why: str, **numbers) -> OpenPack:
        note(why, kind="pack", **numbers)
        return OpenPack(skip=True)

    # 小丑牌包：没空位就直接跳过，别浪费
    joker_indices = [i for i, c in enumerate(cards) if c.set == "JOKER"]
    if joker_indices:
        if state.joker_slots_free <= 0:
            return _skip("小丑牌位满了，拿了也放不下 → 跳过")
        best_index = max(
            joker_indices,
            key=lambda i: effective_joker_value(
                cards[i].key, state.joker_count, state.ante, config.params
            ),
        )
        if joker_value(cards[best_index].key) >= config.params.buy_threshold:
            return _pick(best_index, "从包里拿小丑牌",
                         静态估值=joker_value(cards[best_index].key))
        return _skip("包里的小丑牌都不够格 → 跳过")

    # 消耗牌槽满了：包里若是消耗牌（星球 / 塔罗 / 幻灵），拿了会**静默丢失**。
    #
    # 根因在 mod：`pack.lua:139-152` 只校验**小丑牌位**，对消耗牌没有任何检查，
    # 于是 `open_pack(card=N)` 不报错、牌也不进槽——白拿。
    # 证据：实测"从商店买来的 Mercury 总会被用掉，从包里拿的却大多从没被用过"。
    if state.consumable_slots_free <= 0 and any(
        c.set in ("PLANET", "TAROT", "SPECTRAL") for c in cards
    ):
        return _skip("消耗牌槽满了，拿了也放不下 → 跳过",
                     空槽=state.consumable_slots_free)

    # 星球包：先挑主牌型的，其次挑**已经在投入的备选**牌型的
    planet_indices = [i for i, c in enumerate(cards) if c.key in PLANET_HAND]
    if planet_indices:
        accepted = accepted_planet_hands(state, config.params)
        for hand_type in accepted:          # 主牌型在前，备选在后
            for index in planet_indices:
                if PLANET_HAND[cards[index].key] == hand_type:
                    role = "主牌型" if hand_type == accepted[0] else "备选牌型"
                    return _pick(index, f"从包里拿{role}的星球牌",
                                 牌型=hand_type, 角色=role)
        # 锁死牌型时，包里没有可吃牌型的星球牌就**宁可不要**——
        # 拿了别的牌型就是分兵，等级一分散 γ 就永远切不过去。
        if config.params.lock_planet_type:
            return _skip("包里没有可吃牌型的星球牌（锁死中，不拿别的）→ 跳过",
                         可吃牌型=list(accepted),
                         包内星球=[PLANET_HAND[c.key] for c in cards
                                   if c.key in PLANET_HAND])
        best_index = max(
            planet_indices,
            key=lambda i: planet_value(cards[i], state),
        )
        if planet_value(cards[best_index], state) <= PLANET_OFFTYPE_VALUE:
            return _skip("包里的星球牌都不是我们在打的牌型 → 跳过")
        return _pick(best_index, "从包里拿估值最高的星球牌")

    # 塔罗 / 幻灵：挑我们真的会用的；都用不上就跳过
    hand = state.hand.cards

    def _pack_targets(index: int) -> tuple[int, ...]:
        """需要指定目标的牌，把目标裁到游戏允许的张数。

        原来一律不指定目标，于是 The Sun / The Moon / The Star / The World
        这些东西会被游戏拒掉（实测 "requires 1-3 target card(s). Provided: 0"），
        包就等于白买了。
        """
        key = cards[index].key
        best = Solver.from_state(state).best(hand) if hand else None
        scoring = best.indices if best is not None else ()
        # 和商店里用塔罗时同一套口径：流派偏好的牌排前面，算分牌补位
        preferred = _merge_targets(
            archetype_target_order(state, archetype, hand, config.params), scoring
        )
        return consumable_targets(state, key, preferred, hand=hand)

    spectral_usable = [
        i for i, c in enumerate(cards)
        if c.key in SAFE_SPECTRAL_KEYS or c.key in TARGETED_SPECTRAL_KEYS
        or (c.key == WRAITH_KEY and state.money <= WRAITH_MAX_MONEY
            and state.joker_slots_free > 0)
    ]
    if spectral_usable:
        # Black Hole（全牌型 +1 级）和 The Soul（传奇丑）优先
        for index in spectral_usable:
            if cards[index].key in SAFE_SPECTRAL_KEYS:
                return _pick(index, "从包里拿无条件收益的幻灵牌")
        index = spectral_usable[0]
        targets = _pack_targets(index)
        note("从包里拿幻灵牌（给算分牌加蜡封）", kind="pack",
             拿了=cards[index].label or cards[index].key, 目标=list(targets))
        return OpenPack(card=index, targets=targets)

    usable = [
        i for i, c in enumerate(cards)
        if c.key in SAFE_CONSUMABLE_KEYS or c.key in TARGETED_CONSUMABLE_KEYS
    ]
    if usable:
        # 先挑不需要目标的（不会失败），都没有再挑需要目标的并带上目标
        for index in usable:
            if cards[index].key in SAFE_CONSUMABLE_KEYS:
                return _pick(index, "从包里拿无目标塔罗牌（不会失败）")
        for index in usable:
            targets = _pack_targets(index)
            if targets:
                note("从包里拿需要指定目标的塔罗牌", kind="pack",
                     拿了=cards[index].label or cards[index].key, 目标=list(targets))
                return OpenPack(card=index, targets=targets)
        return _skip("包里的塔罗牌都需要目标，但手里没有可指定的牌 → 跳过")

    # 标准包：优先挑**已经强化好**的牌。
    #
    # 原来挑"卖出价最高的"——那是**钱**的口径，完全看不见强化。而强化正是
    # 标准包的全部意义：玻璃牌算分 ×2、钢铁牌留手 ×1.5，都和 ×Mult 相乘。
    if all(c.set in ("DEFAULT", "ENHANCED") for c in cards):
        def _card_worth(i: int) -> float:
            card = cards[i]
            worth = ENHANCEMENT_VALUE.get(card.enhancement or "", 0.0)
            if card.seal:
                worth += 1.0                      # 蜡封 = 多触发一次
            if card.edition:
                worth += 1.5                      # 闪箔/全息/多彩
            return worth + card.sell_cost * 0.05  # 同档里再按卖出价微调

        index = max(range(len(cards)), key=_card_worth)
        picked = cards[index]
        return _pick(index, "标准包：优先挑强化好的牌",
                     强化=picked.enhancement or "无", 蜡封=picked.seal or "无")

    return _skip("包里没有我们会用的牌 → 跳过")
