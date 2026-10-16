"""把一次 eval 的产出整理成可发布的结果文件（放进 results/，会被提交）。

输入（都在 runs/，不提交）：
    run100.json          逐局结果
    journal_100.jsonl    逐步决策日志
    run100.log           运行日志（取参数与进度）

输出（results/）：
    run-<n>.json            逐局结果（原样）
    run-<n>.md              人读的汇总：结果指标 + 机制指标 + 失败分析
    journal-<n>.jsonl.gz    逐步决策日志（压缩后发布，是数据的完整形态）

用法：
    python tools/run_report.py <名字> --games 100
例：
    python tools/run_report.py run-100 --games 100
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import shutil
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from balatro_bot.joker_data import JOKER_DATA  # noqa: E402
from balatro_bot.jokers import MULTIPLICATIVE_JOKERS  # noqa: E402
from balatro_bot.params import TunableParams  # noqa: E402

LABEL_TO_KEY = {d["name"]: k for k, d in JOKER_DATA.items() if d.get("name")}
PACK_KINDS = ("Buffoon", "Arcana", "Spectral", "Standard", "Celestial")


def is_mult(key: str | None) -> bool:
    if key is None:
        return False
    config = (JOKER_DATA.get(key) or {}).get("config") or {}
    return bool(config.get("Xmult")) or key in MULTIPLICATIVE_JOKERS


def split_games(path: Path) -> list[list[dict]]:
    games: list[list[dict]] = []
    previous = -1
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        step = rec.get("step") or 0
        if step <= previous or not games:
            games.append([])
        previous = step
        games[-1].append(rec)
    return games


def mechanism_metrics(journal: Path) -> dict:
    mult_ante5: list[int] = []
    mult_final: list[int] = []
    swaps = 0
    sold_mult = 0
    enhanced: list[int] = []
    packs: collections.Counter[str] = collections.Counter()
    kinds: collections.Counter[str] = collections.Counter()

    for game in split_games(journal):
        by_ante: dict[int, list[str]] = {}
        best: collections.Counter[str] = collections.Counter()
        for rec in game:
            kinds[rec.get("kind", "?")] += 1
            snap = rec.get("snapshot") or {}
            jokers, ante = snap.get("jokers"), snap.get("ante")
            if isinstance(jokers, list) and isinstance(ante, int):
                by_ante[ante] = jokers
            why = rec.get("why") or ""
            if rec.get("kind") == "sell" and " 换 " in why:
                swaps += 1
                first = why[len("卖 "):].split(" 换 ")[0]
                key = LABEL_TO_KEY.get(first.split(" ", 1)[-1].strip())
                if is_mult(key):
                    sold_mult += 1
            if rec.get("kind") == "pack" and "补充包" in why:
                for kind in PACK_KINDS:
                    if kind in why:
                        packs[kind] += 1
            text = (snap.get("build") or {}).get("改造")
            if text:
                for part in text.split(","):
                    key, _, value = part.strip().partition(":")
                    if value.isdigit():
                        best[key] = max(best[key], int(value))

        def count_mult(labels: list[str]) -> int:
            return sum(1 for label in labels if is_mult(LABEL_TO_KEY.get(label)))

        if 5 in by_ante:
            mult_ante5.append(count_mult(by_ante[5]))
        top = max(by_ante) if by_ante else 0
        mult_final.append(count_mult(by_ante.get(top, [])))
        enhanced.append(sum(best.values()))

    n = max(1, len(mult_final))
    return {
        "局数": len(mult_final),
        "×Mult@底注5（均）": round(statistics.mean(mult_ante5), 2) if mult_ante5 else 0.0,
        "到过底注5且手上≥1张×Mult": f"{sum(1 for v in mult_ante5 if v)}/{len(mult_ante5)}",
        "局终×Mult（均）": round(statistics.mean(mult_final), 2),
        "局终0张×Mult的局": sum(1 for v in mult_final if v == 0),
        "换牌次数": swaps,
        "换牌/局": round(swaps / n, 2),
        "卖掉乘法牌": sold_mult,
        "改造张数/局": round(statistics.mean(enhanced), 2),
        "补充包": dict(packs),
        "决策种类": dict(kinds.most_common()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="结果名，例如 run-100")
    ap.add_argument("--games", type=int, required=True)
    ap.add_argument("--json", default=None, help="逐局结果 JSON（默认 runs/run100.json）")
    ap.add_argument("--journal", default=None, help="决策日志（默认 runs/journal_100.jsonl）")
    args = ap.parse_args()

    stem = args.name.split("-")[-1]
    json_path = Path(args.json) if args.json else ROOT / "runs" / f"run{stem}.json"
    journal = (Path(args.journal) if args.journal
               else ROOT / "runs" / f"journal_{stem}.jsonl")
    out = ROOT / "results"
    out.mkdir(exist_ok=True)

    runs = json.loads(json_path.read_text(encoding="utf-8"))
    antes = [r["ante"] for r in runs]
    wins = sum(1 for r in runs if r.get("won"))
    errors = sum(1 for r in runs if r.get("error"))
    early = sum(1 for a in antes if a <= 2)
    dist = collections.Counter(antes)
    steps = [r.get("steps") or 0 for r in runs]

    metrics = mechanism_metrics(journal)
    params = TunableParams()

    shutil.copyfile(json_path, out / f"{args.name}.json")
    with open(journal, "rb") as src, gzip.open(out / f"journal-{stem}.jsonl.gz",
                                              "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)

    lines = [
        f"# 评测结果：{args.name}（{len(runs)} 局）",
        "",
        "> 由 `tools/run_report.py` 从 `runs/` 的原始产出生成，",
        "> 原始逐局结果见同目录的 JSON，逐步决策日志见 `.jsonl.gz`。",
        "",
        "## 设置",
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| 局数 | " + str(len(runs)) + " |",
        "| 种子 | 逐局不同 |",
        "| 速度 | 10 倍速（`BALATROBOT_FAST=1`） |",
        "| 牌组 / 难度 | RED / WHITE |",
        "| 弃牌决策 | plan（概率评分） |",
        "",
        "关键参数（`balatro_bot/params.py`）：",
        "",
        "```text",
        f"compound_switch_guard      = {params.compound_switch_guard}      # 复利牌不当换牌牺牲品",
        f"filler_value_ceiling       = {params.filler_value_ceiling}",
        f"filler_free_antes          = {params.filler_free_antes}",
        f"filler_penalty_per_ante    = {params.filler_penalty_per_ante}       # 廉价填充牌估值随底注递减",
        f"standard_pack_bonus        = {params.standard_pack_bonus}       # 实测净负，默认关",
        f"crisis_uses_potential      = {params.crisis_uses_potential}      # 危急度按潜力估",
        "```",
        "",
        "## 结果",
        "",
        "```text",
        f"平均底注   {statistics.mean(antes):.2f}",
        f"中位底注   {statistics.median(antes):.0f}",
        f"最高底注   {max(antes)}",
        f"通关       {wins}/{len(runs)} = {wins / len(runs) * 100:.1f}%",
        f"底注≤2 早死 {early}/{len(runs)} = {early / len(runs) * 100:.0f}%",
        f"出错中止   {errors}",
        f"平均步数   {statistics.mean(steps):.0f}",
        "```",
        "",
        "底注分布：",
        "",
        "```text",
        "  ".join(f"A{a}:{dist[a]}" for a in sorted(dist)),
        "```",
        "",
        "## 机制指标",
        "",
        "结果指标在 100 局上仍然噪声不小；机制指标方差小，而且直接回答",
        "「改动有没有生效」。",
        "",
        "```text",
    ]
    for key, value in metrics.items():
        if key == "决策种类":
            continue
        lines.append(f"{key:<24} {value}")
    lines += [
        "```",
        "",
        "决策种类分布：",
        "",
        "```text",
        "  ".join(f"{k}:{v}" for k, v in metrics["决策种类"].items()),
        "```",
        "",
    ]

    md = out / f"{args.name}.md"
    md.write_text("\n".join(lines), encoding="utf-8")

    print(f"  {md}")
    print(f"  {out / (args.name + '.json')}")
    print(f"  {out / ('journal-' + stem + '.jsonl.gz')}")
    print()
    print(f"  平均底注 {statistics.mean(antes):.2f}   最高 {max(antes)}   "
          f"通关 {wins}/{len(runs)}   底注≤2 早死 {early}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
