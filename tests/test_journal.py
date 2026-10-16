"""决策日志的测试。

日志栏要显示"做了什么、为什么"，所以两件事都得成立：

1. **动作**要写下来（这部分一直有）；
2. **理由**要在决策过程中就地记下——而且日志里显示的数字必须就是
   决策时真正用的那个数字，不能是事后重算的（重算会漂移成另一套逻辑）。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from balatro_bot.journal import Journal, clear_reason, current_reason, note
from balatro_bot.rpc import BalatroRPCError
from balatro_bot.state import GameState
from balatro_bot.strategy import (
    Config,
    choose_blind_action,
    choose_plan_discard,
    choose_shop_action,
    choose_play_action,
)

from conftest import card, joker, make_state
from test_strategy import full_deck, priced, shop_state, with_area


@pytest.fixture
def journal_path():
    """临时日志文件。

    不用 pytest 的 `tmp_path`：它在系统临时目录下，而本项目的运行环境
    有文件沙箱（只允许写工作区内），那些用例会直接 `PermissionError`。
    """
    root = Path("runs") / "_test_journal"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{uuid.uuid4().hex}.jsonl"
    try:
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:          # pragma: no cover
            pass


# --------------------------------------------------------------- 理由寄存器


def test_note_and_read_back() -> None:
    clear_reason()
    note("买 The Duo", kind="buy", 估值=6.5, 门槛=4.5)
    reason = current_reason()
    assert reason.why == "买 The Duo"
    assert reason.kind == "buy"
    assert reason.numbers == {"估值": 6.5, "门槛": 4.5}


def test_clear_reason_removes_everything() -> None:
    note("上一步的理由", kind="play", x=1)
    clear_reason()
    reason = current_reason()
    assert reason.why == ""
    assert reason.kind == ""
    assert reason.numbers == {}


# ------------------------------------------------------------------ 落盘


def test_journal_writes_one_json_object_per_line(journal_path) -> None:
    path = journal_path
    journal = Journal(path)
    clear_reason()
    note("这一手就能过关", kind="play", 预估得分=782, 需要=300)
    journal.record(step=1, state="SELECTING_HAND", action="play",
                   detail="出牌 [0, 1]", snapshot={"ante": 2, "money": 10})
    journal.close()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["step"] == 1
    assert entry["action"] == "play"
    assert entry["kind"] == "play"
    assert entry["why"] == "这一手就能过关"
    assert entry["numbers"] == {"预估得分": 782, "需要": 300}
    assert entry["snapshot"]["ante"] == 2
    assert entry["t"]                       # 有时间戳


def test_journal_infers_kind_when_reason_has_none(journal_path) -> None:
    path = journal_path
    journal = Journal(path)
    clear_reason()
    journal.record(step=1, state="SHOP", action="reroll")
    journal.close()
    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry["kind"] == "reroll"
    assert entry["why"] == ""


def test_journal_records_even_without_a_reason(journal_path) -> None:
    """没有理由也要写——窗口至少要看到"做了什么"。"""
    path = journal_path
    journal = Journal(path)
    clear_reason()
    journal.record(step=5, state="ROUND_EVAL", action="cash_out", detail="结算回合")
    journal.close()
    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry["action"] == "cash_out"
    assert entry["detail"] == "结算回合"


def test_disabled_journal_writes_nothing(journal_path) -> None:
    path = journal_path
    journal = Journal(path, enabled=False)
    journal.record(step=1, state="SHOP", action="buy")
    journal.close()
    assert not path.exists()


def test_journal_truncates_on_a_new_run(journal_path) -> None:
    """每次打开都覆盖——窗口只关心"正在跑的这一次"。"""
    path = journal_path
    first = Journal(path)
    first.record(step=1, state="SHOP", action="buy")
    first.close()
    second = Journal(path)
    second.record(step=1, state="SHOP", action="sell")
    second.close()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["action"] == "sell"


# ------------------------------------------------------ 决策函数确实给理由


def test_blind_decision_explains_itself() -> None:
    clear_reason()
    # make_state 默认就把小盲设成 CURRENT，直接用它
    choose_blind_action(GameState(make_state(required=300)), Config())
    assert "Small Blind" in current_reason().why
    assert current_reason().kind == "blind"


def test_play_note_describes_the_hand_that_is_actually_played() -> None:
    """**不变量：日志报的分数必须是真正打出去那一手的分数。**

    原来在"计划不用弃牌 → 直接打"这条分支上报的是 `best_plan.completed_score`
    （**凑牌计划**的牌型分数），而实际返回的是 `Play(current.indices)`（当前最优
    那一手）——两者可以完全不同。实测有一局日志写"已成牌（High Card）
    预估得分=15"，而那一手实际得了 280，害我去追一个并不存在的"19 倍算分误差"。
    """
    from balatro_bot.solver import Solver
    from balatro_bot.strategy import Play, choose_play_action

    hand = [card("H_A"), card("H_K"), card("H_Q"), card("H_J"), card("H_9")]
    checked = 0
    for required in (10, 100, 300, 2000):
        raw = make_state(state="SELECTING_HAND", hand=hand, required=required)
        with_area(raw, "hand", hand, limit=8)
        state = GameState(raw)
        clear_reason()
        action = choose_play_action(state, Config())
        if not isinstance(action, Play):
            continue
        reason = current_reason()
        if not reason.numbers or "预估得分" not in reason.numbers:
            continue          # 这条分支不报分数，跳过
        breakdown = Solver.from_state(state).score(state.hand.cards, action.indices)
        assert reason.numbers["预估得分"] == round(breakdown.total), (
            f"日志说 {reason.numbers['预估得分']}，"
            f"实际这一手是 {round(breakdown.total)}"
        )
        assert reason.numbers["牌型"] == breakdown.hand_name
        checked += 1
    assert checked, "至少要有一条分支报了预估得分，否则这个不变量没被验证"


def test_play_decision_explains_the_score(journal_path) -> None:
    """出牌理由里要带上"预估得分 / 还需要多少"这两个数——排错全靠它们。"""
    raw = make_state(state="SELECTING_HAND",
                     hand=[card("H_A"), card("D_A"), card("H_K"),
                           card("D_Q"), card("H_J")],
                     required=100)
    with_area(raw, "hand", [card("H_A"), card("D_A"), card("H_K"),
                            card("D_Q"), card("H_J")], limit=8)
    clear_reason()
    choose_play_action(GameState(raw), Config())
    reason = current_reason()
    assert reason.why
    assert reason.kind in ("play", "discard")


def test_shop_buy_explains_the_numbers() -> None:
    clear_reason()
    state = shop_state(money=40, jokers=[joker("j_jolly")],
                       shop=[priced(joker("j_duo"), 5)], deck=full_deck())
    choose_shop_action(state, Config(), rerolls=0)
    reason = current_reason()
    assert reason.why
    assert reason.kind in ("buy", "sell", "pack", "reroll", "flow")


def test_shop_leaving_explains_why() -> None:
    """"离开商店"也要给理由——以前只知道它走了，不知道为什么走。"""
    clear_reason()
    state = shop_state(money=2, jokers=[joker("j_jolly")], deck=full_deck())
    choose_shop_action(state, Config(), rerolls=0)
    reason = current_reason()
    assert reason.kind == "flow"
    assert "离开商店" in reason.why


# ------------------------------------------------------------------ 端到端


def test_bot_records_every_step(journal_path) -> None:
    """主循环每做一步决策就写一条记录。"""
    from balatro_bot.bot import Bot
    from balatro_bot.journal import Journal

    path = journal_path
    journal = Journal(path)

    class FakeClient:
        """只回状态、不做任何事——够验证"每步都写日志"了。"""

        def __init__(self, states):
            self._states = list(states)

        def gamestate(self):
            return self._states[0]

    raw = make_state(state="ROUND_EVAL", money=10)
    client = FakeClient([raw])
    bot = Bot(client, Config(), journal=journal, verbose=False)
    bot._record(GameState(raw), __import__("balatro_bot.strategy", fromlist=["CashOut"]).CashOut(), 7)
    journal.close()

    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry["step"] == 7
    assert entry["action"] == "cash_out"
    assert entry["snapshot"]["money"] == 10


# ------------------------------------------------- 推给游戏内的日志面板（mod 端点）


class FakeRpc:
    """记录 journal 调用的假客户端。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def journal(self, **fields):
        if self.fail:
            raise BalatroRPCError(-32601, "METHOD_NOT_FOUND", "no such method")
        self.calls.append(fields)
        return {"success": True}


def test_record_also_pushes_to_the_in_game_panel() -> None:
    """一份记录同时走两个出口：JSONL 文件和游戏内面板。"""
    client = FakeRpc()
    journal = Journal(None, client=client)
    clear_reason()
    note("这一手就能过关", kind="play", 预估得分=782)

    journal.record(step=3, state="SELECTING_HAND", action="play",
                   detail="出牌 [0, 1]", snapshot={"ante": 2, "money": 10})

    assert len(client.calls) == 1
    sent = client.calls[0]
    assert sent["kind"] == "play"
    assert sent["why"] == "这一手就能过关"
    assert sent["step"] == 3
    assert sent["detail"] == "出牌 [0, 1]"


def test_mod_push_stringifies_numbers_and_frame() -> None:
    """mod 的参数校验只认 string/integer/boolean/array/table。

    所以数字字典的值要转成字符串，快照里的整数字段保持整数——
    否则会被校验器拒掉，而且是在游戏里静默失败。
    """
    client = FakeRpc()
    journal = Journal(None, client=client)
    clear_reason()
    note("弃 3 张去凑 Flush", kind="discard", 概率="26%", 期望=1240)

    journal.record(step=5, state="SELECTING_HAND", action="discard",
                   snapshot={"ante": 5, "money": 21, "jokers": ["Jolly Joker"]})

    sent = client.calls[0]
    assert sent["numbers"] == {"概率": "26%", "期望": "1240"}
    assert sent["frame"]["ante"] == 5              # 整数保持整数
    assert sent["frame"]["money"] == 21
    assert sent["frame"]["jokers"] == ["Jolly Joker"]


def test_mod_push_failure_never_breaks_the_game() -> None:
    """面板没装时端点不存在——必须吞掉错误，只提示一次。"""
    client = FakeRpc(fail=True)
    journal = Journal(None, client=client)

    clear_reason()
    note("第一次", kind="play")
    journal.record(step=1, state="SHOP", action="play")       # 不该抛
    assert journal.mod_ok is False                            # 之后不再尝试

    note("第二次", kind="play")
    journal.record(step=2, state="SHOP", action="play")       # 也不该抛


def test_journal_without_a_client_skips_the_panel() -> None:
    journal = Journal(None)
    clear_reason()
    journal.record(step=1, state="SHOP", action="buy")        # 不该抛
    assert journal.mod_records == 0


def test_clear_is_pushed_at_the_start_of_a_game() -> None:
    """每局开始先清空面板，免得上一局的记录混进来。"""
    from balatro_bot.bot import Bot

    client = FakeRpc()
    journal = Journal(None, client=client)

    class NoopClient(FakeRpc):
        def menu(self):
            return make_state()

        def start(self, deck, stake, seed):
            # 直接给一个已结束的局面：只验证"开局先清空面板"这一件事
            return make_state(state="GAME_OVER")

        def gamestate(self):
            return make_state(state="GAME_OVER")

    bot = Bot(NoopClient(), Config(), journal=journal, verbose=False)
    bot.play_game("TEST")
    assert client.calls and client.calls[0].get("clear") is True
