"""查 build 字段在原始记录里长什么样（尤其"主流派"消失的那几条）。"""

import json
import sys
from pathlib import Path


def main() -> int:
    recs = [
        json.loads(line)
        for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    games: list[list[dict]] = []
    for rec in recs:
        if rec.get("step") == 1 or not games:
            games.append([])
        games[-1].append(rec)
    seg = games[idx - 1]
    print(f"第 {idx} 局，{len(seg)} 条记录")
    print(f'  {"step":>5} {"state":<16} {"kind":<9} build')
    for rec in seg:
        build = (rec.get("snapshot") or {}).get("build")
        if build is None:
            shown = "（无 build 字段）"
        else:
            shown = " ".join(f"{k}={v}" for k, v in build.items())
        print(f'  {rec.get("step"):>5} {rec.get("state", ""):<16} {rec.get("kind", ""):<9} {shown}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
