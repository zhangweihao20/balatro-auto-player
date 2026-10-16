"""把 API 返回的原始 dict 包成只读、好用的对象。

GameState 是"某一瞬间的快照"，因此内部会缓存解析结果（牌型表、各牌区）。
这一点很重要：算分的每一步都要读牌型基础分，如果不缓存，一次弃牌决策里
会重复解析同一张表上万次（实测占了 45% 的时间）。

缓存意味着**调用方不能修改返回的对象**（牌区的 cards 列表等）。只读使用即可。
状态变化后要重新取一次 gamestate 构造新的 GameState，不要在原对象上改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from .cards import BASE_HAND_SCORE, Card, HAND_NAMES


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class Area:
    """牌区。下标就是 play/discard/buy 等方法要用的索引。"""

    cards: list[Card] = field(default_factory=list)
    count: int = 0
    limit: int = 0
    highlighted_limit: int = 0

    @classmethod
    def from_raw(cls, raw: Any) -> "Area":
        if not isinstance(raw, dict):
            return cls()
        cards = [Card.from_dict(c) for c in (raw.get("cards") or []) if isinstance(c, dict)]
        return cls(
            cards=cards,
            count=_int(raw.get("count"), len(cards)),
            limit=_int(raw.get("limit")),
            highlighted_limit=_int(raw.get("highlighted_limit")),
        )

    def __len__(self) -> int:
        return len(self.cards)

    def __iter__(self) -> Iterator[Card]:
        return iter(self.cards)

    def __getitem__(self, index: int) -> Card:
        return self.cards[index]

    @property
    def is_full(self) -> bool:
        return bool(self.limit) and len(self.cards) >= self.limit


@dataclass(slots=True)
class Blind:
    type: str = "SMALL"
    status: str = "UPCOMING"
    name: str = ""
    effect: str = ""
    score: int = 0
    tag_name: str | None = None
    tag_effect: str | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "Blind":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            type=str(raw.get("type", "SMALL")),
            status=str(raw.get("status", "UPCOMING")),
            name=str(raw.get("name", "")),
            effect=str(raw.get("effect", "")),
            score=_int(raw.get("score")),
            tag_name=raw.get("tag_name"),
            tag_effect=raw.get("tag_effect"),
        )


@dataclass(slots=True)
class HandInfo:
    """某一种牌型的当前数值（已含等级加成）。"""

    name: str
    order: int = 0
    level: int = 1
    chips: int = 0
    mult: int = 0
    played: int = 0
    played_this_round: int = 0

    @property
    def base_score(self) -> int:
        return self.chips * self.mult


@dataclass(slots=True)
class Round:
    hands_left: int = 0
    hands_played: int = 0
    discards_left: int = 0
    discards_used: int = 0
    reroll_cost: int = 0
    chips: int = 0


class GameState:
    """一次快照。构造代价很低，状态变化后重新取即可。"""

    __slots__ = ("raw", "_areas", "_hands", "_round", "_blinds", "_current_blind")

    def __init__(self, raw: dict[str, Any]) -> None:
        if not isinstance(raw, dict):
            raise TypeError(f"GameState 需要 dict，收到 {type(raw).__name__}")
        self.raw = raw
        self._areas: dict[str, Area] = {}
        self._hands: dict[str, HandInfo] | None = None
        self._round: Round | None = None
        self._blinds: dict[str, Blind] | None = None
        self._current_blind: Blind | None = None

    # ------------------------------------------------------------ 基本字段

    @property
    def state(self) -> str:
        return str(self.raw.get("state", "UNKNOWN"))

    @property
    def ante(self) -> int:
        return _int(self.raw.get("ante_num"), 1)

    @property
    def round_num(self) -> int:
        return _int(self.raw.get("round_num"), 1)

    @property
    def money(self) -> int:
        return _int(self.raw.get("money"))

    @property
    def seed(self) -> str:
        return str(self.raw.get("seed", ""))

    @property
    def deck(self) -> str:
        return str(self.raw.get("deck", ""))

    @property
    def stake(self) -> str:
        return str(self.raw.get("stake", ""))

    @property
    def won(self) -> bool:
        return bool(self.raw.get("won", False))

    @property
    def is_game_over(self) -> bool:
        return self.state == "GAME_OVER"

    # -------------------------------------------------------------- 各个区域

    def _area(self, name: str) -> Area:
        cached = self._areas.get(name)
        if cached is None:
            cached = Area.from_raw(self.raw.get(name))
            self._areas[name] = cached
        return cached

    @property
    def hand(self) -> Area:
        return self._area("hand")

    @property
    def jokers(self) -> Area:
        return self._area("jokers")

    @property
    def consumables(self) -> Area:
        return self._area("consumables")

    @property
    def shop(self) -> Area:
        return self._area("shop")

    @property
    def vouchers(self) -> Area:
        """商店货架上**待售**的优惠券。"""
        return self._area("vouchers")

    @property
    def used_vouchers(self) -> frozenset[str]:
        """本局**已经买过**的优惠券（key 集合）。

        注意和 `vouchers` 不是一回事：那个是货架上待售的。

        mod 上报的是 `{voucher_key: description}` 字典（gamestate.lua:757），
        这里只取 key。某些优惠券会改变全局参数，机器人必须知道自己在不在
        这些状态里——典型的是 Telescope：它让天体包**第一张保底**是
        "打得最多的牌型"的星球牌，命中率从 25% 直接变成 100%。
        """
        raw = self.raw.get("used_vouchers")
        if not isinstance(raw, dict):
            return frozenset()
        return frozenset(str(key) for key in raw)

    def owns_voucher(self, *keys: str) -> bool:
        """是否持有其中任意一张优惠券。"""
        owned = self.used_vouchers
        return any(key in owned for key in keys)

    @property
    def packs(self) -> Area:
        return self._area("packs")

    @property
    def open_pack(self) -> Area:
        return self._area("pack")

    @property
    def draw_pile(self) -> Area:
        """牌堆剩余的牌。注意这里只有组成，顺序未知。"""
        return self._area("cards")

    @property
    def discard_pile(self) -> Area:
        """已打出 / 已弃掉的牌（`G.discard`）。

        原版 mod 不导出这个区，本项目的 gamestate 补丁加的（见
        `mod_additions/README.md`）。少了它，"牌库里还有几张 STEEL / K"
        会被系统性低估——钢铁流、人头流这类靠牌库计数的判断会直接判错。
        """
        return self._area("discard")

    @property
    def play_area(self) -> Area:
        """正在结算 / 刚打完的牌（`G.play`）。同样是本项目的补丁加的。"""
        return self._area("play")

    @property
    def full_deck(self) -> tuple[Card, ...]:
        """**整副牌** = 抽牌堆 + 手牌 + 弃牌堆 + 出牌区。

        四个区在游戏里是互斥的，所以合起来就是完整的一副牌。用它做
        "牌库构成"统计才准确——只用 `draw_pile` 会漏掉已经打出/弃掉的牌。
        按 `id` 去重只是兜底（正常不会重叠）。
        """
        seen: set[Any] = set()
        out: list[Card] = []
        for area in (self.draw_pile, self.hand, self.discard_pile, self.play_area):
            for card in area.cards:
                if card.id in seen:
                    continue
                seen.add(card.id)
                out.append(card)
        return tuple(out)

    # ------------------------------------------------------------ 回合与盲注

    @property
    def round(self) -> Round:
        if self._round is None:
            raw = self.raw.get("round")
            if not isinstance(raw, dict):
                self._round = Round()
            else:
                self._round = Round(
                    hands_left=_int(raw.get("hands_left")),
                    hands_played=_int(raw.get("hands_played")),
                    discards_left=_int(raw.get("discards_left")),
                    discards_used=_int(raw.get("discards_used")),
                    reroll_cost=_int(raw.get("reroll_cost")),
                    chips=_int(raw.get("chips")),
                )
        return self._round

    @property
    def blinds(self) -> dict[str, Blind]:
        if self._blinds is None:
            raw = self.raw.get("blinds")
            if not isinstance(raw, dict):
                self._blinds = {}
            else:
                self._blinds = {k: Blind.from_raw(v) for k, v in raw.items()}
        return self._blinds

    @property
    def current_blind(self) -> Blind:
        """当前（或即将开打）的盲注。

        实测真机的 status 取值：
            * 出牌阶段        → CURRENT
            * 选盲注阶段      → SELECT（待打的那个）
            * 其余            → UPCOMING / DEFEATED / SKIPPED
        注意 BLIND_SELECT 时 round_num 是 **0**，所以不能拿它当唯一依据，
        否则底注 1 会被误判成 Boss（历史上这里真的踩过这个坑）。
        """
        if self._current_blind is None:
            found: Blind | None = None
            for wanted in ("CURRENT", "SELECT"):
                for blind in self.blinds.values():
                    if blind.status == wanted:
                        found = blind
                        break
                if found is not None:
                    break
            if found is None:
                fallback = {1: "small", 2: "big", 3: "boss"}.get(self.round_num, "small")
                found = self.blinds.get(fallback, Blind())
            self._current_blind = found
        return self._current_blind

    @property
    def required_score(self) -> int:
        """本回合需要达到的分数。"""
        return self.current_blind.score

    @property
    def score(self) -> int:
        """本回合已经拿到的分数。"""
        return self.round.chips

    @property
    def score_remaining(self) -> int:
        return max(0, self.required_score - self.score)

    # ---------------------------------------------------------------- 牌型表

    @property
    def hands(self) -> dict[str, HandInfo]:
        """牌型表。已缓存——返回的对象是共享的，不要修改。"""
        if self._hands is None:
            raw = self.raw.get("hands")
            if not isinstance(raw, dict):
                self._hands = {}
            else:
                self._hands = {
                    name: HandInfo(
                        name=name,
                        order=_int(value.get("order")),
                        level=_int(value.get("level"), 1),
                        chips=_int(value.get("chips")),
                        mult=_int(value.get("mult")),
                        played=_int(value.get("played")),
                        played_this_round=_int(value.get("played_this_round")),
                    )
                    for name, value in raw.items()
                    if isinstance(value, dict)
                }
        return self._hands

    def hand_base(self, name: str) -> tuple[int, int]:
        """取牌型的基础 (chips, mult)。

        以状态里的 hands 表为准（它已含等级加成）；读不到就退回一级基础值。
        牌型名对不上时退回兜底表而不是报错，免得一个小改名就让整个机器人崩溃。
        """
        table = self.hands
        info = table.get(name)
        if info is None:
            for candidate, value in table.items():
                if candidate.lower() == name.lower():
                    info = value
                    break
        if info is not None and (info.chips or info.mult):
            return info.chips, info.mult
        return BASE_HAND_SCORE.get(name, (5, 1))

    def hand_level(self, name: str) -> int:
        info = self.hands.get(name)
        return info.level if info else 1

    def known_hand_names(self) -> list[str]:
        table = self.hands
        if table:
            return [n for n in HAND_NAMES if n in table] or list(table)
        return list(HAND_NAMES)

    # ---------------------------------------------------------------- 便捷判断

    @property
    def joker_count(self) -> int:
        return len(self.jokers)

    @property
    def joker_slots_free(self) -> int:
        area = self.jokers
        return max(0, area.limit - len(area)) if area.limit else 0

    @property
    def consumable_slots_free(self) -> int:
        area = self.consumables
        return max(0, area.limit - len(area)) if area.limit else 0

    @property
    def booster_slots_free(self) -> int:
        area = self.packs
        return max(0, area.limit - len(area)) if area.limit else 0

    @property
    def money_after_interest(self) -> int:
        """下一回合的利息（每 5 块 1 块，上限 5 块）。"""
        return min(5, self.money // 5)

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志
        return (
            f"<GameState {self.state} ante={self.ante} round={self.round_num} "
            f"money={self.money} score={self.score}/{self.required_score}>"
        )
