"""Boss / 精英盲注的**约束建模**。

这个模块只管一件事：让机器人对"这一回合世界是什么样"的理解与游戏一致。
至于"知道了之后怎么打"，是策略层的事，不在这里。

## 为什么需要单独建模

游戏的 Boss 约束走两条实现路径（见 `blind.lua`）：

1. **`Blind:debuff_card(card)`** —— 逐张压制。游戏会把 `card.debuff = true`
   写进状态，机器人**只要算分时跳过这些牌就够了**，不需要额外建模。
   走这条路的：The Club / The Goad / The Head / The Window / The Plant / The Pillar。

2. **`Blind:debuff_hand(cards, hand, handname, check)`** —— 整手作废、
   改动合法动作、或**在算分之前改状态**。这条路的后果**不体现在出牌前的
   状态里**，机器人必须自己建模，否则算出来的分是错的。

## 两个实测过的建模错误（本模块建立的原因）

* **The Arm**：`functions_state_events.lua:614` 的结算顺序是

      if not G.GAME.blind:debuff_hand(...) then
          mult = mod_mult(G.GAME.hands[text].mult)   -- 已经是降级后的值

  The Arm 在 `debuff_hand` 里先执行 `level_up_hand(..., -1)` 且**不返回 true**，
  所以**这一手按降级后的等级算分**。机器人读的是出牌前的状态
  → 等级 > 1 时固定高估一个等级。

* **Crimson Heart**：`Card:set_debuff` 对**小丑牌也生效**
  （`blind.lua:647` 专门处理 `card.area == G.jokers`），状态里能读到标记，
  但算分循环完全没检查 → 每手都高估自己的分数。

## 匹配方式

按**英文名**匹配，不按效果文本。因为 mod 上报的 `blinds.boss.name` 取自
`G.P_BLINDS[key].name`，那是游戏内部的英文原名，**不随游戏语言变化**；
而 `effect` 是本地化文本，中文环境下会变成中文，拿它做关键词匹配很脆。
"""

from __future__ import annotations

from dataclasses import dataclass, fields

__all__ = [
    "BossEffect",
    "BOSS_EFFECTS",
    "boss_effect",
    "is_elite",
    "unmodelled_effects",
]


@dataclass(frozen=True, slots=True)
class BossEffect:
    """一条盲注约束对**模型**的影响。

    字段按"机器人需要做什么"分组，`None`/`False` 表示这条盲注没有该效果。
    """

    name: str
    # ---------------------------------------------------------- 算分模型
    # 这些改动**不进状态**，必须由机器人自己算进去
    halves_base_score: bool = False            # The Flint：基础筹码/倍率减半
    decreases_played_hand_level: bool = False  # The Arm：打出的牌型等级 -1（算分前）
    disables_one_joker_per_hand: bool = False  # Crimson Heart：每手禁用一张小丑牌

    # ------------------------------------------------- 逐张压制（状态已覆盖）
    # 游戏会把 card.debuff 写进状态，机器人只要算分时跳过即可。
    # 这里记录是为了自检和日志，不是额外要做的计算。
    debuffs_suit: str | None = None            # The Club / Goad / Head / Window
    debuffs_faces: bool = False                # The Plant
    debuffs_played_this_ante: bool = False     # The Pillar
    debuffs_all_cards: bool = False            # Verdant Leaf

    # ---------------------------------------------------------- 合法动作
    exact_cards: int | None = None             # The Psychic：必须打满 5 张
    bans_repeat_hand_types: bool = False       # The Eye
    locks_first_hand_type: bool = False        # The Mouth
    bans_most_played_hand: bool = False        # The Ox
    takes_money_per_card: bool = False         # The Tooth
    discards_after_play: int = 0               # The Hook

    # ------------------------------------- 状态可变的（约束语义，非即时后果）
    debuff_lifted_by_selling_joker: bool = False  # Verdant Leaf：卖一张牌即解除
    forces_one_card: bool = False                 # Cerulean Bell
    shuffles_jokers: bool = False                 # Amber Acorn

    # ------------------------------------------------- 状态已直接反映
    # 这些不需要机器人做任何计算，状态里就是对的。记录用于自检。
    starts_with_zero_discards: bool = False    # The Water
    one_hand_only: bool = False                # The Needle
    hand_size_delta: int = 0                   # The Manacle
    score_multiplier: float = 2.0              # The Wall(4) / Violet Vessel(6)
    cards_face_down: bool = False              # The House / Wheel / Fish / Mark
    draws_after_play: int = 0                  # The Serpent
    elite: bool = False                        # Ante 8 精英盲注

    def describe(self) -> str:  # pragma: no cover - 仅用于日志
        active = [
            f.name for f in fields(self)
            if f.name not in ("name", "elite", "score_multiplier")
            and getattr(self, f.name) not in (False, None, 0)
        ]
        if self.score_multiplier != 2.0:
            active.append(f"score_multiplier={self.score_multiplier}")
        return f"{self.name}: " + (", ".join(active) if active else "无特殊约束")


# ---------------------------------------------------------------- 约束表
#
# 28 条，key 是游戏内部的英文盲注名（小写），与 G.P_BLINDS[*].name 一致。
# 数据来源：runs/game-source/game.lua 的 G.P_BLINDS 定义
#          + blind.lua 的 debuff_card / debuff_hand 实现。
# 用 tools/list_bosses.py 可以重新导出对照。

BOSS_EFFECTS: dict[str, BossEffect] = {
    # ------------------------------------------------------------ 普通 Boss
    "the arm": BossEffect(
        "The Arm", decreases_played_hand_level=True,
    ),
    "the club": BossEffect("The Club", debuffs_suit="Clubs"),
    "the eye": BossEffect("The Eye", bans_repeat_hand_types=True),
    "the fish": BossEffect("The Fish", cards_face_down=True),
    "the flint": BossEffect("The Flint", halves_base_score=True),
    "the goad": BossEffect("The Goad", debuffs_suit="Spades"),
    "the head": BossEffect("The Head", debuffs_suit="Hearts"),
    "the hook": BossEffect("The Hook", discards_after_play=2),
    "the house": BossEffect("The House", cards_face_down=True),
    "the manacle": BossEffect("The Manacle", hand_size_delta=-1),
    "the mark": BossEffect("The Mark", cards_face_down=True),
    "the mouth": BossEffect("The Mouth", locks_first_hand_type=True),
    "the needle": BossEffect("The Needle", one_hand_only=True, score_multiplier=1.0),
    "the ox": BossEffect("The Ox", bans_most_played_hand=True),
    "the pillar": BossEffect("The Pillar", debuffs_played_this_ante=True),
    "the plant": BossEffect("The Plant", debuffs_faces=True),
    "the psychic": BossEffect("The Psychic", exact_cards=5),
    "the serpent": BossEffect("The Serpent", draws_after_play=3),
    "the tooth": BossEffect("The Tooth", takes_money_per_card=True),
    "the wall": BossEffect("The Wall", score_multiplier=4.0),
    "the water": BossEffect("The Water", starts_with_zero_discards=True),
    "the wheel": BossEffect("The Wheel", cards_face_down=True),
    "the window": BossEffect("The Window", debuffs_suit="Diamonds"),
    # ------------------------------------------------------------ 精英盲注
    "amber acorn": BossEffect("Amber Acorn", shuffles_jokers=True, elite=True),
    "cerulean bell": BossEffect("Cerulean Bell", forces_one_card=True, elite=True),
    "crimson heart": BossEffect(
        "Crimson Heart", disables_one_joker_per_hand=True, elite=True,
    ),
    "verdant leaf": BossEffect(
        "Verdant Leaf", debuffs_all_cards=True,
        debuff_lifted_by_selling_joker=True, elite=True,
    ),
    "violet vessel": BossEffect("Violet Vessel", score_multiplier=6.0, elite=True),
}


def boss_effect(name: str | None) -> BossEffect | None:
    """按盲注名查约束模型。非 Boss / 认不出来时返回 None。

    认不出来**不报错**——游戏更新或换语言时应该降级成"无特殊约束"继续跑，
    而不是让整个机器人崩掉。调用方可以用 unmodelled_effects() 做自检。
    """
    if not name:
        return None
    return BOSS_EFFECTS.get(name.strip().lower())


def is_elite(name: str | None) -> bool:
    effect = boss_effect(name)
    return bool(effect and effect.elite)


def unmodelled_effects(game_names: list[str]) -> list[str]:
    """游戏里有、但约束表里没有的盲注名。

    给自检用：游戏更新加了新 Boss 时，这个列表会提醒该补建模了，
    而不是静默地把新约束当成"没有约束"。
    """
    known = set(BOSS_EFFECTS)
    normalised = {name.strip().lower() for name in game_names}
    # 小盲/大盲没有约束，不算缺口
    normalised -= {"small blind", "big blind"}
    return sorted(normalised - known)
