"""BalatroBot 的 JSON-RPC 2.0 客户端。

刻意只用标准库（urllib），所以整个项目不需要任何第三方依赖，
用系统自带的 Python 就能跑。

接口约定（来自 https://coder.github.io/balatrobot/）：
    请求  {"jsonrpc":"2.0","method":"play","params":{"cards":[0,1]},"id":1}
    成功  {"jsonrpc":"2.0","result":{...},"id":1}
    失败  {"jsonrpc":"2.0","error":{"code":-32002,"message":"...","data":{"name":"INVALID_STATE"}},"id":1}

所有 play/discard/buy/pack/sell/use/rearrange 里的下标都是
对应 Area 的 cards 数组的 **0 基下标**，不是 Card.id。
"""

from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 12346

# error.data.name 的取值
ERR_INTERNAL = "INTERNAL_ERROR"      # -32000 服务端故障
ERR_BAD_REQUEST = "BAD_REQUEST"      # -32001 参数非法
ERR_INVALID_STATE = "INVALID_STATE"  # -32002 当前状态不允许该操作
ERR_NOT_ALLOWED = "NOT_ALLOWED"      # -32003 规则不允许


class BalatroRPCError(RuntimeError):
    """mod 返回的 JSON-RPC 层错误。"""

    def __init__(self, code: int | None, name: str | None, message: str) -> None:
        super().__init__(f"{name or 'RPC_ERROR'}: {message}")
        self.code = code
        self.name = name
        self.message = str(message)

    @property
    def is_state_error(self) -> bool:
        """状态不匹配——通常意味着本地状态过期了，重新取一次即可。"""
        return self.name == ERR_INVALID_STATE

    @property
    def is_rule_error(self) -> bool:
        """规则不允许——重试也没用，换一个动作。"""
        return self.name == ERR_NOT_ALLOWED

    @property
    def is_transient(self) -> bool:
        return self.name == ERR_INTERNAL


class BalatroConnectionError(RuntimeError):
    """传输层失败：mod 没在监听，或者游戏中途挂了。"""


class BalatroClient:
    """到游戏内 mod 的同步 HTTP 客户端。

    只负责传输和错误分类，不含任何游戏策略。
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = 60.0,
    ) -> None:
        self.url = f"http://{host}:{port}"
        self.timeout = timeout
        self._ids = itertools.count(1)
        self.call_count = 0

    # ---------------------------------------------------------------- 传输层

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """发一次请求，返回 result。出错抛异常。"""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": next(self._ids),
        }
        if params is not None:
            payload["params"] = params

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # 有些错误会带非 200 状态码，body 里仍然是合法的 JSON-RPC 错误
            raw = exc.read()
            if not raw:
                raise BalatroConnectionError(f"{method}: HTTP {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BalatroConnectionError(
                f"{method}: 无法连接 {self.url}（{exc}）。游戏是否在运行、mod 是否加载？"
            ) from exc

        self.call_count += 1

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            preview = raw[:200].decode("utf-8", "replace")
            raise BalatroConnectionError(f"{method}: 返回的不是 JSON：{preview!r}") from exc

        if not isinstance(data, dict):
            raise BalatroConnectionError(f"{method}: 返回格式异常：{data!r}")

        if "error" in data and data["error"]:
            err = data["error"]
            detail = err.get("data") or {}
            raise BalatroRPCError(err.get("code"), detail.get("name"), err.get("message", ""))

        return data.get("result")

    # ---------------------------------------------------------------- 查询类

    def health(self) -> dict[str, Any]:
        """连通性检查，任何状态下都能调用。"""
        return self.call("health")

    def is_alive(self) -> bool:
        try:
            return self.health().get("status") == "ok"
        except (BalatroRPCError, BalatroConnectionError):
            return False

    def gamestate(self) -> dict[str, Any]:
        """取当前完整状态，任何状态下都能调用。"""
        return self.call("gamestate")

    def journal(self, **fields: Any) -> dict[str, Any]:
        """把一步决策交给游戏里的日志面板（本项目对 mod 的附加端点）。

        面板没装时会抛 BalatroRPCError（method not found），调用方负责忽略——
        日志面板是**锦上添花**，绝不能因为它没装就影响对局。

        字段见 mod_additions/src/lua/endpoints/journal.lua：
            kind / action / why / detail / numbers / frame / step / clear
        """
        return self.call("journal", {k: v for k, v in fields.items() if v is not None})

    # ---------------------------------------------------------------- 动作类

    def menu(self) -> dict[str, Any]:
        """回到主菜单（任意状态可调用）。"""
        return self.call("menu")

    def start(self, deck: str, stake: str, seed: str | None = None) -> dict[str, Any]:
        """开新局；要求当前在 MENU。seed 可选，用于可复现的实验。"""
        params: dict[str, Any] = {"deck": deck, "stake": stake}
        if seed is not None:
            params["seed"] = seed
        return self.call("start", params)

    def select(self) -> dict[str, Any]:
        """选中当前盲注，进入出牌阶段。要求 BLIND_SELECT。"""
        return self.call("select")

    def skip(self) -> dict[str, Any]:
        """跳过盲注（只能跳 Small/Big，Boss 不能跳）。要求 BLIND_SELECT。"""
        return self.call("skip")

    def play(self, cards: list[int]) -> dict[str, Any]:
        """打出若干张手牌（0 基下标，1~5 张）。要求 SELECTING_HAND。"""
        return self.call("play", {"cards": list(cards)})

    def discard(self, cards: list[int]) -> dict[str, Any]:
        """弃掉若干张手牌并补牌。要求 SELECTING_HAND。"""
        return self.call("discard", {"cards": list(cards)})

    def cash_out(self) -> dict[str, Any]:
        """结算回合收益，进入商店。要求 ROUND_EVAL。"""
        return self.call("cash_out")

    def next_round(self) -> dict[str, Any]:
        """离开商店，回到盲注选择。要求 SHOP。"""
        return self.call("next_round")

    def buy(self, *, card: int | None = None, voucher: int | None = None,
            pack: int | None = None) -> dict[str, Any]:
        """购买；card/voucher/pack 三选一，0 基下标。要求 SHOP。"""
        params: dict[str, Any] = {}
        if card is not None:
            params["card"] = card
        if voucher is not None:
            params["voucher"] = voucher
        if pack is not None:
            params["pack"] = pack
        return self.call("buy", params)

    def open_pack(self, *, card: int | None = None, targets: list[int] | None = None,
                  skip: bool = False) -> dict[str, Any]:
        """从已打开的补充包里选牌。要求处在开包状态。"""
        params: dict[str, Any] = {}
        if card is not None:
            params["card"] = card
        if targets is not None:
            params["targets"] = list(targets)
        if skip:
            params["skip"] = True
        return self.call("pack", params)

    def sell(self, *, joker: int | None = None,
             consumable: int | None = None) -> dict[str, Any]:
        """卖掉小丑牌或消耗牌。"""
        params: dict[str, Any] = {}
        if joker is not None:
            params["joker"] = joker
        if consumable is not None:
            params["consumable"] = consumable
        return self.call("sell", params)

    def reroll(self) -> dict[str, Any]:
        """重摇商店，花钱。要求 SHOP。"""
        return self.call("reroll")

    def use(self, consumable: int, cards: list[int] | None = None) -> dict[str, Any]:
        """使用一张消耗牌，可选指定手牌目标。"""
        params: dict[str, Any] = {"consumable": consumable}
        if cards:
            params["cards"] = list(cards)
        return self.call("use", params)

    def rearrange(self, **areas: list[int]) -> dict[str, Any]:
        """重排顺序；每次只能传 hand / jokers / consumables 之一，且必须是完整排列。"""
        allowed = {"hand", "jokers", "consumables"}
        keys = set(areas)
        if keys - allowed:
            raise ValueError(f"rearrange 只接受 {sorted(allowed)}，收到 {sorted(keys)}")
        if len(keys) != 1:
            raise ValueError("rearrange 每次只能重排一个区域")
        return self.call("rearrange", {k: list(v) for k, v in areas.items()})

    # ------------------------------------------------- 调试 / 测试专用（需要 DebugPlus）

    def save(self, path: str) -> dict[str, Any]:
        return self.call("save", {"path": path})

    def load(self, path: str) -> dict[str, Any]:
        return self.call("load", {"path": path})

    def screenshot(self, path: str) -> dict[str, Any]:
        return self.call("screenshot", {"path": path})

    def set_values(self, **values: Any) -> dict[str, Any]:
        """调试用：直接改钱/筹码/底注等。"""
        return self.call("set", dict(values))
