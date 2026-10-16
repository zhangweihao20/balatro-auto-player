"""小丑牌效果模型。

为什么需要这个文件
------------------
出牌阶段要选"打哪几张牌"，唯一的依据就是算分。牌型的基础 chips/mult 可以从
状态里的 ``hands`` 表精确读到（实测 100% 一致），牌面点数、强化、版本也是
确定的，唯独小丑牌的效果必须自己建模。

三个关键设计
------------
1. **数值从游戏读，不靠猜。** 状态里的 ``Card.effect`` 是这张牌的当前 UI 描述
   （例如 ``"X1.8 Mult"``、``"+24 Mult"``），成长类小丑牌的当前值就在里面。
   :func:`parse_xmult` / :func:`parse_mult` / :func:`parse_chips` 把它读出来，
   因此"打到第 6 底注时 Constellation 是几倍"这种问题不需要估算。

2. **key 必须真实存在。** key 拼错不会报错，只会静默地按 0 收益处理，机器人
   于是持续低估自己的分数。:mod:`balatro_bot.joker_data` 是从游戏本体提取的
   权威定义，测试会逐条校验本文件里所有 key。

3. **不认识的按 0 收益处理**，并在日志里点名（``unknown_jokers``），
   方便按实际遇到的情况补模型。低估比高估安全：低估只会让机器人保守。

已知的建模边界
--------------
下面这些会改变"牌型怎么算"，本模块不处理牌型判定，因此它们的模型是不完整的，
统一列在 :data:`KNOWN_UNSUPPORTED` 里（不当作"未建模"刷日志）：
    j_four_fingers（4 张即可成顺/同花）、j_smeared（红桃=方块、黑桃=梅花）、
    j_shortcut（顺子可以跳号）、j_pareidolia（所有牌视为人头牌，已部分支持）。

反触发类小丑牌（j_hack / j_sock_and_buskin / j_dusk / j_hanging_chad / j_mime）
和 Splash 的效果实现在 :mod:`balatro_bot.scoring` 里，本文件的表里不需要重复。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .cards import Card, FACE_RANKS

SUIT_KEYS = ("H", "D", "C", "S")

EVEN_RANKS = frozenset("2468T")
ODD_RANKS = frozenset("A3579")

# 可能提供"乘区"的小丑牌（静态分类，用于槽位排序）。
#
# 游戏按槽位从左到右结算，(基础 + 加法) × 乘区 > (基础 × 乘区) + 加法，
# 所以这些牌应该排在加法牌右边。**必须用静态分类**：如果改成"看这一手实际
# 触发没触发"，条件型乘区牌（例如 Card Sharp 只在本回合重复牌型时 x3）
# 在回合第一手就不会被识别出来，重排于是永远不会发生。
MULTIPLICATIVE_JOKERS = frozenset({
    # 无条件的乘区
    "j_steel_joker", "j_cavendish", "j_ramen", "j_constellation", "j_hologram",
    "j_glass", "j_obelisk", "j_lucky_cat", "j_vampire", "j_madness",
    "j_hit_the_road", "j_throwback", "j_campfire", "j_loyalty_card",
    # 条件型乘区
    "j_photograph", "j_triboulet", "j_duo", "j_trio", "j_family", "j_order",
    "j_tribe", "j_flower_pot", "j_seeing_double", "j_baseball", "j_blackboard",
    "j_card_sharp", "j_acrobat", "j_bloodstone", "j_drivers_license", "j_ancient",
    # 复制类：本身不产生乘区，但通常用来复制乘区牌，所以放右边
    "j_blueprint", "j_brainstorm",
})

# 复制邻居的小丑牌，位置本身就是策略选择（Blueprint 复制右边那张）
COPY_JOKERS = frozenset({"j_blueprint", "j_brainstorm"})


def is_multiplicative(key: str) -> bool:
    """这张小丑牌是不是乘区类（决定它该排在左边还是右边）。"""
    return key in MULTIPLICATIVE_JOKERS


# 加区（+倍率）小丑牌的手写名单。规格："前期加倍率提升明显"。
# 和乘区合起来就是"能提升倍率"的牌——经济充足时优先买它们
# （规格："在经济充足的情况下尝试提升倍率"）。
#
# 刻意不收：纯筹码丑（方形小丑等，规格里排最后）、纯经济丑、纯功能丑。
# keys 由 test_jokers.py 对着 joker_data.py 的权威表逐个校验，写错会被测试抓住。
#
# 名字带下划线是因为它**不是最终集合**：下面减去乘区，保证两个类目互不重叠。
_ADDITIVE_MULT_KEYS = frozenset({
    # 无条件 +4 倍率家族
    "j_joker", "j_jolly", "j_zany", "j_mad", "j_crazy", "j_droll",
    # 花色触发 +3 倍率
    "j_greedy_joker", "j_lusty_joker", "j_wrathful_joker", "j_gluttenous_joker",
    # 特定牌触发
    "j_photograph", "j_scholar", "j_fibonacci", "j_smiley", "j_even_steven",
    "j_odd_todd", "j_walkie_talkie", "j_scary_face", "j_arrowhead",
    "j_onyx_agate", "j_bloodstone", "j_flower_pot",
    # 条件/成长型加区
    "j_banner", "j_abstract", "j_half", "j_mystic_summit", "j_blue_joker",
    "j_green_joker", "j_red_card", "j_ride_the_bus", "j_ice_cream",
    "j_supernova", "j_popcorn", "j_misprint", "j_raised_fist", "j_flash",
    "j_bull", "j_bootstraps", "j_trousers", "j_hit_the_road", "j_lucky_cat",
    "j_ceremonial", "j_erosion", "j_castle", "j_wee", "j_runner",
    "j_shoot_the_moon", "j_swashbuckler", "j_stencil", "j_blackboard",
    "j_baseball", "j_matador", "j_gros_michel", "j_juggler", "j_troubadour",
    # 复制/翻倍/成长类（间接或直接提倍率）
    "j_blueprint", "j_brainstorm", "j_mime", "j_baron", "j_triboulet",
    "j_hologram", "j_constellation", "j_campfire", "j_obelisk",
    "j_loyalty_card", "j_ramen", "j_glass", "j_vampire",
    "j_steel_joker", "j_madness", "j_throwback", "j_drivers_license",
    "j_idol", "j_ancient", "j_seeing_double", "j_hanging_chad",
    "j_sock_and_buskin", "j_hack", "j_dusk", "j_card_sharp", "j_acrobat",
    "j_cavendish", "j_duo", "j_trio", "j_family", "j_order", "j_tribe",
    "j_yorick", "j_caino",
})

# 上面那份名单里混着一些乘区牌（Blueprint、Duo、Triboulet 之类），
# 减掉之后加区才是纯粹的"+倍率"。两个类目互不重叠，便于推理和测试。
ADDITIVE_MULT_JOKERS = _ADDITIVE_MULT_KEYS - MULTIPLICATIVE_JOKERS


def raises_multiplier(key: str) -> bool:
    """这张小丑牌是否"提升倍率"（加区或乘区）。

    用于"经济充足时优先花钱提倍率"——排除纯筹码丑、纯经济丑和纯功能丑。
    """
    return key in MULTIPLICATIVE_JOKERS or key in ADDITIVE_MULT_JOKERS


# 这些牌的效果由 scoring / cards 模块实现，不算"未建模"
HANDLED_ELSEWHERE = frozenset({
    "j_hack", "j_sock_and_buskin", "j_dusk", "j_hanging_chad", "j_mime", "j_splash",
    # Pareidolia（"所有牌都算人头牌"）不是自己算分，而是改变**上下文**：
    # PlayContext.all_faces 就是它，其他小丑牌的人头牌计数会跟着变。
    "j_pareidolia",
})

# 已知但未完整支持（会改变牌型判定），不当作未建模刷日志，但 README 里有说明
KNOWN_UNSUPPORTED = frozenset({
    "j_four_fingers", "j_smeared", "j_shortcut",
})

# 会**随时间 / 使用变强**的牌。
#
# 为什么单独一张表：机器人的换牌逻辑按"**当前**边际贡献"挑最差的一张去换掉
# （`choose_shop_action` 里的 `worst_index`）。而成长型/乘法型的价值不在"现在
# 这一手多打多少分"，而在"再打 N 手之后多打多少分"——
#
#   * 男爵（手里每张 K ×1.5）：手里没 K 时边际贡献约等于 0
#   * 星座（每买一张星球牌 ×0.1）：刚买时 ×1.0，等于没有
#   * 绿色丑（每打一手 +1 倍率）：第一手时只是个 +1
#
# 于是它们**必然**在还没长起来的时候被判成最差、拿去换掉。实测后果：
# 机器人每局换 3.6 次牌，363 次换牌里 60 次卖掉的是乘法牌，局终 54% 的局
# 一张 ×Mult 都没有（`tools/mult_diagnosis.py` 可复算）。
#
# 这张表是**手工**整理的（游戏数据里没有"会成长"这个字段）。乘法那一半直接
# 复用 MULTIPLICATIVE_JOKERS，这里只补上"会成长的加法牌"。
GROWTH_JOKERS = frozenset({
    # 每打一手 / 每次触发就涨
    "j_green_joker",        # 每打一手 +1 倍率
    "j_ride_the_bus",       # 连续不出人头就 +1 倍率
    "j_supernova",          # 倍率 = 该牌型打过的次数
    "j_spare_trousers",     # 每打一次两对 +2 倍率
    "j_red_card",           # 每跳过一个小丑包 +3 倍率
    "j_flash",              # 每次重打出牌 +2 倍率
    "j_wee",                # 每打出一张 2 涨筹码
    "j_castle",             # 每弃掉一张对应花色涨筹码
    # 按摧毁 / 消耗涨
    "j_ceremonial_dagger",  # 每摧毁一张牌涨倍率
    "j_fortune_teller",     # 每用一张塔罗 +1 倍率
    "j_glass",              # 每碎一张玻璃牌 ×0.75
    "j_lucky_cat",          # 每次幸运触发 ×0.25
    "j_campfire",           # 每卖一张牌 ×0.5
    "j_hologram",           # 每买一张牌 ×0.25
    "j_constellation",      # 每买一张星球牌 ×0.1
    "j_obelisk",            # 连续不重复牌型 ×1
    "j_madness",            # 每次选盲注 ×0.5
    "j_vampire",            # 每张增强牌 ×0.2
    "j_hit_the_road",       # 每弃一张 J ×0.5
})

# 复利牌 = 乘法牌 ∪ 成长牌。换牌时**不当牺牲品**，也用更宽的门槛去买。
COMPOUNDING_JOKERS = MULTIPLICATIVE_JOKERS | GROWTH_JOKERS


def is_compounding(key: str) -> bool:
    """这张牌的价值会不会**随时间增长**（而不是一次性加成）。"""
    return key in COMPOUNDING_JOKERS


# 有效但不需要在算分里体现的牌（纯经济/功能类）
NO_SCORE_EFFECT = frozenset({
    "j_credit_card", "j_chaos", "j_dna", "j_certificate", "j_cartomancer",
    "j_diet_cola", "j_drunkard", "j_egg", "j_faceless", "j_gift",
    "j_hallucination", "j_invisible", "j_juggler", "j_luchador", "j_mail",
    "j_marble", "j_midas_mask", "j_mr_bones", "j_oops", "j_perkeo",
    "j_riff_raff", "j_ring_master", "j_rocket", "j_satellite", "j_seance",
    "j_sixth_sense", "j_space", "j_superposition", "j_ticket", "j_to_the_moon",
    "j_todo_list", "j_trading", "j_troubadour", "j_turtle_bean", "j_vagabond",
    "j_burglar", "j_astronomer", "j_chicot", "j_delayed_grat", "j_cloud_9",
    "j_8_ball", "j_fortune_teller", "j_caino", "j_selzer",
    # 下面这几张只给钱、不改手牌等级，或者效果已经体现在状态里：
    #   Matador   出牌触发 Boss 限制时给 $8      → 只影响钱
    #   Golden    每回合 +$4                     → 只影响钱
    #   Burnt     升级弃掉的牌型等级              → 等级已反映在状态的 hands 表里
    #   Merry Andy +3 弃牌 / -1 手牌上限          → 都已反映在状态里
    "j_matador", "j_golden", "j_burnt", "j_merry_andy",
})


@dataclass(slots=True)
class JokerEffect:
    """一张小丑牌对本次出牌的贡献。"""

    chips: float = 0.0
    mult: float = 0.0
    xmult: float = 1.0
    money: float = 0.0

    def merge(self, other: "JokerEffect") -> None:
        self.chips += other.chips
        self.mult += other.mult
        self.xmult *= other.xmult
        self.money += other.money


@dataclass(slots=True)
class PlayContext:
    """一次候选出牌的上下文。

    刻意不持有 GameState：蒙特卡洛采样会构造成千上万个上下文，只带模型真正
    会读到的字段，避免每次采样都重建整个状态对象。

    ``played`` 是**真正参与算分的牌**（不是打出的全部牌）：游戏里花色/点数类
    小丑牌数的是 context.scoring_hand。``cards_played`` 才是打出的总张数。
    """

    played: tuple[Card, ...]
    held: tuple[Card, ...]
    hand_name: str
    joker_keys: frozenset[str] = frozenset()
    joker_count: int = 0
    hand_played: int = 0
    discards_left: int = 0
    deck_size: int = 0
    money: int = 0
    cards_played: int = 0
    is_final_hand: bool = False
    deck_composition: tuple[Card, ...] = ()
    hands_played_this_round: frozenset[str] = frozenset()
    # 本局每种牌型打过几次（Supernova 用）
    hand_plays_run: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------ 查询辅助

    @property
    def all_faces(self) -> bool:
        """pareidolia：所有牌都算人头牌。"""
        return "j_pareidolia" in self.joker_keys

    def count_suit(self, suit: str) -> int:
        return sum(1 for c in self.played if _matches_suit(c, suit))

    def count_rank(self, *ranks: str) -> int:
        wanted = set(ranks)
        return sum(1 for c in self.played if c.rank in wanted and not c.is_stone)

    def count_parity(self, parity: frozenset[str]) -> int:
        return sum(1 for c in self.played if c.rank in parity and not c.is_stone)

    def count_faces(self) -> int:
        if self.all_faces:
            return sum(1 for c in self.played if not c.is_stone)
        return sum(1 for c in self.played if c.rank in FACE_RANKS)

    def held_rank(self, *ranks: str) -> int:
        wanted = set(ranks)
        return sum(1 for c in self.held if c.rank in wanted and not c.is_stone)

    def held_faces(self) -> int:
        return sum(1 for c in self.held if c.rank in FACE_RANKS)

    def held_all_black(self) -> bool:
        """手里的牌是否全是黑桃或梅花（Blackboard 用）。"""
        for card in self.held:
            if card.is_stone or card.is_wild:
                return False
            if card.suit not in ("S", "C"):
                return False
        return bool(self.held)

    def deck_count(self, predicate: Callable[[Card], bool]) -> int:
        """整副牌里满足条件的张数。

        近似值：手上 + 牌堆。已经打出/弃掉的牌 mod 不暴露，所以会略微低估。
        """
        return sum(1 for c in self.deck_composition if predicate(c))

    @property
    def lowest_held_rank_value(self) -> int:
        from .cards import RANK_CHIPS

        values = [RANK_CHIPS.get(c.rank, 0) for c in self.held if c.rank and not c.is_stone]
        return min(values) if values else 0

    @property
    def suits_in_play(self) -> set[str]:
        return {c.suit for c in self.played if c.suit and not c.is_stone}


def _matches_suit(card: Card, suit: str) -> bool:
    if card.is_stone:
        return False
    if card.is_wild:
        return True
    return card.suit == suit


# ------------------------------------------------- 从描述文本读取当前数值

# 描述文本会跟随游戏语言，所以中英文一起匹配。
_MULT_WORD = r"(?:Mult|倍率)"
_CHIPS_WORD = r"(?:Chips|筹码)"

_XMULT_RE = re.compile(rf"[Xx×]\s*([\d.]+)\s*{_MULT_WORD}")
_MULT_RE = re.compile(rf"\+\s*([\d.]+)\s*{_MULT_WORD}")
_CHIPS_RE = re.compile(rf"\+\s*([\d.]+)\s*{_CHIPS_WORD}")

# 描述里的花色名 -> 内部代号
_SUIT_WORDS = {
    "Diamonds": "D", "Hearts": "H", "Spades": "S", "Clubs": "C",
    "方块": "D", "红桃": "H", "黑桃": "S", "梅花": "C",
    "方塊": "D", "紅桃": "H",
}

# "当前值"段的标记。成长类小丑牌的描述模板形如：
#     "+#1# Mult per hand played ... (Currently +#3# Mult)"
# 前一个数字是"每单位增量"，括号里的才是当前总值。
# 不区分这个，Green Joker 会被当成 +1 而不是实际的 +11；
# 对乘区类更是灾难——Constellation 会从 X1.8 变成 X0.1。
_CURRENT_MARKERS = ("Currently", "currently", "当前", "當前")


def current_segment(text: str) -> str:
    """取描述里"(Currently ...)"那一段；没有就返回全文。"""
    best = -1
    for marker in _CURRENT_MARKERS:
        index = text.find(marker)
        if index >= 0 and (best < 0 or index < best):
            best = index
    return text[best:] if best >= 0 else text


def parse_xmult(card: Card, default: float) -> float:
    """从描述里读当前倍率乘数，例如 "X1.8 Mult"。读不到就用默认值。"""
    match = _XMULT_RE.search(current_segment(card.effect or ""))
    return float(match.group(1)) if match else default


def parse_mult(card: Card, default: float) -> float:
    match = _MULT_RE.search(current_segment(card.effect or ""))
    return float(match.group(1)) if match else default


def parse_chips(card: Card, default: float) -> float:
    match = _CHIPS_RE.search(current_segment(card.effect or ""))
    return float(match.group(1)) if match else default


def parse_suit(card: Card) -> str | None:
    """从描述里读出"这一轮是哪个花色"（Ancient Joker 每回合换花色）。

    描述里花色是直接填进去的（例如 "...with Hearts suit give X1.5 Mult"），
    所以不需要额外状态就能读到。
    """
    text = card.effect or ""
    for word, code in _SUIT_WORDS.items():
        if word in text:
            return code
    return None


# The Idol 的目标是"某一张具体的牌"（点数 + 花色），每回合变一次。
# 这个信息**不在状态里**：mod 只上报 modifier / value(点数花色+effect) / state，
# 小丑牌的 config 没有上报。所以只能从描述文本里读。
#
# 中英文语序相反：
#     英文 "Each played 7 of Hearts gives X2 Mult when scored"
#     中文 "每张打出的红桃7在计分时给予 X2 倍率"
# 所以不能靠固定语序，改成"取离花色词最近的那个点数"——
# 文本里另一个数字是倍率（X2），离花色远得多，不会抢。
_RANK_TOKEN_RE = re.compile(r"(10|[2-9]|Jack|Queen|King|Ace|[JQKA])")
_RANK_WORD_TO_CODE = {"Jack": "J", "Queen": "Q", "King": "K", "Ace": "A"}


def _normalise_rank(token: str) -> str | None:
    if token in _RANK_WORD_TO_CODE:
        return _RANK_WORD_TO_CODE[token]
    if token == "10":
        return "T"
    return token if token in ("2", "3", "4", "5", "6", "7", "8", "9", "T",
                              "J", "Q", "K", "A") else None


def parse_idol_target(card: Card) -> tuple[str | None, str | None]:
    """读出 The Idol 当前指定的 (点数, 花色)。读不到返回 (None, None)。"""
    text = card.effect or ""
    if not text:
        return None, None

    suit_index, suit_code = -1, None
    for word, code in _SUIT_WORDS.items():
        index = text.find(word)
        if index >= 0 and (suit_index < 0 or index < suit_index):
            suit_index, suit_code = index, code
    if suit_code is None:
        return None, None

    best_rank: str | None = None
    best_distance: int | None = None
    for match in _RANK_TOKEN_RE.finditer(text):
        # 距离按"与花色词的最近边缘"算
        distance = min(abs(match.start() - suit_index),
                       abs(match.end() - (suit_index + 1)))
        if best_distance is None or distance < best_distance:
            rank = _normalise_rank(match.group(1))
            if rank is not None:
                best_distance, best_rank = distance, rank
    return best_rank, suit_code


# --------------------------------------------------------------------- 模型

Model = Callable[[PlayContext, Card], JokerEffect]


def _suit_mult(suit: str) -> Model:
    def model(ctx: PlayContext, card: Card) -> JokerEffect:
        return JokerEffect(mult=3.0 * ctx.count_suit(suit))
    return model


def _suit_chips(suit: str, chips: float) -> Model:
    def model(ctx: PlayContext, card: Card) -> JokerEffect:
        return JokerEffect(chips=chips * ctx.count_suit(suit))
    return model


def _hand_is(ctx: PlayContext, *names: str) -> bool:
    """打出的牌型是否满足"包含 names 中任意一个"的条件。"""
    from .cards import contains_hand

    return any(contains_hand(ctx.hand_name, name) for name in names)


def _pair(ctx: PlayContext) -> bool:
    return _hand_is(ctx, "Pair")


def _two_pair(ctx: PlayContext) -> bool:
    return _hand_is(ctx, "Two Pair")


def _three(ctx: PlayContext) -> bool:
    return _hand_is(ctx, "Three of a Kind")


def _four(ctx: PlayContext) -> bool:
    return _hand_is(ctx, "Four of a Kind")


def _straight(ctx: PlayContext) -> bool:
    return _hand_is(ctx, "Straight")


def _flush(ctx: PlayContext) -> bool:
    return _hand_is(ctx, "Flush")


def _type_mult(amount: float, *names: str) -> Model:
    def model(ctx: PlayContext, card: Card) -> JokerEffect:
        return JokerEffect(mult=amount if _hand_is(ctx, *names) else 0.0)
    return model


def _type_chips(amount: float, *names: str) -> Model:
    def model(ctx: PlayContext, card: Card) -> JokerEffect:
        return JokerEffect(chips=amount if _hand_is(ctx, *names) else 0.0)
    return model


def _scaled_xmult(default: float = 1.0) -> Model:
    """成长类乘区：当前值直接从描述里读。"""
    def model(ctx: PlayContext, card: Card) -> JokerEffect:
        return JokerEffect(xmult=parse_xmult(card, default))
    return model


def _flat_xmult(value: float) -> Model:
    def model(ctx: PlayContext, card: Card) -> JokerEffect:
        return JokerEffect(xmult=value)
    return model


def _ancient_model(ctx: PlayContext, card: Card) -> JokerEffect:
    """Ancient Joker：指定花色的每张算分牌给 x1.5。

    花色每回合轮换，但它直接写在描述文本里，所以能精确读到而不是猜。
    """
    suit = parse_suit(card)
    per_card = parse_xmult(card, 1.5)
    hits = ctx.count_suit(suit) if suit else 0
    return JokerEffect(xmult=per_card ** hits)


def _idol_model(ctx: PlayContext, card: Card) -> JokerEffect:
    """The Idol：指定的那一张牌每被打出一次就给 X2 倍率。

    目标牌（点数 + 花色）每回合变，信息只在描述文本里（见 parse_idol_target）。
    读不到目标时返回空效果——宁可当它不提分，也不要瞎猜一个点数花色，
    那会让机器人高估自己一大截。
    """
    rank, suit = parse_idol_target(card)
    if rank is None or suit is None:
        return JokerEffect()
    per_card = parse_xmult(card, 2.0)
    hits = sum(
        1 for candidate in ctx.played
        if candidate.rank == rank and _matches_suit(candidate, suit)
    )
    return JokerEffect(xmult=per_card ** hits)


JOKER_MODELS: dict[str, Model] = {
    # ---------------------------------------------------------- 常驻数值
    "j_joker": lambda ctx, c: JokerEffect(mult=4.0),
    "j_abstract": lambda ctx, c: JokerEffect(mult=3.0 * ctx.joker_count),
    "j_misprint": lambda ctx, c: JokerEffect(mult=11.5),           # 0~23 随机，取期望
    "j_half": lambda ctx, c: JokerEffect(mult=20.0 if ctx.cards_played <= 3 else 0.0),
    "j_banner": lambda ctx, c: JokerEffect(chips=30.0 * ctx.discards_left),
    "j_mystic_summit": lambda ctx, c: JokerEffect(mult=15.0 if ctx.discards_left == 0 else 0.0),
    "j_blue_joker": lambda ctx, c: JokerEffect(chips=2.0 * ctx.deck_size),
    "j_green_joker": lambda ctx, c: JokerEffect(mult=parse_mult(c, float(ctx.hand_played))),
    # Bull / Bootstraps / Stone 的描述里 "(Currently ...)" 已经是总值，不用再乘
    "j_bull": lambda ctx, c: JokerEffect(chips=parse_chips(c, 2.0 * ctx.money)),
    "j_bootstraps": lambda ctx, c: JokerEffect(mult=parse_mult(c, 2.0 * (ctx.money // 5))),
    "j_red_card": lambda ctx, c: JokerEffect(mult=parse_mult(c, 3.0)),
    "j_popcorn": lambda ctx, c: JokerEffect(mult=parse_mult(c, 20.0)),
    "j_flash": lambda ctx, c: JokerEffect(mult=parse_mult(c, 0.0)),
    "j_ceremonial": lambda ctx, c: JokerEffect(mult=parse_mult(c, 0.0)),
    "j_swashbuckler": lambda ctx, c: JokerEffect(mult=parse_mult(c, 0.0)),
    "j_stencil": lambda ctx, c: JokerEffect(
        xmult=1.0 + 1.0 * max(0, JOKER_SLOTS - ctx.joker_count)
    ),
    "j_stone": lambda ctx, c: JokerEffect(chips=parse_chips(c, 25.0)),
    "j_steel_joker": lambda ctx, c: JokerEffect(
        xmult=1.0 + 0.2 * ctx.deck_count(lambda x: x.enhancement == "STEEL")
    ),
    "j_drivers_license": lambda ctx, c: JokerEffect(
        xmult=parse_xmult(c, 1.0) if ctx.deck_count(
            lambda x: x.enhancement is not None) >= 16 else 1.0
    ),
    "j_hiker": lambda ctx, c: JokerEffect(),   # 给牌加永久筹码，见 README 的已知边界

    # ------------------------------------------------------- 按花色加成
    "j_greedy_joker": _suit_mult("D"),
    "j_lusty_joker": _suit_mult("H"),
    "j_wrathful_joker": _suit_mult("S"),
    "j_gluttenous_joker": _suit_mult("C"),
    "j_arrowhead": _suit_chips("S", 50.0),
    "j_onyx_agate": lambda ctx, c: JokerEffect(mult=7.0 * ctx.count_suit("C")),
    "j_rough_gem": lambda ctx, c: JokerEffect(money=1.0 * ctx.count_suit("D")),
    "j_bloodstone": lambda ctx, c: JokerEffect(xmult=1.25 ** ctx.count_suit("H")),
    "j_ancient": _ancient_model,
    # The Idol 的目标牌写在描述里（见 parse_idol_target）
    "j_idol": _idol_model,

    # ------------------------------------------------------- 按点数加成
    "j_scholar": lambda ctx, c: JokerEffect(chips=20.0 * ctx.count_rank("A"),
                                            mult=4.0 * ctx.count_rank("A")),
    "j_walkie_talkie": lambda ctx, c: JokerEffect(
        chips=10.0 * ctx.count_rank("T", "4"), mult=4.0 * ctx.count_rank("T", "4")),
    "j_fibonacci": lambda ctx, c: JokerEffect(mult=8.0 * ctx.count_rank("A", "2", "3", "5", "8")),
    "j_even_steven": lambda ctx, c: JokerEffect(mult=4.0 * ctx.count_parity(EVEN_RANKS)),
    "j_odd_todd": lambda ctx, c: JokerEffect(chips=31.0 * ctx.count_parity(ODD_RANKS)),
    "j_smiley": lambda ctx, c: JokerEffect(mult=5.0 * ctx.count_faces()),
    "j_scary_face": lambda ctx, c: JokerEffect(chips=30.0 * ctx.count_faces()),
    "j_photograph": lambda ctx, c: JokerEffect(xmult=2.0 if ctx.count_faces() > 0 else 1.0),
    "j_business": lambda ctx, c: JokerEffect(money=1.0 * ctx.count_faces()),
    "j_triboulet": lambda ctx, c: JokerEffect(
        xmult=parse_xmult(c, 2.0) ** (ctx.count_rank("K", "Q", "J") or 0)
    ),
    "j_wee": lambda ctx, c: JokerEffect(chips=parse_chips(c, 0.0)),
    "j_hiker": lambda ctx, c: JokerEffect(chips=5.0 * len(ctx.played)),

    # ------------------------------------------------------- 按牌型加成
    "j_jolly": _type_mult(8.0, "Pair"),
    "j_zany": _type_mult(12.0, "Three of a Kind"),
    "j_mad": _type_mult(10.0, "Two Pair"),
    "j_crazy": _type_mult(12.0, "Straight"),
    "j_droll": _type_mult(10.0, "Flush"),
    "j_sly": _type_chips(50.0, "Pair"),
    "j_wily": _type_chips(100.0, "Three of a Kind"),
    "j_clever": _type_chips(80.0, "Two Pair"),
    "j_devious": _type_chips(100.0, "Straight"),
    "j_crafty": _type_chips(80.0, "Flush"),
    "j_duo": lambda ctx, c: JokerEffect(xmult=2.0 if _pair(ctx) else 1.0),
    "j_trio": lambda ctx, c: JokerEffect(xmult=3.0 if _three(ctx) else 1.0),
    "j_family": lambda ctx, c: JokerEffect(xmult=4.0 if _four(ctx) else 1.0),
    "j_order": lambda ctx, c: JokerEffect(xmult=3.0 if _straight(ctx) else 1.0),
    "j_tribe": lambda ctx, c: JokerEffect(xmult=2.0 if _flush(ctx) else 1.0),
    "j_flower_pot": lambda ctx, c: JokerEffect(
        xmult=3.0 if all(ctx.count_suit(s) > 0 for s in SUIT_KEYS) else 1.0),
    "j_seeing_double": lambda ctx, c: JokerEffect(
        xmult=2.0 if (ctx.count_suit("C") > 0 and len(ctx.suits_in_play) > 1) else 1.0),
    "j_baseball": lambda ctx, c: JokerEffect(xmult=parse_xmult(c, 1.5) ** _uncommon_count(ctx)),

    # ----------------------------------------------------- 留在手里的牌
    "j_baron": lambda ctx, c: JokerEffect(xmult=1.5 ** ctx.held_rank("K")),
    "j_shoot_the_moon": lambda ctx, c: JokerEffect(mult=13.0 * ctx.held_rank("Q")),
    "j_raised_fist": lambda ctx, c: JokerEffect(mult=2.0 * ctx.lowest_held_rank_value),
    "j_reserved_parking": lambda ctx, c: JokerEffect(money=0.5 * ctx.held_faces()),
    "j_blackboard": lambda ctx, c: JokerEffect(
        xmult=parse_xmult(c, 3.0) if ctx.held_all_black() else 1.0),

    # --------------------------------------------------- 成长类乘区
    "j_constellation": _scaled_xmult(1.0),
    "j_hologram": _scaled_xmult(1.0),
    "j_glass": _scaled_xmult(1.0),
    "j_obelisk": _scaled_xmult(1.0),
    "j_lucky_cat": _scaled_xmult(1.0),
    "j_vampire": _scaled_xmult(1.0),
    "j_madness": _scaled_xmult(1.0),
    "j_hit_the_road": _scaled_xmult(1.0),
    "j_throwback": _scaled_xmult(1.0),
    "j_campfire": _scaled_xmult(1.0),
    "j_loyalty_card": lambda ctx, c: JokerEffect(
        xmult=1.0 + (parse_xmult(c, 4.0) - 1.0) / 5.0),   # 每 5 手触发一次，取期望

    # --------------------------------------------------- 固定乘区
    "j_cavendish": _flat_xmult(3.0),
    "j_ramen": lambda ctx, c: JokerEffect(xmult=parse_xmult(c, 2.0)),
    # Yorick：每弃掉 N 张牌涨 X1 倍率，当前总值在"（当前为 X# 倍率）"里
    "j_yorick": _scaled_xmult(1.0),
    # Erosion：牌组每比初始少一张就 +4 倍率，当前总值在"（当前为 +# 倍率）"里
    "j_erosion": lambda ctx, c: JokerEffect(mult=parse_mult(c, 0.0)),
    # Stuntman：+250 筹码（-2 手牌上限已经体现在状态的牌数里）
    "j_stuntman": lambda ctx, c: JokerEffect(chips=parse_chips(c, 250.0)),
    # Blueprint / Brainstorm 的收益由 model_joker 里"复制邻居"的逻辑给出，
    # 这里给一个兜底值（没有可复制的邻居时按 x1 处理，即收益 0）
    "j_blueprint": lambda ctx, c: JokerEffect(),
    "j_brainstorm": lambda ctx, c: JokerEffect(),
    "j_card_sharp": lambda ctx, c: JokerEffect(
        xmult=parse_xmult(c, 3.0) if ctx.hand_name in ctx.hands_played_this_round else 1.0),
    "j_acrobat": lambda ctx, c: JokerEffect(xmult=3.0 if ctx.is_final_hand else 1.0),

    # --------------------------------------------------- 条件性数值
    "j_trousers": lambda ctx, c: JokerEffect(
        mult=parse_mult(c, 0.0) if _two_pair(ctx) else 0.0),
    "j_ride_the_bus": lambda ctx, c: JokerEffect(
        mult=parse_mult(c, 0.0) if ctx.count_faces() == 0 else 0.0),
    "j_square": lambda ctx, c: JokerEffect(chips=parse_chips(c, 0.0)),
    "j_runner": lambda ctx, c: JokerEffect(
        chips=parse_chips(c, 0.0) if _straight(ctx) else 0.0),
    "j_ice_cream": lambda ctx, c: JokerEffect(chips=parse_chips(c, 100.0)),
    "j_castle": lambda ctx, c: JokerEffect(chips=parse_chips(c, 0.0)),
    "j_gros_michel": lambda ctx, c: JokerEffect(mult=15.0),
    "j_supernova": lambda ctx, c: JokerEffect(
        mult=float(ctx.hand_plays_run.get(ctx.hand_name, 0))),
}

# Stencil 需要知道总小丑牌位，这里写死常用的 5，避免引入状态依赖
JOKER_SLOTS = 5


def _uncommon_count(ctx: PlayContext) -> int:
    """其他小丑牌里稀有度为"罕见"的数量（Baseball Card 用）。"""
    from .joker_data import JOKER_DATA

    count = 0
    for key in ctx.joker_keys:
        if key == "j_baseball":
            continue
        if (JOKER_DATA.get(key) or {}).get("rarity") == 2:
            count += 1
    return count


# ------------------------------------------------------------------ 检索接口

def model_joker(
    key: str,
    ctx: PlayContext,
    card: Card,
    *,
    joker_index: int | None = None,
    jokers: tuple[Card, ...] = (),
    _depth: int = 0,
) -> JokerEffect:
    """查表算一张小丑牌的贡献；没有模型就返回零收益。

    Blueprint 和 Brainstorm 需要知道自己在哪个槽位、以及左右邻居是谁，
    所以这里接收 joker_index / jokers。Blueprint 复制右边那张、Brainstorm
    复制最左边那张——它们的收益就是被复制者在这一刻的收益
    （被复制的那张自己在原位置还会再触发一次）。
    """
    if _depth < 3 and jokers:
        copied: Card | None = None
        copied_index: int | None = None
        if key == "j_blueprint" and joker_index is not None:
            if joker_index + 1 < len(jokers):
                copied, copied_index = jokers[joker_index + 1], joker_index + 1
        elif key == "j_brainstorm":
            if jokers[0].key != "j_brainstorm" or _depth > 0:
                copied, copied_index = jokers[0], 0
        if copied is not None:
            return model_joker(
                copied.key, ctx, copied,
                joker_index=copied_index, jokers=jokers, _depth=_depth + 1,
            )

    model = JOKER_MODELS.get(key)
    if model is None:
        return JokerEffect()
    return model(ctx, card)


def unknown_jokers(keys: list[str] | tuple[str, ...]) -> list[str]:
    """筛出真正没有建模的小丑牌，用于日志提示。"""
    return sorted({
        k for k in keys
        if k not in JOKER_MODELS
        and k not in HANDLED_ELSEWHERE
        and k not in KNOWN_UNSUPPORTED
        and k not in NO_SCORE_EFFECT
    })


def retrigger_bonus(ctx: PlayContext, card: Card, index: int) -> int:
    """这张牌额外触发几次（红蜡封 + 相关小丑牌）。返回额外次数。"""
    extra = 0
    if card.has_red_seal:
        extra += 1

    keys = ctx.joker_keys
    if "j_hack" in keys and card.rank in {"2", "3", "4", "5"}:
        extra += 1
    if "j_sock_and_buskin" in keys and card.rank in FACE_RANKS:
        extra += 1
    if "j_dusk" in keys and ctx.is_final_hand:
        extra += 1
    if "j_hanging_chad" in keys and index == 0:
        extra += 2
    return extra


# --------------------------------------------------------------- 商店估值

# 买牌时的价值评估（0~10）。不是精确胜率，只是把明显更值得买的排前面。
#
# 总原则来自视频："倍率丑 > 倍乘丑 > 筹码丑"，以及"泛用性越好，抓取优先级越高。
# 例如卡文迪许 > 老千小丑 > 杂技演员"——同为 x3，无条件的（卡文迪许）比要
# 本回合重复牌型的（老千）高，只在最后一手生效的（杂技演员）最低。
# 配套的还有 strategy.py 的 synergy_adjustment()：牌型和专精目标不一致时再降一档。
JOKER_VALUE: dict[str, float] = {
    # 顶级引擎：能指数级滚起来
    "j_blueprint": 10.0, "j_brainstorm": 10.0, "j_baron": 9.5, "j_mime": 9.5,
    "j_triboulet": 9.5, "j_perkeo": 9.5, "j_invisible": 9.0, "j_dna": 9.0,
    "j_rocket": 9.0, "j_ceremonial": 8.5, "j_hologram": 8.5, "j_constellation": 8.5,
    "j_campfire": 8.5, "j_obelisk": 8.5, "j_loyalty_card": 8.0, "j_ramen": 8.0,
    "j_glass": 8.0, "j_caino": 9.0, "j_yorick": 9.0, "j_burnt": 9.0,
    # 强力倍率
    "j_steel_joker": 8.0, "j_stone": 7.0, "j_vampire": 8.0, "j_midas_mask": 7.5,
    "j_photograph": 6.0, "j_hanging_chad": 7.5, "j_sock_and_buskin": 7.5,
    "j_hack": 7.0, "j_dusk": 7.0, "j_flower_pot": 7.0, "j_card_sharp": 6.5,
    "j_seeing_double": 7.0, "j_duo": 7.5, "j_trio": 7.5, "j_family": 8.0,
    "j_order": 7.5, "j_tribe": 7.5, "j_bloodstone": 7.0, "j_arrowhead": 6.5,
    "j_onyx_agate": 6.0, "j_scholar": 5.5, "j_fibonacci": 6.5, "j_scary_face": 6.0,
    "j_smiley": 5.5, "j_even_steven": 5.0, "j_odd_todd": 5.0, "j_walkie_talkie": 5.0,
    "j_blackboard": 7.5, "j_acrobat": 5.5, "j_baseball": 8.0, "j_cavendish": 8.5,
    "j_throwback": 7.0, "j_madness": 7.5, "j_hit_the_road": 7.0, "j_lucky_cat": 7.5,
    "j_trousers": 7.0, "j_ride_the_bus": 6.0, "j_supernova": 6.5, "j_erosion": 7.0,
    "j_drivers_license": 8.0, "j_idol": 7.0, "j_ancient": 6.5, "j_matador": 5.5,
    "j_castle": 6.0, "j_ice_cream": 6.5, "j_runner": 5.5, "j_square": 5.0,
    "j_wee": 6.0, "j_gros_michel": 6.5, "j_juggler": 5.5, "j_troubadour": 6.0,
    "j_mail": 6.0, "j_luchador": 4.5, "j_mr_bones": 5.0, "j_chicot": 4.0,
    # 好的即战力 / 加成
    "j_joker": 6.0, "j_abstract": 6.5, "j_half": 8.0, "j_banner": 5.5,
    "j_mystic_summit": 5.0, "j_blue_joker": 5.0, "j_green_joker": 6.5,
    "j_bull": 6.0, "j_bootstraps": 6.5, "j_popcorn": 7.0, "j_misprint": 5.0,
    "j_jolly": 5.5, "j_zany": 5.5, "j_mad": 5.5, "j_crazy": 5.5, "j_droll": 5.5,
    "j_sly": 4.5, "j_wily": 4.5, "j_clever": 4.5, "j_devious": 4.5, "j_crafty": 4.5,
    "j_greedy_joker": 5.0, "j_lusty_joker": 5.0, "j_wrathful_joker": 5.0,
    "j_gluttenous_joker": 5.0, "j_shoot_the_moon": 6.5, "j_raised_fist": 5.5,
    "j_red_card": 5.5, "j_flash": 5.0, "j_swashbuckler": 4.0, "j_stencil": 7.5,
    "j_hiker": 6.0,
    # 关键功能牌
    "j_four_fingers": 8.5, "j_smeared": 8.5, "j_shortcut": 8.0,
    "j_pareidolia": 8.0, "j_splash": 6.0, "j_burglar": 6.5, "j_merry_andy": 6.0,
    "j_certificate": 6.0, "j_marble": 5.5, "j_delayed_grat": 4.5,
    # 经济牌
    "j_golden": 7.0, "j_to_the_moon": 7.5, "j_rough_gem": 6.5,
    "j_business": 5.5, "j_reserved_parking": 6.0, "j_egg": 4.5,
    "j_faceless": 5.0, "j_ticket": 5.5, "j_trading": 4.0, "j_seance": 3.5,
    "j_8_ball": 5.0, "j_gift": 5.5, "j_satellite": 6.5, "j_fortune_teller": 4.0,
    "j_cloud_9": 6.0, "j_astronomer": 5.0, "j_cartomancer": 6.0,
}

DEFAULT_JOKER_VALUE = 4.0


def joker_value(key: str) -> float:
    """买牌时的参考价值分。"""
    return JOKER_VALUE.get(key, DEFAULT_JOKER_VALUE)
