"""参数调优：用真机对局数据修改策略参数。

对应策略规格的最后一条：

    "为每一项决策（买小丑牌、消费、丢牌等行为）设置函数，通过不断地测试来
     修改参数以达到通关目标。"

做法是最朴素的**坐标下降**：一次只动一个参数，在一组候选值上各跑一遍评测，
留下平均底注最高的那个，再动下一个参数。参数之间互相影响的损失由多轮
（--rounds）来补偿。

需要游戏在跑（评测要驱动真实对局）：

    tools\\start_game.cmd                     # 另一个窗口
    python tools\\tune.py --games 20 --rounds 2
    python tools\\tune.py --list               # 只看有哪些参数可调

注意 40 局评测约 12 分钟，参数一多耗时会成倍增长。先用 --games 12 粗调，
再对最有希望的少数参数用 --games 40 细调。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from balatro_bot.bot import Bot  # noqa: E402
from balatro_bot.params import DEFAULT_PARAMS_PATH, TunableParams  # noqa: E402
from balatro_bot.rpc import BalatroClient  # noqa: E402
from balatro_bot.strategy import Config  # noqa: E402

# 每个参数的候选网格。范围依据是对局观察，不是穷举。
# 想调别的参数就加到这里，或者用 --param/--values 临时指定。
GRIDS: dict[str, list[float]] = {
    "discard_aversion": [0.1, 0.2, 0.28, 0.4, 0.6],
    "discard_bonus": [0.0, 0.06, 0.12, 0.25],
    "gamma_early": [0.4, 0.55, 0.7, 0.85],
    "gamma_late": [1.0, 1.2, 1.45, 1.8],
    "switch_point": [0.4, 0.6, 0.8, 1.2],
    "big_hand_reference": [100.0, 140.0, 200.0, 300.0],
    "buy_threshold": [3.5, 4.0, 4.5, 5.0, 5.5],
    "synergy_bonus": [0.0, 1.0, 1.5, 2.5],
    "synergy_penalty": [0.0, 1.0, 2.0, 3.0],
    "empty_slot_bonus": [0.0, 1.0, 2.0, 3.0],
    "multi_bonus": [0.0, 1.0, 2.0, 3.0],
    "interest_floor": [10.0, 15.0, 25.0, 40.0],
    "interest_floor_ante": [2.0, 3.0, 4.0, 5.0],
    "money_reserve": [0.0, 4.0, 8.0, 12.0],
    "reroll_min_money": [10.0, 15.0, 20.0, 30.0],
    "prefer_hand_tolerance": [0.0, 0.06, 0.12, 0.2],
    "keep_invested_level": [2.0, 3.0, 4.0],
}


@dataclass
class Score:
    """一组参数在一批种子上的成绩。"""

    games: int
    wins: int
    average_ante: float
    median_ante: float
    errors: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    def key(self) -> tuple:
        """排序依据：先看通关数，再看平均底注，最后看中位数（更抗离群）。"""
        return (self.wins, round(self.average_ante, 3), round(self.median_ante, 3))

    def describe(self) -> str:
        return (f"通关 {self.wins}/{self.games}  平均底注 {self.average_ante:.2f}  "
                f"中位 {self.median_ante:.0f}  出错 {self.errors}")


class Evaluator:
    """在一批固定种子上评测一组参数。"""

    def __init__(self, client: BalatroClient, deck: str, stake: str, base: Config) -> None:
        self.client = client
        self.deck = deck
        self.stake = stake
        self.base = base

    def run(self, params: TunableParams, seeds: list[str]) -> Score:
        config = Config(
            deck=self.deck, stake=self.stake,
            params=params,
            discard_strategy=self.base.discard_strategy,
            use_discards=self.base.use_discards,
            skip_blinds=self.base.skip_blinds,
        )
        bot = Bot(self.client, config, verbose=False)
        antes: list[int] = []
        wins = 0
        errors = 0
        for seed in seeds:
            result = bot.play_game(seed)
            antes.append(result.ante)
            if result.won:
                wins += 1
            if result.error:
                errors += 1
        return Score(
            games=len(seeds), wins=wins,
            average_ante=statistics.mean(antes) if antes else 0.0,
            median_ante=statistics.median(antes) if antes else 0.0,
            errors=errors,
        )


def coordinate_descent(
    evaluator: Evaluator,
    params: TunableParams,
    seeds: list[str],
    names: list[str],
    rounds: int,
    log,
) -> tuple[TunableParams, Score]:
    best_params = params
    best_score = evaluator.run(best_params, seeds)
    log(f"起点：{best_score.describe()}")

    for round_index in range(1, rounds + 1):
        improved = False
        for name in names:
            grid = GRIDS.get(name)
            if not grid:
                log(f"  跳过 {name}（没有候选网格）")
                continue
            current = getattr(best_params, name)
            trials: list[tuple[float, Score]] = []
            for value in grid:
                if value == current:
                    continue
                candidate = best_params.replace(**{name: value})
                score = evaluator.run(candidate, seeds)
                trials.append((value, score))
                log(f"  [{name}={value}] {score.describe()}")
            if not trials:
                continue
            value, score = max(trials, key=lambda pair: pair[1].key())
            if score.key() > best_score.key():
                best_params = best_params.replace(**{name: value})
                best_score = score
                improved = True
                log(f"  ✓ {name} → {value}（{score.describe()}）")
            else:
                log(f"  · {name} 保持 {current}")

        if not improved:
            log(f"第 {round_index} 轮没有改进，提前结束")
            break
    return best_params, best_score


def main() -> int:
    parser = argparse.ArgumentParser(description="用对局数据调优策略参数")
    parser.add_argument("--games", type=int, default=12, help="每组参数跑多少局")
    parser.add_argument("--rounds", type=int, default=1, help="坐标下降轮数")
    parser.add_argument("--param", action="append", default=[],
                        help="只调这些参数（可重复）；默认调全部有网格的参数")
    parser.add_argument("--values", default=None,
                        help="配合单个 --param 使用，逗号分隔的候选值")
    parser.add_argument("--params", default=None, help="从 JSON 读取起始参数")
    parser.add_argument("--out", default=str(DEFAULT_PARAMS_PATH),
                        help="最优参数写到哪里")
    parser.add_argument("--seed-prefix", default="BOT")
    parser.add_argument("--deck", default="RED")
    parser.add_argument("--stake", default="WHITE")
    parser.add_argument("--discard-strategy", choices=("plan", "monte-carlo"),
                        default="plan")
    parser.add_argument("--list", action="store_true", help="列出可调参数和候选值后退出")
    args = parser.parse_args()

    if args.list:
        print(f"可调参数（{len(GRIDS)} 个有候选网格）：")
        for name, grid in GRIDS.items():
            print(f"  {name:26} {grid}")
        print("\n其它可调参数（需自己用 --values 给候选）：")
        for name in TunableParams().to_dict():
            if name not in GRIDS:
                print(f"  {name}")
        return 0

    if args.values and len(args.param) != 1:
        print("--values 必须配合恰好一个 --param 使用")
        return 2

    names = args.param or [name for name in GRIDS]
    if args.values:
        GRIDS[args.param[0]] = [float(v) for v in args.values.split(",") if v.strip()]

    client = BalatroClient()
    if not client.is_alive():
        print("连不上游戏。先在另一个窗口运行 tools\\start_game.cmd")
        return 1

    params = TunableParams.load(args.params) if args.params else TunableParams()
    seeds = [f"{args.seed_prefix}{i:03d}" for i in range(args.games)]
    base = Config(discard_strategy=args.discard_strategy)

    print(f"评测规模：每组参数 {len(seeds)} 局，种子 {seeds[0]}..{seeds[-1]}")
    print(f"待调参数：{', '.join(names)}，{args.rounds} 轮\n")

    def log(message: str) -> None:
        print(message, flush=True)

    evaluator = Evaluator(client, args.deck.upper(), args.stake.upper(), base)
    best_params, best_score = coordinate_descent(
        evaluator, params, seeds, names, args.rounds, log
    )

    path = best_params.save(args.out)
    print(f"\n最优：{best_score.describe()}")
    print(f"参数已保存到 {path}")
    print(json.dumps(best_params.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
