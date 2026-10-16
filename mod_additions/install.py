"""把决策日志面板安装进 balatrobot mod。

为什么要脚本而不是手动改：mod 升级会覆盖 `balatrobot.lua`，需要能**重新应用**。
所以这里把改动做成幂等的——已经装过再跑一次不会重复插入。

    python mod_additions/install.py            # 安装 / 重新应用
    python mod_additions/install.py --uninstall # 卸载（还原 balatrobot.lua）
    python mod_additions/install.py --check     # 只检查当前状态

安装做了什么
------------
1. 拷贝两个新文件（不动 mod 原有文件）：
       src/lua/ui/journal.lua          面板绘制
       src/lua/endpoints/journal.lua   接收决策的端点
2. 在 `balatrobot.lua` 里插入两处**带标记**的改动：
       * 加载并 setup 面板模块
       * 把 journal.lua 注册进 BB_ENDPOINTS
   插入前会把原文件备份成 `balatrobot.lua.bak-journal`。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

MOD_DIR = Path(os.environ.get("APPDATA", "")) / "Balatro" / "Mods" / "balatrobot"
ENTRY = "balatrobot.lua"
BACKUP = "balatrobot.lua.bak-journal"

SOURCE_ROOT = Path(__file__).resolve().parent

FILES = (
    "src/lua/ui/journal.lua",
    "src/lua/endpoints/journal.lua",
)

BEGIN = "-- >>> 决策日志面板（本项目附加，见 mod_additions/README.md）"
END = "-- <<< 决策日志面板"

HOOK_BLOCK = f"""{BEGIN}
BB_JOURNAL = assert(SMODS.load_file("src/lua/ui/journal.lua"))()
BB_JOURNAL.setup()
{END}
"""

ENDPOINT_LINE = '  "src/lua/endpoints/journal.lua",\n'
ENDPOINT_ANCHOR = '  "src/lua/endpoints/use.lua",\n'

# ---------------------------------------------- gamestate 的补丁：导出弃牌堆
#
# 原版 mod 只导出 `cards`(G.deck) 和 `hand`，**不导出 G.discard / G.play**。
# 后果：机器人看不到"已打出/已弃掉"的牌，于是"牌库里还有几张 STEEL / K"
# 被系统性低估——钢铁流、人头流这类**靠牌库计数**的判断直接判错。
#
# 这里只加两个**新增字段**，不动任何现有键，所以不会破坏 state.py 的解析。

GAMESTATE = "src/lua/utils/gamestate.lua"
GAMESTATE_BACKUP = "gamestate.lua.bak-discard"
GAMESTATE_BEGIN = "-- >>> 导出弃牌堆（本项目附加，见 mod_additions/README.md）"
GAMESTATE_END = "-- <<< 导出弃牌堆"
GAMESTATE_ANCHOR = "    state_data.hand = extract_area(G.hand)\n  end\n"
GAMESTATE_BLOCK = f"""{GAMESTATE_BEGIN}
  -- 已打出 / 已弃掉的牌。牌库构成 = cards + hand + discard + play，
  -- 少了两项就会低估 STEEL / 点数分布，进而误判流派。
  if G.discard then
    state_data.discard = extract_area(G.discard)
  end

  if G.play and not G.play.REMOVED then
    state_data.play = extract_area(G.play)
  end
{GAMESTATE_END}
"""

# ------------------------------------------ cash_out 的补丁：等结算 UI 建好
#
# `G.STATE` 变成 `ROUND_EVAL` 与 `G.round_eval` 被创建（`game.lua:3315`，
# 在 `Game:update_round_eval` 里）**不是同一时刻**。原版端点一看到状态是
# ROUND_EVAL 就立刻调 `G.FUNCS.cash_out`，UI 还没建好时会让**游戏本身崩溃**：
#
#     functions/common_events.lua:1234: attempt to index field 'round_eval' (a nil value)
#
# 这是崩溃、不是报错——整个进程死掉。实测五轮 30 局 eval 里都恰好崩在
# **第 14 局**（同一种子 BOT013，最深/通关的那一局），把最好的成绩直接抹掉，
# 也污染了所有历史平均值。和 `G.buttons` 那类竞态同源，但后果严重得多。

CASH_OUT = "src/lua/endpoints/cash_out.lua"
CASH_OUT_BACKUP = "cash_out.lua.bak-roundeval"
CASH_OUT_BEGIN = "-- >>> 等结算 UI 建好（本项目附加，见 mod_additions/README.md）"
CASH_OUT_END = "-- <<< 等结算 UI 建好"
CASH_OUT_ANCHOR = "    G.FUNCS.cash_out({ config = {} })\n"
CASH_OUT_BLOCK = f"""{CASH_OUT_BEGIN}
    local _frames_waited = 0
    G.E_MANAGER:add_event(Event({{
      trigger = "condition",
      blocking = false,
      func = function()
        if G.STATE ~= G.STATES.ROUND_EVAL then return false end
        if not G.round_eval then
          _frames_waited = _frames_waited + 1
          -- 兜底：万一这一局的结算 UI 压根不出现，等够约 10 秒就照旧调用
          if _frames_waited < 600 then return false end
        end
        G.FUNCS.cash_out({{ config = {{}} }})
        return true
      end,
    }}))
    {CASH_OUT_END}
"""

# ------------------------------------------ pack 的补丁：没有目标牌就别等手牌
#
# 原来只要包是塔罗/幻灵就等"手牌就绪"，而就绪条件里包含
# `G.hand.cards[1].T.x`（第一张手牌的位置）。**手牌为空时它永远是 nil**，
# 条件永远不成立 → 这个端点**永久挂住** → 客户端 60 秒超时。
# 实测在底注 9 打开塔罗包时踩到（`pack: timed out`，游戏进程仍存活，
# 所以是挂起不是崩溃）。
#
# 不需要目标牌时根本用不到手牌（例如 The Hermit 是无目标塔罗），直接跳过等待。

PACK = "src/lua/endpoints/pack.lua"
PACK_BACKUP = "pack.lua.bak-handwait"
PACK_BEGIN = "-- >>> 没有目标牌就不要等手牌（本项目附加，见 mod_additions/README.md）"
PACK_END = "-- <<< 没有目标牌就不要等手牌"
PACK_ANCHOR = '    local needs_hand = pack_key == "Tarot" or pack_key == "Spectral"\n'
PACK_BLOCK = f"""{PACK_BEGIN}
    -- 只有**真的要用到目标牌**时才需要手牌就绪。不加 `args.targets` 这一条时，
    -- 手牌为空的局面会让上面的就绪条件（含 `G.hand.cards[1].T.x`）永远不成立，
    -- 端点永久挂起。
    local needs_hand = (pack_key == "Tarot" or pack_key == "Spectral")
      and args.targets ~= nil and #args.targets > 0
    {PACK_END}
"""

# 等待事件的超时兜底。
#
# `pack.lua` 里的等待事件（等状态稳定 / 等回到商店 / 等手牌就绪）**都没有超时**：
# 条件一旦永不成立，端点就**永久挂住**。实测在第 10 局 / 底注 9 稳定复现
# （`pack: timed out`，而游戏进程仍存活——是挂起不是崩溃）。
#
# 具体是哪个条件不成立没有查到（试过"手牌为空"那条假设，不是它）。但
# "**端点永远不该挂住**"本身就该是硬保证，所以这里加一道与 cash_out 同款的
# 帧计数兜底：等够约 15 秒就回一个错误，让客户端报错、对局继续，而不是
# 干等 60 秒然后整局报废。
PACK_TIMEOUT_BEGIN = "-- >>> 给 pack 的等待事件加超时（本项目附加，见 mod_additions/README.md）"
PACK_TIMEOUT_END = "-- <<< 给 pack 的等待事件加超时"
PACK_TIMEOUT_ANCHOR = '    sendDebugMessage("Init pack()", "BB.ENDPOINTS")\n'
PACK_TIMEOUT_BLOCK = f"""{PACK_TIMEOUT_BEGIN}
    local _pack_waited = 0
    local function _pack_give_up()
      _pack_waited = _pack_waited + 1
      if _pack_waited < 900 then return false end
      send_response({{
        message = "pack: 等待游戏状态超时（兜底，避免端点永久挂住）",
        name = BB_ERROR_NAMES.BAD_REQUEST,
      }})
      return true
    end
    {PACK_TIMEOUT_END}
"""


def status() -> None:
    print(f"mod 目录：{MOD_DIR}")
    print(f"  存在：{MOD_DIR.is_dir()}")
    for rel in FILES:
        target = MOD_DIR / rel
        print(f"  {'已装' if target.exists() else '缺失'}  {rel}")
    text = (MOD_DIR / ENTRY).read_text(encoding="utf-8") if (MOD_DIR / ENTRY).exists() else ""
    print(f"  入口补丁：{'已打' if BEGIN in text else '未打'}")
    print(f"  端点注册：{'已注册' if ENDPOINT_LINE.strip() in text else '未注册'}")
    gs = MOD_DIR / GAMESTATE
    gs_text = gs.read_text(encoding="utf-8") if gs.exists() else ""
    print(f"  弃牌堆导出：{'已打' if GAMESTATE_BEGIN in gs_text else '未打'}")
    co = MOD_DIR / CASH_OUT
    co_text = co.read_text(encoding="utf-8") if co.exists() else ""
    print(f"  结算守卫：  {'已打' if CASH_OUT_BEGIN in co_text else '未打'}")
    pk = MOD_DIR / PACK
    pk_text = pk.read_text(encoding="utf-8") if pk.exists() else ""
    print(f"  开包手牌守卫：{'已打' if PACK_BEGIN in pk_text else '未打'}")
    print(f"  等待超时兜底：{'已打' if PACK_TIMEOUT_BEGIN in pk_text else '未打'}")
    print(f"  备份：{'有' if (MOD_DIR / BACKUP).exists() else '无'}")


def patched_gamestate_text(original: str) -> str:
    """给 gamestate.lua 加上弃牌堆 / 出牌区的导出（纯函数）。"""
    if GAMESTATE_BEGIN in original:
        return original
    if GAMESTATE_ANCHOR not in original:
        raise SystemExit(
            "找不到 gamestate 的插入锚点 state_data.hand = extract_area(G.hand)；"
            "mod 可能改版了"
        )
    return original.replace(
        GAMESTATE_ANCHOR, GAMESTATE_ANCHOR + "\n" + GAMESTATE_BLOCK, 1
    )


def patched_cash_out_text(original: str) -> str:
    """给 cash_out.lua 加上"等结算 UI 建好"的守卫（纯函数）。"""
    if CASH_OUT_BEGIN in original:
        return original
    if CASH_OUT_ANCHOR not in original:
        raise SystemExit(
            "找不到 cash_out 的插入锚点 G.FUNCS.cash_out({ config = {} })；"
            "mod 可能改版了"
        )
    return original.replace(CASH_OUT_ANCHOR, CASH_OUT_BLOCK, 1)


def patched_pack_text(original: str) -> str:
    """给 pack.lua 加两处守卫：不等无用的手牌 + 等待事件有超时（纯函数）。"""
    text = original
    if PACK_BEGIN not in text:
        if PACK_ANCHOR not in text:
            raise SystemExit(
                "找不到 pack 的插入锚点 local needs_hand = ...；mod 可能改版了"
            )
        text = text.replace(PACK_ANCHOR, PACK_BLOCK, 1)

    if PACK_TIMEOUT_BEGIN not in text:
        if PACK_TIMEOUT_ANCHOR not in text:
            raise SystemExit(
                '找不到 pack 的插入锚点 sendDebugMessage("Init pack()"...)；'
                "mod 可能改版了"
            )
        text = text.replace(
            PACK_TIMEOUT_ANCHOR,
            PACK_TIMEOUT_ANCHOR + "\n" + PACK_TIMEOUT_BLOCK,
            1,
        )
        # 给每个等待事件的 `return false` 前面插一道超时检查。
        # 逐行精确匹配（不用子串替换：12 空格缩进里含有 10 空格缩进那串，
        # 用子串会误伤并留下多余空格）。
        out: list[str] = []
        for line in text.splitlines(keepends=True):
            bare = line.rstrip("\r\n")
            if bare.strip() == "return false" and (
                bare.startswith(" " * 10) or bare.startswith(" " * 12)
            ):
                indent = " " * (len(bare) - len(bare.lstrip()))
                out.append(f"{indent}if _pack_give_up() then return true end\n")
            out.append(line)
        text = "".join(out)
    return text


def patched_entry_text(original: str) -> str:
    """把两处改动打进入口文件文本（纯函数，便于单测与复用）。"""
    text = original
    if BEGIN not in text:
        anchor = "BB_SETTINGS.setup()\n"
        if anchor not in text:
            raise SystemExit(
                "找不到插入锚点 BB_SETTINGS.setup()；入口文件可能改版了"
            )
        text = text.replace(anchor, anchor + "\n" + HOOK_BLOCK, 1)

    if ENDPOINT_LINE.strip() not in text:
        if ENDPOINT_ANCHOR not in text:
            raise SystemExit(
                f"找不到端点注册锚点 {ENDPOINT_ANCHOR.strip()}；入口文件可能改版了"
            )
        text = text.replace(ENDPOINT_ANCHOR, ENDPOINT_ANCHOR + ENDPOINT_LINE, 1)
    return text


def emit_entry() -> int:
    """生成一份**已经打好补丁**的入口文件到 mod_additions/。

    用途：有些机器上 python 写不进 `%AppData%`（安全软件按程序拦截），
    这时可以让用户用**系统自带的 copy 命令**把现成文件覆盖过去，
    完全不经过 python。
    """
    entry = MOD_DIR / ENTRY
    if not entry.exists():
        print(f"找不到 {entry}")
        return 1
    patched = patched_entry_text(entry.read_text(encoding="utf-8"))
    out = SOURCE_ROOT / ENTRY
    out.write_text(patched, encoding="utf-8")
    print(f"已生成 {out}")
    print(f"  面板加载块：{'有' if BEGIN in patched else '缺'}")
    print(f"  端点注册：  {'有' if ENDPOINT_LINE.strip() in patched else '缺'}")
    print(f"  行数：{len(patched.splitlines())}")

    game_state = MOD_DIR / GAMESTATE
    if not game_state.exists():
        print(f"找不到 {game_state}")
        return 1
    patched_gs = patched_gamestate_text(game_state.read_text(encoding="utf-8"))
    out_gs = SOURCE_ROOT / GAMESTATE
    out_gs.parent.mkdir(parents=True, exist_ok=True)
    out_gs.write_text(patched_gs, encoding="utf-8")
    print(f"已生成 {out_gs}")
    print(f"  弃牌堆导出：{'有' if GAMESTATE_BEGIN in patched_gs else '缺'}")

    cash_out = MOD_DIR / CASH_OUT
    if not cash_out.exists():
        print(f"找不到 {cash_out}")
        return 1
    patched_co = patched_cash_out_text(cash_out.read_text(encoding="utf-8"))
    out_co = SOURCE_ROOT / CASH_OUT
    out_co.write_text(patched_co, encoding="utf-8")
    print(f"已生成 {out_co}")
    print(f"  结算守卫：  {'有' if CASH_OUT_BEGIN in patched_co else '缺'}")

    pack = MOD_DIR / PACK
    if not pack.exists():
        print(f"找不到 {pack}")
        return 1
    patched_pk = patched_pack_text(pack.read_text(encoding="utf-8"))
    out_pk = SOURCE_ROOT / PACK
    out_pk.write_text(patched_pk, encoding="utf-8")
    print(f"已生成 {out_pk}")
    print(f"  开包手牌守卫：{'有' if PACK_BEGIN in patched_pk else '缺'}")
    return 0


def install() -> int:
    if not MOD_DIR.is_dir():
        print(f"找不到 mod 目录：{MOD_DIR}")
        return 1

    for rel in FILES:
        source = SOURCE_ROOT / rel
        target = MOD_DIR / rel
        if not source.exists():
            print(f"缺少源文件：{source}")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        print(f"  已拷贝 {rel}")

    entry = MOD_DIR / ENTRY
    original = entry.read_text(encoding="utf-8")
    backup = MOD_DIR / BACKUP
    if not backup.exists():
        shutil.copy2(entry, backup)
        print(f"  已备份 {ENTRY} -> {BACKUP}")

    text = patched_entry_text(original)
    if text != original:
        entry.write_text(text, encoding="utf-8")
        print(f"  已写入 {ENTRY}（含面板加载块 + 端点注册）")
    else:
        print("  入口文件无变化（补丁已存在）")

    # 顺手把现成的入口文件也留一份，方便"python 写不进 AppData"的机器手动拷
    (SOURCE_ROOT / ENTRY).write_text(text, encoding="utf-8")

    game_state = MOD_DIR / GAMESTATE
    original_gs = game_state.read_text(encoding="utf-8")
    gs_backup = MOD_DIR / GAMESTATE_BACKUP
    if not gs_backup.exists():
        shutil.copy2(game_state, gs_backup)
        print(f"  已备份 {GAMESTATE} -> {GAMESTATE_BACKUP}")
    patched_gs = patched_gamestate_text(original_gs)
    if patched_gs != original_gs:
        game_state.write_text(patched_gs, encoding="utf-8")
        print(f"  已写入 {GAMESTATE}（导出弃牌堆 / 出牌区）")
    else:
        print("  gamestate 无变化（补丁已存在）")
    out_gs = SOURCE_ROOT / GAMESTATE
    out_gs.parent.mkdir(parents=True, exist_ok=True)
    out_gs.write_text(patched_gs, encoding="utf-8")

    cash_out = MOD_DIR / CASH_OUT
    original_co = cash_out.read_text(encoding="utf-8")
    co_backup = MOD_DIR / CASH_OUT_BACKUP
    if not co_backup.exists():
        shutil.copy2(cash_out, co_backup)
        print(f"  已备份 {CASH_OUT} -> {CASH_OUT_BACKUP}")
    patched_co = patched_cash_out_text(original_co)
    if patched_co != original_co:
        cash_out.write_text(patched_co, encoding="utf-8")
        print(f"  已写入 {CASH_OUT}（等结算 UI 建好）")
    else:
        print("  cash_out 无变化（补丁已存在）")
    out_co = SOURCE_ROOT / CASH_OUT
    out_co.write_text(patched_co, encoding="utf-8")

    pack = MOD_DIR / PACK
    original_pk = pack.read_text(encoding="utf-8")
    pk_backup = MOD_DIR / PACK_BACKUP
    if not pk_backup.exists():
        shutil.copy2(pack, pk_backup)
        print(f"  已备份 {PACK} -> {PACK_BACKUP}")
    patched_pk = patched_pack_text(original_pk)
    if patched_pk != original_pk:
        pack.write_text(patched_pk, encoding="utf-8")
        print(f"  已写入 {PACK}（没有目标牌就不等手牌）")
    else:
        print("  pack 无变化（补丁已存在）")
    out_pk = SOURCE_ROOT / PACK
    out_pk.write_text(patched_pk, encoding="utf-8")

    print("\n安装完成。重启游戏后生效（面板默认显示，F8 可隐藏）。")
    return 0


def uninstall() -> int:
    entry = MOD_DIR / ENTRY
    backup = MOD_DIR / BACKUP
    if backup.exists():
        shutil.copy2(backup, entry)
        print(f"  已从 {BACKUP} 还原 {ENTRY}")
    else:
        text = entry.read_text(encoding="utf-8")
        text = text.replace(ENDPOINT_LINE, "")
        start = text.find(BEGIN)
        if start != -1:
            end = text.find(END, start)
            if end != -1:
                text = text[:start] + text[end + len(END) + 1:]
        entry.write_text(text, encoding="utf-8")
        print(f"  已就地移除补丁（没有备份）")

    for rel in FILES:
        target = MOD_DIR / rel
        if target.exists():
            target.unlink()
            print(f"  已删除 {rel}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="安装决策日志面板到 balatrobot mod")
    parser.add_argument("--uninstall", action="store_true", help="卸载并还原")
    parser.add_argument("--check", action="store_true", help="只检查状态")
    parser.add_argument("--emit-entry", action="store_true",
                        help="只生成打好补丁的 balatrobot.lua（不动 mod 目录，"
                             "给 python 写不进 AppData 的机器手动拷贝用）")
    args = parser.parse_args()

    if args.emit_entry:
        return emit_entry()
    if args.check:
        status()
        return 0
    if args.uninstall:
        return uninstall()
    return install()


if __name__ == "__main__":
    sys.exit(main())
