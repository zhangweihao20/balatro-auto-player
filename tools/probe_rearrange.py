"""探测 rearrange 接口在真机上是否可用。

代码里"把小丑牌排成加法在前、乘区在后"能免费加分，但这条路径依赖
rearrange 接口。这个脚本打一局，等拿到 2 张以上小丑牌后真的调一次重排，
确认接口形状正确、顺序确实变了。

    python tools/probe_rearrange.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from balatro_bot.rpc import BalatroClient, BalatroRPCError  # noqa: E402
from balatro_bot.state import GameState  # noqa: E402


def main() -> int:
    client = BalatroClient()
    if not client.is_alive():
        print("连不上游戏，先用 tools\\start_game.cmd 启动")
        return 1

    client.menu()
    state = GameState(client.start("RED", "WHITE", "PROBE01"))

    for _ in range(300):
        if state.is_game_over:
            break
        current = state.state
        if current == "BLIND_SELECT":
            state = GameState(client.select())
        elif current == "SELECTING_HAND":
            from balatro_bot.solver import Solver

            best = Solver.from_state(state).best(state.hand.cards)
            if best is None:
                state = GameState(client.gamestate())
                continue
            state = GameState(client.play(list(best.indices)))
        elif current == "ROUND_EVAL":
            state = GameState(client.cash_out())
        elif current == "SHOP":
            money = state.money
            bought = False
            # 优先买小丑牌，好凑够 2 张来测重排
            for index, card in enumerate(state.shop.cards):
                if card.set == "JOKER" and card.buy_cost <= money:
                    state = GameState(client.buy(card=index))
                    bought = True
                    break
            if not bought:
                for index, card in enumerate(state.shop.cards):
                    if card.buy_cost <= money:
                        state = GameState(client.buy(card=index))
                        bought = True
                        break
            if not bought:
                state = GameState(client.next_round())
        else:
            state = GameState(client.gamestate())

        jokers = state.jokers.cards
        if len(jokers) >= 2:
            print(f"拿到 {len(jokers)} 张小丑牌：{[c.label or c.key for c in jokers]}")
            identity = list(range(len(jokers)))
            swapped = identity[1:] + identity[:1]      # 轮转一位，保证是合法排列

            try:
                client.rearrange(jokers=identity)
                print(f"  rearrange(恒等排列 {identity}) → 成功")
            except BalatroRPCError as exc:
                print(f"  rearrange 失败：{exc}")
                return 1

            try:
                client.rearrange(jokers=swapped)
                print(f"  rearrange(轮转 {swapped}) → 成功")
            except BalatroRPCError as exc:
                print(f"  rearrange 失败：{exc}")
                return 1

            after = GameState(client.gamestate())
            keys = [c.key for c in after.jokers.cards]
            print(f"  重排后顺序：{keys}")
            print("rearrange 接口可用 ✓")
            return 0
    else:
        print("跑完一局都没凑到 2 张小丑牌，无法验证")
        return 1

    print("游戏结束都没凑到 2 张小丑牌，无法验证")
    return 1


if __name__ == "__main__":
    sys.exit(main())
