"""状态解析测试。

这里的用例直接来自对真机的观察（tools/inspect_state.py 的输出），
所以它们钉住的是"游戏实际怎么返回"，而不是文档怎么写。
"""

from __future__ import annotations

from balatro_bot.state import GameState

from conftest import card, hands_table, make_state


def with_blind_status(raw: dict, statuses: dict[str, str]) -> dict:
    for name, status in statuses.items():
        raw["blinds"][name]["status"] = status
    return raw


def test_current_blind_uses_current_status() -> None:
    """出牌阶段：status=CURRENT 的盲注就是正在打的。"""
    raw = with_blind_status(
        make_state(blind_type="BOSS"), {"boss": "CURRENT", "small": "DEFEATED"}
    )
    state = GameState(raw)
    assert state.current_blind.type == "BOSS"
    assert state.required_score == 600


def test_current_blind_uses_select_status_in_blind_select() -> None:
    """选盲注阶段：待打的盲注 status=SELECT，且此时 round_num 是 0。

    这是一个真实踩过的坑：曾经的兜底逻辑按 round_num 推断，0 匹配不到任何
    分支于是落到 Boss，导致底注 1 就以为要打 600 分的 Boss。
    """
    raw = with_blind_status(
        make_state(state="BLIND_SELECT"),
        {"small": "SELECT", "big": "UPCOMING", "boss": "UPCOMING"},
    )
    raw["round_num"] = 0
    state = GameState(raw)

    assert state.current_blind.type == "SMALL"
    assert state.current_blind.name == "Small Blind"
    assert state.required_score == 300


def test_current_blind_prefers_current_over_select() -> None:
    raw = with_blind_status(
        make_state(state="SELECTING_HAND"), {"small": "CURRENT", "big": "SELECT"}
    )
    state = GameState(raw)
    assert state.current_blind.type == "SMALL"


def test_current_blind_falls_back_by_round_number() -> None:
    """mod 没给任何状态时，按回合数推断——0 应该当成第一个盲注而不是 Boss。"""
    raw = with_blind_status(make_state(), {})
    for name in ("small", "big", "boss"):
        raw["blinds"][name]["status"] = "UPCOMING"

    raw["round_num"] = 0
    assert GameState(raw).current_blind.type == "SMALL"

    raw["round_num"] = 2
    assert GameState(raw).current_blind.type == "BIG"

    raw["round_num"] = 3
    assert GameState(raw).current_blind.type == "BOSS"


def test_score_remaining_uses_round_chips() -> None:
    state = GameState(make_state(chips=120, required=300))
    assert state.score == 120
    assert state.required_score == 300
    assert state.score_remaining == 180


def test_score_remaining_never_negative() -> None:
    state = GameState(make_state(chips=999, required=300))
    assert state.score_remaining == 0


def test_hands_table_is_cached() -> None:
    """算分每一步都要读牌型表，必须缓存（否则一次决策会重复解析上万次）。"""
    state = GameState(make_state())
    assert state.hands is state.hands
    assert state.round is state.round
    assert state.blinds is state.blinds
    assert state.current_blind is state.current_blind
    assert state.hand is state.hand


def test_hand_base_reads_leveled_values() -> None:
    table = hands_table(overrides={"Flush": (90, 12)})
    state = GameState(make_state(hands=table))
    assert state.hand_base("Flush") == (90, 12)


def test_hand_base_case_insensitive_fallback() -> None:
    """牌型名大小写不一致时也应该能匹配上，而不是静默退回兜底值。"""
    table = hands_table()
    table["pair"] = table.pop("Pair")
    state = GameState(make_state(hands=table))
    assert state.hand_base("Pair") == (10, 2)


def test_hand_base_falls_back_when_table_empty() -> None:
    state = GameState(make_state(hands={}))
    assert state.hand_base("Four of a Kind") == (60, 7)
    assert state.hand_base("不存在的牌型") == (5, 1)


def test_missing_areas_are_empty_not_errors() -> None:
    """不同状态下 mod 会省略不同区域，必须容错。"""
    raw = make_state()
    for name in ("hand", "jokers", "shop", "vouchers", "packs", "pack", "cards",
                 "consumables"):
        raw.pop(name, None)
    state = GameState(raw)
    assert len(state.hand.cards) == 0
    assert len(state.jokers.cards) == 0
    assert len(state.draw_pile.cards) == 0
    assert state.joker_slots_free == 0
    assert state.booster_slots_free == 0


def test_slot_counts() -> None:
    raw = make_state(jokers=[{"id": 1, "key": "j_joker", "set": "JOKER", "label": "J",
                              "value": {"effect": ""},
                              "modifier": {"seal": None, "edition": None,
                                           "enhancement": None, "eternal": False,
                                           "perishable": None, "rental": False},
                              "state": {"debuff": False, "hidden": False,
                                        "highlight": False},
                              "cost": {"sell": 1, "buy": 5}}])
    raw["jokers"]["limit"] = 5
    state = GameState(raw)
    assert state.joker_count == 1
    assert state.joker_slots_free == 4


def test_deck_and_pile_parsing() -> None:
    state = GameState(make_state(
        hand=[card("H_A"), card("D_K")],
        deck=[card("S_2"), card("C_3")],
    ))
    assert [c.key for c in state.hand.cards] == ["H_A", "D_K"]
    assert [c.key for c in state.draw_pile.cards] == ["S_2", "C_3"]
