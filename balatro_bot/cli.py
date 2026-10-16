"""命令行入口。

    python -m balatro_bot status                     看游戏和 mod 是否就绪
    python -m balatro_bot play --games 1             打一局
    python -m balatro_bot eval --games 20            批量跑固定种子，统计通关率
    python -m balatro_bot solve                      只看当前局面会怎么打（不下指令）
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import asdict

from .bot import Bot, setup_logging, wait_for_game
from .params import TunableParams
from .rpc import DEFAULT_HOST, DEFAULT_PORT, BalatroClient, BalatroConnectionError

# 决策日志默认路径。放在 runs/ 下（已 gitignore），窗口默认也读这里。
DEFAULT_JOURNAL = "runs/journal.jsonl"
from .journal import Journal
from .solver import Solver
from .state import GameState
from .strategy import Config, choose_play_action
from .scoring import describe_jokers


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=DEFAULT_HOST, help="mod 监听地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="mod 监听端口")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--log-file", default=None, help="同时把日志写入文件")
    parser.add_argument("--journal", default=DEFAULT_JOURNAL, metavar="PATH",
                        help=f"把每一步决策（做了什么 + 为什么）写成 JSONL，"
                             f"供 tools/decision_log.py 实时显示（默认 {DEFAULT_JOURNAL}）")
    parser.add_argument("--no-journal", action="store_true", help="不写决策日志")
    parser.add_argument("--no-mod-journal", action="store_true",
                        help="不把决策推给游戏内的日志面板（默认推；面板用 "
                             "mod_additions/install.py 安装）")


def _add_game_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deck", default="RED", help="牌组（RED/BLUE/... 共 15 种）")
    parser.add_argument("--stake", default="WHITE", help="难度（WHITE/RED/... 共 8 种）")
    parser.add_argument("--seed", default=None, help="固定种子，用于可复现实验")
    parser.add_argument("--skip-blinds", action="store_true",
                        help="跳过有高价值标签的盲注（默认不跳）")
    parser.add_argument("--no-discards", action="store_true", help="禁用弃牌搜索（排错用）")
    parser.add_argument("--discard-strategy", choices=("plan", "monte-carlo"),
                        default="plan",
                        help="弃牌决策算法：plan=概率评分函数（默认），"
                             "monte-carlo=补牌采样")
    parser.add_argument("--discard-samples", type=int, default=32,
                        help="monte-carlo 路径的补牌样本数")
    parser.add_argument("--decision-delay", type=float, default=0.0,
                        help="每次决策前睡多少秒（节流），0=全速")
    parser.add_argument("--params", default=None,
                        help="从 JSON 读取可调参数（见 tools/tune.py）")
    parser.add_argument("--save-params", default=None,
                        help="把本次使用的参数（含自动生成的初始值）写出到 JSON")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="临时覆盖单个参数，可重复，例如 --set discard_aversion=0.3")


def _apply_overrides(params, overrides: list[str]):
    """处理 --set KEY=VALUE。类型按原字段推断，写错立刻报错而不是静默忽略。"""
    if not overrides:
        return params
    data = params.to_dict()
    for item in overrides:
        if "=" not in item:
            raise SystemExit(f"--set 需要 KEY=VALUE 形式，收到 {item!r}")
        key, raw = item.split("=", 1)
        key = key.strip()
        if key not in data:
            raise SystemExit(f"没有这个参数：{key!r}")
        current = data[key]
        try:
            if isinstance(current, bool):
                value = raw.strip().lower() in ("1", "true", "yes")
            elif isinstance(current, int):
                value = int(raw)
            else:
                value = float(raw)
        except ValueError as exc:
            raise SystemExit(f"--set {key}={raw} 类型不对：{exc}") from exc
        data[key] = value
    return params.from_dict(data)


def build_config(args: argparse.Namespace) -> Config:
    params = TunableParams.load(args.params) if getattr(args, "params", None) else TunableParams()
    params = _apply_overrides(params, getattr(args, "set", []) or [])
    return Config(
        deck=args.deck.upper(),
        stake=args.stake.upper(),
        seed=args.seed,
        skip_blinds=args.skip_blinds,
        use_discards=not args.no_discards,
        discard_strategy=args.discard_strategy,
        discard_samples=args.discard_samples,
        decision_delay=args.decision_delay,
        params=params,
    )


def _client(args: argparse.Namespace) -> BalatroClient:
    return BalatroClient(host=args.host, port=args.port)


def _maybe_save_params(args: argparse.Namespace, config: Config) -> None:
    """--save-params：把本次实际使用的参数写出来，方便复现和调参。"""
    target = getattr(args, "save_params", None)
    if not target:
        return
    path = config.params.save(target)
    print(f"参数已写出：{path}")


def cmd_status(args: argparse.Namespace) -> int:
    client = _client(args)
    if not client.is_alive():
        print(f"连不上 {client.url}")
        print("检查清单：")
        print("  1. 游戏是否已经在运行")
        print("  2. 是否用 `uvx balatrobot serve` 启动的（mod 需要它注入环境变量）")
        print("  3. 端口是否一致（--port）")
        return 1

    state = GameState(client.gamestate())
    print(f"连接正常：{client.url}")
    print(f"状态：{state.state}")
    print(f"底注 {state.ante} / 第 {state.round_num} 回合 / 钱 ${state.money}")
    print(f"分数 {state.score} / 需要 {state.required_score}")
    print(f"手牌 {len(state.hand.cards)} 张，小丑牌 {len(state.jokers.cards)} 张")
    print(f"小丑牌：{describe_jokers(state)}")
    return 0


def cmd_solve(args: argparse.Namespace) -> int:
    """对当前局面做一次求解，只打印结果，不发送任何操作。"""
    client = _client(args)
    state = GameState(client.gamestate())
    if state.state != "SELECTING_HAND":
        print(f"当前状态是 {state.state}，只有 SELECTING_HAND 才能求解出牌")
        return 1

    solver = Solver.from_state(state)
    plays = solver.enumerate(state.hand.cards)[: args.top]
    print(f"手牌：{[str(c) for c in state.hand.cards]}")
    print(f"小丑牌：{describe_jokers(state)}")
    print(f"需要 {state.score_remaining} 分，剩 {state.round.hands_left} 手 / "
          f"{state.round.discards_left} 次弃牌")
    print()
    for rank, candidate in enumerate(plays, 1):
        cards = [str(state.hand.cards[i]) for i in candidate.indices]
        print(f"{rank:2d}. {candidate.score.describe()}  {cards}")

    rng = None  # 弃牌估计按局面确定性播种，保证同一种子可复现
    action = choose_play_action(state, build_config(args), rng)
    print()
    print(f"策略层决定：{action}")
    return 0


def _make_journal(args: argparse.Namespace, client=None):
    """按命令行开关建一个决策日志。

    两个出口：
    * JSONL 文件 —— 供 tools/decision_log.py 的独立窗口和事后复盘；
    * mod 端点   —— 面板直接画在游戏窗口右侧（`mod_additions/install.py` 安装）。

    `--no-journal` 关掉文件出口；`--no-mod-journal` 关掉面板出口。
    """
    if getattr(args, "no_journal", False):
        return None
    path = getattr(args, "journal", None)
    if not path and (client is None or getattr(args, "no_mod_journal", False)):
        return None
    push_client = None if getattr(args, "no_mod_journal", False) else client
    return Journal(path or None, client=push_client)


def cmd_play(args: argparse.Namespace) -> int:
    setup_logging(args.verbose, args.log_file)
    client = _client(args)
    if not wait_for_game(client, timeout=args.wait):
        print(f"等待 {args.wait} 秒仍连不上 {client.url}")
        return 1

    config = build_config(args)
    _maybe_save_params(args, config)
    journal = _make_journal(args, client)
    try:
        bot = Bot(client, config, verbose=args.verbose, journal=journal)
        result = bot.play_game(args.seed)
    finally:
        if journal is not None:
            journal.close()
    print(result.summary)
    return 0 if result.won else 1


def cmd_eval(args: argparse.Namespace) -> int:
    """批量跑种子，统计通关率。"""
    setup_logging(args.verbose, args.log_file)
    client = _client(args)
    if not wait_for_game(client, timeout=args.wait):
        print(f"等待 {args.wait} 秒仍连不上 {client.url}")
        return 1

    if args.seeds_file:
        with open(args.seeds_file, encoding="utf-8") as handle:
            seeds = [line.strip() for line in handle if line.strip()]
    else:
        seeds = [f"{args.seed_prefix}{i:03d}" for i in range(args.games)]

    config = build_config(args)
    _maybe_save_params(args, config)
    journal = _make_journal(args, client)
    bot = Bot(client, config, verbose=args.verbose, journal=journal)

    results = []

    def on_result(result) -> None:
        results.append(result)
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as handle:
                json.dump([asdict(r) for r in results], handle,
                          ensure_ascii=False, indent=2)

    concurrency_note = ""
    if len(seeds) < args.games:
        concurrency_note = f"（种子文件只有 {len(seeds)} 个，按实际数量跑）"

    print(f"开始评测 {len(seeds)} 局{concurrency_note}")
    bot.run(len(seeds), seeds, on_result=on_result)

    stats = bot.stats
    if journal is not None:
        journal.close()
    print()
    print("=" * 46)
    print(f"总局数    {stats.games}")
    print(f"通关      {stats.wins}")
    print(f"通关率    {stats.win_rate * 100:.1f}%")
    print(f"平均底注  {stats.average_ante:.2f}")
    print(f"出错局数  {stats.errors}")
    print("=" * 46)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """打开决策日志窗口。

    实现在 `tools/decision_log.py`：它是独立的（只 tail 一个 JSONL 文件，
    不依赖机器人进程），所以机器人重启窗口也不用重开。
    """
    import subprocess
    from pathlib import Path

    tool = Path(__file__).resolve().parent.parent / "tools" / "decision_log.py"
    if not tool.exists():
        print(f"找不到 {tool}")
        return 1
    command = [sys.executable, str(tool), "--journal", args.journal,
               "--interval", str(args.interval)]
    if args.no_topmost:
        command.append("--no-topmost")
    if args.demo:
        command.append("--demo")
    return subprocess.call(command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="balatro-bot",
        description="通过 BalatroBot 接口自动玩小丑牌（Balatro）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="检查游戏与 mod 是否就绪")
    _add_common(status)
    status.set_defaults(func=cmd_status)

    solve = subparsers.add_parser("solve", help="求解当前局面（不发送操作）")
    _add_common(solve)
    _add_game_options(solve)
    solve.add_argument("--top", type=int, default=10, help="列出前几个候选打法")
    solve.set_defaults(func=cmd_solve)

    play = subparsers.add_parser("play", help="打一局")
    _add_common(play)
    _add_game_options(play)
    play.add_argument("--wait", type=float, default=60.0, help="等待游戏就绪的秒数")
    play.set_defaults(func=cmd_play)

    evaluate = subparsers.add_parser("eval", help="批量评测通关率")
    _add_common(evaluate)
    _add_game_options(evaluate)
    evaluate.add_argument("--games", type=int, default=20, help="跑多少局")
    evaluate.add_argument("--seeds-file", default=None, help="每行一个种子的文件")
    evaluate.add_argument("--seed-prefix", default="BOT", help="自动生成种子时的前缀")
    evaluate.add_argument("--json-out", default=None, help="把每局结果写成 JSON")
    evaluate.add_argument("--wait", type=float, default=60.0, help="等待游戏就绪的秒数")
    evaluate.set_defaults(func=cmd_eval)

    watch = subparsers.add_parser(
        "watch", help="打开决策日志窗口（摆在游戏旁边）"
    )
    watch.add_argument("--journal", default=DEFAULT_JOURNAL,
                       help=f"要显示的 JSONL（默认 {DEFAULT_JOURNAL}）")
    watch.add_argument("--interval", type=float, default=0.2, help="轮询间隔秒数")
    watch.add_argument("--no-topmost", action="store_true", help="不置顶")
    watch.add_argument("--demo", action="store_true", help="生成示例日志（不需要跑游戏）")
    watch.set_defaults(func=cmd_watch)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BalatroConnectionError as exc:
        print(f"连接失败：{exc}")
        return 2
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
