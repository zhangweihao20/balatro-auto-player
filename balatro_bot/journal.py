"""决策日志：把"做了什么、为什么"记成结构化记录，供实时窗口显示。

分工
----
* **策略层**（`strategy.py`）只调用 `note(理由, 数字...)` 就地记下理由——
  它不碰文件、不碰网络，所以测试仍然可以完全离线跑。
* **主循环**（`bot.py`）在拿到动作之后取出理由，连同局面快照写进 `Journal`。
* **查看器**（`tools/decision_log.py`）实时 tail 这个 JSONL 文件。

为什么用"当前理由"这个寄存器，而不是给动作对象加字段
--------------------------------------------------
1. 机器人是单线程，一次只做一个决策，不存在竞争；
2. 13 个动作类型都加 `why` 字段会把动作定义污染得很乱；
3. 理由和数字在决策函数里都是**现成的**（已经算出来了），就地记下最准，
   也让"日志里的数字"和"真正用于决策的数字"必然是同一个。

为什么用 JSONL 而不是普通文本
--------------------------
窗口要按动作类型上色、按底注过滤、把数字单独排版。文本日志还得再解析一遍，
而 JSONL 每行一个对象，读一行就能直接渲染。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

@dataclass
class Reason:
    """当前决策的理由。"""

    why: str = ""
    kind: str = ""
    numbers: dict[str, Any] = field(default_factory=dict)


_CURRENT = Reason()


def note(why: str, *, kind: str = "", **numbers: Any) -> None:
    """记录当前决策的理由。**在决策函数 return 之前调用。**

    `kind` 用于窗口上色（play / discard / buy / sell / reroll / use / pack /
    blind / rearrange / refresh），留空则从动作推断。
    """
    _CURRENT.why = why
    _CURRENT.kind = kind
    _CURRENT.numbers = dict(numbers)


def clear_reason() -> None:
    """每次决策前清空，避免上一步的理由残留下来误导日志。"""
    _CURRENT.why = ""
    _CURRENT.kind = ""
    _CURRENT.numbers.clear()


def current_reason() -> Reason:
    return _CURRENT


# ------------------------------------------------------------------ 落盘


def _frame_value(value: Any) -> Any:
    """把快照里的一个值转成 mod 参数校验能接受的形式。

    校验器只认 string / integer / boolean / array / table，而且顶层声明为
    `table` 的字段**不能是数组**（`jokers` 这种列表要留在里面，不能提到顶层）。

    所以这里：整数/布尔/字符串原样保留，列表保留成列表（元素也按同一规则处理），
    其它一律转成字符串——绝不静默丢掉字段，否则面板上会缺信息且无从察觉。
    """
    if isinstance(value, bool) or isinstance(value, (int, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_frame_value(item) for item in value]
    return str(value)


class Journal:
    """把决策写成 JSONL，**并且**（可选）推给游戏里的日志面板。

    两个出口各有用处：
    * JSONL —— 事后复盘、`tools/decision_log.py` 的独立窗口都能读；
    * mod 端点 —— 面板直接画在游戏窗口右侧，不用另开窗口。

    推给 mod 是**尽力而为**：面板没安装时端点不存在，会抛 RPC 错误；
    这里全部吞掉并只提示一次。日志面板绝不能影响对局。
    """

    def __init__(
        self,
        path: str | Path | None,
        *,
        enabled: bool = True,
        client: Any = None,
    ) -> None:
        self.path = Path(path) if path else None
        self.enabled = bool(enabled and self.path)
        self._handle: TextIO | None = None
        self.records = 0
        # 面板出口
        self.client = client
        self.mod_ok = client is not None
        self._mod_warned = False
        self.mod_records = 0

    def open(self) -> None:
        if not self.enabled or self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 每局评测覆盖旧文件：窗口只关心"正在跑的这一次"
        self._handle = self.path.open("w", encoding="utf-8")

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def push_to_mod(
        self,
        *,
        kind: str,
        action: str,
        why: str = "",
        detail: str = "",
        numbers: dict[str, Any] | None = None,
        frame: dict[str, Any] | None = None,
        step: int | None = None,
        clear: bool = False,
    ) -> None:
        """把一条决策推给游戏里的面板。失败只提示一次，不抛异常。"""
        if not self.mod_ok or self.client is None:
            return
        # mod 的参数校验只认 string/integer/boolean/array/table，
        # 所以数字和快照都转成字符串值，避免类型不匹配被拒。
        payload: dict[str, Any] = {"clear": clear} if clear else {}
        if kind:
            payload["kind"] = str(kind)
        if action:
            payload["action"] = str(action)
        if why:
            payload["why"] = str(why)
        if detail:
            payload["detail"] = str(detail)
        if numbers:
            payload["numbers"] = {str(k): str(v) for k, v in numbers.items()}
        if frame:
            payload["frame"] = {str(k): _frame_value(v) for k, v in frame.items()}
        if step is not None:
            payload["step"] = int(step)
        try:
            self.client.journal(**payload)
            self.mod_records += 1
        except Exception as exc:                      # noqa: BLE001 - 面板不该影响对局
            self.mod_ok = False
            if not self._mod_warned:
                self._mod_warned = True
                warn = ("决策日志面板不可用（mod 里没装 journal 端点？"
                        "运行 mod_additions/install.py 安装）：%s")
                import logging
                logging.getLogger(__name__).info(warn, exc)

    def record(
        self,
        *,
        step: int,
        state: str,
        action: str,
        detail: str = "",
        reason: Reason | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        """写一条决策记录。没有理由也要写——窗口至少要看到"做了什么"。"""
        reason = reason or _CURRENT
        kind = reason.kind or _infer_kind(action)

        # 游戏内面板（尽力而为，失败不影响下面的文件写入）
        self.push_to_mod(
            kind=kind, action=action, why=reason.why, detail=detail,
            numbers=reason.numbers, frame=snapshot, step=step,
        )

        if not self.enabled:
            return
        if self._handle is None:
            self.open()
        if self._handle is None:      # pragma: no cover - 打开失败
            return

        entry: dict[str, Any] = {
            "step": step,
            "t": time.strftime("%H:%M:%S"),
            "state": state,
            "action": action,
            "detail": detail,
            "kind": kind,
            "why": reason.why,
        }
        if reason.numbers:
            entry["numbers"] = reason.numbers
        if snapshot:
            entry["snapshot"] = snapshot
        try:
            self._handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._handle.flush()
        except OSError:               # pragma: no cover - 磁盘问题不该中断对局
            return
        self.records += 1


_KIND_BY_ACTION = {
    "play": "play",
    "discard": "discard",
    "buy": "buy",
    "sell": "sell",
    "reroll": "reroll",
    "use": "use",
    "open_pack": "pack",
    "select": "blind",
    "skip": "blind",
    "rearrange": "rearrange",
    "cash_out": "flow",
    "next_round": "flow",
    "gamestate": "refresh",
}


def _infer_kind(action: str) -> str:
    return _KIND_BY_ACTION.get(action, "other")
