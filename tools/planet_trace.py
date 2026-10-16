"""追踪某局里星球牌的完整生命周期：从哪来、有没有用掉。"""

import json
import sys
from pathlib import Path

PLANET_WORDS = (
    "Pluto", "Mercury", "Uranus", "Venus", "Saturn", "Jupiter",
    "Earth", "Mars", "Neptune", "Ceres", "Eris", "Planet X",
    "冥王星", "水星", "天王星", "金星", "土星", "木星",
    "地球", "火星", "海王星", "谷神星", "厄里斯",
)


def main() -> int:
    path = sys.argv[1]
    recs = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # 切局
    games: list[list[dict]] = []
    for rec in recs:
        if rec.get("step") == 1 or not games:
            games.append([])
        games[-1].append(rec)

    print(f"共 {len(games)} 段（每段一局）\n")
    for idx, seg in enumerate(games, 1):
        events = []
        for rec in seg:
            why = rec["why"]
            nums = rec.get("numbers") or {}
            text = why + " " + " ".join(f"{k}={v}" for k, v in nums.items())
            if any(p in text for p in PLANET_WORDS):
                events.append((rec["step"], rec["kind"], why, nums))
        if not events:
            continue
        print(f"===== 第 {idx} 局（{len(seg)} 步）=====")
        for step, kind, why, nums in events:
            got = nums.get("拿了") or ""
            hand = nums.get("牌型") or ""
            print(f"  {step:>4} {kind:<7} {why[:44]:<46} {got} {hand}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
