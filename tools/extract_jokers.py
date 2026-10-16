"""从 Balatro 游戏本体里提取小丑牌的权威定义。

为什么需要这个
--------------
Balatro.exe 是 LÖVE 融合包，游戏源码 zip 就附加在 exe 末尾，可以当普通 zip 读。
其中 game.lua 里的 ``self.P_CENTERS`` 表定义了每一张牌，小丑牌的数值全在
``config`` 字段里，例如：

    j_greedy_joker = {..., config = {extra = {s_mult = 3, suit = 'Diamonds'}}}

这是唯一权威的来源。靠记忆手写效果模型的问题是：**key 拼错了不会报错，
只会静默地按 0 收益处理**，机器人于是持续低估自己的分数。本脚本把定义提取成
Python 数据模块，既用于生成通用模型，也用于校验手写表里的每个 key 真实存在。

    python tools/extract_jokers.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHED_GAME_LUA = PROJECT_ROOT / "runs" / "game-source" / "game.lua"
OUTPUT = PROJECT_ROOT / "balatro_bot" / "joker_data.py"

# 游戏本体的位置，按优先级：命令行参数 --exe、环境变量 BALATRO_DIR、常见安装位置。
#     python tools\extract_jokers.py --exe "D:\Games\Balatro\Balatro.exe"
#     setx BALATRO_DIR "D:\Games\Balatro"
DEFAULT_GAME_DIR = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Balatro")


def _find_exe(argv: list[str]) -> Path | None:
    for i, arg in enumerate(argv):
        if arg == "--exe" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if arg.startswith("--exe="):
            return Path(arg.split("=", 1)[1])
    env = os.environ.get("BALATRO_DIR")
    if env:
        return Path(env) / "Balatro.exe"
    return DEFAULT_GAME_DIR / "Balatro.exe"

# ---------------------------------------------------------------- Lua 解析


class LuaTableParser:
    """只够用的 Lua 表解析器：支持嵌套表、字符串、数字、布尔、nil。

    不支持的（这份数据里也没出现）：函数、运算表达式、长字符串、变量引用。
    遇到不认识的内容会抛错而不是猜，避免静默解析出错误的数值。
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def error(self, message: str) -> ValueError:
        line = self.text.count("\n", 0, self.pos) + 1
        snippet = self.text[self.pos : self.pos + 60].replace("\n", "\\n")
        return ValueError(f"{message}（第 {line} 行附近：{snippet!r}）")

    def skip_space(self) -> None:
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char in " \t\r\n":
                self.pos += 1
            elif self.text.startswith("--", self.pos):
                # 行注释
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end < 0 else end + 1
            else:
                break

    def parse_table(self) -> dict | list:
        if self.pos >= len(self.text) or self.text[self.pos] != "{":
            raise self.error("期望 '{'")
        self.pos += 1
        mapping: dict[Any, Any] = {}
        array: list[Any] = []
        has_keys = False

        while True:
            self.skip_space()
            if self.pos >= len(self.text):
                raise self.error("表没有闭合")
            if self.text[self.pos] == "}":
                self.pos += 1
                break

            key = None
            if self.text[self.pos] == "[":
                self.pos += 1
                key = self.parse_value()
                self.skip_space()
                if self.pos >= len(self.text) or self.text[self.pos] != "]":
                    raise self.error("期望 ']'")
                self.pos += 1
                self.skip_space()
                if self.pos >= len(self.text) or self.text[self.pos] != "=":
                    raise self.error("期望 '='")
                self.pos += 1
            else:
                saved = self.pos
                name = self.parse_identifier()
                if name is not None:
                    self.skip_space()
                    if self.pos < len(self.text) and self.text[self.pos] == "=":
                        key = name
                        self.pos += 1
                    else:
                        self.pos = saved
                        name = None

            value = self.parse_value()
            if key is not None:
                mapping[key] = value
                has_keys = True
            else:
                array.append(value)

            self.skip_space()
            if self.pos < len(self.text) and self.text[self.pos] in ",;":
                self.pos += 1

        if has_keys and array:
            # 混合表：这份数据里不该出现，出现说明解析假设不成立
            raise self.error("遇到混合键/数组的表")
        return mapping if has_keys else array

    def parse_identifier(self) -> str | None:
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", self.text[self.pos :])
        if not match:
            return None
        self.pos += match.end()
        return match.group(0)

    def parse_value(self) -> Any:
        self.skip_space()
        if self.pos >= len(self.text):
            raise self.error("值意外结束")
        char = self.text[self.pos]

        if char == "{":
            return self.parse_table()

        if char in "'\"":
            quote = char
            self.pos += 1
            start = self.pos
            while self.pos < len(self.text) and self.text[self.pos] != quote:
                if self.text[self.pos] == "\\":
                    self.pos += 1
                self.pos += 1
            value = self.text[start : self.pos]
            self.pos += 1
            return value

        number = re.match(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", self.text[self.pos :])
        if number:
            self.pos += number.end()
            raw = number.group(0)
            value: Any = float(raw) if ("." in raw or "e" in raw.lower()) else int(raw)
            return self.maybe_arithmetic(value)

        word = self.parse_dotted_identifier()
        if word == "true":
            return True
        if word == "false":
            return False
        if word in ("nil", "NULL"):
            return None
        if word is not None:
            # 例如 G.C.BLACK —— 当成字符串保留，取值本身对算分不重要
            return word
        raise self.error(f"无法解析的字符 {char!r}")

    def parse_dotted_identifier(self) -> str | None:
        """读取 a.b.c 形式的路径；单个标识符也走这里。"""
        first = self.parse_identifier()
        if first is None:
            return None
        parts = [first]
        while self.pos < len(self.text) and self.text[self.pos] == ".":
            saved = self.pos
            self.pos += 1
            nxt = self.parse_identifier()
            if nxt is None:
                self.pos = saved
                break
            parts.append(nxt)
        return ".".join(parts)

    def maybe_arithmetic(self, value: Any) -> Any:
        """处理 1/4、2*3 这类简单数值表达式（游戏数据里确实存在）。"""
        if not isinstance(value, (int, float)):
            return value
        result = float(value)
        touched = False
        while True:
            self.skip_space()
            if self.pos >= len(self.text) or self.text[self.pos] not in "+-*/":
                break
            operator = self.text[self.pos]
            self.pos += 1
            self.skip_space()
            operand_match = re.match(
                r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", self.text[self.pos :]
            )
            if not operand_match:
                raise self.error(f"运算符 {operator} 后面不是数字")
            self.pos += operand_match.end()
            operand = float(operand_match.group(0))
            if operator == "+":
                result += operand
            elif operator == "-":
                result -= operand
            elif operator == "*":
                result *= operand
            else:
                result /= operand
            touched = True
        if not touched:
            return value
        return int(result) if result.is_integer() else result


def read_game_lua(exe: Path | None = None) -> str:
    """优先从游戏 exe 直接读取，读不到就退回之前提取的副本。

    游戏装在别处时用 `--exe` 或环境变量 `BALATRO_DIR` 指定；
    两条路都不通时，只要 `runs/game-source/game.lua` 在就仍然能生成。
    """
    exe = exe or _find_exe(sys.argv[1:])
    if exe.exists():
        try:
            with zipfile.ZipFile(exe) as archive:
                return archive.read("game.lua").decode("utf-8", "replace")
        except (zipfile.BadZipFile, KeyError) as exc:
            print(f"从 exe 读取失败（{exc}），改用缓存副本", file=sys.stderr)
    if CACHED_GAME_LUA.exists():
        return CACHED_GAME_LUA.read_text(encoding="utf-8", errors="replace")
    raise SystemExit(
        f"找不到游戏源码：{exe} 打不开，{CACHED_GAME_LUA} 也不存在。\n"
        "请用 --exe 指定 Balatro.exe，或设环境变量 BALATRO_DIR。"
    )


def find_entry_spans(source: str, prefix: str) -> list[tuple[str, int, int]]:
    """找出形如 `  j_xxx = { ... }` 的每条定义的字符区间。

    逐条解析而不是整表解析：整表里只要有一条用了不支持的语法，整张表就废了；
    逐条解析则只损失那一条，而且能报出具体是哪个 key 失败。
    """
    spans: list[tuple[str, int, int]] = []
    pattern = re.compile(rf"^\s*({prefix}[A-Za-z0-9_]*)\s*=\s*\{{", re.MULTILINE)
    for match in pattern.finditer(source):
        key = match.group(1)
        brace = source.index("{", match.start())
        end = match_brace(source, brace)
        if end is None:
            continue
        spans.append((key, brace, end))
    return spans


def match_brace(source: str, start: int) -> int | None:
    """从 start 处的 '{' 开始做括号配对，跳过字符串和注释，返回配对的 '}' 下标。"""
    depth = 0
    pos = start
    length = len(source)
    while pos < length:
        char = source[pos]

        if char == "-" and source.startswith("--", pos):
            long_comment = re.match(r"--\[(=*)\[", source[pos:])
            if long_comment:
                closer = "]" + long_comment.group(1) + "]"
                end = source.find(closer, pos)
                pos = length if end < 0 else end + len(closer)
            else:
                end = source.find("\n", pos)
                pos = length if end < 0 else end + 1
            continue

        if char in "'\"":
            quote = char
            pos += 1
            while pos < length:
                if source[pos] == "\\":
                    pos += 2
                    continue
                if source[pos] == quote:
                    pos += 1
                    break
                pos += 1
            continue

        long_string = re.match(r"\[(=*)\[", source[pos:])
        if long_string:
            closer = "]" + long_string.group(1) + "]"
            end = source.find(closer, pos)
            pos = length if end < 0 else end + len(closer)
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return pos + 1
        pos += 1
    return None


def extract_jokers(source: str) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """提取所有小丑牌定义，返回 (成功表, 失败列表)。"""
    spans = find_entry_spans(source, "j_")
    if not spans:
        raise SystemExit("在游戏源码里没有找到任何 j_ 定义")

    jokers: dict[str, Any] = {}
    failures: list[tuple[str, str]] = []
    for key, brace, end in spans:
        try:
            table = LuaTableParser(source[brace:end]).parse_table()
        except ValueError as exc:
            failures.append((key, str(exc)))
            continue
        if isinstance(table, dict):
            jokers[key] = table
        else:
            failures.append((key, "解析结果不是字典"))
    return jokers, failures


def main() -> int:
    source = read_game_lua()
    jokers, failures = extract_jokers(source)
    print(f"解析出小丑牌 {len(jokers)} 张，失败 {len(failures)} 条")
    for key, reason in failures:
        print(f"  ✗ {key}: {reason}")

    # 只保留建模需要的字段，减小生成文件体积
    trimmed: dict[str, dict[str, Any]] = {}
    for key, value in sorted(jokers.items()):
        trimmed[key] = {
            "name": value.get("name"),
            "rarity": value.get("rarity"),
            "cost": value.get("cost"),
            "effect": value.get("effect"),
            "config": value.get("config") or {},
        }

    lines = [
        '"""小丑牌定义——由工具自动生成，不要手改。',
        "",
        "数据来源：Balatro.exe 内 game.lua 的 self.P_CENTERS 表。",
        "重新生成：python tools/extract_jokers.py",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        f"# 共 {len(trimmed)} 张小丑牌",
        "JOKER_DATA: dict[str, dict[str, Any]] = {",
    ]
    for key, value in trimmed.items():
        lines.append(f"    {key!r}: {value!r},")
    lines.append("}")
    lines.append("")

    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写入 {OUTPUT}（{OUTPUT.stat().st_size} 字节）")

    # 顺便统计一下 effect 分类，看看 config 能覆盖多少种情况
    from collections import Counter

    effects = Counter(v["effect"] for v in trimmed.values())
    print("\neffect 分类分布（前 25）：")
    for name, count in effects.most_common(25):
        print(f"  {count:3d}  {name!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
