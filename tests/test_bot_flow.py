"""主循环的控制流测试。

用一个按脚本推进的假客户端替代真实游戏，因此不需要启动 Balatro 就能验证：
状态机能不能走完整局、接口报错后能不能恢复、卡住时能不能自己脱身。

这里不测策略好不好（那要靠真实对局和种子评测），只测"流程不会断"。
"""

from __future__ import annotations

import pytest

from balatro_bot.bot import Bot, _translate
from balatro_bot.rpc import BalatroRPCError
from balatro_bot.state import GameState
from balatro_bot.strategy import (
    Buy,
    CashOut,
    Config,
    Discard,
    NextRound,
    OpenPack,
    Play,
    Refresh,
    Reroll,
    SelectBlind,
    Sell,
    SkipBlind,
    UseConsumable,
)

from conftest import card, joker, make_state

BLIND_BY_ROUND = {1: "SMALL", 2: "BIG", 3: "BOSS"}


class FakeClient:
    """最小可用的假游戏，只实现主循环会用到的接口。"""

    def __init__(self, *, max_ante: int = 2, play_score: int = 1000) -> None:
        self.calls: list[tuple] = []
        self.state_name = "MENU"
        self.ante = 1
        self.round_num = 1
        self.chips = 0
        self.required = 100
        self.hands_left = 4
        self.discards_left = 3
        self.money = 4
        self.max_ante = max_ante
        self.play_score = play_score
        self.won = False
        self.hand_cards = [card(k) for k in
                           ("H_2", "H_3", "H_4", "H_5", "H_6", "D_9", "C_T", "S_J")]
        self.jokers: list[dict] = []
        self.consumables: list[dict] = []
        self.shop: list[dict] = []
        # 让指定次数的调用先失败一次，用来测容错
        self.fail_select_times = 0
        self.fail_use_times = 0

    # ------------------------------------------------------------ 内部

    @property
    def blind_type(self) -> str:
        return BLIND_BY_ROUND.get(self.round_num, "BOSS")

    def _raw(self) -> dict:
        raw = make_state(
            hand=self.hand_cards,
            jokers=self.jokers,
            chips=self.chips,
            required=self.required,
            money=self.money,
            hands_left=self.hands_left,
            discards_left=self.discards_left,
            state=self.state_name,
        )
        raw["ante_num"] = self.ante
        raw["round_num"] = self.round_num
        raw["won"] = self.won
        raw["consumables"] = {"count": len(self.consumables), "limit": 2,
                              "cards": self.consumables}
        raw["shop"] = {"count": len(self.shop), "limit": 2, "cards": self.shop}
        for name, blind_type in (("small", "SMALL"), ("big", "BIG"), ("boss", "BOSS")):
            raw["blinds"][name]["status"] = (
                "CURRENT" if blind_type == self.blind_type else "UPCOMING"
            )
        return raw

    def _deal(self) -> None:
        self.chips = 0
        self.hands_left = 4
        self.discards_left = 3

    # ------------------------------------------------------------ 接口

    def health(self) -> dict:
        return {"status": "ok"}

    def gamestate(self) -> dict:
        return self._raw()

    def menu(self) -> dict:
        self.state_name = "MENU"
        return self._raw()

    def start(self, deck: str, stake: str, seed: str | None = None) -> dict:
        self.calls.append(("start", deck, stake, seed))
        self.ante, self.round_num, self.won = 1, 1, False
        self._deal()
        self.state_name = "BLIND_SELECT"
        return self._raw()

    def select(self) -> dict:
        self.calls.append(("select",))
        if self.fail_select_times > 0:
            self.fail_select_times -= 1
            raise BalatroRPCError(-32002, "INVALID_STATE", "not in BLIND_SELECT")
        self.state_name = "SELECTING_HAND"
        return self._raw()

    def skip(self) -> dict:
        self.calls.append(("skip",))
        self.state_name = "BLIND_SELECT"
        return self._raw()

    def play(self, cards: list[int]) -> dict:
        self.calls.append(("play", tuple(cards)))
        self.chips += self.play_score
        if self.chips >= self.required:
            self.state_name = "ROUND_EVAL"
        else:
            self.hands_left -= 1
            if self.hands_left <= 0:
                self.state_name = "GAME_OVER"
        return self._raw()

    def discard(self, cards: list[int]) -> dict:
        self.calls.append(("discard", tuple(cards)))
        self.discards_left -= 1
        return self._raw()

    def cash_out(self) -> dict:
        self.calls.append(("cash_out",))
        self.money += 10
        self.state_name = "SHOP"
        return self._raw()

    def next_round(self) -> dict:
        self.calls.append(("next_round",))
        if self.state_name == "SHOP":
            if self.blind_type == "BOSS":
                self.ante += 1
                self.round_num = 1
            else:
                self.round_num += 1
            if self.ante > self.max_ante:
                self.won = True
                self.state_name = "GAME_OVER"
            else:
                self.state_name = "BLIND_SELECT"
            self._deal()
        return self._raw()

    def buy(self, card=None, voucher=None, pack=None) -> dict:
        self.calls.append(("buy", card, voucher, pack))
        return self._raw()

    def open_pack(self, card=None, targets=None, skip=False) -> dict:
        self.calls.append(("open_pack", card, skip))
        return self._raw()

    def sell(self, joker=None, consumable=None) -> dict:
        self.calls.append(("sell", joker, consumable))
        return self._raw()

    def reroll(self) -> dict:
        self.calls.append(("reroll",))
        return self._raw()

    def use(self, consumable: int, cards: list[int] | None = None) -> dict:
        self.calls.append(("use", consumable, tuple(cards or ())))
        if self.fail_use_times > 0:
            self.fail_use_times -= 1
            raise BalatroRPCError(-32001, "BAD_REQUEST", "needs targets")
        return self._raw()


def make_bot(client: FakeClient, **config_kwargs) -> Bot:
    config = Config(seed="TEST")
    for key, value in config_kwargs.items():
        setattr(config, key, value)
    return Bot(client, config, verbose=False)


# ------------------------------------------------------------------- 测试


def test_bot_completes_a_game() -> None:
    """完整跑通一局：选盲注 → 出牌 → 结算 → 商店 → 下一回合 → 通关。"""
    client = FakeClient(max_ante=2)
    bot = make_bot(client)
    result = bot.play_game("TEST")

    assert result.error is None, result.error
    assert result.won is True
    assert bot.stats.games == 1
    assert bot.stats.wins == 1

    kinds = [call[0] for call in client.calls]
    assert kinds[0] == "start"
    for expected in ("select", "play", "cash_out", "next_round"):
        assert expected in kinds, f"没有调用 {expected}"


def test_bot_plays_until_round_cleared() -> None:
    """每回合至少打出一手牌，且打出的张数在 1~5 之间。"""
    client = FakeClient(max_ante=2)
    bot = make_bot(client)
    bot.play_game("TEST")

    plays = [call for call in client.calls if call[0] == "play"]
    assert plays, "一次都没有出牌"
    for _, indices in plays:
        assert 1 <= len(indices) <= 5
        assert len(set(indices)) == len(indices)


def test_bot_recovers_from_invalid_state() -> None:
    """select 报 INVALID_STATE 时应重新同步状态并继续，而不是崩溃。"""
    client = FakeClient(max_ante=1)
    client.fail_select_times = 1
    bot = make_bot(client)
    result = bot.play_game("TEST")

    assert result.error is None, result.error
    assert result.won is True


def _consumable(key: str, label: str = "") -> dict:
    return {
        "id": 1, "key": key, "set": "TAROT", "label": label or key,
        "value": {"effect": ""},
        "modifier": {"seal": None, "edition": None, "enhancement": None,
                     "eternal": False, "perishable": None, "rental": False},
        "state": {"debuff": False, "hidden": False, "highlight": False},
        "cost": {"sell": 1, "buy": 3},
    }


def test_bot_uses_planet_card_immediately() -> None:
    """星球牌应该第一时间用掉，然后继续正常出牌。"""
    client = FakeClient(max_ante=1)
    client.consumables = [_consumable("c_mercury", "Mercury")]
    bot = make_bot(client)
    result = bot.play_game("TEST")

    assert result.error is None, result.error
    uses = [call for call in client.calls if call[0] == "use"]
    assert uses and uses[0][1] == 0          # 用了第 0 张
    assert uses[0][2] == ()                  # 星球牌不需要目标
    assert any(call[0] == "play" for call in client.calls)


def test_bot_targets_best_play_cards_for_tarot() -> None:
    """需要手牌目标的塔罗牌，目标应该取自当前最优出牌。"""
    client = FakeClient(max_ante=1)
    client.consumables = [_consumable("c_magician", "The Magician")]
    bot = make_bot(client)
    bot.play_game("TEST")

    uses = [call for call in client.calls if call[0] == "use"]
    assert uses, "应该使用了塔罗牌"
    targets = uses[0][2]
    assert targets, "塔罗牌应该带上手牌目标"
    assert len(targets) <= 5
    assert len(set(targets)) == len(targets)


def test_bot_gives_up_on_always_rejected_consumable() -> None:
    """消耗牌怎么用都被拒时，应标记不可用并继续打牌，不能反复重试。"""
    client = FakeClient(max_ante=1)
    client.consumables = [_consumable("c_mercury", "Mercury")]

    class RejectingClient(FakeClient):
        def use(self, consumable, cards=None):  # type: ignore[override]
            self.calls.append(("use", consumable, tuple(cards or ())))
            raise BalatroRPCError(-32003, "NOT_ALLOWED", "nope")

    rejecting = RejectingClient(max_ante=1)
    rejecting.consumables = client.consumables
    bot = make_bot(rejecting)
    result = bot.play_game("TEST")

    assert result.error is None, result.error
    uses = [call for call in rejecting.calls if call[0] == "use"]
    assert 0 < len(uses) <= 2, f"应该尝试一两次就放弃，实际 {len(uses)} 次"
    assert any(call[0] == "play" for call in rejecting.calls)
    assert bot._bad_consumables, "被拒绝的消耗牌应该被记下来"


# -------------------------------------------- 消耗牌失败的三分类（瞬时锁 / 张数 / 真不可用）


def test_required_card_count_reads_the_self_describing_error() -> None:
    """mod 的张数报错是**自描述**的——按它纠正就不需要维护一张对照表。

    那张 `CONSUMABLE_TARGET_COUNT` 表已经错过一次：`c_chariot` 写成"最多 2 张"，
    游戏实为"恰好 1 张"（`use.lua:100` 的 `min_cards == max_cards` 分支）。
    """
    from balatro_bot.bot import required_card_count

    assert required_card_count(
        "Consumable 'The Chariot' requires exactly 1 card (provided: 2)") == 1
    assert required_card_count(
        "Consumable 'The Devil' requires exactly 2 cards (provided: 0)") == 2
    assert required_card_count(
        "Consumable 'X' requires at least 1 card (provided: 0)") == 1
    assert required_card_count(
        "Consumable 'X' requires at most 3 cards (provided: 5)") == 3
    # 读不出来时要返回 None，而不是瞎猜一个张数
    assert required_card_count(
        "Consumable 'X' cannot be used at this time") is None
    assert required_card_count("") is None


def test_transient_use_lock_is_distinguished_from_a_real_refusal() -> None:
    """同是 NOT_ALLOWED，动画瞬时锁和"空间不足"要分开。"""
    from balatro_bot.bot import is_transient_use_lock

    assert is_transient_use_lock(BalatroRPCError(
        -32003, "NOT_ALLOWED",
        "Consumable 'Mercury' cannot be used at this time"))
    assert not is_transient_use_lock(BalatroRPCError(
        -32003, "NOT_ALLOWED",
        "Cannot use consumable 'X': insufficient space"))
    assert not is_transient_use_lock(BalatroRPCError(
        -32001, "BAD_REQUEST", "needs targets"))


def test_transient_lock_retries_and_still_uses_the_planet() -> None:
    """**核心回归测试。**

    游戏播动画时 `Card:can_use_consumeable()`（`card.lua:1524-1527`）返回 false，
    mod 报 "cannot be used at this time"。原来的处理是**整局拉黑**——实测 20 局里
    `c_mercury`（升 Pair 的星球牌）因此被拉黑 3 次，那一整局的星球成长直接断掉，
    表现就是"拿到星球牌 0.90 张/局、只用掉 0.40 张"。
    """

    class LockedOnce(FakeClient):
        """第一次 use 报瞬时锁，之后正常。"""

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.locked = 0

        def use(self, consumable: int, cards=None) -> dict:
            if self.locked == 0:
                self.locked += 1
                self.calls.append(("use", consumable, tuple(cards or ())))
                raise BalatroRPCError(
                    -32003, "NOT_ALLOWED",
                    "Consumable 'Mercury' cannot be used at this time")
            return super().use(consumable, cards)

    client = LockedOnce(max_ante=1)
    client.consumables = [_consumable("c_mercury", "Mercury")]
    bot = make_bot(client)
    bot.ui_retry_delay = 0.0            # 测试里不要真的睡
    result = bot.play_game("TEST")

    assert result.error is None, result.error
    assert "c_mercury" not in bot._bad_consumables, "瞬时锁不该整局拉黑"
    uses = [call for call in client.calls if call[0] == "use"]
    assert len(uses) >= 2, f"应该等一下重试并最终用掉，实际只调了 {len(uses)} 次"
    assert any(call[0] == "play" for call in client.calls)


def test_permanently_locked_consumable_is_deferred_not_blacklisted() -> None:
    """一直报瞬时锁时，也只冻结**本回合**——下回合的 SELECTING_HAND 还要再试。

    用"本回合冻结"而不是"不处理"，是为了避免每一步都重试同一张牌把步数耗光。
    """

    class AlwaysLocked(FakeClient):
        def use(self, consumable: int, cards=None) -> dict:
            self.calls.append(("use", consumable, tuple(cards or ())))
            raise BalatroRPCError(
                -32003, "NOT_ALLOWED",
                "Consumable 'Mercury' cannot be used at this time")

    client = AlwaysLocked(max_ante=1)
    client.consumables = [_consumable("c_mercury", "Mercury")]
    bot = make_bot(client)
    bot.ui_retry_delay = 0.0
    result = bot.play_game("TEST")

    assert result.error is None, result.error
    assert "c_mercury" not in bot._bad_consumables, "不该整局拉黑"
    assert bot._deferred_consumables, "应该被冻结在本回合"
    assert all(where[0] >= 1 for where in bot._deferred_consumables.values())
    assert any(call[0] == "play" for call in client.calls), "冻结之后要继续出牌"


def test_use_retries_with_the_card_count_from_the_error() -> None:
    """张数不对时按报错里的张数纠正后重试（`c_chariot` 要恰好 1 张）。"""

    class CountCorrecting(FakeClient):
        def use(self, consumable: int, cards=None) -> dict:
            given = list(cards or ())
            self.calls.append(("use", consumable, tuple(given)))
            if len(given) != 1:
                raise BalatroRPCError(
                    -32001, "BAD_REQUEST",
                    "Consumable 'The Chariot' requires exactly 1 card "
                    f"(provided: {len(given)})")
            return self._raw()

    client = CountCorrecting(max_ante=1)
    client.consumables = [_consumable("c_chariot", "The Chariot")]
    bot = make_bot(client)
    result = bot.play_game("TEST")

    assert result.error is None, result.error
    uses = [call for call in client.calls if call[0] == "use"]
    assert any(len(call[2]) == 1 for call in uses), \
        f"应该按报错带 1 张目标重试，实际 {uses}"
    assert "c_chariot" not in bot._bad_consumables


def test_bot_uses_consumables_in_the_shop_too() -> None:
    """商店里也要用消耗牌，而且**在 SELECTING_HAND 被瞬时锁冻住之后要重试**。

    `use` 端点在 SHOP 是允许的（`use.lua:39` 的 requires_state 含 SHOP）。
    在商店里用掉星球牌有两个好处：早一级早吃加成；**腾出消耗牌槽**——
    槽满了从包里拿星球牌会静默丢失（mod 的 pack.lua 不校验消耗牌位）。

    冻结按 (底注, 回合, **状态**) 记，所以从 SELECTING_HAND 进到 SHOP 会重新尝试。
    """

    class LockedInHand(FakeClient):
        """SELECTING_HAND 里报瞬时锁，其它状态正常。"""

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.use_log: list[tuple[str, int]] = []

        def use(self, consumable: int, cards=None) -> dict:
            self.calls.append(("use", consumable, tuple(cards or ())))
            self.use_log.append((self.state_name, consumable))
            if self.state_name == "SELECTING_HAND":
                raise BalatroRPCError(
                    -32003, "NOT_ALLOWED",
                    "Consumable 'Mercury' cannot be used at this time")
            # 用掉了就从槽里拿掉，否则会一直被选中
            if consumable < len(self.consumables):
                self.consumables.pop(consumable)
            return self._raw()

    client = LockedInHand(max_ante=1)
    client.consumables = [_consumable("c_mercury", "Mercury")]
    bot = make_bot(client)
    bot.ui_retry_delay = 0.0
    result = bot.play_game("TEST")

    assert result.error is None, result.error
    assert any(state == "SHOP" for state, _ in client.use_log), \
        f"应该在商店里也尝试用消耗牌，实际 {client.use_log}"
    assert "c_mercury" not in bot._bad_consumables


def test_bot_escapes_stall() -> None:
    """如果某个动作被反复拒绝导致状态不动，卡死保护应该强制推进。"""
    client = FakeClient(max_ante=1)
    bot = make_bot(client)

    class StuckClient(FakeClient):
        """永远停在 SELECTING_HAND，且拒绝出牌。"""

        def play(self, cards):  # type: ignore[override]
            self.calls.append(("play-rejected", tuple(cards)))
            raise BalatroRPCError(-32001, "BAD_REQUEST", "rejected")

        def gamestate(self):  # type: ignore[override]
            self.state_name = "SELECTING_HAND"
            return self._raw()

    stuck = StuckClient(max_ante=1)
    stuck.state_name = "SELECTING_HAND"
    bot = make_bot(stuck)
    bot.max_steps = 40          # 这一步纯属"不会死循环"的验证，不需要跑满
    result = bot.play_game("TEST")

    # 出牌一直被拒，最终会撞上最大步数而中止，但绝不能死循环
    assert result.steps <= bot.max_steps
    assert any(call[0] == "play-rejected" for call in stuck.calls)


def test_bot_does_not_loop_forever_on_unknown_state() -> None:
    """未知状态只刷新，不发送非法操作。"""
    client = FakeClient(max_ante=1)
    client.state_name = "SOME_FUTURE_STATE"
    bot = make_bot(client)
    bot.max_steps = 12
    result = bot.play_game("TEST")
    # 未知状态无法推进，应该被最大步数拦住而不是无限循环
    assert result.steps <= 12


# ------------------------------------------------------------- 动作翻译


@pytest.mark.parametrize(
    ("action", "method", "params"),
    [
        (SelectBlind(), "select", {}),
        (SkipBlind(), "skip", {}),
        (Play((1, 2)), "play", {"cards": [1, 2]}),
        (Discard((0,)), "discard", {"cards": [0]}),
        (CashOut(), "cash_out", {}),
        (NextRound(), "next_round", {}),
        (Refresh(), "gamestate", {}),
        (Buy(card=2), "buy", {"card": 2}),
        (Buy(voucher=1), "buy", {"voucher": 1}),
        (Buy(pack=0), "buy", {"pack": 0}),
        (Sell(joker=3), "sell", {"joker": 3}),
        (Reroll(), "reroll", {}),
        (UseConsumable(0), "use", {"consumable": 0}),
        (UseConsumable(1, (2, 3)), "use", {"consumable": 1, "cards": [2, 3]}),
        (OpenPack(card=1), "open_pack", {"card": 1}),
        (OpenPack(skip=True), "open_pack", {"skip": True}),
        (OpenPack(targets=(0, 1)), "open_pack", {"targets": [0, 1]}),
    ],
)
def test_translate(action, method: str, params: dict) -> None:
    translated_method, translated_params, _ = _translate(action)
    assert translated_method == method
    assert translated_params == params


# ------------------------------------------- mod 的 UI 就绪竞态（正常速度才撞得上）

UI_RACE_MESSAGE = (
    "[SMODS balatrobot \"src/lua/endpoints/discard.lua\"]:91: "
    "attempt to index field 'buttons' (a nil value)"
)


class _UiRacyClient:
    """第一次 discard 抛 mod 的 UI 竞态错误，之后正常。

    真实成因：正常速度下选完盲注之后游戏 UI 还没重建完，而 mod 会直接索引
    `G.buttons.UIRoot`（discard.lua:91 / play.lua:79），那一刻它是 nil。
    fast 模式下动画快 6 倍，撞不上这个窗口——所以这个 bug 只在正常速度下暴露。
    """

    def __init__(self) -> None:
        self.discard_calls = 0
        self.gamestate_calls = 0

    def gamestate(self) -> dict:
        self.gamestate_calls += 1
        return make_state(state="SELECTING_HAND")

    def discard(self, cards) -> dict:
        self.discard_calls += 1
        if self.discard_calls == 1:
            raise BalatroRPCError(-32000, "INTERNAL_ERROR", UI_RACE_MESSAGE)
        return make_state(state="SELECTING_HAND")


def test_ui_readiness_race_is_detected() -> None:
    from balatro_bot.bot import is_ui_readiness_race

    assert is_ui_readiness_race(
        BalatroRPCError(-32000, "INTERNAL_ERROR", UI_RACE_MESSAGE)
    )
    # 其它错误不能被误判成这个竞态，否则会掩盖真正的问题
    for name, message in (
        ("INVALID_STATE", "not in hand selection"),
        ("BAD_REQUEST", "too many cards"),
        ("INTERNAL_ERROR", "something else broke entirely"),
    ):
        assert not is_ui_readiness_race(BalatroRPCError(-32000, name, message)), name


def test_ui_readiness_race_is_retried_instead_of_aborting() -> None:
    """撞上这个竞态要重试，而不是把整局判为出错中止。"""
    client = _UiRacyClient()
    bot = Bot(client, Config(), verbose=False)
    bot.ui_retry_delay = 0.0            # 测试里别真睡

    state = bot._apply(Discard((0, 1)))

    assert client.discard_calls == 2    # 第一次失败、第二次成功
    assert isinstance(state, GameState)


def test_ui_readiness_race_gives_up_gracefully_after_one_retry() -> None:
    """重试也失败时返回当前状态、不抛异常——让主循环继续而不是整局中止。"""

    class AlwaysRacy(_UiRacyClient):
        def discard(self, cards) -> dict:
            self.discard_calls += 1
            raise BalatroRPCError(-32000, "INTERNAL_ERROR", UI_RACE_MESSAGE)

    client = AlwaysRacy()
    bot = Bot(client, Config(), verbose=False)
    bot.ui_retry_delay = 0.0

    state = bot._apply(Discard((0, 1)))

    assert client.discard_calls == 2
    assert isinstance(state, GameState)
