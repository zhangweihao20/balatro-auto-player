"""自动化小丑牌（Balatro）机器人。

通过 BalatroBot mod 暴露的 JSON-RPC 2.0 接口读取游戏状态并发送操作，
不做截图识别，因此速度和准确度都远高于视觉方案。

模块划分：
    rpc.py       JSON-RPC 客户端（标准库实现，无第三方依赖）
    cards.py     牌对象与牌型判定
    state.py     游戏状态的只读封装
    jokers.py    小丑牌效果模型（决定算分准确度）
    scoring.py   一次出牌的算分
    search.py    在全部候选打法里搜索最优解
    strategy.py  出牌 / 弃牌 / 选盲注 / 商店的决策策略
    bot.py       主状态机
    cli.py       命令行入口
"""

__version__ = "0.1.0"
