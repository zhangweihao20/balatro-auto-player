# BalatroLLM Rulebook Extraction — for a deterministic (non-LLM) Balatro bot

**Sources read in full** (`D:\balatrollm\src\balatrollm\strategies\`):
`default\STRATEGY.md.jinja` (730 lines), `conservative\STRATEGY.md.jinja` (720 lines), `aggressive\STRATEGY.md.jinja` (345 lines), `default\GAMESTATE.md.jinja` (335 lines, **byte-identical to aggressive\GAMESTATE.md.jinja**), `conservative\GAMESTATE.md.jinja` (341 lines), `default\MEMORY.md.jinja` (34 lines), `conservative\MEMORY.md.jinja` (46 lines); framing from `D:\balatrollm\docs\strategies.md`.
Also consulted (to pin down exact action semantics, not rules): `default\TOOLS.json`, the three `manifest.json`, and `src\balatrollm\bot.py` (the "always select, never skip" policy).

**Notation used below:** `<d>` = default guide, `<c>` = conservative guide, `<a>` = aggressive guide. Everything is quoted/derived from those files. Anything I add from outside knowledge is explicitly tagged **[outside knowledge]**.

> ⚠️ **Read Section F first if you only read one section.** Three of the default guide's worked scoring examples are arithmetically wrong, the conservative guide's "correct joker order" worked example is *inverted*, and one boss blind appears under two different names. An implementer who trusts the prose over the arithmetic will ship a wrong scorer.

---

## A. Core scoring rules

### A.1 The formula (all three guides agree on the shape)

```
Score = (Base Chips + Cards Value + Chip Additions) × (Base Multiplier + Mult Additions) × Mult Multipliers
```
quoted verbatim in `<d>` L103 and `<a>` L71. `<c>` L120 writes the identical thing as
`Final Score = (Base Chips + Bonus Chips) × (Base Mult + Bonus Mult) × (xMult factors)`.

Terms, as defined in `<d>` L105–110:
- **Base Chips** — "the base chips associated with the hand type."
- **Cards Value** — "the sum of the ranks of the cards that are used in the hand type. **Ace is worth 11 and face cards are worth 10**."
- **Chip Additions** — "originates from jokers and cards enhancements."
- **Base Multiplier** — base mult of the hand type.
- **Mult Additions** — jokers + enhanced cards.
- **Mult Multipliers** — jokers + enhanced cards.

**Critically: there is no separate final `× Mult` step.** The whole thing is `(chips) × (mult)` where "mult" is the accumulated number, and ×Mult factors multiply *that mult value*. Every correct worked example in `<d>` confirms this (e.g. `(14 + 20 + 50) × (2 + 10) = 84 × 12 = 1008`).

### A.2 Chip value of a card (`<c>` L131–137, "Card Chip Values")

| Rank | Chips |
|---|---|
| 2–10 | face value |
| J, Q, K | 10 |
| A | 11 |

### A.3 Which cards score — the part most likely to be implemented wrongly

- `<d>` L122: "Generally, **only the cards relevant to the hand are scored**. All others are not scored so there is **not concept of a kicker** like in poker. For example, if an Ace is played as a High Card with 4 other cards, only the High Card base amount and the Ace's values are used for the hand's score. The other cards (up to 4) are just discarded and have no effect. However, sometimes it can be beneficial to play extra cards as an extra discard while still having the main hand score."
- `<d>` L155 (Straight): "Five cards in a row (consecutive ranks)." — **all five rank in the example**.
- `<d>` L147–152 (Flush) says "Five cards that share the same suit" and its example sums **all five** cards (`11+10+10+5+4 = 40`) — **but that example's total is only consistent if one card does NOT score** (see F.3). The Flush example is internally contradictory.
- `<d>` L140–145 (Full House) description does not say which cards score, and its example sums **all five** (`10+10+10+2+2 = 34`, total printed as 296, correct total 292 — see F.2).
- `<d>` L128–131 (Straight Flush) sums all five.
- `<c>` L157 does not state the rule; `<c>` L415 ("Stone … +50 Chips, no rank/suit") and `<d>` L475 ("**Enhanced card always scores, except when debuffed**") are the only explicit per-card scoring statements.
- **Debuffed cards**: `<d>` L29 — "The debuffed cards, appearing with a red cross, **can still make Poker Hands but won't be scored**." Also `<d>` GAMESTATE L117: "this card is debuffed and will not be counted in scoring."

> **Not covered:** the docs never state which specific cards score for Pair / Two Pair / Three of a Kind / Four of a Kind / Full House beyond their "they may be played with N other unscored cards" descriptions. Implementer must assume the standard rule **[outside knowledge]**: for Pair/Two Pair/Trips/Quads, only the cards forming the rank groups score; for Full House, all five (the trips + the pair) score. The conservative level-up table's Royal Flush row (`<c>` L152) implies only the same 5 cards score in a Royal Flush.

### A.4 Hand types and base values — **the two guides disagree on High Card**

`<d>` L124–187 ("There are 10 hand types (weakest to strongest)") gives, in the order printed:

| # (as listed) | Hand | Base Chips | Base Mult | `<d>` line |
|---|---|---|---|---|
| 1 | Straight Flush | 100 | 8 | 129–130 |
| 2 | Four of a Kind | 60 | 7 | 136–137 |
| 3 | Full House | 40 | 4 | 143–144 |
| 4 | Flush | 35 | 4 | 150–151 |
| 5 | Straight | 30 | 4 | 157–158 |
| 6 | Three of a Kind | 30 | 3 | 165–166 |
| 7 | Two Pair | 20 | 2 | 171–172 |
| 8 | Pair | 10 | 2 | 178–179 |
| 9 | High Card | **4** | 1 | 185–186 |

(`<d>`'s list is ordered *strongest first*, despite saying "weakest to strongest".) `<a>` L86–94 gives the same 9 hands, **same values except High Card = 5 Chips, 1 Mult**, and is also printed strongest-first. `<c>` L141–155 (stated weakest-first in the table, and it is) is the only source with complete values and the secret hands:

| Hand | Base Chips | Base Mult | Level-up bonus (chips, mult) |
|---|---|---|---|
| High Card | **5** | 1 | +10, +1 |
| Pair | 10 | 2 | +15, +1 |
| Two Pair | 20 | 2 | +20, +1 |
| Three of a Kind | 30 | 3 | +20, +2 |
| Straight | 30 | 4 | +30, +3 |
| Flush | 35 | 4 | +15, +2 |
| Full House | 40 | 4 | +25, +2 |
| Four of a Kind | 60 | 7 | +30, +3 |
| Straight Flush | 100 | 8 | +40, +4 |
| Royal Flush | 100 | 8 | +40, +4 |
| Five of a Kind | 120 | 12 | +35, +3 |
| Flush House | 140 | 14 | +40, +4 |
| Flush Five | 160 | 16 | +40, +3 |

**Rule:** `<d>` L189 — "The Base Chips and Base Multiplier for the described hand types refers to **level 1** hand types." `<d>` L120 — "At the beginning of a run, all Hands start at Level 1, and are generally upgraded through Planet Cards."

### A.5 Level-up increments (Planet cards) — `<d>` L375–383, 9 planets

| Planet | Hand | Increment (verbatim) |
|---|---|---|
| Pluto | High Card | "+1 Mult and +10 Chips" |
| Mercury | Pair | "+1 Mult and +15 Chips" |
| Uranus | Two Pair | "+1 Mult and +20 Chips" |
| Venus | Three of a Kind | "+2 Mult and +20 Chips" |
| Saturn | Straight | "+3 Mult and +30 Chips" |
| Jupiter | Flush | "+2 Mult and +15 Chips" |
| Earth | Full House | "+2 Mult and +25 Chips" |
| Mars | Four of a Kind | "+3 Mult and +30 Chips" |
| Neptune | Straight Flush | "+4 Mult and +40 Chips" |

These match `<c>`'s Level Up Bonus column **exactly, for all nine**. `<c>` additionally supplies Five of a Kind / Flush House / Flush Five increments (table above) and a Royal Flush row. `<a>` L146–151 lists only the 9 planet→hand mappings, no numbers.
Other level effects: **Black Hole** — "Upgrade every poker hand by 1 level" (`<d>` L408). **The Arm** boss — "Decrease level of played poker hand by 1 (hand levels can go as low as **Level 1**, and are **reduced before scoring**)" (`<d>` L48).

### A.6 Order of operations (verbatim, `<d>` L112–114)

> "When playing a hand, effects activate in a specific sequence: **Boss Blind effects first, then Jokers and card effects trigger as cards are scored from left to right**. When multiple Jokers trigger from the same condition, they **always activate from left to right based on their position in your Joker slots**. This sequence matters for strategy - placing additive effects (+Mult, +Chips) to the left of multiplicative effects (×Mult) maximizes your score, since additions happen before multiplications."

`<c>` L123–129 phases it as: 1 Calculate Base → 2 Add Card Chips → 3 Apply Card Enhancements → 4 Apply Jokers Left-to-Right: +Chips, then +Mult, then xMult → 5 Apply Editions (Foil +50 Chips, Holographic +10 Mult, Polychrome x1.5 Mult). `<a>` L82: "Effects activate left to right… **Place additive effects (+Mult, +Chips) LEFT of multiplicative effects (xMult)**."

### A.7 The scoring-order arithmetic the docs actually demonstrate

`<d>` L508–515 — the canonical example (this one is **correct**):
> "Consider a hand scoring 50 Chips × 4 Mult with two Jokers: Joker A: +10 Mult, Joker B: ×2 Mult. If ordered A then B: `50 × (4 + 10) × 2 = 50 × 28 = 1,400`. If ordered B then A: `50 × (4 × 2 + 10) = 50 × 18 = 900`."

So `×Mult` jokers multiply the **running mult**, and a later `+Mult` adds to the already-multiplied value. Also `<d>` L516: "The same applies to scoring cards - **place cards with multiplicative effects (Glass, Polychrome) rightmost in your played hand**."

### A.8 Hand-size / play-size limits

- `<d>` L9 / `<a>` L9: initial hand of **8 cards**; "Under normal circumstances, you can play **up to 5 cards at once**"; discards also up to 5 cards.
- `<d>` L15: "Each round provides **4 hands and 3 discards** as the baseline… Players draw **8 cards** at the start of each round."
- `<d>` GAMESTATE L98–99: `hand.count / hand.limit`, and "**CRITICAL LIMIT: You can play or discard MAXIMUM {{ G.hand.highlighted_limit }} cards per action**"; TOOLS.json: "0-based indices of cards to play (**1-5 cards**)" — minimum 1 card, maximum 5 (modifiable by vouchers/hand modifiers).
- Draw-back rule: `<d>` L530 — "**You ALWAYS draw back the number of cards you played**"; `<d>` L9 — after a discard you draw the same number of cards you discarded, added to your current hand.

### A.9 Blind requirements / ante targets

`<d>` L15: "The first Ante requires **300 chips for the Small Blind, 450 for the Big Blind, and 600 for the Boss Blind**." `<c>` L59–68 gives the full table (base / SB / BB / Boss):

| Ante | Base | Small | Big | Boss |
|---|---|---|---|---|
| 1 | 300 | 300 | 450 | 600 |
| 2 | 800 | 800 | 1,200 | 1,600 |
| 3 | 2,800 | 2,800 | 4,200 | 5,600 |
| 4 | 6,000 | 6,000 | 9,000 | 12,000 |
| 5 | 11,000 | 11,000 | 16,500 | 22,000 |
| 6 | 20,000 | 20,000 | 30,000 | 40,000 |
| 7 | 35,000 | 35,000 | 52,500 | 70,000 |
| 8 | 50,000 | 50,000 | 75,000 | 100,000 |

Multipliers: Big = 1.5× base, Boss = 2× base (`<c>` L52–55 prints only "Base payout / 1.5x payout / 2x payout" for the *money* reward column; the score multipliers above are inferred from the table). `<d>` L23: "You start at Ante 1 and go up an Ante level for every Boss Blind defeated. By completing Ante 8 and finishing the Showdown Blind, the run is considered 'won'." Defeat condition: `<d>` L19 — "the player has to **meet or surpass** the required score"; scores of played hands **sum** into the round score (`<d>` L9).

### A.10 Modifier values that feed scoring

**Editions** (`<d>` L488–496, `<c>` L397–403, `<a>` L114–119): Base = nothing; **Foil +50 Chips when scored**; **Holographic +10 Mult when scored**; **Polychrome ×1.5 Mult when scored**; **Negative +1 Joker Slot** (Jokers only).
**Enhancements** (`<d>` L466–477): **Bonus +30 Chips when scored**; **Mult +4 Mult when scored**; **Wild** = considered every suit simultaneously; **Glass ×2 Mult when scored, 1 in 4 chance to be destroyed after all scoring is finished**; **Steel ×1.5 Mult *while held in hand***; **Stone value set to +50 Chips, no rank or suit, always scores except when debuffed**; **Gold $3 if held in hand at end of round**; **Lucky 1 in 5 chance +20 Mult and 1 in 15 chance $20**.
**Seals** (`<d>` L479–486): **Gold** — "$3 when this card is played **and scores**"; **Red** — "Retrigger this card 1 time. Retriggering means that the effect of the cards is applied again **including counting again in the score calculation**"; **Blue** — creates the Planet Card for the final played poker hand of the round if **held in hand** (needs room); **Purple** — creates a Tarot **when discarded** (needs room).
Debuff interaction (`<d>` L464): "Debuffed cards and Jokers from Boss Blinds have **all modifier effects disabled except for Negative (+1 slot) and Stone (no rank and suit)**. Debuffed wild cards **revert back into their original suit**."
Joker rarity appearance rates (`<d>` L246): "Common – **70%**, Uncommon – **25%**, Rare – **5%**, Legendary - only a **0.3%**."

---

## B. Economy rules

### B.1 Exact numbers

| Item | Value | Source |
|---|---|---|
| Starting money | **$4** | `<d>` L15, `<c>` L39, `<a>` L15 |
| Starting deck | standard **52-card** (13 ranks × 4 suits) | all three |
| Starting Joker slots | **5** empty | all three |
| Starting consumable slots | **2** | all three |
| Hands / discards per round (baseline) | **4 hands, 3 discards** | `<d>` L15, `<c>` L41 |
| Draw at round start | **8 cards** | `<d>` L15 |
| Interest | "**$1 interest per $5 saved (maximum $5 interest at $25+)**" | `<d>` L15, `<a>` L15, `<c>` L184 |
| Interest formula as implemented in the template | `min(money // 5, 5)` | `<c>` GAMESTATE L18 |
| Money per unspent hand | "**$1 per unspent hand after defeating a blind**" | `<d>` L15, `<d>` L615 |
| Round base payout | "**Small ($3), Big ($4), Boss ($5) base payouts**" | `<c>` L185 |
| Shop reroll cost | "starts at **$5** and increases by **$1** each reroll, **resetting to $5 upon entering a new shop**" | `<d>` L205; `<a>` L100 |
| Sell value | "**usually half of its buy cost**" | `<d>` L201; `<d>` L596 "Everything sells for HALF its purchase price" |
| Shop default stock | **2 random cards** (usually Jokers, occasionally Tarots/Planets) + **1 Voucher** + **2 Booster Packs** | `<d>` L195–200, `<a>` L98 |
| Booster packs per shop | `<c>` L168 says "**1-2 booster packs**" (conflicts with `<d>`'s "2") | `<c>` L168 |
| Consumables generated elsewhere | "appear in the consumable slots, **unless no more space is available**, and any attempt to generate or buy more consumables is **disabled**" | `<d>` L334 |

**Interest cap escalation (vouchers):** Seed Money raises the cap to **$10** (`<d>` L438); Money Tree raises it to **$20** (`<d>` L439).
⚠️ **`<c>` disagrees**: "<c>| Seed Money | Interest cap to **$50**" and "<c>| Money Tree | Interest cap to **$100**" (`<c>` L370–371), and `<c>` L34 says "interest caps at **$25** balance". `<d>` is presumably right for the real game; the conservative guide's cap numbers are wrong relative to `<d>`.

### B.2 Every money source / sink stated in the docs

**Gains**
- $1 per unspent hand at cash-out (`<d>` L15).
- Interest `min(money // 5, 5)` at cash-out (`<d>` L15, `<c>` GAMESTATE L18).
- Blind base payouts Small $3 / Big $4 / Boss $5 (`<c>` L185).
- **Gold Card** enhancement: $3 if held in hand at end of round (`<d>` L476).
- **Gold Seal**: "$3 when this card is played and scores" (`<d>` L483) — `<a>` L175 and `<c>` L427 simplify to "$3 when played".
- **Egg Joker**: "Gains **$3 of sell value** at end of round" — sell value, not cash (`<d>` L302).
- **The Hermit** (Tarot): "Doubles money (**Max of $20**) (**Min of $0**)" (`<d>` L354); `<c>` L316 "Double money (max $20)".
- **Temperance** (Tarot): "Gives the total sell value of all current Jokers (**Max of $50**)" (`<d>` L359).
- **Immolate** (Spectral): "Destroys 5 random cards in hand, **gain $20**" (`<d>` L400; `<a>` L245 "Use to thin deck and get shop money").
- **Investment Tag**: "+$25 after beating boss" (`<c>` L601); `<a>` L256 "Double money" (**disagrees** — double vs +$25).
- **Lucky Card**: 1 in 15 chance to give **$20** (`<d>` L477).

**Losses / zeroing**
- **The Tooth** boss: "**Lose $1 per card played**" (`<d>` L63).
- **The Ox** boss: "Playing your most played hand this run **sets money to $0**" (`<d>` L44).
- **Wraith** (Spectral): "Creates a random Rare Joker, **sets money to $0**" (`<d>` L396); `<a>` L242: "Use in Antes 1-3. The Rare Joker is worth more than money."
- **Rental** joker modifier: "costs **$1 per round**" (`<d>` GAMESTATE L45–46).
- **Director's Cut** voucher: "Reroll Boss Blind 1 time per Ante, **$10 per roll**" (`<d>` L446); Retcon = unlimited, $10 per roll (`<d>` L447). `<c>` L390–391 calls Retcon "Reroll Boss free" (**disagrees**).

### B.3 Economy vouchers (exact wording, `<d>` L418–449)

| Voucher | Effect |
|---|---|
| Overstock | +1 card slot available in shop (to 3 slots) |
| Overstock Plus | +1 card slot available in shop (to 4 slots) |
| Clearance Sale | **All cards and packs in shop are 25% off** |
| Liquidation | **All cards and packs in shop are 50% off** |
| Hone | Foil/Holographic/Polychrome cards appear **2X** more often |
| Glow Up | Foil/Holographic/Polychrome cards appear **4X** more often |
| Reroll Surplus | **Rerolls cost $2 less** |
| Reroll Glut | **Rerolls cost an additional $2 less** |
| Crystal Ball | +1 consumable slot |
| Omen Globe | Spectral cards may appear in any of the Arcana Packs |
| Telescope | Celestial Packs always contain the Planet Card for your **most played poker hand** |
| Observatory | **Planet Cards in your consumable area give ×1.5 Mult for their specified poker hand** |
| Grabber / Nacho Tong | +1 hand per round / an additional +1 hand per round (permanent) |
| Wasteful / Recyclomancy | +1 discard each round / an additional +1 discard each round |
| Tarot Merchant / Tarot Tycoon | Tarots appear 2X / 4X more frequently in shop |
| Planet Merchant / Planet Tycoon | Planets appear 2X / 4X more frequently in shop |
| Seed Money / Money Tree | interest cap to $10 / $20 |
| Blank | "Does nothing?" |
| Antimatter | **+1 Joker slot** |
| Magic Trick | Playing cards can be purchased from the shop |
| Illusion | Playing cards in shop may have an Enhancement, Edition, and/or a Seal |
| Hieroglyph | **-1 Ante, -1 hand each round** |
| Petroglyph | -1 Ante again, -1 discard each round |
| Director's Cut / Retcon | Boss reroll 1×/Ante at $10 per roll / unlimited at $10 per roll |
| Paint Brush / Palette | +1 hand size / +1 hand size again |

⚠️ `<c>` L372–392 re-describes several of these incorrectly: "Blank | **+1 joker slot**", "Liquidation | **+1 shop slot**", "Omen Globe | **+1 consumable slot**, Spectral in Arcana", "Reroll Glut | **Rerolls free**", "Liquidation Sale | All items 50% off" (there is no "Liquidation Sale"). These mis-descriptions are the conservative guide's shorthand, contradicting `<d>`.

### B.4 Hold vs spend — the explicit thresholds found

- `<d>` L613–616: "**Reach $25 as quickly as possible to earn maximum $5 interest per round**… Don't hoard excessively - **spending on key Jokers/upgrades often provides better returns than interest**." `<d>` L716: "**Hoarding Money**: Interest caps at $5 - spending on improvements often yields better returns."
- `<c>` L9/L537–539: "**Maintain $25+ for maximum interest at all times after Ante 2**"; "After Ante 2, **never drop below $25** unless you will literally lose otherwise."
- `<c>` L551–556 — break the $25 rule only if: "(1) You will lose the current blind without the purchase; (2) A game-changing joker/voucher appears AND you're in **Ante 6+**; (3) You need to reroll for a specific piece to survive."
- `<c>` L449: early game buy "S-tier or A-tier joker you can afford **while maintaining $5 minimum for interest**".
- `<c>` L547–549: "$25 for 8 rounds = **$40** interest earned; $20 for 8 rounds = **$32**; Difference = $8 = almost 2 free jokers."
- `<a>` L279–283: Antes 1–3 "**Spend freely** on Jokers and rerolls. Finding the right Joker matters more than interest"; Antes 4–6 "Build toward $25… **A powerful Joker is worth breaking your interest threshold**"; Antes 7–8 "**Spend everything** on final optimizations. Money doesn't matter if you can't beat the final boss."
- `<c>` L603: Coupon Tag → "Free items in shop"; D6 Tag → "Free rerolls".

---

## C. Shop and build strategy

### C.1 Purchase priority orders (each guide states one; they differ)

`<c>` L558–568 — "**Shop Spending Priority**: When you have money above $25: 1. Jokers that upgrade your build (if slot available) 2. Celestial packs (for hand leveling) 3. Vouchers (**Grabber, Wasteful, Blank first**) 4. Arcana packs 5. Buffoon packs 6. Single cards (if excellent for build) 7. **Rerolls (only with $30+)**."
`<c>` L180 — "**Conservative Pack Priority: Celestial > Arcana > Buffoon > Standard >>> Spectral**."
`<d>` L225–236 — packs: "**Buffoon Packs**: Buy when you have joker slots available and need synergy. **Arcana Packs**: Good for card enhancements early game. **Celestial Packs**: Buy when you have a dominant hand type to level up. **Standard Packs**: Useful for building specific suits/ranks. **Spectral Packs**: High risk/reward…". Also `<d>` L233–236: "For Arcana/Spectral packs, cards are used **immediately**… **Buffoon/Standard packs add cards to your collection (not used immediately)**."
Pack contents as stated by `<c>` L172–179: Arcana = 3 Tarots (choose 1); Celestial = 3 Planets (choose 1); Standard = 3 Playing cards (choose 1); **Buffoon = 2 Jokers (choose 1)**; **Spectral = 2 Spectrals (choose 1)**.

### C.2 Reroll policy

- `<d>` L205 + `<a>` L100: $5 start, +$1 per reroll, resets to $5 each shop. Reroll replaces the **2 random cards** only — "**rerolling the shop does not restock Booster Packs or the Voucher**" (`<d>` L207). Packs restock only in a new shop; "the Voucher restocks after defeating the boss blind" (`<d>` L207; `<d>` L412 "refresh or restock in the shop after the players first completed round, or after defeating the boss blind of each ante").
- `<a>` L190–203 reroll budget: "**Ante 1: Spend up to $10-15 on rerolls if needed. Ante 2: Spend up to $8-10 on rerolls. Later Antes: Only reroll if seeking specific synergy pieces.**"
- `<c>` L18/L447: "**Never reroll** - save every dollar for interest" (Antes 1–3); L467 "**Reroll only with $30+** - never drop below interest cap" (Antes 4–6); L487 "**Reroll for key pieces**" (Antes 7–8).
- Reroll synergy: **Flash Card** Joker "gains **+2 Mult per reroll** in the shop" (`<d>` L321–324); **Reroll Surplus** −$2, **Reroll Glut** −$2 more, **D6 Tag** "Free rerolls" (`<c>` L604).

### C.3 Sell policy (the docs take opposite stances)

- `<d>` L594: "**CRITICAL RULE: NEVER SELL JOKERS UNLESS ABSOLUTELY NECESSARY**." Math given: buy $8 → sell $4 → lost $4 ("= 4 unspent hands worth of money = almost a full round of interest"); "Do this 3 times and you've thrown away $9 - nearly two full rounds of maximum interest." `<d>` L603–607 — only sell when: "A Joker directly conflicts with your strategy…; Egg/Gift Card has grown massive sell value; You need exactly $X for a game-winning Legendary/crucial upgrade."
- `<c>` L16/L685: "**Never sell jokers**… Only sell if literally required to survive."
- `<a>` L287–298 **does sell**: "Sell when: A Joker no longer fits your committed strategy; You need exactly $X for a game-changing purchase; You're replacing a +Mult with an xMult; A scaling Joker has stopped scaling (capped effects). Don't sell: Scaling Jokers that haven't peaked; Your only xMult Joker; **Negative edition Jokers (slot is valuable)**."
- Selling is also a *mechanic trigger*: **Verdant Leaf** Showdown requires "1 Joker sold" to un-debuff all cards (`<d>` L70) — which `<d>`'s "never sell" rule makes a trap. (The named counters `Luchador`, `Diet Cola`, `Invisible Joker` are listed as D-tier "requires selling" in `<c>` L277–280.)

### C.4 Joker tier guidance (only `<c>` gives a full tier list) — `<c>` L190–281

**S-Tier (Always Buy)** and the effect text `<c>` assigns:
`Joker` +4 Mult (unconditional) · `Greedy/Lusty/Wrathful/Gluttonous` +3 Mult per Diamond/Heart/Spade/Club scored · `Jolly` +8 Mult if hand contains Pair · `Zany` +12 Mult if hand contains Three of a Kind · `Mad` +10 Mult if hand contains Two Pair · `Crazy` +12 Mult if hand contains Straight · `Droll` +10 Mult if hand contains Flush · `Half Joker` +20 Mult if played hand ≤3 cards · `Banner` +30 Chips **per discard remaining** · `Mystic Summit` +15 Mult when 0 discards left · `Loyalty Card` ×4 Mult every 6 hands · `Ice Cream` +100 Chips (decreases over time; "sell before empty") · `Popcorn` +20 Mult (decreases per round; "sell before empty").

**A-Tier (Usually Buy)**: `Sly` +50 Chips if Pair · `Wily` +100 Chips if Three of a Kind · `Clever` +80 Chips if Two Pair · `Devious` +100 Chips if Straight · `Crafty` +80 Chips if Flush · `Fibonacci` +8 Mult for each Ace/2/3/5/8 · `Scary Face` +30 Chips per face card · `Even Steven` +4 Mult per even-ranked card · `Odd Todd` +31 Chips per odd-ranked card · `Scholar` +20 Chips/+4 Mult per Ace played · `Walkie Talkie` +10 Chips/+4 Mult per 10 or 4 played · `Hack` retrigger 2/3/4/5 · `Supernova` +1 Mult per hand type played this run · `Ride the Bus` +1 Mult per consecutive hand without face · `Green Joker` +1 Mult per hand played, −1 per discard.
(`<d>` L625 names "Good Early Jokers: **Scary Face (+30 Chips for face cards), Smiley Face (+4 Mult for face cards), Odd Todd, Even Steven**".)

**B-Tier (Situational)**: `Blackboard` ×3 Mult if all cards in hand are Spades or Clubs · `Smeared Joker` Hearts≡Diamonds, Clubs≡Spades · `Shortcut` straights may skip 1 rank · `Four Fingers` Flushes/Straights with 4 cards · `Splash` every card counts in scoring · `Raised Fist` adds 2× lowest card rank as Mult · `Photograph` first face card gives ×2 Mult · `Ancient Joker` ×1.5 Mult for each suit (changes each round) · `Ceremonial Dagger` +Mult when blind kills joker to right.

**C-Tier (Avoid)**: `Gros Michel` +15 Mult, 1/6 chance to destroy · `Cavendish` ×3 Mult, 1/1000 chance to destroy · `Lucky Cat` ×0.25 Mult per Lucky trigger · `Oops! All 6s` doubles all probabilities · `Business Card` chance for $2 per face card · `Egg` +3 sell value per round · `Riff-raff` creates 2 Commons when blind selected · `Madness` ×0.5 Mult, destroys random joker on blind select · `Vampire` ×Mult per enhanced card, removes enhancements.

**D-Tier (Never Buy)**: `Throwback` ×Mult per blind skipped ("We never skip blinds") · `Midas Mask` face cards become Gold · `Luchador` sell to disable Boss Blind · `Glass Joker` ×0.75 Mult per Glass destroyed · `Diet Cola` sell for free rerolls · `Invisible Joker` after 2 rounds, sell to duplicate joker · `Campfire` ×Mult, resets when joker sold.

`<a>` L123–134 names its priority categories instead of tiers: **Scaling Jokers (HIGHEST PRIORITY)**: Green Joker, Supernova, Spare Trousers, Fortune Teller, Flash Card. **xMult Jokers (CRITICAL)**: "Examples: The Idol, Photograph, Steel Joker, Glass Joker — **Stack as many as possible**." **Blueprint/Brainstorm**: "Copies other jokers - turns one broken joker into two."
`<d>` L708 names "**The Scaling Machine**: Core: Jokers that permanently grow (**Spare Trousers, Flash Card, Fortune Teller**); Support: Reroll cost reduction (Reroll Surplus voucher)."

### C.5 Target hand types and named archetypes

`<d>` L557–577 "Recommended Hand Types by Difficulty": **Easiest = Flush** ("Requires only matching suits, not specific ranks"; synergies Checkered Deck, Smeared Joker, suit-changing Tarots); **Moderate = Full House & Two Pair** ("Benefits from Pair and Three of a Kind Jokers simultaneously — The Jokers that give Chips and Mult when a hand contains a Pair or a Three of a Kind also work with larger hands"); **Difficult = Straight** ("Straights scale very well… Benefits from Four Fingers (4-card straights) or Shortcut").
`<d>` L555: "**Typically it's best to commit to one kind of hand, and build your whole strategy around that. Spreading resources across multiple hand types dilutes your scoring potential.**"
`<c>` L157: "Focus on **Flush, Full House, or Four of a Kind**." `<a>` L226–229 ranking: "1. **Flush** 2. **Straight** 3. **Full House/Two Pair**".
**Named archetypes:**
- `<d>` L693–698 **The Baron Build** — Core: "Baron (Kings held in hand give ×1.5 Mult each)"; Enhancement: "Red Seal on Kings for retriggering"; Support: "Mime (retriggers all cards held in hand)"; Deck Goal: "Multiple Kings, thin out other cards."
- `<d>` L700–704 **The Flush Engine** — Core: "Smeared Joker or Checkered Deck"; Support: "Four Fingers (all 4-card hands count as Flushes)"; Scaling: "Any flush-specific Joker (Lusty Joker for Hearts, Greedy for Diamonds)."
- `<d>` L706–710 **The Scaling Machine** — (above).
- `<c>` L610–678 four builds: `Flush Factory` (Droll +10 Mult Flush, Crafty +80 Chips Flush, suit joker, any xMult; support Smeared/Four Fingers/Wild), `Full House Fortress` (Jolly +8, Zany +12, Sly +50 Chips, Wily +100 Chips; support Hack, Fibonacci, Scary Face), `Four of a Kind Foundation` (Zany + Wily + any xMult; support Hanging Chad "retrigger first card", Mime **"retrigger leftmost card"**, Half Joker), `Conservative All-Rounder` (Joker +4 Mult, Banner, Supernova, Mystic Summit).
- `<a>` L213–220 example **correct joker order**: "Position 1: Square Joker (+4 Chips per card in deck); Position 2: Green Joker (+1 Mult per hand played); Position 3: Steel Joker (×1 + 0.2 per Steel Card); Position 4: The Idol (×2 if specific card played); Position 5: Blueprint (copies The Idol = another ×2)."

### C.6 Deck manipulation targets

- `<a>` L270: "**Target deck size: 30-40 cards focused on your primary hand type.**"
- `<a>` L262–275 methods: The Hanged Man Tarot, destroying Spectrals, Standard Packs with enhanced cards. Prioritise adding "Cards of your flush suit; Cards that complete straights; Enhanced cards (Glass, Steel, Lucky)".
- `<d>` L632 (Antes 4–6): "Start removing low-value cards and enhancing key cards." L640 (Antes 7–8): "**Thin deck to essential cards only**"; "Enhancement Focus: Glass Cards (×2 Mult), Steel Cards (×1.5 while held), Gold Cards (money generation)".
- `<d>` L649–652 Tarot priority: "(1) Suit/Rank changes for your primary strategy (The Star/Moon/Sun/World for flushes); (2) Enhancements on frequently scored cards (The Lovers for Wild Card, The Chariot for Steel); (3) Deck thinning via The Hanged Man; (4) Resource generation via The Hermit (doubles money up to $20)."
- `<c>` L320–323 **Avoid** The Hanged Man ("Reduces deck reliability") — the two guides **disagree** on deck thinning.
- `<c>` L325–332: "**Always use Planet cards** — they provide permanent, risk-free upgrades"; priority "1. Planet for your primary hand type 2. Planet for your secondary hand type 3. Any planet (all upgrades help)." `<d>` L656: "**Focused Investment**: Level one hand type exclusively."
- Observatory synergy (`<d>` L657): holding Planets in consumable slots gives passive ×1.5 Mult. Telescope (`<d>` L658): Celestial packs guarantee your most-played hand's planet.

### C.7 Consumable tier lists (only `<c>` is formal)

Sun/Ace → `<c>` L289–323 Tarot S-Tier: The Magician (Lucky "Guaranteed +20 Mult when triggered" — the "guaranteed" claim contradicts `<d>`'s 1-in-5), The Empress (Mult +4), The Hierophant (Bonus +30 Chips), Strength (rank +1 on up to 2 cards, "no downside"), The Chariot (Steel ×1.5 in hand), The Star/Moon/Sun/World (convert up to 3 cards to a suit), Temperance ("Only if desperate"), The Devil (Gold +$3/round). A-Tier: The Fool, The Lovers (Wild, "up to 2 cards" — `<d>` L351 says "1 selected card"), The Wheel of Fortune (1/4 edition), The Tower. B-Tier: The Hermit, The High Priestess, Death. Avoid: The Hanged Man.
`<c>` L338–346 acceptable Spectrals: Familiar, Grim, Incantation, Talisman, Aura, Sigil. **Never use** (L348–358): Immolate, Ankh, Hex, Cryptid, The Soul, "Black Hole | Upgrade every hand by 1 level | **Actually good, but rare**" (self-contradicting entry), Ectoplasm, Ouija. `<a>` L153–158 takes the opposite line ("USE AGGRESSIVELY": Wraith, Hex, Ankh, The Soul, Black Hole) and L247: "**Glass Cards are GOOD despite the break risk** - x2 Mult is enormous. A Glass Card that scores 3 times before breaking provided x8 Mult total."

### C.8 Phase plans with numbers

`<d>` L620–641: **Antes 1–3 Foundation** — "obtaining a solid Chips or Mult joker"; "Economy Goal: **Build toward $25** for interest"; "Flexibility: Keep options open, don't hard-commit". **Antes 4–6 Scaling** — "Transition from flat bonuses to multiplicative effects"; deck manipulation; "Look for ×Mult Jokers that match your strategy"; planet investment into primary hand. **Antes 7–8+ Optimization** — "Critical Jokers: Scaling Jokers that grow over time (**Blueprint copying strong Jokers, Brainstorm**)"; thin deck; Glass/Steel/Gold focus.
`<c>` L436–494: Antes 1–3 goals "**Reach $25 balance by end of Ante 2**; Establish primary hand type (Flush or Pairs/Full House); Acquire 2-3 reliable jokers", rules "Never skip blinds… Minimize shop spending… **Never reroll**", hand strategy "Focus on Pairs initially… Transition to Flush or Full House by Ante 3". Antes 4–6 goals "Maintain $25+… Have 4-5 synergistic jokers… **Level up primary hand type to 4+**". Antes 7–8 goals "**Score 70,000+ per hand consistently**; Have optimized joker order; Counter Boss Blind effectively".
`<c>` L712–718 win summary: "**Economy**: Maintain $25+ from Ante 2 onward; **Jokers**: 4-5 synergistic jokers with xMult; **Hand Type**: **Level 6+ on your primary hand**; **Consistency**: Play the same hand type **90%** of the time; **Adaptation**: Have a backup plan for Boss Blinds."

---

## D. Play-phase heuristics

### D.1 The central play-vs-discard rule: "hidden discard" (playing extra cards)

`<d>` L518–551, verbatim highlights:
- "**KEY INSIGHT: Every hand you play is also a discard opportunity**"
- "Play a Pair (2 cards) = Score the pair AND cycle 2 cards"; "Play a Pair (2 cards) + 2 unscored cards = You score the pair AND cycle 4 cards"; "**You ALWAYS draw back the number of cards you played**".
- Worked example — hand `S_K, D_K, H_3, C_2, C_5, S_7, C_9, C_J`: "Option A: Play `S_K, D_K` → Score the Kings, draw 2 new cards. Option B: Play `S_K, D_K, H_3, S_7` → Score the Kings, **draw 3 new cards**… **Option B is strictly better**… Notice that we are not discarding Clubs as junk cards because we are setting the hand for playing a flush next time." *(The example text says "play Pair + 2 unscored" but Option B lists 4 cards = 2 scored + 2 unscored and says "draw 3"; see F.8.)*
- "**Rule of thumb: If you can score ANY points, play the hand with maximum cards rather than discarding.** The only exception is when you're one card away from a massive hand and need to preserve specific cards."
- `<a>` L302–311 agrees: "**Always play the hand that scores MOST POINTS, even if it uses more resources.**… A Pair + 3 junk cards cycles more cards than just the Pair… Don't hold cards hoping for better hands unless: You're one card away from a MUCH better hand; You have discards remaining."

### D.2 Discard conservation

- `<c>` L702–703: "**Save at least 1 discard for emergencies unless you're on final hand.**"
- `<c>` L676–677: "Use discards sparingly for Banner bonus" (`Banner` = +30 Chips per discard remaining).
- `<c>` L213/L210: `Mystic Summit` +15 Mult at 0 discards; `Green Joker` +1 Mult per hand played, **−1 per discard**.
- `<d>` L543 / `<c>` L626: dig for your hand type with discards; `<c>` L678: "Play consistently, don't rely on specific hands."

### D.3 Which cards to discard / play

- `<d>` L543: don't discard cards that are part of the next hand's suit ("we are not discarding Clubs as junk cards because we are setting the hand for playing a flush next time").
- `<d>` L649–651, `<c>` L623–626: convert the deck toward one suit, "Discard non-suit cards aggressively", keep the primary hand's pieces.
- `<c>` L691: "A Pair that you can hit is better than a Straight Flush you can't."
- `<d>` L718: "**Playing Too Fast**: you can play the same cards but get different points, **re-organize your played hand to max out before committing**." And `<d>` L727: "**Mathematical optimization** - Always calculate whether reordering Jokers or cards improves score."

### D.4 Last hand / low hands

- `<c>` L89: The Needle ("Only one hand allowed") — "Must score in exactly one play."
- `<a>` L321: "**The Needle (1 hand): Your multiplier stack should one-shot it.**"
- `<c>` L703: last hand is the exception to saving a discard.
- `<a>` L337: win by building "**a single, overwhelming scoring turn by Ante 8**".

### D.5 Boss-blind general handling

- `<d>` L685–687: "**Maintain at least two viable hand types by Ante 3. Keep one 'emergency' consumable for Boss Blind adaptation. Consider Boss Blind effects when choosing between similar Jokers.**"
- `<d>` L29–33 mechanics: debuff = "can still make Poker Hands but **won't be scored**"; face-down cards = "A hand played including face-down cards will have its type displayed as '???'; **scoring and other effects will still perform as normal**"; hand-size/type restrictions = "Hands that don't meet the restriction **can still be played, but will not be scored or activate other effects, acting like a discard**."
- `<a>` L314–323: "Keep a secondary hand option **until Ante 3**; After Ante 3, commit fully and accept some Boss Blinds may be harder; Use **Director's Cut** voucher to reroll bad Boss Blinds if available."
- `<c>` L72: "**Always have a backup plan**" and L699–700: "**Check what the Boss Blind does BEFORE committing to a strategy.**"

---

## E. Boss blinds and tags

### E.1 Boss Blind table (verbatim effects from `<d>` L43–65; `<a>` L30–52 is equivalent minus "hand levels can go as low as Level 1, and are reduced before scoring" and "changes every hand")

| Boss | Effect (verbatim) |
|---|---|
| The Hook | "Discards 2 random cards held in hand after every played hand" |
| The Ox | "Playing your most played hand this run sets money to $0" |
| The House | "First hand is drawn face down" |
| The Wall | "Extra large blind" |
| The Wheel | "1 in 7 cards get drawn face down during the round" |
| The Arm | "Decrease level of played poker hand by 1 (hand levels can go as low as Level 1, and are reduced before scoring)" |
| The Club | "All Club cards are debuffed" |
| The Fish | "Cards drawn face down after each hand played" |
| The Psychic | "Must play 5 cards (not all cards need to score)" |
| The Goad | "All Spade cards are debuffed" |
| The Water | "Start with 0 discards" |
| The Window | "All Diamond cards are debuffed" |
| The Manacle | "-1 Hand Size" |
| The Eye | "No repeat hand types this round (every hand played this round must be of a different type and not previously played this round)" |
| The Mouth | "Only one hand type can be played this round" |
| The Plant | "All face cards are debuffed" |
| The Serpent | "After Play or Discard, always draw 3 cards (ignores hand size)" |
| The Pillar | "Cards played previously this Ante (during Small and Big Blinds) are debuffed" |
| The Needle | "Play only 1 hand" |
| The Head | "All Heart cards are debuffed" |
| The Tooth | "Lose $1 per card played" |
| The Flint | "Base Chips and Mult for played poker hands are halved for the entire round" |
| The Mark | "All face cards are drawn face down" |

**Showdown Boss Blinds (Ante 8, `<d>` L67–73)**: Amber Acorn "Flips and shuffles all Joker cards" · Verdant Leaf "All cards debuffed until 1 Joker sold" · Violet Vessel "Very large blind" · Crimson Heart "One random Joker disabled every hand (changes every hand)" · Cerulean Bell "Forces 1 card to always be selected". Note `<d>` L27 says "Boss Blinds are randomly selected", and L35–37 that Showdown Blinds "are identical to regular Boss Blinds."

`<c>` L74–101 is the only source that pairs each boss with a counter, and it **adds one boss not present elsewhere**: "**The Diamond** | All Diamond cards are debuffed | Rely on Hearts, Clubs, Spades" — the same effect `<d>` attributes to **The Window**. `<c>` also says "The Wall | **4x base blind chips**" (vs `<d>` "Extra large blind") and `<c>` L85 "The House … **Discard first hand entirely**", L94 "The Serpent … **Good for card cycling**", L86 "The Manacle … **Play 4-card hands, avoid Flush**", L90 "The Ox … **Use secondary hand type**", L91 "The Pillar … **Use different cards each hand**", L93 "The Psychic … **No 4-of-a-kind or smaller hands**".

### E.2 Tags — `<c>` L587–604 is the only tag table

| Tag | Effect | `<c>` skip value |
|---|---|---|
| Negative Tag | Next joker is Negative (+1 slot) | HIGH |
| Rare Tag | Next shop has Rare joker | HIGH |
| Uncommon Tag | Next shop has Uncommon joker | MEDIUM |
| Voucher Tag | Free voucher | HIGH |
| Double Tag | Doubles next tag | MEDIUM |
| Charm Tag | Free Arcana pack | MEDIUM |
| Meteor Tag | Free Celestial pack | HIGH |
| Ethereal Tag | Free Spectral pack | LOW |
| Standard Tag | Free Standard pack | LOW |
| Buffoon Tag | Free Buffoon pack | MEDIUM |
| Investment Tag | **+$25 after beating boss** | HIGH |
| Boss Tag | Reroll Boss Blind | LOW |
| Coupon Tag | Free items in shop | MEDIUM |
| D6 Tag | Free rerolls | LOW |

Skip conditions: `<c>` L574–585 — never skip in Antes 1–3, never when below $20, never when joker slots are full; from Ante 4+ skip only if all three hold: "$25+ and will maintain it; You can definitely beat the Boss Blind; The tag is valuable (**Voucher, Rare, Negative**)". `<a>` L249–259: "Skip Small/Big Blinds when the tag reward enables your strategy… **Uncommon/Rare Tag**: Free Joker; **Negative Tag**: Negative edition on next Joker; **Investment Tag: Double money**; **Ethereal Tag**: Free Spectral card. Only skip if you're confident you can beat the Boss Blind."
**Policy in the shipped bot** (`docs\strategies.md` L200–207, confirmed in `src\balatrollm\bot.py` L204–208): "`ROUND_EVAL` always calls `cash_out`"; "`BLIND_SELECT` **always calls `select` (never `skip`)** because Tags are not supported yet by `balatrobot`". Doc's own comment: this "always play the blind" policy "is a reasonable baseline for `RED` deck on `WHITE` stake." ⚠️ **`Throwback` (+xMult per blind skipped, `<c>` L275) and the entire tag-skip strategy are therefore dead code under this bot.**

### E.3 Boss blinds and card availability

- `<d>` L464: "Debuffed cards and Jokers from Boss Blinds have all modifier effects disabled **except for Negative (+1 slot) and Stone** (no rank and suit). Debuffed wild cards revert back to their original suit."
- `<d>` L31: face-down cards still score normally, only the hand *label* becomes "???".
- `<d>` L33: hands violating a size/type restriction "will not be scored or activate other effects, **acting like a discard**" — i.e. still consumes the hand.

---

## F. Corrections to likely-wrong assumptions (read this before coding)

**F.1 `×Mult` does not multiply `(base + additions)` the way a literal reading suggests; and `<c>`'s worked example of "correct order" is inverted.**
`<c>` L513–529 claims: base 100 Chips / 10 Mult, jokers `[+50 Chips], [+10 Mult], [x2 Mult], [x1.5 Mult]`; "Correct order" gives **9,000 points** and "Wrong order [x2, x1.5, +50, +10]" gives **6,000**. Recomputing with the formula from `<d>`/`<c>`: correct order = `(100+50) × ((10+10)×2) × 1.5 = 150 × 40 × 1.5 = 9,000`; wrong order = `(100+50) × (10×2×1.5+10) = 150 × 40 = 6,000`. Both printed totals reproduce **only if ×Mult multiplies the running mult and later +Mult is added afterwards** — which is `<d>` L508–515's own arithmetic. So: **implement mult as a running accumulator; ×Mult multiplies the accumulator; +Mult adds to the accumulator at that point in left-to-right order.** Do **not** implement `chips × (base + adds) × allXmults` (that gives 3,750 for the correct order and 3,000 for the wrong one), and do not implement `(chips) × (base + adds + late adds) × xmults` either. The `<c>` example is right; only its prose framing is confusing.

**F.2 Three of `<d>`'s own worked examples are arithmetically wrong — do not copy their totals.**
- Full House (`<d>` L145): listed `H_K, C_K, D_K, S_2, D_2`, "scores ((10+10+10+2+2) + 40) × (4) = **296**". `(34+40)×4 = 296` ✓, but if only the 3 Kings + pair 2s score, that is all five cards, so the formula is right and the hand is consistent. **Not an error.**
- Flush (`<d>` L152): listed `H_A, H_K, H_T, H_5, H_4`, "scores ((11 + 10 + 10 + 5 + 4)) + 35) × (4) = **300**". `(40+35)×4 = 300` ✓ by the printed sum, **but `<d>`'s own rule (L122: only relevant cards score, no kicker) and every other Flush in the docs imply `H_5`/`H_4` do not score**, giving `(31+35)×4 = 264`. **The Flush example contradicts both the rule and the real game.**
- Straight (`<d>` L159): `(10+10+9+8+7) + 30) × 4` printed as **296**; actual = `(44+30)×4 = 296` ✓. *(Equal to the Flush total by coincidence.)* **Not an error.**
- Two Pair (`<d>` L173): listed `H_A, D_A, C_Q, H_4, C_4`, "scores ((11 + 11 + 4 + 4)) + 20) × (2) = **100**". Actual = `(30+20)×2 = **100**`? `11+11+4+4 = 30`, `(30+20)×2 = 100` ✓. **Not an error** (the extra C_Q correctly does not score).
- High Card (`<d>` L187): "This hand scores (11 + **5**) × (1) = **16**" — but High Card's base is stated as **4** two lines earlier (L185) and the Ace is 11, so `11 + 4 = 15`, and `<c>`/`<a>` say base **5**, which gives **16**. **`<d>`'s High Card example uses the conservative/aggressive base value (5) while its own table says 4.** Net effect: implement High Card base as **5** (2 of 3 guides + the only self-consistent example agree), and treat `<d>` L185's "4" as the typo.

**F.3 Which cards score is under-specified per hand, and the Flush example is the trap.** See A.3. Rule to trust: "only the cards relevant to the hand are scored… **no kicker**" (`<d>` L122). Standard behaviour for Flush = all 5 played cards are the flush suit by definition, so all 5 score; the `<d>` Flush example's numbers were written as if 2 cards were junk, which cannot happen in a legal Flush. Use `sum(rank values of the 5 suited cards) + 35` at level 1.

**F.4 An extra card in a played hand still cycles a draw, and the draw count equals the number of cards played — not the number of cards that scored.** `<d>` L524–543: "**You ALWAYS draw back the number of cards you played**"; play a Pair + 2 junk = cycle 4 cards. So the scorer and the draw-counter must be independent; scoring may return fewer "scoring cards" than the played list length.

**F.5 Retrigger (Red Seal) re-counts the card in the score calculation entirely** — `<d>` L484: "Retriggering means that the effect of the cards is applied again **including counting again in the score calculation**." So a Red-Sealed King of Spades with a Greedy Joker adds its 10 chips *and* its +3 Mult *twice*, and any per-card joker effect also fires twice. `<c>` L428 "Retrigger card | Very strong"; `<a>` L174 "**Red Seal**: Retrigger card 1 time (**DOUBLES scoring effects**)". For comparison, `<c>` L232 defines `Hack` as "Retrigger 2, 3, 4, 5 cards" and `<a>`/`<c>` describe Hanging Chad as "retrigger first card" and Mime as "retrigger leftmost card" — note `<d>` L697 instead says Mime "**retriggers all cards held in hand**". **These two descriptions of Mime are incompatible**; `<d>` matches real Balatro **[outside knowledge]**, and `<c>` L655 is the suspect line.

**F.6 Held-in-hand effects exist and are separate from scored effects.** Steel = "×1.5 Mult **while held in hand**" (`<d>` L474); Baron = "Kings **held in hand** give ×1.5 Mult each" (`<d>` L695); Gold Card = "$3 if **held in hand** at end of round" (`<d>` L476); Blue Seal "if **held in hand**" (`<d>` L485); Mime "retriggers all cards **held in hand**" (`<d>` L697). An implementer who only scores the played cards will silently lose all of these. The docs never specify whether held-in-hand mult is applied before or after the played cards resolve — **not covered**; `<d>` L114 only orders "Boss Blind effects first, then Jokers and card effects trigger as cards are scored from left to right" and `<d>` L516 says to "place cards with multiplicative effects (Glass, Polychrome) **rightmost in your played hand**", implying per-card scoring resolves in played order.

**F.7 "Contains a Pair" is never defined — a real gap for engine implementation.** `<d>` L569: "The Jokers that give Chips and Mult when a hand contains a Pair or a Three of a Kind also work with larger hands"; `<c>` L631–634 pairs `Jolly` (+8 Mult if Pair) with `Zany` (+12 Mult if Trips) inside the **Full House Fortress** build, i.e. a Full House triggers **both** the Pair and the Trips jokers. So "contains" is a *set of satisfied hand-type predicates over the played cards*, not "the hand's final type equals X". Which predicates a Flush House/Flush Five satisfies is **not covered** by these files. Also `<d>` L51 (The Psychic) confirms only "not all cards need to score" — restriction is on cards *played* (5), not cards *scoring*.

**F.8 `<d>`'s "hidden discard" example miscounts its own draw.** `<d>` L542: "Option B: Play `S_K, D_K, H_3, S_7` → Score the Kings, **draw 3 new cards**". Four cards were played, so by `<d>` L530 ("You ALWAYS draw back the number of cards you played") the draw is **4**, not 3. The public hand size stays at hand limit; the extra card comes from the smaller played count. Implement: `draw = len(played_cards)`.

**F.9 Boss Blind naming mismatch.** `<c>` L78 lists **The Diamond** (debuff Diamonds) which `<d>`/`<a>` do not list at all; `<d>`'s equivalent is **The Window**. The real game has both `The Window` (Diamonds debuffed) and `The Goad`/`The Head`/`The Club` for the other suits **[outside knowledge]**; `<c>`'s "The Diamond" row should be treated as a mis-label of The Window. Also `<c>` L96 "The Wall | **4x base blind chips**" vs `<d>` "Extra large blind" — `<c>` supplies the only number for The Wall ("4x base"), which is **not corroborated** anywhere else in these files.

**F.10 Interest cap numbers conflict three ways.** `<d>` L438–439: Seed Money cap **$10**, Money Tree cap **$20**. `<c>` L370–371: **$50** and **$100**. `<c>` L34: "interest caps at **$25** balance" (a balance figure, not a cap). `<c>` GAMESTATE L18 hardcodes `min(money // 5, 5)` regardless of vouchers. For a deterministic bot, **the `<d>` values are the ones to encode**, and the GAMESTATE template is a bug worth reporting to the parent (it ignores Seed Money/Money Tree).

**F.11 Investment Tag value conflicts.** `<c>` L601 "**+$25** after beating boss" vs `<a>` L256 "**Double money**". Pick one; the files disagree.

**F.12 The shop/reroll/Voucher restock rules are subtle.** Booster packs do **not** refresh on reroll and only restock in a new shop; the Voucher restocks after defeating the **boss blind** (`<d>` L207, L412). Reroll replaces only the **2 random cards** (`<d>` L205). A reroll counter must reset to $5 on shop entry — and note `<c>` GAMESTATE L183 renders `G.round.reroll_cost` directly, so the engine may already track it.

**F.13 `<c>`'s "guaranteed" claims about Lucky cards are wrong.** `<c>` L292: "The Magician … **Guaranteed +20 Mult when triggered**"; `<d>` L477: "Lucky Cards: … a **1 in 5** chance to give +20 Mult and a **1 in 15** chance to give $20." Same pattern at `<c>` L310 "The Wheel of Fortune | 1/4 chance… | **Low chance but high value**" (consistent) vs `<a>` L140. Trust `<d>`.

**F.14 `<c>` L509's own "order" example is degenerate.** "Wrong order: [Steel Joker ×1.5] → [Joker +4 Mult] = (Chips) × (Mult + 4) × 1.5 / Right order: [Joker +4 Mult] → [Steel Joker ×1.5] = (Chips) × (Mult + 4) × 1.5" — the guide itself then writes "Wait, these look the same? The difference is when you have MULTIPLE jokers". Confirm: **with one ×Mult and one +Mult the order genuinely does not matter**; ordering only matters when a +Mult sits *after* a ×Mult (F.1). Also `<a>` L211 says to order ×Mult jokers "in ascending order of multiplication" — **multiplication is commutative, so this is meaningless** unless a later joker's effect depends on the current mult value; **[outside knowledge]** the real reason to order ×Mult jokers is per-card vs per-hand trigger conditions and Blueprint/Brainstorm copy direction, which these files do not explain.

**F.15 "The Ox" targets your most-played hand "this run"; `<c>` interpolates a template variable that will never render.**
`<d>` L44: "Playing your most played hand this run sets money to $0". `<c>` L90 literally contains `| The Ox | Playing a #most_played_hand# sets money to $0 |` — a raw placeholder in a Jinja file, so it renders as-is. Harmless for rules, but it shows the conservative guide is less maintained than `<d>`.

**F.16 The bot's own loop never skips blinds and never asks the LLM at ROUND_EVAL/BLIND_SELECT.** `bot.py` L200–208 and `docs\strategies.md` L200–207. Consequence for a deterministic bot: all tag-skip heuristics (Section E.2) and `Throwback` are **unreachable** unless the parent changes the loop. `docs` L207 explicitly endorses "always play the blind" for **RED deck on WHITE stake** — the exact configuration in Section G.

**F.17 Two mechanics that silently invalidate "score > target" checks.** (1) Boss effects resolve **before** scoring (`<d>` L114: "Boss Blind effects first"), so The Arm's level −1 and The Flint's halving must be applied in the scorer, not after. (2) A restriction-violating hand "**will not be scored or activate other effects, acting like a discard**" (`<d>` L33) — it still consumes a hand. The Eye/The Mouth/The Psychic therefore turn a "big score" play into a wasted hand rather than a failed one.

---

## G. Top 15 ranked actionable heuristics (White Stake, Red Deck)

Ranked by expected win-rate impact for a deterministic bot on White Stake with the standard Red Deck (and assuming the shipped "always select, never skip" loop). Each is written as an implementable IF/THEN with the doc numbers.

1. **Joker ordering (largest single multiplier of score).**
   IF jokers are ever added/rearranged, THEN reorder so all `+Chips` and `+Mult` jokers precede all `×Mult` jokers, preserving relative order within each group; THEN reorder the played hand so `×Mult` per-card effects (Glass, Polychrome) are rightmost.
   *Source:* `<d>` L112–114, L504–516 (worked: A→B = 1,400 vs B→A = 900); `<a>` L209–211; `<c>` L502–531.

2. **Play max cards rather than discarding whenever any points are scoreable.**
   IF a legal scoring hand exists in hand, THEN play it with the largest legal card count (up to `highlighted_limit`, normally 5), choosing as filler the cards you would otherwise discard, while never discarding cards that belong to the next target hand.
   *Source:* `<d>` L524–551 ("If you can score ANY points, play the hand with maximum cards rather than discarding"; draw = cards played); `<a>` L302–311.

3. **Hold a $25 floor from Ante 2 onward (interest is `min(money//5, 5)`).**
   IF `ante_num >= 2` AND a purchase/reroll would leave money < $25, THEN skip it unless (a) the blind is currently un-winnable without it, (b) it is Ante ≥ 6 and the item is a game-changing joker/voucher, or (c) it is a reroll needed to survive.
   *Source:* `<c>` L9, L464, L537–539, L551–556; `<d>` L613; `<c>` GAMESTATE L18.

4. **Never sell a joker for value reasons; only to free a slot or unblock a boss.**
   IF considering a sale, THEN require one of: joker conflicts with the committed hand type; Egg/Gift Card sell value is large; you need exactly $X for a winning purchase; or the active Showdown is **Verdant Leaf** (needs "1 Joker sold").
   *Source:* `<d>` L594–609; `<c>` L16, L685.

5. **Commit to exactly one hand type and route all planets there.**
   IF the run's first strong hand-specific joker/planet identifies a type, THEN use every Planet on that type only, buy only jokers that support it, and skip jokers that boost other types even if powerful; target **level 6+** on the primary hand; play that hand type **~90%** of the time.
   *Source:* `<d>` L555, L581–586, L656; `<c>` L157, L716–717; `<a>` L222–235.

6. **Keep at least two viable hand types through Ante 3, plus one emergency consumable for the boss.**
   IF `ante_num <= 3`, THEN maintain a secondary hand type and do not spend your last consumable slot; THEN hand the boss-blind effect to a specific counter-plan before selecting the blind.
   *Source:* `<d>` L685–687; `<c>` L72, L699–700.

7. **Boss-blind counter table (apply before the blind, not during it).**
   IF boss = suit-debuff (The Club/Goad/Window/Head), THEN do not rely on that suit; IF The Plant (face cards debuffed), THEN switch to number cards; IF The Pillar, THEN use different cards than in the Small/Big blinds of this ante; IF The Manacle (−1 hand size), THEN plan 4-card hands and avoid Flush; IF The Needle (1 hand) or The Wall, THEN one-shot with the full multiplier stack; IF The Psychic (must play 5 cards), THEN avoid Pair/Two Pair/Trips as the scoring play (a <5-card play is wasted); IF The Eye, THEN use a different hand type each play; IF The Mouth, THEN use one type only; IF The Hook/The Tooth, THEN minimise cards played; IF The Arm, THEN use your highest-levelled hand (level drops by 1 **before** scoring); IF The Flint, THEN rely on ×Mult/level-independent sources since base chips and mult are halved.
   *Source:* `<d>` L43–65, L671–681; `<c>` L74–101.

8. **Shift from flat bonuses to ×Mult by Ante 4; stack multiple ×Mult jokers.**
   IF `ante_num >= 4`, THEN prefer ×Mult jokers over +Mult/+Chips; THEN prioritise scaling jokers (Green Joker, Supernova, Spare Trousers, Fortune Teller, Flash Card) and copy effects (Blueprint, Brainstorm) once an ×Mult joker exists.
   *Source:* `<d>` L630–641; `<a>` L123–134, L336–343 ("x2 ×1.5 ×1.5 = x4.5").

9. **Shop purchase order when flush with cash (>$25).**
   IF money > $25, THEN buy in this order: build-upgrading joker (if a slot is open) → Celestial pack → economy/utility voucher (Grabber, Wasteful, Blank → Antimatter) → Arcana pack → Buffoon pack → single card if excellent → reroll only if money ≥ $30.
   *Source:* `<c>` L558–568; pack priority "Celestial > Arcana > Buffoon > Standard >>> Spectral" `<c>` L180.

10. **Reroll budget by ante (the only numeric budget in the docs).**
    IF Ante 1, THEN reroll up to **$10–15** total if hunting for a first ×Mult/scaling joker; IF Ante 2, THEN up to **$8–10**; IF Ante ≥ 3, THEN reroll only for a named synergy piece; base cost **$5**, **+$1 per reroll**, resets each shop, and never restocks packs/voucher.
    *Source:* `<a>` L190–203; `<d>` L205–207.

11. **Deck thinning and enhancement targets.**
    IF mid-game (Antes 4–6), THEN start destroying low-value/duplicate cards and enhancing the cards you actually score; IF by Antes 7–8, THEN thin to essentials; target **30–40 cards** for a focused build; prefer Bonus (+30 Chips), Mult (+4 Mult), Steel (×1.5 in hand), Glass (×2, 1-in-4 break), Gold ($3 in hand at end of round).
    *Source:* `<a>` L261–275; `<d>` L632, L640–641; `<d>` L466–477.

12. **Economy micro-rules that are pure free money.**
    IF a blind is beaten, THEN cash out with as many **unspent hands** as possible ($1 each) and never below the interest band; IF Gold Card/Gold Seal are available, THEN value them at **$3** each; IF **The Hermit** appears, THEN note it doubles money to a **max of $20**; IF **Temperance**, THEN value it at total joker sell value (**max $50**).
    *Source:* `<d>` L15, L354, L359, L476, L483; `<c>` L185.

13. **Consistency over high-rolls in the play phase.**
    IF a reliable moderate hand is available, THEN play/settle it rather than chasing a Straight Flush; THEN use discards only to find your committed type, keeping at least **1 discard** in reserve unless it is the final hand.
    *Source:* `<d>` L723–728; `<c>` L11, L691, L702–703.

14. **Buy packs for the phase you are in.**
    IF an open joker slot and no synergy, THEN buy Buffoon packs; IF early game and you need card quality, THEN Arcana; IF you already know your dominant hand type, THEN Celestial (all planets into that type); Spectral only when the specific card is one you are willing to run — and remember Arcana/Spectral cards are used **immediately** while Buffoon/Standard add to the collection.
    *Source:* `<d>` L225–236; `<c>` L172–180.

15. **You get a free "discard" from every play and a free reorder before committing.**
    IF you are about to play, THEN first reorder the played hand to maximise score and choose the filler cards deliberately (they cycle a draw of `len(played)`); THEN do not discard cards of the suit/rank you are building toward.
    *Source:* `<d>` L518–543, L718, L727.

*Runner-up (16): Joker rarity is not value — Common = 70%, Uncommon = 25%, Rare = 5%, Legendary = 0.3%, and `<d>` L715 warns "Common Jokers often outperform Rare ones for specific strategies"; do not pay a premium for rarity.*
*Runner-up (17): With the shipped loop the bot always selects and never skips (`docs\strategies.md` L200–207, `bot.py` L204–208), so build no logic on tags or `Throwback` unless the parent changes that policy.*

---

## Appendix — Items the docs do **not** cover (so the implementer must supply or probe them)

- Definition of "contains a Pair/Three of a Kind" for larger hands (F.7). **Not covered** beyond the Full House Fortress implication.
- Which specific cards score in Pair / Two Pair / Trips / Quads / Full House. **Not covered** by rule text (only by examples, one of which is wrong).
- Effect resolution order for **held-in-hand** effects (Steel, Baron, Mime, Gold at round end) relative to scored-card effects. **Not covered.**
- Whether a Face-down ("???") hand's joker hand-type conditions still apply — `<d>` L31 says only that scoring and effects "will still perform as normal". **Ambiguous.**
- Exact boss effects for The Wall ("Extra large blind" — no multiplier in `<d>`; `<c>` says 4x, `<a>` says "Very large" for Violet Vessel). **Under-specified.**
- Boss-blind score tables per ante beyond `<c>`'s Base column. **Not covered.**
- Shop item base price list (no joker/consumable prices given; only reroll $5 and sell = half). **Not covered** beyond `<d>` L201, L205.
- Booster pack prices (only shown as runtime `pack.cost.buy` in GAMESTATE L202). **Not covered.**
- Whether Coupon/D6 tags are honored by `balatrobot`. `docs` says Tags are unsupported. **Not covered.**
