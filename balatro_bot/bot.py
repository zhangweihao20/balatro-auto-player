"""机器人主循环：把游戏状态机跑起来。

设计要点
--------
**状态机而不是脚本**：每一步都重新取一次状态，再决定下一步做什么。
Balatro 有若干过渡状态（HAND_PLAYED / DRAW_TO_HAND / NEW_ROUND /
PLAY_TAROT / *_PACK），与其预测流程，不如每轮问一次"现在是什么状态"。

**容错优先**：接口调用可能因为状态过期返回 INVALID_STATE，
也可能因为规则不允许返回 NOT_ALLOWED。前者重新同步状态即可，
后者必须换一个动作——否则会陷入"重试同一个被拒绝的动作"的死循环。
本模块用两张表记录"已经被拒绝过的东西"，并把重复动作识别为卡住。

**卡死保护**：即便出现预料之外的状态组合，也会在连续 N 步没有进展后
强制推进（该打的打、该走的走），保证一局一定能结束。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .build import build_snapshot
from .jokers import unknown_jokers
from .journal import Journal, clear_reason, current_reason
from .rpc import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_NOT_ALLOWED,
    BalatroClient,
    BalatroConnectionError,
    BalatroRPCError,
)
from .solver import Solver
from .state import GameState
from .strategy import (
    Action,
    Buy,
    CashOut,
    Config,
    Discard,
    NextRound,
    OpenPack,
    Play,
    Rearrange,
    Refresh,
    Reroll,
    SelectBlind,
    Sell,
    SkipBlind,
    UseConsumable,
    choose_blind_action,
    choose_consumable_action,
    choose_pack_action,
    choose_play_action,
    choose_rearrange_action,
    choose_sell_action,
    choose_shop_action,
    play_constraints,
)

log = logging.getLogger("balatro_bot")

# 这些状态只是动画/过渡，什么都不用做，重新取状态即可
TRANSIENT_STATES = frozenset({
    "HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND", "PLAY_TAROT",
})

# 开包状态。Steamodded 用 SMODS_BOOSTER_OPENED，原版用 *_PACK。
PACK_STATES = frozenset({
    "SMODS_BOOSTER_OPENED", "TAROT_PACK", "PLANET_PACK", "SPECTRAL_PACK",
    "STANDARD_PACK", "BUFFOON_PACK",
})


@dataclass
class GameResult:
    """一局的结果。"""

    seed: str = ""
    won: bool = False
    ante: int = 0
    round_num: int = 0
    steps: int = 0
    money: int = 0
    error: str | None = None

    @property
    def summary(self) -> str:
        if self.error:
            return f"出错中止：{self.error}（底注 {self.ante}）"
        if self.won:
            return f"通关！底注 {self.ante}，用了 {self.steps} 步"
        return f"失败于底注 {self.ante} 第 {self.round_num} 回合，用了 {self.steps} 步"


@dataclass
class Stats:
    """跨局统计，用来算胜率。"""

    games: int = 0
    wins: int = 0
    errors: int = 0
    ante_reached: list[int] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    @property
    def average_ante(self) -> float:
        return sum(self.ante_reached) / len(self.ante_reached) if self.ante_reached else 0.0


class Bot:
    """通过 BalatroBot 接口驱动游戏。"""

    # mod 的 UI 就绪竞态重试前等多久。正常速度下选完盲注到 UI 建好大约 0.5~1 秒；
    # 取 1.2 秒留足余量（这是每局最多撞一两次的路径，慢一点无所谓）。
    UI_RETRY_DELAY = 1.2

    def __init__(
        self,
        client: BalatroClient,
        config: Config | None = None,
        *,
        max_steps: int = 4000,
        max_stall: int = 4,
        dry_run: bool = False,
        verbose: bool = True,
        journal: "Journal | None" = None,
    ) -> None:
        self.client = client
        self.config = config or Config()
        self.max_steps = max_steps
        self.max_stall = max_stall
        self.dry_run = dry_run
        self.verbose = verbose
        self.ui_retry_delay = self.UI_RETRY_DELAY
        self.stats = Stats()
        # 决策日志：给"游戏旁边的日志栏"实时读。测试/离线时不传就没有开销。
        self.journal = journal

        # 被规则拒绝过的东西，避免反复尝试同一个非法动作
        self._bad_consumables: set[str] = set()
        # 因为**瞬时锁**（游戏在播动画）而这一步先不碰的消耗牌：
        # key -> (底注, 回合, 状态)。只冻结**同一个状态里的同一个回合**——
        # 下一回合、或换个状态（例如从 SELECTING_HAND 进到 SHOP）都会再试，
        # 所以星球牌不会因为一次动画竞态就整局报废，也不会每一步都重试。
        self._deferred_consumables: dict[str, tuple[int, int, str]] = {}

        # 主 build 流派识别（阶段一：只写日志，不参与决策）。
        # 见 balatro_bot/build.py 与 docs/strategy-math.md §16。
        self._build_snapshot: Any = None
        self._build_for: tuple[int, int] | None = None
        self._build_committed: str | None = None
        self._skip_pack = False
        # 本回合是否已经检查过小丑牌槽位顺序
        self._rearranged_for: tuple[int, int] | None = None
        # 本回合是否为解除 Boss 压制卖过小丑牌（兜底：正常靠状态的 debuff 标记判断）
        self._sold_for: tuple[int, int] | None = None

    # ------------------------------------------------------------------ 主流程

    def play_game(self, seed: str | None = None) -> GameResult:
        """在 MENU 状态下开一局并打到结束。"""
        self._bad_consumables.clear()
        self._deferred_consumables.clear()
        self._build_snapshot = None
        self._build_for = None
        self._build_committed = None
        self._skip_pack = False
        self._rearranged_for = None
        self._sold_for = None
        if self.journal is not None:
            # 新的一局：先把游戏内的面板清空，免得上一局的记录混进来
            self.journal.push_to_mod(kind="", action="", clear=True)

        if not self.dry_run:
            self.client.menu()
            state = GameState(
                self.client.start(self.config.deck, self.config.stake, seed)
            )
        else:
            state = GameState(self.client.gamestate())

        result = GameResult(seed=state.seed or (seed or ""))
        log.info("开局：牌组 %s，难度 %s，种子 %s",
                 self.config.deck, self.config.stake, state.seed)

        steps = 0
        stall = 0
        last_signature: tuple | None = None
        shop_rerolls = 0
        recorded_unknown: set[str] = set()

        while True:
            if steps >= self.max_steps:
                result.error = f"超过最大步数 {self.max_steps}"
                log.error(result.error)
                break

            if state.is_game_over:
                break

            if state.state == "MENU":
                # 意外回到了菜单（例如游戏崩了或被手动操作），重新开一局
                log.warning("状态意外回到 MENU，重新开局")
                state = GameState(
                    self.client.start(self.config.deck, self.config.stake, seed)
                )
                continue

            # 换回合时重置商店重摇计数
            if state.state == "SHOP" and last_signature and last_signature[0] != "SHOP":
                shop_rerolls = 0

            unknown = unknown_jokers([c.key for c in state.jokers.cards])
            new_unknown = set(unknown) - recorded_unknown
            if new_unknown:
                recorded_unknown |= new_unknown
                log.info("遇到未建模的小丑牌（按 0 收益处理）：%s", ", ".join(sorted(new_unknown)))

            # 节流：每次决策前歇一下。决策本身只要 4~6 ms，但整局墙钟的大头是
            # **游戏自己结算动作**（单次 play 就要 1~5 秒）。调大这个值可以让
            # 机器人放慢节奏，避免把游戏压得太紧（长跑时游戏会因内存泄漏变慢）。
            if self.config.decision_delay > 0:
                time.sleep(self.config.decision_delay)

            action = self._dispatch(state, shop_rerolls)
            self._record(state, action, steps)
            signature = (state.state, repr(action), state.ante, state.round_num,
                         len(state.hand.cards), state.money)
            if signature == last_signature:
                stall += 1
            else:
                stall = 0
            last_signature = signature

            if stall >= self.max_stall:
                log.warning("连续 %d 步没有进展（状态 %s），强制推进", stall, state.state)
                action = self._force_action(state)
                stall = 0
                last_signature = None

            if isinstance(action, Reroll):
                shop_rerolls += 1

            steps += 1
            try:
                state = self._apply(action)
            except BalatroConnectionError as exc:
                result.error = str(exc)
                log.error("连接中断：%s", exc)
                break
            except BalatroRPCError as exc:
                # _apply 已经处理了可恢复的错误；走到这里说明不可恢复
                result.error = str(exc)
                log.error("接口错误：%s", exc)
                break

        result.steps = steps
        result.ante = state.ante
        result.round_num = state.round_num
        result.money = state.money
        result.won = state.won
        if not result.seed:
            result.seed = state.seed

        self.stats.games += 1
        self.stats.ante_reached.append(result.ante)
        if result.won:
            self.stats.wins += 1
        if result.error:
            self.stats.errors += 1
        log.info(
            "本局结束：%s ｜ 钱 $%d ｜ 小丑牌 %s",
            result.summary, result.money,
            ", ".join(c.label or c.key for c in state.jokers.cards) or "无",
        )
        return result

    def run(self, games: int = 1, seeds: list[str] | None = None,
            on_result: Callable[[GameResult], None] | None = None) -> Stats:
        """连续跑多局。"""
        for index in range(games):
            seed = seeds[index] if seeds and index < len(seeds) else None
            result = self.play_game(seed)
            log.info("进度 %d/%d  胜率 %.1f%%  平均底注 %.2f",
                     index + 1, games, self.stats.win_rate * 100,
                     self.stats.average_ante)
            if on_result:
                on_result(result)
        return self.stats

    # ------------------------------------------------------------------ 决策

    def _record(self, state: GameState, action: Action, step: int) -> None:
        """把这一步决策写进日志（动作 + 理由 + 局面快照）。

        理由由策略层在决策过程中就地记下（`journal.note`），这里只负责取出来。
        这样"日志里显示的数字"和"真正用于决策的数字"必然是同一个。
        """
        if self.journal is None:
            return
        try:
            method, params, detail = _translate(action)
        except Exception:  # pragma: no cover - 日志不该影响主流程
            method, params, detail = type(action).__name__, {}, ""
        snapshot = {
            "ante": state.ante,
            "round": state.round_num,
            "money": state.money,
            "chips": state.round.chips,
            "need": state.score_remaining,
            "hands": state.round.hands_left,
            "discards": state.round.discards_left,
            "jokers": [c.label or c.key for c in state.jokers.cards],
            # 消耗牌槽的内容要记下来：排查"拿到的星球牌为什么没被用掉"时，
            # 没有这一项就分不清"没拿到"和"拿到了没用"（实测这两者都存在）。
            "consumables": [c.key for c in state.consumables.cards],
        }
        # 主 build 流派识别的结果（阶段一：只观测）。放在快照里而不是另开一条
        # 记录，是为了不动日志格式和面板的类别配色。
        if self._build_snapshot is not None:
            snapshot["build"] = self._build_snapshot.numbers()
        self.journal.record(
            step=step,
            state=state.state,
            action=method,
            detail=detail,
            reason=current_reason(),
            snapshot=snapshot,
        )

    def _blocked_consumables(self, state: GameState) -> set[str]:
        """这一步不该再碰的消耗牌：整局拉黑的 + 本状态本回合因瞬时锁冻结的。

        冻结按 (底注, 回合, 状态) 生效，所以下一回合、或换个状态都会重新尝试。
        """
        blocked = set(self._bad_consumables)
        current = (state.ante, state.round_num, state.state)
        blocked.update(
            key for key, where in self._deferred_consumables.items() if where == current
        )
        return blocked

    def _targets_for(self, state: GameState, count: int) -> list[int] | None:
        """给需要目标牌的消耗牌挑 `count` 张。

        优先**真正算分的那几张**，不够再从手里补——和 `strategy.consumable_targets`
        同一套口径。保持偏好顺序（不排序）：Death 那类"左变右"的牌，顺序有意义。
        """
        hand = state.hand.cards
        if count <= 0 or count > len(hand):
            return None
        best = Solver.from_state(state).best(hand)
        preferred = list(best.indices) if best is not None else []
        pool = preferred + [i for i in range(len(hand)) if i not in preferred]
        return pool[:count]

    def _refresh_build(self, state: GameState) -> None:
        """刷新"主 build 流派"识别（**阶段一：只记录，不改变任何决策**）。

        每个 (底注, 回合) 只算一次。识别用的是**静态成色占比**，不用真实边际
        贡献——后者会和阶段二循环依赖（详见 `build.py` 的模块文档）。
        """
        key = (state.ante, state.round_num)
        if self._build_for == key and self._build_snapshot is not None:
            return
        try:
            snapshot = build_snapshot(state, previous=self._build_committed)
        except Exception as exc:      # noqa: BLE001 - 观测功能绝不能影响对局
            log.debug("流派识别失败（忽略）：%s", exc)
            return
        self._build_snapshot = snapshot
        self._build_committed = snapshot.committed
        self._build_for = key

    def _dispatch(self, state: GameState, shop_rerolls: int) -> Action:
        """按当前状态决定下一步动作。"""
        clear_reason()          # 先清空，避免上一步的理由残留
        current = state.state
        self._refresh_build(state)

        if current == "BLIND_SELECT":
            return choose_blind_action(state, self.config)
        if current == "SELECTING_HAND":
            consumable = choose_consumable_action(
                state, self.config, self._blocked_consumables(state),
                archetype=self._build_committed,
            )
            if consumable is not None:
                return consumable

            # 每回合做一次"开局检查"：报告 Boss 盲注限制，并尝试免费的小丑牌重排。
            # 游戏按槽位从左到右结算，加法在前、乘区在后得分更高。
            round_key = (state.ante, state.round_num)
            if self._rearranged_for != round_key:
                self._rearranged_for = round_key
                notes = play_constraints(state, self.config).notes
                if notes:
                    # 这条不受 verbose 控制：Boss 规则会改变打法，必须留痕
                    log.info("   盲注限制：%s", "；".join(notes))
                if len(state.jokers.cards) >= 2:
                    reorder = choose_rearrange_action(state)
                    if reorder is not None:
                        return reorder

            # 解除 Boss 压制：目前只有 Verdant Leaf（全体卡牌被压制，
            # 卖任意一张小丑牌即解除）。不解除的话整回合只能用牌型基础
            # 筹码打分，Ante 8 几乎必然失败。
            if self._sold_for != round_key:
                sell = choose_sell_action(state, self.config)
                if sell is not None:
                    self._sold_for = round_key
                    log.info("   卖出小丑牌 #%d 以解除 %s 的卡牌压制",
                             sell.joker, state.current_blind.name)
                    return sell

            # 不传 rng：弃牌估计按局面确定性播种，保证同一种子可复现
            action = choose_play_action(state, self.config)
            if self.verbose:
                self._log_play_plan(state, action)
            return action

        if current == "ROUND_EVAL":
            return CashOut()

        if current == "SHOP":
            # 商店里也**先用消耗牌**，再决定买什么。
            #
            # `use` 端点在 SHOP 状态是允许的（`use.lua:39` 的 requires_state
            # 就含 SHOP），而且必须在这里做：星球牌早用一级就早一回合吃到等级加成，
            # 而且**腾出消耗牌槽**——槽满了从包里拿星球牌会静默丢失
            # （mod 的 pack.lua 不校验消耗牌位）。
            # 需要手牌目标的塔罗在商店里没有手牌，`choose_consumable_action`
            # 自己会跳过它们，所以这里是安全的。
            consumable = choose_consumable_action(
                state, self.config, self._blocked_consumables(state),
                archetype=self._build_committed,
            )
            if consumable is not None:
                return consumable
            return choose_shop_action(
                state, self.config, shop_rerolls,
                archetype=self._build_committed,
            )

        if current in PACK_STATES:
            if self._skip_pack:
                return OpenPack(skip=True)
            return choose_pack_action(state, self.config,
                                      archetype=self._build_committed)

        if current in TRANSIENT_STATES:
            return Refresh()

        # 未知状态：只刷新状态，交给卡死保护兜底
        log.debug("未知状态 %s，仅刷新状态", current)
        return Refresh()

    @staticmethod
    def _log_play_plan(state: GameState, action: Action) -> None:
        """把"这一手能打多少分、还差多少"写进日志。

        排错时最需要的就是这两个数：预测得分明显偏低说明模型有问题，
        预测得分够高却打不过说明策略在浪费手数。
        """
        round_info = state.round
        try:
            solver = Solver.from_state(state)
        except Exception:  # pragma: no cover - 日志不该影响主流程
            return

        if isinstance(action, Play):
            breakdown = solver.score(state.hand.cards, action.indices)
            log.info(
                "   计划：%s ｜ 本轮还差 %d 分（剩 %d 手 / %d 弃牌）",
                breakdown.describe(), state.score_remaining,
                round_info.hands_left, round_info.discards_left,
            )
        elif isinstance(action, Discard):
            best = solver.best(state.hand.cards)
            current = f"{best.total:.0f}" if best else "?"
            log.info(
                "   计划：弃 %d 张找牌 ｜ 当前最好 %s 分，本轮还差 %d 分"
                "（剩 %d 手 / %d 弃牌）",
                len(action.indices), current, state.score_remaining,
                round_info.hands_left, round_info.discards_left,
            )

    def _force_action(self, state: GameState) -> Action:
        """卡住时的保底动作：保证一定能让状态往前走。"""
        current = state.state
        if current == "BLIND_SELECT":
            return SelectBlind()
        if current == "SELECTING_HAND":
            solver = Solver.from_state(state)
            best = solver.best(state.hand.cards)
            if best is not None:
                return Play(best.indices)
            # 手牌为空说明状态不正常，刷新比乱发操作安全
            return Refresh()
        if current == "SHOP":
            return NextRound()
        if current in PACK_STATES:
            return OpenPack(skip=True)
        if current == "ROUND_EVAL":
            return CashOut()
        return Refresh()

    # ------------------------------------------------------------------ 执行

    def _apply(self, action: Action) -> GameState:
        """执行动作，处理可恢复的错误，返回新状态。

        对 INVALID_STATE / NOT_ALLOWED / BAD_REQUEST 都会重新同步状态而不是
        直接放弃——这些绝大多数只是本地状态比游戏慢了一步。
        """
        method, params, label = _translate(action)
        if self.verbose:
            log.info("→ %s", label)

        if self.dry_run:
            return GameState(self.client.gamestate())

        try:
            raw = getattr(self.client, method)(**params)
        except BalatroRPCError as exc:
            handled = self._handle_error(exc, action, method, params)
            if handled is not None:
                return handled
            raise
        return GameState(raw)

    def _handle_error(
        self,
        exc: BalatroRPCError,
        action: Action,
        method: str,
        params: dict,
    ) -> GameState | None:
        """尝试从一次失败的调用里恢复；返回新状态表示已恢复，None 表示放弃。"""
        log.warning("调用 %s 失败：%s", method, exc)

        # mod 的 **UI 就绪竞态**：正常速度下选完盲注之后，游戏的 UI 还没重建完，
        # 而 mod 会直接索引 `G.buttons.UIRoot`——
        #     discard.lua:91   UIBox:get_UIE_by_ID("discard_button", G.buttons.UIRoot)
        #     play.lua:79      UIBox:get_UIE_by_ID("play_button",    G.buttons.UIRoot)
        # 那一刻 `G.buttons` 还是 nil，于是抛 INTERNAL_ERROR、整局被中止。
        #
        # fast 模式下动画快 6 倍，撞不上这个窗口；正常速度下第一次 discard/play
        # 几乎必然踩中（实测）。这不是我们的调用有问题，等一下重试就好。
        if is_ui_readiness_race(exc):
            log.info("   UI 还没建好（%s），等 %.1f 秒后重试",
                     method, self.ui_retry_delay)
            time.sleep(self.ui_retry_delay)
            try:
                self.client.gamestate()          # 先刷新一次，给游戏推进的时间
                return GameState(getattr(self.client, method)(**params))
            except BalatroRPCError as retry_exc:
                log.warning("   UI 竞态重试仍然失败：%s", retry_exc)
                return GameState(self.client.gamestate())

        # 消耗牌失败分三类。**只有第三类才该整局拉黑**——前两类是"现在不行"
        # 而不是"这张牌没用"，拉黑会让星球牌（以及造星球牌的 High Priestess）
        # 一整局报废。实测 20 局里 c_mercury 因此被拉黑 3 次，
        # 后果是"拿到星球牌 0.90 张/局、只用掉 0.40 张"。
        if method == "use":
            state = GameState(self.client.gamestate())
            index = params.get("consumable")
            if isinstance(index, int) and index < len(state.consumables.cards):
                card = state.consumables.cards[index]
                key = card.key
                label = card.label or card.key
                given = params.get("cards")
                given_count = len(given) if given else 0

                # ① 张数不对：mod 的报错自带张数，按它纠正再试一次。
                #    不靠硬编码的张数表——那张表已经错过一次（c_chariot）。
                needed = required_card_count(str(exc))
                if needed is not None and needed != given_count:
                    targets = self._targets_for(state, needed)
                    if targets is not None:
                        try:
                            return GameState(self.client.use(index, targets))
                        except BalatroRPCError as retry_exc:
                            log.warning("按报错张数（%d 张）重试仍失败：%s",
                                        needed, retry_exc)

                # ② 瞬时锁（游戏在播动画）：等一下重试；仍失败只冻结**本回合**，
                #    下一回合的 SELECTING_HAND 会再试。
                if is_transient_use_lock(exc):
                    time.sleep(self.ui_retry_delay)
                    try:
                        self.client.gamestate()
                        return GameState(self.client.use(index, given))
                    except BalatroRPCError as retry_exc:
                        log.info("   %s 是动画期间的瞬时锁（%s），这一步先不用，"
                                 "换状态或下一回合再试", label, retry_exc)
                        self._deferred_consumables[key] = (
                            state.ante, state.round_num, state.state
                        )
                    return state

                # ③ 其它：真的不能用，整局拉黑
                self._bad_consumables.add(key)
                log.info("标记消耗牌 %s（%s）为不可用，本局不再尝试", key, exc.name)
            return state

        # 开包选牌被拒 → 直接跳过这个包
        if method == "open_pack" and exc.name in (ERR_BAD_REQUEST, ERR_NOT_ALLOWED):
            state = GameState(self.client.gamestate())
            try:
                return GameState(self.client.open_pack(skip=True))
            except BalatroRPCError as retry_exc:
                log.warning("跳过补充包也失败：%s", retry_exc)
                self._skip_pack = True
            return state

        # 常见的状态过期：重新同步
        if exc.name in (ERR_BAD_REQUEST, ERR_NOT_ALLOWED) or exc.is_state_error:
            try:
                return GameState(self.client.gamestate())
            except (BalatroRPCError, BalatroConnectionError):
                return None
        return None


def is_transient_use_lock(exc: BalatroRPCError | BaseException) -> bool:
    """这是不是"游戏正在播动画"导致的瞬时拒绝（而不是这张牌真的不能用）。

    来源：`Card:can_use_consumeable()`（`card.lua:1524-1527`）在
    `G.play` 里有牌、`G.CONTROLLER.locked`、或 `G.GAME.STOP_USE > 0` 时返回
    false，mod 于是报 `NOT_ALLOWED: Consumable 'X' cannot be used at this time`。

    这三种都是**几百毫秒后就会消失**的状态，等一等再试就行。原来把它当成
    "永久不可用"整局拉黑，后果很重：实测 20 局里 `c_mercury`（升 Pair 的星球牌）
    因此被拉黑 3 次，那一整局的星球成长就断了——拿到的星球牌一半用不掉。
    """
    text = str(exc)
    return ERR_NOT_ALLOWED in text and "cannot be used at this time" in text


# mod 的张数校验报错是**自描述**的（`use.lua:100-141`）：
#     "Consumable 'X' requires exactly N card(s) (provided: M)"
#     "Consumable 'X' requires at least N card(s) (provided: M)"
#     "Consumable 'X' requires at most  N card(s) (provided: M)"
# 所以按报错纠正张数即可，**不需要维护一张 18 项的对照表**——
# 那张表已经错过一次（c_chariot 写成"最多 2 张"，游戏实为"恰好 1 张"）。
_REQUIRED_CARDS = re.compile(r"requires (?:exactly|at least|at most) (\d+) card")


def required_card_count(message: str) -> int | None:
    """从 mod 的报错里取出"这张消耗牌要几张目标牌"；读不出返回 None。"""
    match = _REQUIRED_CARDS.search(message or "")
    return int(match.group(1)) if match else None


def is_ui_readiness_race(exc: BalatroRPCError | BaseException) -> bool:
    """这是不是 mod 的"UI 还没建好"竞态错误。

    特征：mod 报 INTERNAL_ERROR，且消息里提到 `G.buttons` / `buttons` /
    `UIE_by_ID`——也就是它在 UI 元素还没生成时就去索引了。

    单独抽成函数是为了：① 主循环和测试用同一套判据；② 以后 mod 修了之后
    只要这里返回 False 就能整体关掉这个补丁。
    """
    text = str(exc)
    if ERR_INTERNAL not in text:
        return False
    return "buttons" in text or "UIE_by_ID" in text


def _translate(action: Action) -> tuple[str, dict, str]:
    """把动作对象翻译成 (客户端方法名, 关键字参数, 日志文本)。"""
    match action:
        case SelectBlind():
            return "select", {}, "选中盲注"
        case SkipBlind():
            return "skip", {}, "跳过盲注"
        case Play(indices):
            return "play", {"cards": list(indices)}, f"出牌 {list(indices)}"
        case Discard(indices):
            return "discard", {"cards": list(indices)}, f"弃牌 {list(indices)}"
        case CashOut():
            return "cash_out", {}, "结算回合"
        case NextRound():
            return "next_round", {}, "离开商店 / 推进"
        case Refresh():
            return "gamestate", {}, "刷新状态"
        case Rearrange(jokers):
            return "rearrange", {"jokers": list(jokers)}, f"重排小丑牌为 {list(jokers)}"
        case Buy(card=card, voucher=voucher, pack=pack):
            params = {}
            if card is not None:
                params["card"] = card
            if voucher is not None:
                params["voucher"] = voucher
            if pack is not None:
                params["pack"] = pack
            return "buy", params, f"购买 {params}"
        case Sell(joker=joker, consumable=consumable):
            params = {}
            if joker is not None:
                params["joker"] = joker
            if consumable is not None:
                params["consumable"] = consumable
            return "sell", params, f"卖出 {params}"
        case Reroll():
            return "reroll", {}, "重摇商店"
        case UseConsumable(index, cards):
            params: dict = {"consumable": index}
            if cards:
                params["cards"] = list(cards)
            return "use", params, f"使用消耗牌 #{index}"
        case OpenPack(card=card, targets=targets, skip=skip):
            params = {}
            if card is not None:
                params["card"] = card
            if targets:
                params["targets"] = list(targets)
            if skip:
                params["skip"] = True
            return "open_pack", params, f"开包选牌 {params}"
    raise TypeError(f"无法识别的动作：{action!r}")


def setup_logging(verbose: bool = True, logfile: str | None = None) -> None:
    """配置日志：默认打到标准输出，可选同时写文件。"""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logfile:
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def wait_for_game(client: BalatroClient, timeout: float = 60.0,
                  interval: float = 1.0) -> bool:
    """等游戏和 mod 就绪。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.is_alive():
            return True
        time.sleep(interval)
    return False
