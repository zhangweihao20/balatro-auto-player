"""①②③ 的配对报告：逐局判定 + 机制指标（机制指标是主读数）。

结果指标（平均底注）在 10 局上噪声太大；机制指标方差小，而且直接回答
"改动有没有生效"：
    * 局终 / 底注 5 时手上 ×Mult 张数
    * 换牌次数（每局）
    * 每局改造出的张数、Standard 包买了几个
"""

import collections
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from balatro_bot.joker_data import JOKER_DATA  # noqa: E402
from balatro_bot.jokers import MULTIPLICATIVE_JOKERS  # noqa: E402

L2K = {d["name"]: k for k, d in JOKER_DATA.items() if d.get("name")}


def is_mult(key: str | None) -> bool:
    if key is None:
        return False
    config = (JOKER_DATA.get(key) or {}).get("config") or {}
    return bool(config.get("Xmult")) or key in MULTIPLICATIVE_JOKERS


def split(path: Path) -> list[list[dict]]:
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


def metrics(path: Path) -> dict:
    mult_ante5: list[int] = []
    mult_final: list[int] = []
    swaps = 0
    sold_mult = 0
    enhanced: list[int] = []
    packs: collections.Counter[str] = collections.Counter()
    for game in split(path):
        by_ante: dict[int, list[str]] = {}
        best: collections.Counter[str] = collections.Counter()
        for rec in game:
            snap = rec.get("snapshot") or {}
            jokers, ante = snap.get("jokers"), snap.get("ante")
            if isinstance(jokers, list) and isinstance(ante, int):
                by_ante[ante] = jokers
            if rec.get("kind") == "sell" and " 换 " in (rec.get("why") or ""):
                swaps += 1
                first = rec["why"][2:].split(" 换 ")[0]
                key = L2K.get(first.split(" ", 1)[-1].strip())
                if is_mult(key):
                    sold_mult += 1
            if rec.get("kind") == "pack" and "补充包" in (rec.get("why") or ""):
                for kind in ("Buffoon", "Arcana", "Spectral", "Standard", "Celestial"):
                    if kind in rec["why"]:
                        packs[kind] += 1
            build = snap.get("build") or {}
            text = build.get("改造")
            if text:
                for part in text.split(","):
                    key, _, value = part.strip().partition(":")
                    if value.isdigit():
                        best[key] = max(best[key], int(value))

        def count_mult(labels: list[str]) -> int:
            return sum(1 for label in labels if is_mult(L2K.get(label)))

        if 5 in by_ante:
            mult_ante5.append(count_mult(by_ante[5]))
        top = max(by_ante) if by_ante else 0
        mult_final.append(count_mult(by_ante.get(top, [])))
        enhanced.append(sum(best.values()))

    n = len(mult_final)
    return {
        "局数": n,
        "×Mult@底注5": statistics.mean(mult_ante5) if mult_ante5 else 0.0,
        "有×Mult@底注5": (
            f"{sum(1 for v in mult_ante5 if v)}/{len(mult_ante5)}"
        ),
        "局终×Mult": statistics.mean(mult_final),
        "局终0张×Mult": sum(1 for v in mult_final if v == 0),
        "换牌次数": swaps,
        "换牌/局": swaps / n if n else 0,
        "卖掉乘法牌": sold_mult,
        "改造张数/局": statistics.mean(enhanced),
        "补充包": dict(packs),
    }


def main() -> int:
    a, b = Path(sys.argv[1]), Path(sys.argv[2])
    ra, rb = metrics(a), metrics(b)

    ja = {r["seed"]: r for r in json.load(open(sys.argv[3], encoding="utf-8"))}
    jb = {r["seed"]: r for r in json.load(open(sys.argv[4], encoding="utf-8"))}
    shared = sorted(set(ja) & set(jb))
    better = worse = same = 0
    print(f"===== 逐局（{len(shared)} 个共同种子）=====")
    for seed in shared:
        x, y = ja[seed], jb[seed]
        if (x["ante"], x["round_num"]) == (y["ante"], y["round_num"]):
            same += 1
            mark = "相同"
        elif (y["ante"], y["round_num"]) > (x["ante"], x["round_num"]):
            better += 1
            mark = "B 更好"
        else:
            worse += 1
            mark = "B 更差"
        print(f'  {seed:<8} A {x["ante"]:>2}/{x["round_num"]:<3} '
              f'B {y["ante"]:>2}/{y["round_num"]:<3}  {mark}')
    print(f"\n  B 更好 {better} / 相同 {same} / 更差 {worse}")

    print("\n===== 机制指标（主读数）=====")
    keys = list(ra)
    print(f"  {'指标':<16}{'A 基线':>16}{'B 三项全开':>16}")
    for key in keys:
        print(f"  {key:<16}{str(ra[key]):>16}{str(rb[key]):>16}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
