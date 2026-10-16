"""可调参数集中管理。

设计目标（来自策略规格）：
    "为每一项决策（买小丑牌、消费、丢牌等行为）设置函数，通过不断地测试来
    修改参数以达到通关目标。"

所以这里把所有"拍脑袋的常数"集中成一个可序列化的对象：
    * 每个参数都有**自动生成的初始值**（不是随手写的常数，见各字段注释）
    * 可以从 JSON 读写，方便调参工具改写
    * 决策函数只读这个对象，不再散落魔法数字

调参入口：tools/tune.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_PARAMS_PATH = Path("runs") / "params.json"


@dataclass
class TunableParams:
    """全部可调参数。改这里就能改变策略，不需要动决策代码。"""

    # ---------------------------------------------------------- 出牌 / 弃牌
    #
    # 弃牌门槛：弃牌后的**期望得分**要超过现在这一手多少倍，才值得弃牌。
    #   弃牌 ⟺ E ≥ S_cur × (1 + discard_aversion)
    # 调大更保守（多打牌、少赌凑牌），调小更激进。
    # 初始值由 planning.derive_initial_discard_aversion() 在合成手牌上
    # 统计期望提升分布后自动生成（见该函数），之后可由调参工具修正。
    discard_aversion: float = field(default_factory=lambda: _auto_aversion())
    # 经济规则：回合结束每剩一手 +$1，弃牌不给钱，所以出牌机会比弃牌更值钱。
    # 打开时要求"剩余出牌数 > 剩余弃牌数"（两者都不为 0 时）——弃牌数追平
    # 或超过出牌数时，把弃牌门槛降到 0（不亏就弃），先把不值钱的弃牌花掉。
    keep_hands_above_discards: bool = True
    # 弃牌数量系数。规格明确它**不再是惩罚项**：Balatro 里补牌张数 = 弃牌张数，
    # 多弃一张就等于多循环一张、更早看到需要的牌。
    # 初始值由 planning.derive_initial_discard_bonus() 自动生成。
    discard_bonus: float = field(default_factory=lambda: _auto_discard_bonus())
    # 综合评分里"概率的幂次 γ"：γ < 1 更宽容低概率的大牌型（前期），
    # γ > 1 更要求稳定（中后期）。
    # 切换依据**不是底注**，而是"专精牌型的期望值是否超过一般高难度牌型"：
    #   switch_point    —— 专精牌型得分 / 参考牌型得分 达到多少时开始切换
    #   switch_sharpness—— 过渡的陡峭程度
    #   big_hand_reference —— "一般高难度牌型"的基础分参考值（默认同花 35×4=140）
    gamma_early: float = 0.55
    gamma_late: float = 1.45
    switch_point: float = 0.8
    switch_sharpness: float = 0.25
    big_hand_reference: float = 140.0

    # ------------------------------------------------- Boss 约束的应对措施
    # Verdant Leaf（Ante 8 精英）：全体卡牌被压制，卖**任意一张**小丑牌即解除
    # （card.lua:1616 会 G.GAME.blind:disable()）。不卖的话整回合只能用牌型
    # 基础筹码打分，Ante 8 几乎必然失败。开着它就会自动卖最不心疼的那张。
    sell_to_lift_debuff: bool = True
    # The Arm：打出的牌型等级 -1。开着它就会避开"等级 > 1"的牌型
    # （前提是还有别的牌型能打，否则自动放开——见 build_solver 的兜底）。
    arm_level_guard: bool = True

    # ---------------------------------------------------------- 小丑牌
    # 契合度调整（规格："随机应变"，围绕已有牌构筑）
    synergy_bonus: float = 1.5
    synergy_penalty: float = 2.0
    # 空牌位加成：把位置填满 > 只等最好的那张
    empty_slot_bonus: float = 2.0
    few_slot_bonus: float = 1.0
    few_slot_threshold: int = 4
    # 底注达到 multi_ante 之后，乘区牌的估值加 multi_bonus
    multi_ante: int = 4
    multi_bonus: float = 2.0
    # 纯功能牌在 pure_utility_free_ante 之前封顶
    pure_utility_cap: float = 3.5
    pure_utility_free_ante: int = 5

    # ---------------------------------------------------------- 商店 / 经济
    buy_threshold: float = 4.5
    voucher_threshold: float = 5.0
    pack_threshold: float = 5.0
    money_reserve: int = 4
    interest_floor: int = 25
    interest_floor_ante: int = 3
    interest_floor_min_jokers: int = 3
    exception_value: float = 7.5
    reroll_min_money: int = 15
    reroll_cost_ratio: float = 0.5          # 钱扣掉重摇费后仍要在保留额的多少倍以上
    sell_upgrade_margin: float = 1.0
    # 经济充足（钱 ≥ surplus_money）时，换牌门槛再降这么多。设成和
    # sell_upgrade_margin 一样大就等于"只要新牌更好就换"——钱闲着没有收益，
    # 而牌位是稀缺资源。
    surplus_sell_relief: float = 1.0
    # 小丑牌的买卖改按**真实得分贡献**判断（不再只看静态表，原因见
    # strategy.valuation_scenario 的说明）。这两个是"得分要涨多少倍"的门槛：
    #   joker_buy_gain  —— 往空牌位买一张，阵容得分至少涨这么多
    #   joker_swap_gain —— 换牌（有买卖差价损耗）要求更高的涨幅
    joker_buy_gain: float = 0.05
    joker_swap_gain: float = 0.08

    # ------------------------------------------------- 紧急状态（落后 / 过不了关）
    # 实测 10 局里有 7 局是**带着 $47~84 的余额死掉的**，而当时面对的是
    # 11000~35000 的需求——那些钱连零头都算不上，却留着吃 $5 利息。
    # 而且越接近失败，保留额还被抬得越高、重摇预算砍得越少（方向是反的）。
    # 落后或本关过不了时，下面这些放宽会生效：
    #   * 利息保留额归零（钱不花掉就是浪费）
    #   * 重摇预算改成"花得起就一直摇"
    #   * 星球牌 / 天体包加权（升牌型等级是唯一能跟上 2.08 倍涨幅的手段）
    #   * 换牌门槛归零（只要真实得分有提升就换）
    urgency_planet_bonus: float = 3.0
    urgency_celestial_bonus: float = 3.0

    # ------------------------------------------------- 牌型等级：锁死在一个牌型上
    # 实测 10 局里有 6 张天体牌分散在 5 个不同牌型上（Two Pair +2、Straight +1、
    # Pair +1、High Card +1、Straight Flush +1），单一牌型最高只到 +2 级 —— 而
    # 对子要到**第 3 级**才能让 γ 越过切换点。等级一分散，"专精小牌型"这套
    # 策略就永远启动不了。
    #
    #   commit_lead      —— 等级最高的牌型领先第二名多少级就锁定它
    #   lock_planet_type —— 只买专精牌型的星球牌（不在别的牌型上分兵）
    commit_lead: int = 1
    lock_planet_type: bool = True

    # ------------------------------------------- 牌型条件的"软筛"（仅过渡期）
    #
    # 条件型小丑牌的牌型条件原来是 **0/1 硬筛**：
    #     P̂ = 1  若 contains_hand(专精牌型, 要求牌型)  否则 0
    # 专精 Pair 时"要求 Flush"的牌直接判 0。这在成型之后是对的，但在**过渡期**
    # 不对：那时还没有确定流派，Flush 等级可能只比 Pair 低一两级，Flush 系的牌
    # 是**有过渡价值**的，硬筛会把它整类误判成废牌。
    #
    # 改成按等级加权：估值时不止看专精牌型，也看其它"已经投入过的"牌型，
    # 权重按等级。软筛强度由**等级领先**决定，所以只在过渡期生效：
    #
    #     lead = 最高等级 − 第二高等级
    #     soft = 1 − clamp(lead / soft_gate_lead, 0, 1)
    #
    # 全是 1 级时没有"其它可能流派"，soft 的参与牌型为空 → 等于不生效，
    # 回到原来的硬筛。领先 soft_gate_lead 级以上视为已定型，也回到硬筛。
    #
    #   soft_gate_lead  —— 领先几级算"已定型"（越大越晚收手）
    #   soft_hand_types —— 除专精牌型外，最多再纳入几个候选牌型
    #   soft_level_floor—— 候选牌型的等级门槛（升过级 = 投过星球牌）
    #   soft_played_floor—— 候选牌型的**打出次数**门槛
    #
    # 为什么要 `played` 这条：`lock_planet_type = True` 会把所有星球牌都砸在
    # 专精牌型上（实测一局只用了 1 张星球牌），所以其它牌型**永远到不了
    # level ≥ 2**。只按等级挑候选，软筛就永远不会生效——第一版正是这么写的，
    # 实测 31 次买入 0 次触发。过渡期真正的信号是"这个牌型我们实际打过几次"。
    soft_gate_lead: int = 2
    soft_hand_types: int = 1
    soft_level_floor: int = 2
    soft_played_floor: int = 2

    # ------------------------------------------- 主 build 流派（阶段一：只观测）
    #
    # 见 `balatro_bot/build.py` 和 `docs/strategy-math.md` §16。阶段一只把这些
    # 数字写进决策日志，**不改变任何决策**，用来验证"流派能不能被可靠识别"。
    #
    #   build_lineup_norm   —— （已废弃，识别改用占比，不再需要归一化分母）
    #   build_deck_weight   —— 牌库投资占比的权重 λ
    #   build_min_support   —— 首次承诺的最低支持度
    #   build_switch_margin —— 迟滞：新方向要高出这么多才换掉现有计划
    #   build_switch_floor  —— 现有计划暂时不可见时，新方向自己要有的成色
    build_deck_weight: float = 0.5
    build_min_support: float = 0.15
    build_switch_margin: float = 0.25
    build_switch_floor: float = 0.5
    # 阶段二：把流派接进决策
    #   build_card_bonus —— 商店里"属于主流派核心"的小丑牌加多少分
    #   build_target_bonus —— 塔罗改造目标里，流派偏好的牌排到多前面（相对名次）
    build_card_bonus: float = 1.5

    # ------------------------------------------------- 危机度的估法（潜力 vs 现状）
    #
    # 原来 `crisis = 需要 / (score_now × 剩余手数)`，而 `score_now` 是
    # `score_capability`——"**现在这 8 张牌**能打多少"。开局那 8 张是随机的，
    # 所以它系统性偏低、crisis 偏高：实测底注 1 第一手就判成危急 1.7x，
    # 于是门槛归零、开始赌 9% 的 Straight Flush，越赌越危急
    # （`危急=1.1x → 1.9x`，最后差 10 分输掉大盲）。
    #
    # 打开后改用**潜力**：这手牌在剩余弃牌数内真正能打到的水平，
    # 用同一套弃牌计划算 `max over plans [P × 凑成分 + (1−P) × 兜底分]`。
    #
    # 关掉（`--set crisis_uses_potential=False`）即回到原行为，便于配对测量。
    crisis_uses_potential: bool = True

    # ---------------------------------------------------- 牌位与复利（①②③）
    #
    # 背景（数据见 `tools/mult_diagnosis.py` / `tools/deck_channel.py`）：
    #   100 局里 620 次小丑牌买入，其中 70.6% 是估值 ≤6.5 的廉价加法填充牌；
    #   换牌 363 次 = 3.6 次/局；局终 54% 的局一张 ×Mult 都没有。
    #   补充包只买 Arcana/Buffoon/Spectral，**Standard 0 次**；
    #   每局最多改造出 0.14 张 STEEL、0.08 张 GLASS。
    #
    #   ① 复利牌不当换牌牺牲品（它按"当前边际贡献"必然被判成最差）
    compound_switch_guard: bool = True
    #   ② 廉价填充牌的估值随底注递减（把牌位留给复利牌）
    #      penalty = filler_penalty_per_ante × max(0, 底注 − filler_free_antes)
    filler_value_ceiling: float = 7.0
    filler_free_antes: int = 2
    filler_penalty_per_ante: float = 0.5
    #   ③ 牌库改造是第二条乘法通道（玻璃 ×2 / 钢铁 ×1.5，和 ×Mult 相乘）
    #      Standard 包原来估值 4.0 < pack_threshold 5.0 → **永远买不到**。
    #
    #      **但实测这个渠道是亏的**（10 局配对，`tools/abc_report.py`）：
    #        打开后每局买 3.4 个包（≈$15/局），改造张数 1.9 → 2.4/局（只多 0.5），
    #        而平均底注从 5.10 掉到 4.70、逐局 更好3/相同3/更差4。
    #      原因很实在：标准包里**大多是普通牌**，每次只能挑一张，命中率太低。
    #      所以默认 **关掉**（0.0）。③ 真正该走的是塔罗（Justice/Chariot），
    #      不是标准包。参数留着，以后要试可以再开。
    #      ③b（标准包优先挑强化牌）保留——它不花钱，只在真拿到包时起作用。
    standard_pack_bonus: float = 0.0
    enhanced_target: int = 4              # 想攒到几张强化牌

    # ------------------------------------------- 天体包：按需估值（不再用常数）
    # 原来天体包是一个拍脑袋的常数 4.5（低于 5.0 门槛，平时根本不买），
    # 紧急时再 +3.0。实测 30 局买了 75 个天体包、花了 $874，只升了 8 级 ——
    # 因为 12 种星球牌里只有 1 张是我们要的，普通包（展示 3 张）命中率只有 25%。
    #
    # 现在改成算期望：P(命中) × 升一级的得分增幅 ÷ 价格。
    #   celestial_pool_size   —— 星球牌池子大小（基础 12；未解锁的隐藏牌型
    #                            是否占位需要实测校准）
    #   pack_gain_per_dollar  —— 每 $1 至少要买到"当前得分能力的百分之几"
    #   allow_spare_planet_hand —— 允许"主 + 备"两个牌型吃星球牌。备选必须是
    #                             **已经在投入的**（等级 > 1），且不影响 γ。
    celestial_pool_size: int = 12
    pack_gain_per_dollar: float = 0.010
    allow_spare_planet_hand: bool = True
    # 紧急时的补充包门槛**降低比例**（0.25 = 紧急时只要平时四分之一的增益就买）。
    #
    # 一开始紧急是"无条件买"，结果按需估值完全失效：实测把门槛从 1% 提到 2%，
    # 买包数量反而从 190 涨到 200——因为买包几乎都发生在紧急状态，估值根本没
    # 参与决策。改成"降门槛"之后，紧急仍然显著多买，但估值还能区分好坏包
    # （例如紧急时买巨型包、但仍然拒绝超级包）。
    urgent_pack_relief: float = 0.25

    # ------------------------------------------- 星球牌供给类优惠券
    # 这两张不是在"提高单包效率"，而是在**提高星球牌的供给**——这才是瓶颈：
    # 实测 30 局对子平均只升 1.2~1.4 级，而第 3 级才能让 γ 越过切换点。
    #
    #   Telescope        天体包**第一张保底**是"打得最多"牌型的星球牌
    #                    （card.lua:1737）→ 普通包命中率 25% → 100%，即 4 倍
    #   Planet Merchant  商店 planet_rate 4 → 9.6（card.lua:1895），即 2.4 倍
    #   Planet Tycoon    商店 planet_rate 4 → 32，即 8 倍
    #
    # 估值 = 基准值 × 供给倍数（封顶 `voucher_multiplier_cap` 倍，
    # 免得 ×8 那档把整个评分尺度顶爆）。
    planet_rate_base: float = 4.0
    planet_rate_merchant: float = 9.6
    planet_rate_tycoon: float = 32.0
    voucher_multiplier_cap: float = 3.0
    # 经济充足时主动花钱提升倍率（而不是一直攒利息吃那 $5）：
    #   surplus_money      —— 钱达到多少算"经济充足"
    #   surplus_buy_relief —— 购买门槛降低多少（买更多"边缘够格"的牌）
    #   surplus_rerolls    —— 额外允许多少次重摇（多刷几张牌）
    #   surplus_mult_bonus —— 给"能提升倍率"的牌额外加权多少
    surplus_money: int = 25
    surplus_buy_relief: float = 1.5
    surplus_rerolls: int = 2
    surplus_mult_bonus: float = 1.5

    # ---------------------------------------------------------- 牌型专精
    # 超过这个等级的大牌型会覆盖"小牌型优先"的默认目标
    keep_invested_level: int = 3
    # 只在得分差距不超过这个比例时才为了专精换牌型
    prefer_hand_tolerance: float = 0.12

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TunableParams":
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"参数文件里有不认识的字段：{unknown}")
        return cls(**data)

    def save(self, path: str | Path = DEFAULT_PARAMS_PATH) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PARAMS_PATH) -> "TunableParams":
        source = Path(path)
        if not source.exists():
            return cls()
        return cls.from_dict(json.loads(source.read_text(encoding="utf-8")))

    def replace(self, **changes) -> "TunableParams":
        data = self.to_dict()
        data.update(changes)
        return TunableParams.from_dict(data)


def _auto_aversion() -> float:
    """自动生成弃牌门槛的初始值（结果缓存）。

    推导要跑 120 副合成手牌，约 400 ms。它是**确定性**的（固定随机种子），
    所以缓存起来——否则每构造一次 Config() 就重跑一遍：
    测试套件里有几百处 Config()，会白等近一分钟；游戏开局也要多等 0.8 秒。
    """
    if "aversion" not in _DERIVED:
        try:
            from .planning import derive_initial_discard_aversion

            _DERIVED["aversion"] = derive_initial_discard_aversion()
        except Exception:  # pragma: no cover - 推导失败时退回一个保守值
            _DERIVED["aversion"] = 0.25
    return _DERIVED["aversion"]


def _auto_discard_bonus() -> float:
    """自动生成弃牌数量系数的初始值（结果缓存，理由同上）。"""
    if "bonus" not in _DERIVED:
        try:
            from .planning import derive_initial_discard_bonus

            _DERIVED["bonus"] = derive_initial_discard_bonus()
        except Exception:  # pragma: no cover
            _DERIVED["bonus"] = 0.1
    return _DERIVED["bonus"]


# 自动推导结果的缓存。推导是确定性的，进程内只需算一次。
_DERIVED: dict[str, float] = {}
