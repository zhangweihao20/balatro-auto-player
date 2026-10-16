"""测试用的构造工具：直接拼出 API 会返回的那种 dict。"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

# Balatro 1 级牌型基础分，用于测试时构造 hands 表
STANDARD_HANDS: dict[str, tuple[int, int, int]] = {
    # name: (order, chips, mult)
    "Flush Five": (1, 160, 16),
    "Flush House": (2, 140, 14),
    "Five of a Kind": (3, 120, 12),
    "Straight Flush": (4, 100, 8),
    "Four of a Kind": (5, 60, 7),
    "Full House": (6, 40, 4),
    "Flush": (7, 35, 4),
    "Straight": (8, 30, 4),
    "Three of a Kind": (9, 30, 3),
    "Two Pair": (10, 20, 2),
    "Pair": (11, 10, 2),
    "High Card": (12, 5, 1),
}

_next_id = [0]


def _new_id() -> int:
    _next_id[0] += 1
    return _next_id[0]


def card(
    key: str,
    *,
    enhancement: str | None = None,
    edition: str | None = None,
    seal: str | None = None,
    debuff: bool = False,
    label: str | None = None,
) -> dict[str, Any]:
    """构造一张普通牌。key 形如 'H_A'。"""
    return {
        "id": _new_id(),
        "key": key,
        "set": "ENHANCED" if enhancement else "DEFAULT",
        "label": label or key,
        "value": {"suit": key[0], "rank": key[2:], "effect": ""},
        "modifier": {
            "seal": seal, "edition": edition, "enhancement": enhancement,
            "eternal": False, "perishable": None, "rental": False,
        },
        "state": {"debuff": debuff, "hidden": False, "highlight": False},
        "cost": {"sell": 1, "buy": 0},
    }


def joker(key: str, *, edition: str | None = None, label: str | None = None,
          effect: str = "") -> dict[str, Any]:
    """构造一张小丑牌。key 形如 'j_greedy_joker'。

    effect 对应真机上 mod 返回的卡牌描述文本（Card:generate_UIBox_ability_table
    的文字），成长类小丑牌的当前数值就在里面，模型会去解析它。
    """
    return {
        "id": _new_id(),
        "key": key,
        "set": "JOKER",
        "label": label or key,
        "value": {"effect": effect},
        "modifier": {
            "seal": None, "edition": edition, "enhancement": None,
            "eternal": False, "perishable": None, "rental": False,
        },
        "state": {"debuff": False, "hidden": False, "highlight": False},
        "cost": {"sell": 1, "buy": 5},
    }


def consumable(key: str, *, card_set: str = "TAROT", cost: int = 3,
               label: str | None = None) -> dict[str, Any]:
    """构造一张消耗牌（塔罗 / 星球 / 幻灵）。"""
    return {
        "id": _new_id(),
        "key": key,
        "set": card_set,
        "label": label or key,
        "value": {"effect": ""},
        "modifier": {
            "seal": None, "edition": None, "enhancement": None,
            "eternal": False, "perishable": None, "rental": False,
        },
        "state": {"debuff": False, "hidden": False, "highlight": False},
        "cost": {"sell": 1, "buy": cost},
    }


def area(cards: Sequence[dict[str, Any]], *, limit: int = 0) -> dict[str, Any]:
    return {"count": len(cards), "limit": limit, "cards": list(cards)}


def hands_table(
    overrides: dict[str, tuple[int, int]] | None = None,
    levels: dict[str, int] | None = None,
    played: dict[str, int] | None = None,
) -> dict[str, Any]:
    """构造 hands 表。

    `overrides` 覆盖某几个牌型的 (chips, mult) 来模拟升级；
    `levels` 单独设置等级；`played` 设置"本局打过几次"。

    软筛同时看**等级**和**打出次数**——只按等级构造测试会漏掉真实对局里的情况
    （`lock_planet_type` 让其它牌型永远升不了级），第一版就是这么漏的。
    """
    table: dict[str, Any] = {}
    for name, (order, chips, mult) in STANDARD_HANDS.items():
        if overrides and name in overrides:
            chips, mult = overrides[name]
        table[name] = {
            "order": order,
            "level": (levels or {}).get(name, 1),
            "chips": chips, "mult": mult,
            "played": (played or {}).get(name, 0), "played_this_round": 0,
        }
    return table


def make_state(
    hand: Iterable[dict[str, Any]] = (),
    jokers: Iterable[dict[str, Any]] = (),
    *,
    hands: dict[str, Any] | None = None,
    chips: int = 0,
    required: int = 300,
    money: int = 4,
    discards_left: int = 3,
    discards_used: int = 0,
    hands_left: int = 4,
    hands_played: int = 0,
    deck: Iterable[dict[str, Any]] = (),
    state: str = "SELECTING_HAND",
    blind_type: str = "SMALL",
) -> dict[str, Any]:
    hand_list = list(hand)
    joker_list = list(jokers)
    deck_list = list(deck)
    return {
        "state": state,
        "round_num": 1,
        "ante_num": 1,
        "money": money,
        "deck": "RED",
        "stake": "WHITE",
        "seed": "TEST",
        "won": False,
        "used_vouchers": {},
        "hands": hands_table() if hands is None else hands,
        "round": {
            "hands_left": hands_left,
            "hands_played": hands_played,
            "discards_left": discards_left,
            "discards_used": discards_used,
            "reroll_cost": 5,
            "chips": chips,
        },
        "blinds": {
            "small": {
                "type": "SMALL", "status": "CURRENT", "name": "Small Blind",
                "effect": "No special effect", "score": required,
                "tag_name": "Tag", "tag_effect": "effect",
            },
            "big": {
                "type": "BIG", "status": "UPCOMING", "name": "Big Blind",
                "effect": "No special effect", "score": 450,
            },
            "boss": {
                "type": "BOSS", "status": "UPCOMING", "name": "The Hook",
                "effect": "Discards 2 random cards per hand played", "score": 600,
            },
        },
        "jokers": area(joker_list, limit=5),
        "consumables": area([], limit=2),
        "hand": area(hand_list, limit=8),
        "cards": area(deck_list),
        "shop": area([]),
        "vouchers": area([]),
        "packs": area([]),
        "pack": area([]),
    }
