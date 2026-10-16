"""算分模型对真机的自检。

算分是整个机器人的地基：它错了，出牌和弃牌的判断就全错，而且从日志里
看不出来（分数只是"看起来不太对"）。这个工具打一整局，逐手对比
预测分数与游戏实际给出的分数，用来验证模型、并暴露哪些小丑牌还没建模。

    python tools/verify_scoring.py --games 3
    python tools/verify_scoring.py --seed TEST123

判读方法：
    * 比值接近 1.00 → 模型准确
    * 比值持续 > 1    → 模型低估计分（通常是遇到了未建模的小丑牌）
    * 比值 < 1        → 模型高估（多半是小丑牌条件判断写错了）
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from balatro_bot.jokers import unknown_jokers  # noqa: E402
from balatro_bot.rpc import BalatroClient  # noqa: E402
from balatro_bot.state import GameState  # noqa: E402
from balatro_bot.strategy import Config, best_play_choice, build_solver  # noqa: E402

TRANSIENT = {"HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND", "PLAY_TAROT"}
PACK_STATES = {"SMODS_BOOSTER_OPENED", "TAROT_PACK", "PLANET_PACK",
               "SPECTRAL_PACK", "STANDARD_PACK", "BUFFOON_PACK"}


@dataclass
class Observation:
    ante: int
    round_num: int
    blind: str
    hand_name: str
    cards: str
    predicted: float
    actual: float
    jokers: tuple[str, ...]

    @property
    def ratio(self) -> float:
        return self.actual / self.predicted if self.predicted else float("inf")


def play_game(client: BalatroClient, seed: str, deck: str, stake: str,
              max_steps: int = 400) -> list[Observation]:
    client.menu()
    state = GameState(client.start(deck, stake, seed))
    observations: list[Observation] = []
    config = Config()
    steps = 0

    while not state.is_game_over and steps < max_steps:
        steps += 1
        current = state.state

        if current == "BLIND_SELECT":
            state = GameState(client.select())
        elif current == "SELECTING_HAND":
            # 用机器人**真实**的决策（含 Boss 盲注约束和垫牌技巧），
            # 而不是朴素的最优解——否则会在 The Psychic 这类盲注下
            # 打出不计分的牌，得到一堆假的"预测有分实际 0 分"。
            solver, constraints = build_solver(state, config)
            best = best_play_choice(solver, state.hand.cards, constraints)
            if best is None:
                state = GameState(client.gamestate())
                continue
            cards = [state.hand.cards[i] for i in best.indices]
            before = state.round.chips
            names = tuple(c.label or c.key for c in state.jokers.cards)
            blind = state.current_blind.name
            after = GameState(client.play(list(best.indices)))
            gained = after.round.chips - before
            # 过关后 round.chips 会归零，用盲注进度兜底
            if gained <= 0 and after.state == "ROUND_EVAL":
                gained = state.required_score - before
            observations.append(Observation(
                ante=state.ante, round_num=state.round_num, blind=blind,
                hand_name=best.hand_name,
                cards=" ".join(str(c) for c in cards),
                predicted=best.total, actual=float(gained), jokers=names,
            ))
            state = after
        elif current == "ROUND_EVAL":
            state = GameState(client.cash_out())
        elif current == "SHOP":
            state = GameState(client.next_round())
        elif current in PACK_STATES:
            try:
                state = GameState(client.open_pack(skip=True))
            except Exception:
                state = GameState(client.gamestate())
        elif current in TRANSIENT:
            state = GameState(client.gamestate())
        else:
            state = GameState(client.gamestate())

    return observations


def report(observations: list[Observation], verbose: bool) -> None:
    if not observations:
        print("没有采集到任何一次出牌")
        return

    print(f"\n共采集 {len(observations)} 次出牌\n")
    if verbose:
        print(f"{'底注':>4} {'盲注':<12} {'手牌':<18} {'预测':>10} {'实际':>10} {'比值':>7}  小丑牌")
        print("-" * 108)
        for obs in observations:
            print(f"{obs.ante:>4} {obs.blind:<12} {obs.hand_name:<18} "
                  f"{obs.predicted:>10.0f} {obs.actual:>10.0f} {obs.ratio:>7.2f}  "
                  f"{', '.join(obs.jokers)}")

    exact = [o for o in observations if abs(o.ratio - 1.0) < 1e-6]
    close = [o for o in observations if abs(o.ratio - 1.0) < 0.02]
    ratios = [o.ratio for o in observations if o.predicted > 0]

    print()
    print(f"完全一致       {len(exact)}/{len(observations)}"
          f"  ({len(exact) / len(observations) * 100:.0f}%)")
    print(f"误差 2% 以内   {len(close)}/{len(observations)}"
          f"  ({len(close) / len(observations) * 100:.0f}%)")
    if ratios:
        print(f"比值中位数     {statistics.median(ratios):.3f}")
        print(f"比值均值       {statistics.mean(ratios):.3f}")

    # 没预测准的那些，看看当时带了什么小丑牌（多半是没建模的）
    missed: dict[str, int] = {}
    for obs in observations:
        if abs(obs.ratio - 1.0) >= 0.02:
            for key in unknown_jokers(obs.jokers):
                missed[key] = missed.get(key, 0) + 1
    if missed:
        print("\n失准时出现的未建模小丑牌（按出现次数）：")
        for key, count in sorted(missed.items(), key=lambda kv: -kv[1]):
            print(f"  {count:3d}  {key}")


def main() -> int:
    parser = argparse.ArgumentParser(description="对比预测分数与游戏实际分数")
    parser.add_argument("--games", type=int, default=1)
    parser.add_argument("--seed", default=None)
    parser.add_argument("--deck", default="RED")
    parser.add_argument("--stake", default="WHITE")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总")
    args = parser.parse_args()

    client = BalatroClient()
    if not client.is_alive():
        print("连不上游戏，先用 tools\\start_game.cmd 启动")
        return 1

    all_obs: list[Observation] = []
    for index in range(args.games):
        seed = args.seed or f"VERIFY{index:02d}"
        print(f"开局 {index + 1}/{args.games}，种子 {seed}")
        all_obs.extend(play_game(client, seed, args.deck, args.stake))

    report(all_obs, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
