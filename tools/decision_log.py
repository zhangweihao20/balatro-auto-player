"""决策日志窗口：在游戏界面旁边实时显示"做了什么、为什么"。

用法
----
    # 一个窗口跑机器人（会写 runs/journal.jsonl）
    .venv\\Scripts\\python.exe -m balatro_bot eval --games 30 --journal runs\\journal.jsonl

    # 另一个窗口开日志栏（默认就是 runs\\journal.jsonl）
    .venv\\Scripts\\python.exe tools\\decision_log.py

    # 没跑游戏时先看看长什么样
    .venv\\Scripts\\python.exe tools\\decision_log.py --demo

为什么用 tkinter
--------------
* 标准库自带，符合本项目"零第三方依赖"的约定；
* 可以做**置顶窗口**（`-topmost`），摆在游戏旁边不会被盖住。

数据来源
--------
机器人把每一步决策写成一行 JSON（`balatro_bot/journal.py`）。本窗口只做
"tail 文件 + 上色渲染"，不依赖机器人进程，所以机器人重启也不影响窗口。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 按动作类型上色。前景色 + 可选背景色。
COLORS: dict[str, tuple[str, str]] = {
    "play":      ("#1b5e20", "#e8f5e9"),   # 出牌：绿
    "discard":   ("#7f6000", "#fff8e1"),   # 弃牌：黄
    "buy":       ("#0d47a1", "#e3f2fd"),   # 买：蓝
    "sell":      ("#b71c1c", "#ffebee"),   # 卖：红
    "reroll":    ("#4a148c", "#f3e5f5"),   # 重摇：紫
    "pack":      ("#006064", "#e0f7fa"),   # 开包：青
    "use":       ("#e65100", "#fff3e0"),   # 用牌：橙
    "blind":     ("#37474f", "#eceff1"),   # 盲注：灰蓝
    "rearrange": ("#33691e", "#f1f8e9"),   # 重排：浅绿
    "flow":      ("#616161", "#fafafa"),   # 流程：灰
    "refresh":   ("#9e9e9e", "#fafafa"),   # 空转：浅灰
    "other":     ("#424242", "#ffffff"),
}

ACTION_LABEL = {
    "play": "出牌", "discard": "弃牌", "buy": "购买", "sell": "卖出",
    "reroll": "重摇", "use": "用牌", "open_pack": "开包", "select": "选盲注",
    "skip": "跳盲注", "rearrange": "重排", "cash_out": "结算",
    "next_round": "进下一轮", "gamestate": "刷新",
}


class Tail:
    """tail 一个 JSONL 文件；文件被重写（新的一局）时自动从头开始。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None
        self._buffer = ""

    def _open(self) -> None:
        self._handle = self.path.open("r", encoding="utf-8", errors="ignore")
        # 打开已有文件时从头读，这样中途打开窗口也能看到本轮全部记录
        self._handle.seek(0)

    def read_new(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            if self._handle is None:
                self._open()
            elif self.path.stat().st_size < self._handle.tell():
                # 文件变小 = 机器人开了新的一局，重头读
                self._handle.close()
                self._open()
        except OSError:
            return []

        records: list[dict] = []
        try:
            chunk = self._handle.read()
        except (OSError, ValueError):       # pragma: no cover
            return []
        if not chunk:
            return []
        self._buffer += chunk
        *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records


def make_demo_records(path: Path) -> None:
    """生成一份示例日志，方便不跑游戏就先看看窗口长什么样。"""
    samples = [
        {"kind": "blind", "action": "select",
         "why": "选中 Small Blind", "detail": "选中盲注",
         "numbers": {"需求": 300, "类型": "SMALL"}},
        {"kind": "rearrange", "action": "rearrange",
         "why": "重排小丑牌槽位（免费加分：加法在左、乘区在右）",
         "numbers": {"重排前": 640, "重排后": 782}},
        {"kind": "play", "action": "play",
         "why": "这一手就能过关（Pair 782 ≥ 需要 300）",
         "numbers": {"牌型": "Pair", "预估得分": 782, "需要": 300, "剩余手数": 4}},
        {"kind": "flow", "action": "cash_out", "why": "", "detail": "结算回合"},
        {"kind": "buy", "action": "buy",
         "why": "买 The Duo", "numbers": {"估值": 6.5, "门槛": 4.5, "价格": 5,
                                          "阵容得分": "×1.42"}},
        {"kind": "pack", "action": "buy",
         "why": "买天体包 Jumbo Celestial Pack",
         "numbers": {"每元增益": "1.11%", "门槛": "1.00%", "价格": 6}},
        {"kind": "pack", "action": "open_pack",
         "why": "从包里拿主牌型的星球牌",
         "numbers": {"拿了": "Mercury", "牌型": "Pair", "角色": "主牌型"}},
        {"kind": "discard", "action": "discard",
         "why": "弃 3 张去凑 Flush：期望 1240 ≥ 现在 782 × (1+0.18)",
         "numbers": {"凑的牌型": "Flush", "概率": "26%", "凑成分": 3400,
                     "现在打": 782, "期望": 1240, "门槛": "+18%"}},
        {"kind": "use", "action": "use",
         "why": "用星球牌 Mercury（升级 Pair）",
         "numbers": {"牌型": "Pair", "当前等级": 3}},
        {"kind": "sell", "action": "sell",
         "why": "卖小丑牌 #2 以解除 Verdant Leaf 的全体压制",
         "numbers": {"卖": "#2", "被压制的手牌": 8}},
        {"kind": "reroll", "action": "reroll",
         "why": "重摇找换牌目标（牌位已满，只能换）",
         "numbers": {"预算": 16, "已摇": 3, "花费": 5, "注意": "紧急：预算改为花得起就摇"}},
        {"kind": "flow", "action": "next_round",
         "why": "离开商店：重摇预算用完（3/3）",
         "numbers": {"保留额": 25, "余额": 31}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for i, item in enumerate(samples):
            entry = {
                "step": i, "t": time.strftime("%H:%M:%S"), "state": "SHOP",
                "detail": item.get("detail", ""),
                "snapshot": {"ante": 4, "round": 12, "money": 31, "need": 5000,
                             "hands": 4, "discards": 2,
                             "jokers": ["Jolly Joker", "The Duo", "Smiley Face"]},
            }
            entry.update({k: v for k, v in item.items() if k != "detail"})
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


class LogWindow:
    def __init__(self, path: Path, *, interval: float = 0.2, topmost: bool = True):
        import tkinter as tk
        from tkinter import ttk

        self.path = path
        self.interval = interval
        self.tail = Tail(path)
        self.paused = False
        self.follow = True

        self.root = tk.Tk()
        self.root.title("Balatro 决策日志")
        width, height = 560, 940
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        # 默认摆在屏幕右侧——游戏窗口一般在中间偏左
        x = max(0, screen_w - width - 24)
        y = max(0, (screen_h - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(420, 400)
        if topmost:
            self.root.attributes("-topmost", True)

        # ---------------------------------------------------------- 顶部局面栏
        header = tk.Frame(self.root, bg="#263238", padx=8, pady=6)
        header.pack(fill="x")
        self.headline = tk.Label(
            header, text="等待机器人写日志…", bg="#263238", fg="#eceff1",
            font=("Consolas", 10, "bold"), anchor="w", justify="left",
        )
        self.headline.pack(fill="x")
        self.subline = tk.Label(
            header, text="", bg="#263238", fg="#b0bec5",
            font=("Consolas", 9), anchor="w", justify="left",
        )
        self.subline.pack(fill="x")

        # ---------------------------------------------------------- 日志主体
        body = tk.Frame(self.root)
        body.pack(fill="both", expand=True)
        self.text = tk.Text(
            body, wrap="word", font=("Microsoft YaHei UI", 9), padx=8, pady=6,
            state="disabled", cursor="arrow", spacing1=2, spacing3=4,
        )
        scroll = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        for kind, (fg, bg) in COLORS.items():
            self.text.tag_configure(kind, foreground=fg, background=bg)
            self.text.tag_configure(f"{kind}_why", foreground=fg,
                                    background=bg, font=("Microsoft YaHei UI", 10, "bold"))
            self.text.tag_configure(f"{kind}_num", foreground="#546e7a",
                                    background=bg, font=("Consolas", 8))
        self.text.tag_configure("sep", foreground="#cfd8dc",
                                font=("Consolas", 7))

        # ---------------------------------------------------------- 底部按钮
        footer = tk.Frame(self.root, bg="#eceff1", padx=8, pady=6)
        footer.pack(fill="x")
        self.status = tk.Label(footer, text="", bg="#eceff1", fg="#37474f",
                               font=("Consolas", 9), anchor="w")
        self.status.pack(side="left")

        for label, command in (
            ("跟随", self.toggle_follow),
            ("暂停", self.toggle_pause),
            ("清空", self.clear),
        ):
            tk.Button(footer, text=label, command=command, width=6).pack(
                side="right", padx=2
            )

        self.count = 0
        self.root.after(int(interval * 1000), self._tick)

    # ------------------------------------------------------------------ 交互

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self._refresh_status()

    def toggle_follow(self) -> None:
        self.follow = not self.follow
        self._refresh_status()

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.count = 0
        self._refresh_status()

    def _refresh_status(self) -> None:
        flags = []
        if self.paused:
            flags.append("已暂停")
        if not self.follow:
            flags.append("不跟随")
        suffix = ("｜" + "、".join(flags)) if flags else ""
        self.status.configure(text=f"已记录 {self.count} 步{suffix}")

    # ------------------------------------------------------------------ 渲染

    def _append(self, record: dict) -> None:
        kind = record.get("kind") or "other"
        if kind not in COLORS:
            kind = "other"
        why = record.get("why") or ""
        detail = record.get("detail") or ""
        action = record.get("action", "")
        label = ACTION_LABEL.get(action, action)
        numbers = record.get("numbers") or {}

        self.text.configure(state="normal")
        self.text.insert("end", f"[{record.get('t', '')}] {label}",
                         (kind,))
        if detail and not why:
            self.text.insert("end", f"  {detail}", (kind,))
        self.text.insert("end", "\n", (kind,))
        if why:
            self.text.insert("end", f"    {why}\n", (f"{kind}_why",))
        if numbers:
            rendered = "  ".join(f"{k}={v}" for k, v in numbers.items())
            self.text.insert("end", f"    {rendered}\n", (f"{kind}_num",))
        self.text.insert("end", "─" * 46 + "\n", ("sep",))
        self.text.configure(state="disabled")
        if self.follow:
            self.text.see("end")
        self.count += 1

    def _update_header(self, record: dict) -> None:
        snap = record.get("snapshot") or {}
        if not snap:
            return
        need = snap.get("need")
        chips = snap.get("chips")
        money = snap.get("money")
        self.headline.configure(
            text=f"底注 {snap.get('ante', '?')}  第 {snap.get('round', '?')} 回合"
                 f"   ${money if money is not None else '?'}"
        )
        parts = []
        if need is not None:
            parts.append(f"还差 {need}")
        if chips is not None:
            parts.append(f"本关已得 {chips}")
        if snap.get("hands") is not None:
            parts.append(f"出牌 {snap['hands']}")
        if snap.get("discards") is not None:
            parts.append(f"弃牌 {snap['discards']}")
        self.subline.configure(text="   ".join(parts))
        jokers = snap.get("jokers") or []
        if jokers:
            self.root.title(f"Balatro 决策日志 — {', '.join(jokers)[:60]}")

    def _tick(self) -> None:
        if not self.paused:
            records = self.tail.read_new()
            for record in records:
                self._append(record)
                self._update_header(record)
            if records:
                self._refresh_status()
            elif self.count == 0 and not self.path.exists():
                self.headline.configure(text=f"等待 {self.path.name} 出现…")
        self.root.after(int(self.interval * 1000), self._tick)

    def run(self) -> None:
        self._refresh_status()
        self.root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="显示机器人的决策日志")
    parser.add_argument("--journal", default="runs/journal.jsonl",
                        help="机器人写的 JSONL 路径（默认 runs/journal.jsonl）")
    parser.add_argument("--interval", type=float, default=0.2,
                        help="轮询间隔秒数（默认 0.2）")
    parser.add_argument("--no-topmost", action="store_true",
                        help="不置顶（默认置顶，摆在游戏旁边不会被盖住）")
    parser.add_argument("--demo", action="store_true",
                        help="生成示例日志并显示（不需要跑游戏）")
    args = parser.parse_args()

    path = Path(args.journal)
    if args.demo:
        make_demo_records(path)
        print(f"已生成示例日志：{path}")

    try:
        window = LogWindow(path, interval=args.interval, topmost=not args.no_topmost)
    except Exception as exc:                      # pragma: no cover
        print(f"打不开窗口：{exc}")
        print("（需要图形环境；无头环境请直接看 runs/journal.jsonl）")
        return 1
    window.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
