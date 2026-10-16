"""主 build 流的思考路径。

**阶段一：只盘点与识别，不改变任何决策。** 识别结果写进决策日志，用来验证
"流派能不能被可靠识别"这个前提；确认之后再接入买卖（阶段二）。

设计见 `docs/strategy-math.md` §16。这里实现第 0 步（盘点现状）和第 2 步
（转向判断）的**判断部分**。

---

### 两个必须绕开的坑（都是实测踩出来的）

**① 牌库特征必须"会变"。**
第一版的牌库匹配度用"人头牌占比"，而整副牌恒为 52 张、人头恒为 12 张
（`full_deck` = 抽牌堆 + 手牌 + 弃牌堆 + 出牌区，总数永远不变）→ 这个信号
局局相同，等于没提供信息。实测 10 局里 `牌库=52 人头=12` 一字不差。
现在改成**投资信号**：牌库里"被改造过"的目标牌（强化 / 蜡封 / 版本），
那才是机器人真的能改变、也确实在为某个流派投入的东西。

**② 阵容证据不能用边际贡献，会和阶段二循环依赖。**
   人头流要能被识别 → 得看到男爵在贡献
   男爵要贡献     → 锚点里得包含"留手的 K"
   锚点选哪些     → 由识别出的流派决定          ← 循环

所以识别的阵容证据用**静态成色占比**：`Σ core 的静态值 / Σ 全部小丑牌静态值`，
尺度无关（∈[0,1]）、便宜、且不依赖锚点。阶段二再用已经定下来的流派去修锚点。
"静态表不准"那条结论针对的是**买卖决策**（它看不出配合），而这里只用来在流派
之间做**相对**比较，不存在那个问题。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .cards import FACE_RANKS, Card
from .state import GameState

# ------------------------------------------------------------------ 牌库构成

# 牌库"投资信号"：这些是机器人能通过塔罗/幻灵改变、而且确实代表着"在为某个
# 流派投入"的东西。刻意不用"人头牌占比"——整副牌恒为 52 张、人头恒为 12 张，
# 那个信号永远不变（实测确认）。
_FEATURES: Mapping[str, Callable[[Card], bool]] = {
    # 人头牌被改造过（加强了 / 加了蜡封 / 带版本）= 在往人头流投
    "invested_face": lambda c: (
        c.rank in FACE_RANKS and not c.is_stone
        and bool(c.enhancement or c.seal or c.edition)
    ),
    "STEEL": lambda c: c.enhancement == "STEEL",
    "GLASS": lambda c: c.enhancement == "GLASS",
}


@dataclass(slots=True, frozen=True)
class DeckProfile:
    """可见**整副牌**的构成快照。

    用 `GameState.full_deck`（抽牌堆 + 手牌 + 弃牌堆 + 出牌区）而不是只看
    `draw_pile`——后者会漏掉已经打出/弃掉的牌，让"牌库里还有几张 STEEL / K"
    被系统性低估。见 `mod_additions/README.md` 的 gamestate 补丁。
    """

    size: int
    faces: int
    features: Mapping[str, int]
    by_rank: Mapping[str, int]
    by_suit: Mapping[str, int]
    by_enhancement: Mapping[str, int]
    seals: int
    editions: int

    @classmethod
    def from_cards(cls, cards: Iterable[Card]) -> "DeckProfile":
        materialised = list(cards)
        ranks: Counter[str] = Counter()
        suits: Counter[str] = Counter()
        enhancements: Counter[str] = Counter()
        features: Counter[str] = Counter()
        faces = seals = editions = 0
        for card in materialised:
            if card.rank:
                ranks[card.rank] += 1
            if card.suit:
                suits[card.suit] += 1
            if card.enhancement:
                enhancements[card.enhancement] += 1
            if card.rank in FACE_RANKS and not card.is_stone:
                faces += 1
            if card.seal:
                seals += 1
            if card.edition:
                editions += 1
            for name, predicate in _FEATURES.items():
                if predicate(card):
                    features[name] += 1
        return cls(
            size=len(materialised),
            faces=faces,
            features=dict(features),
            by_rank=dict(ranks),
            by_suit=dict(suits),
            by_enhancement=dict(enhancements),
            seals=seals,
            editions=editions,
        )

    def count_of(self, feature: str | None) -> int:
        """牌库里满足某个投资特征的张数。"""
        if feature is None:
            return 0
        return self.features.get(feature, 0)

    def share_of(self, feature: str | None) -> float:
        """投资特征的**占比**（0~1），用来做尺度无关的流派匹配。"""
        if not feature or self.size <= 0:
            return 0.0
        return self.count_of(feature) / self.size


# ------------------------------------------------------------------ 流派定义


@dataclass(slots=True, frozen=True)
class Archetype:
    """一个 build 流派。

    `core`     持有这些牌才谈得上走这个流派（硬前提）
    `feature`  牌库里的**投资特征**（见 `_FEATURES`）；None 表示不依赖牌库改造
    """

    key: str
    label: str
    core: frozenset[str]
    feature: str | None = None
    note: str = ""


_ARCHETYPES: tuple[Archetype, ...] = (
    Archetype(
        "hand", "牌型流",
        frozenset({
            "j_jolly", "j_zany", "j_mad", "j_crazy", "j_droll",
            "j_sly", "j_wily", "j_clever", "j_devious", "j_crafty",
            "j_duo", "j_trio", "j_family", "j_order", "j_tribe",
        }),
        note="靠牌型触发；等级由星球牌喂起来，不依赖牌库改造",
    ),
    Archetype(
        "face", "人头流",
        frozenset({
            "j_baron", "j_mime", "j_triboulet", "j_shoot_the_moon",
            "j_photograph", "j_smiley", "j_scary_face",
        }),
        feature="invested_face",
        note="K/Q/J 要在**手里**（男爵是手里每张 K ×1.5），不是打出去",
    ),
    Archetype(
        "steel", "钢铁流",
        frozenset({"j_steel_joker"}),
        feature="STEEL",
        note="手里留 STEEL；最吃「弃牌堆可见」这条（见 gamestate 补丁）",
    ),
    Archetype(
        "glass", "玻璃流",
        frozenset({"j_glass", "j_lucky_cat"}),
        feature="GLASS",
        note="GLASS 算分时 ×2，要在**打出的牌**里",
    ),
    Archetype(
        "money", "经济流",
        frozenset({"j_bull", "j_bootstraps", "j_to_the_moon", "j_rocket", "j_golden"}),
        note="读 money，不依赖牌面构成",
    ),
    Archetype(
        "splash", "全牌流",
        frozenset({"j_splash"}),
        note="所有打出的牌都算分，配合每张计数类（Fibonacci / Scholar…）",
    ),
)

ARCHETYPES: Mapping[str, Archetype] = {a.key: a for a in _ARCHETYPES}


# ------------------------------------------------------------------ 识别


@dataclass(slots=True, frozen=True)
class ArchetypeScore:
    key: str
    label: str
    support: float
    lineup_share: float               # 阵容证据：核心牌占阵容静态成色的比例
    deck_invest: float                # 牌库投资占比
    lineup_jokers: tuple[str, ...]    # 命中的核心牌（证据，进日志）

    def describe(self) -> str:
        return f"{self.label} {self.support:.2f}"


@dataclass(slots=True, frozen=True)
class BuildSnapshot:
    """第 0 步的产出：现状盘点 + 流派识别结果。"""

    hand_type: str | None
    hand_lead: int
    deck: DeckProfile
    scores: tuple[ArchetypeScore, ...]
    committed: str | None

    def top(self, n: int = 2) -> tuple[ArchetypeScore, ...]:
        return self.scores[:n]

    def numbers(self) -> dict[str, Any]:
        """给决策日志用的一行数字。"""
        invest = ", ".join(
            f"{name}:{count}" for name, count in sorted(self.deck.features.items())
            if count
        )
        numbers: dict[str, Any] = {
            "牌型": self.hand_type or "?",
            "等级领先": self.hand_lead,
            "牌库": self.deck.size,
            "人头": self.deck.faces,
        }
        if invest:
            numbers["改造"] = invest
        for score in self.scores[:2]:
            numbers[score.label] = f"{score.support:.2f}"
        if self.committed:
            numbers["主流派"] = ARCHETYPES[self.committed].label
        return numbers


def _default_params() -> Any:
    """默认参数。

    放在函数里取是为了避免和 `strategy` 形成模块级循环导入——`strategy.py`
    里才有 `DEFAULT_PARAMS`（它已经载入过 `runs/params.json`）。
    """
    from .strategy import DEFAULT_PARAMS

    return DEFAULT_PARAMS


def _lineup_shares(keys: Sequence[str]) -> dict[str, float]:
    """每个流派核心牌的**静态成色占比**。

    ```text
    share(A) = Σ_{j ∈ core(A) ∩ 阵容} 静态值(j) / Σ_{所有 j ∈ 阵容} 静态值(j)
    ```

    用占比而不是绝对分，是为了**尺度无关**——它回答的是"阵容的成色有多大
    比例压在这个方向上"，而不是"这个方向值多少分"。
    """
    from .jokers import joker_value

    values = {key: max(0.0, joker_value(key)) for key in keys}
    total = sum(values.values())
    if total <= 0:
        return {}
    shares: dict[str, float] = {}
    for archetype in _ARCHETYPES:
        hits = [k for k in keys if k in archetype.core]
        if not hits:
            continue
        shares[archetype.key] = sum(values[k] for k in hits) / total
    return shares


def archetype_scores(
    state: GameState,
    *,
    deck: DeckProfile | None = None,
    params: Any = None,
) -> list[ArchetypeScore]:
    """给每个流派打支持度，降序返回。

    ```text
    support(A) = 阵容成色占比(A) + λ · 牌库投资占比(A)
    ```

    **硬前提**：`core(A) ∩ 阵容 = ∅` 的流派直接不进候选——没有核心牌就谈不上
    走这条路。纯连续值在边界上会突然翻转，加个硬前提更好调试。
    """
    params = params or _default_params()
    deck = deck or DeckProfile.from_cards(state.full_deck)
    keys = [card.key for card in state.jokers.cards]
    shares = _lineup_shares(keys)

    scores: list[ArchetypeScore] = []
    for archetype in _ARCHETYPES:
        if archetype.key not in shares:
            continue                                   # 硬前提：没有核心牌
        hits = tuple(k for k in keys if k in archetype.core)
        deck_invest = deck.share_of(archetype.feature)
        scores.append(
            ArchetypeScore(
                key=archetype.key,
                label=archetype.label,
                support=shares[archetype.key] + params.build_deck_weight * deck_invest,
                lineup_share=shares[archetype.key],
                deck_invest=deck_invest,
                lineup_jokers=hits,
            )
        )
    scores.sort(key=lambda s: s.support, reverse=True)
    return scores


def commit_archetype(
    scores: Sequence[ArchetypeScore],
    previous: str | None,
    params: Any = None,
) -> str | None:
    """第 2 步的**承诺**：带迟滞地决定主流派。

    **语义要点**：主流派是一份**计划**，不是对当前阵容的**描述**。
    它不该因为阵容临时变化（买进/卖掉一张牌）就消失——那是"没有主见"，
    而实测正是这样：11 局最终全部退回"未承诺"，轨迹是
    `— → 人头流@16 → 牌型流@38 → —`，即**能承诺但守不住**。

    ```text
    没有承诺过         → 支持度 ≥ build_min_support 才承诺
    最高就是承诺过的    → 保持
    否则               → 新方向要**明显压过**现有计划才换：
                          现有计划还在 → 高出 previous · (1 + margin)
                          现有计划暂时不可见 → 高出 build_switch_floor
    阵容暂时空了        → 保持承诺（计划不因一次买卖而作废）
    ```

    迟滞的理由和 `committed_hand` 的锁定一样：投资方向来回摆会把好不容易
    攒起来的等级/牌位全掀翻。
    """
    params = params or _default_params()
    if not scores:
        return previous                    # 阵容暂时为空 → 守住计划
    best = scores[0]
    if previous is None:
        return best.key if best.support >= params.build_min_support else None
    if best.key == previous:
        return previous

    current = next((s for s in scores if s.key == previous), None)
    if current is None:
        # 承诺的流派暂时没有核心牌了：新方向得自己有足够成色才值得改计划
        return best.key if best.support >= params.build_switch_floor else previous
    if best.support > current.support * (1.0 + params.build_switch_margin):
        return best.key
    return previous


def hand_lead(state: GameState, params: Any = None) -> int:
    """最高牌型等级 − 第二高等级（软筛和流派识别都用这个量）。"""
    levels = sorted((info.level for info in state.hands.values()), reverse=True)
    if len(levels) < 2:
        return 0
    return levels[0] - levels[1]


def build_snapshot(
    state: GameState,
    *,
    previous: str | None = None,
    params: Any = None,
) -> BuildSnapshot:
    """第 0 步 + 第 2 步的判断部分，合成一个快照。"""
    from .strategy import committed_hand

    params = params or _default_params()
    deck = DeckProfile.from_cards(state.full_deck)
    scores = archetype_scores(state, deck=deck, params=params)
    return BuildSnapshot(
        hand_type=committed_hand(state, params),
        hand_lead=hand_lead(state, params),
        deck=deck,
        scores=tuple(scores),
        committed=commit_archetype(scores, previous, params),
    )
