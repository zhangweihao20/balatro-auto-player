-- src/lua/ui/journal.lua
--
-- 决策日志面板：把机器人的每一步决策画在游戏窗口右侧，风格跟随游戏。
--
-- 这是本项目对 balatrobot mod 的**附加**文件，不改动 mod 原有逻辑。
-- 仓库里的对应副本在 mod_additions/src/lua/ui/journal.lua；mod 升级后重新拷入即可。
--
-- 数据流：
--     机器人（Python）──journal 端点──► BB_JOURNAL.add(record) ──► 每帧绘制
--
-- 为什么用端点推送而不是读文件：机器人本来就通过 JSON-RPC 说话，多一个端点
-- 是最短路径；而且不用把仓库路径硬编码进游戏里。

BB_JOURNAL = {}

-- 保留最近多少条（面板一次只显示得下十几条，留点余量方便查看）
local MAX_RECORDS = 200

-- 面板几何
local MARGIN = 10
local PAD = 8

-- 正文高度 ≈ 窗口高度 / 这个数。
--
-- 这个值直接决定"清晰度/字号"和"一屏能看多少条"的取舍：**调小 = 字更大、
-- 显示条数更少**。布局是"从下往上量、从上往下排"的，所以条数变少只会让最老的
-- 记录从顶部滚出去，不会截断任何一条。
--
-- 46 → 1152p 下约 25px（和游戏自己的小号 UI 文字相当）；想更大就继续调小。
local BODY_HEIGHT_DIVISOR = 46
local HEAD_SCALE = 1.28

BB_JOURNAL.records = {}
BB_JOURNAL.visible = true
BB_JOURNAL.toggle_key = "f8"

-- ------------------------------------------------------- 给面板腾出真正的空间
--
-- 目标：面板不遮挡任何游戏内容。
--
-- 做法：**让游戏以为窗口比实际窄 `reserve` 像素**。
-- 游戏的一切布局都由 `love.resize(w, h)` 的 `w` 推导（`G.TILESCALE`、`G.ROOM.T.x`、
-- `G.CANVAS` 的尺寸、最后那句 `draw(self.CANVAS, 0, 0)`），所以只要在 resize 时
-- 把 `w` 减掉一段，游戏就会把内容排在左侧、画布也只建那么宽——右侧自然空出一条
-- **真正的空白**，面板画在那里就完全不遮挡。
--
-- 两个关键性质让这个做法很干净：
--   1. 游戏是**按高度充满**的（窗口比 `WINDOWTRANS` 的宽高比更宽时走 else 分支），
--      所以 `G.TILESCALE` 只由高度决定 —— 加宽窗口**不会**让游戏变大或变小；
--   2. 画布仍是 1:1 贴在屏幕原点，像素比例没变，所以**鼠标拾取不需要任何补偿**。
--
-- 剩下的问题是空间从哪来：内容宽是定值（实测 1965px），2048 宽的窗口左右各只剩
-- 42px，不够。所以这里会**先把窗口加宽**到能容纳"内容 + 两侧留边 + 面板"为止
-- （上限是屏幕宽度）。加宽只是让游戏两侧的空白变多，游戏本身不变。

BB_JOURNAL.reserve_w = 400        -- 想给面板留的宽度（像素）
BB_JOURNAL.min_side = 24          -- 内容两侧至少留这么宽，保证不会被切
BB_JOURNAL.auto_widen = true      -- 自动加宽窗口以腾出空间
BB_JOURNAL.reserve_now = 0        -- 当前实际让出的宽度

-- ------------------------------------------------------------------ 配色
--
-- 尽量取游戏自己的调色板（G.C），取不到再用兜底值——这样即使游戏改版
-- 换了颜色，面板也会跟着变，而不是变成一块突兀的外来 UI。

local function game_colour(key, fallback)
  local c = G and G.C and G.C[key]
  if type(c) == "table" then
    return { c[1] or fallback[1], c[2] or fallback[2],
             c[3] or fallback[3], c[4] or fallback[4] or 1 }
  end
  return fallback
end

local function with_alpha(colour, alpha)
  return { colour[1], colour[2], colour[3], alpha }
end

-- 按动作类别上色。和 tools/decision_log.py 的窗口用同一套语义。
local KIND_COLOUR = {
  play      = { 0.45, 0.88, 0.45 },
  discard   = { 0.98, 0.82, 0.35 },
  buy       = { 0.42, 0.72, 1.00 },
  sell      = { 1.00, 0.45, 0.40 },
  reroll    = { 0.78, 0.58, 1.00 },
  pack      = { 0.45, 0.92, 0.92 },
  use       = { 1.00, 0.66, 0.30 },
  rearrange = { 0.62, 0.92, 0.55 },
  blind     = { 0.80, 0.84, 0.88 },
  flow      = { 0.68, 0.68, 0.72 },
  refresh   = { 0.55, 0.55, 0.58 },
  other     = { 0.75, 0.75, 0.75 },
}

local KIND_LABEL = {
  play = "出牌", discard = "弃牌", buy = "购买", sell = "卖出",
  reroll = "重摇", use = "用牌", pack = "开包", blind = "盲注",
  rearrange = "重排", flow = "流程", refresh = "刷新", other = "—",
}

local function kind_colour(kind)
  return KIND_COLOUR[kind] or KIND_COLOUR.other
end

--- 游戏内容的逻辑宽度（像素），按**当前**的 TILESCALE 算。
--- 面板定位用它——它反映的是游戏实际画出来的尺寸。
---
--- 必须在 `draw` 之前定义：Lua 是词法作用域，定义在后面的 local 在 `draw` 里
--- 会被当成全局名（nil）而不是向上查找。
local function content_width()
  local scale = G.TILESCALE or 1
  local tiles = G.WINDOWTRANS and G.WINDOWTRANS.w
  if not tiles then
    -- 兜底：TILE_W = 20、ROOM_PADDING_W = 1 → WINDOWTRANS.w = 22
    tiles = 22
  end
  return tiles * (G.TILESIZE or 20) * scale
end

--- 游戏**自己**会算出的 TILESCALE（不受我们"谎报窗口宽度"的影响）。
---
--- 为什么必须自己重算：reserve 的计算如果读 `G.TILESCALE`，读到的就是上一次
--- 谎报之后的缩水值 → 内容宽算小 → 让出更多 → 游戏更小 …… 形成反馈回路，
--- 游戏会被一路挤小。第一版就是这么写的，实测"内容宽"从 1965 掉到 1606。
---
--- 游戏的原式（main.lua:330-334）：
---     if w/h < orig_ratio then  scale = orig_scale * w / prev.w
---     else                      scale = orig_scale * h / prev.h
local function natural_scale(w, h)
  local p = G.window_prev
  if not p or not p.orig_scale or not p.w or not p.h then
    return G.TILESCALE or 1
  end
  if w / h < p.orig_ratio then
    return p.orig_scale * w / p.w
  end
  return p.orig_scale * h / p.h
end

local function natural_content(w, h)
  local tiles = (G.WINDOWTRANS and G.WINDOWTRANS.w) or 22
  return tiles * (G.TILESIZE or 20) * natural_scale(w, h)
end

-- ------------------------------------------------------------------ 字体
--
-- **按目标像素高度新建字体，然后以 scale = 1 绘制。**
--
-- 反面做法（第一版）：直接用游戏那个大字体，缩小到 1/11 画出来。游戏的字体是
-- 按 render_scale=140 加载的（实测 font:getHeight() = 203），缩小 11 倍会走
-- LÖVE 的线性过滤，字就发虚——用户实测反馈"字体太模糊"。
--
-- 正面做法：用同一个字体文件（描述表里的 `file` 字段，实测是
-- resources/fonts/NotoSansSC-Bold.ttf）按目标高度新建一个，字形直接按最终
-- 像素尺寸栅格化，天然清晰。

local FONT_CACHE = {}

local function current_desc()
  local lang = (G and G.LANG) or (G and G.LANGUAGES and G.LANGUAGES["en-us"])
  return lang and lang.font
end

--- 建一个"行高约等于 target 像素"的字体。字体的行高和字号不是 1:1
--- （实测 size=140 → height=203），所以按实测比例修正一次。
local function font_for_height(path, target)
  local key = path .. "|" .. target
  if FONT_CACHE[key] then return FONT_CACHE[key] end

  local ok, font = pcall(function()
    local size = target
    local f = love.graphics.newFont(path, size)
    local h = f:getHeight()
    if h and h > 0 and h ~= target then
      size = math.max(6, math.floor(size * target / h + 0.5))
      f = love.graphics.newFont(path, size)
    end
    return f
  end)
  if ok and font then
    FONT_CACHE[key] = font
    return font
  end
  return nil
end

--- 返回 (正文字体, 标题字体, 正文倍率, 标题倍率)。
---
--- 自己建字体成功时倍率都是 1（清晰）；失败时退回游戏字体 + 缩小倍率（会有点虚，
--- 但至少能用，不会因为字体文件改版就整个面板不显示）。
local function get_fonts()
  local body_px = math.max(13, math.floor(love.graphics.getHeight() / BODY_HEIGHT_DIVISOR))
  local head_px = math.floor(body_px * HEAD_SCALE)
  local desc = current_desc()
  local path = desc and desc.file

  if path then
    local body = font_for_height(path, body_px)
    local head = font_for_height(path, head_px)
    if body and head then return body, head, 1, 1 end
  end

  local base = (desc and desc.FONT) or love.graphics.getFont()
  local h = base:getHeight()
  local scale = (h and h > 0) and (body_px / h) or 1
  return base, base, scale, scale * HEAD_SCALE
end

--- 把量到的度量打进 mod 日志，方便换分辨率 / 换语言后核对（只打一次）。
local function log_metrics(body, head, body_scale)
  if BB_JOURNAL._logged_metrics then return end
  BB_JOURNAL._logged_metrics = true
  pcall(function()
    local desc = current_desc()
    sendInfoMessage(string.format(
      "journal metrics: win=%dx%d bodyH=%s headH=%s scale=%.3f file=%s gameFontH=%s",
      love.graphics.getWidth(), love.graphics.getHeight(),
      tostring(body:getHeight()), tostring(head:getHeight()),
      body_scale, tostring(desc and desc.file),
      tostring(desc and desc.FONT and desc.FONT:getHeight())),
      "BB.JOURNAL")
  end)
end

-- ------------------------------------------------------------------ 记录

--- 加一条决策记录。
---@param record table {kind, action, why, detail, numbers, frame, step}
function BB_JOURNAL.add(record)
  if type(record) ~= "table" then return end
  record.t = os.date("%H:%M:%S")

  -- 数字先拼成一行文本（排序固定，免得每次顺序乱跳）。
  -- 在这里做而不是在 draw 里：measure 和 draw 都要用它，而且只要算一次。
  if record.numbers and next(record.numbers) then
    local keys = {}
    for k in pairs(record.numbers) do table.insert(keys, k) end
    table.sort(keys)
    local parts = {}
    for _, k in ipairs(keys) do
      parts[#parts + 1] = k .. "=" .. tostring(record.numbers[k])
    end
    record.numbers_text = table.concat(parts, "   ")
  end

  table.insert(BB_JOURNAL.records, record)
  while #BB_JOURNAL.records > MAX_RECORDS do
    table.remove(BB_JOURNAL.records, 1)
  end
end

function BB_JOURNAL.clear()
  BB_JOURNAL.records = {}
end

function BB_JOURNAL.toggle()
  BB_JOURNAL.visible = not BB_JOURNAL.visible
end

-- ------------------------------------------------------------------ 绘制

local function shadow_print(font, text, x, y, colour, scale)
  scale = scale or 1
  love.graphics.setColor(0, 0, 0, 0.65)
  love.graphics.print(text, x + 1.5 * scale, y + 1.5 * scale, 0, scale, scale)
  love.graphics.setColor(colour)
  love.graphics.print(text, x, y, 0, scale, scale)
end

--- 把 UTF-8 文本拆成单字符（不依赖 Lua 5.3 的 `utf8` 库，行为确定）。
local function split_chars(text)
  local out, i, n = {}, 1, #text
  while i <= n do
    local byte = text:byte(i)
    local len = 1
    if byte >= 0xF0 then len = 4
    elseif byte >= 0xE0 then len = 3
    elseif byte >= 0xC0 then len = 2 end
    if i + len - 1 > n then len = 1 end
    out[#out + 1] = text:sub(i, i + len - 1)
    i = i + len
  end
  return out
end

--- 按字符把文本折成若干行。
---
--- 为什么不用 `font:getWrap` + `printf`：两者的换行规则在**中文**下不一致——
--- 中文没有空格，printf 会在字符间断行，而 getWrap 报的是按"词"算的行数。
--- 于是"量出来的高度"比"实际画出来的高度"小，多行文字就越叠越密（实测整片糊在一起）。
--- 这里改成自己折行、自己逐行 print，**量多少就画多少**。
---
--- 折好的行缓存在记录上，按限制宽度做键——只在宽度变化时重算。
local function wrap_text(font, text, limit)
  local lines = {}
  if not text or text == "" then return lines end
  if limit <= 0 then return { text } end

  local current = ""
  for _, ch in ipairs(split_chars(text)) do
    local candidate = current .. ch
    if current ~= "" and font:getWidth(candidate) > limit then
      table.insert(lines, current)
      current = ch
    else
      current = candidate
    end
  end
  if current ~= "" then table.insert(lines, current) end
  return lines
end

local function ensure_lines(font, record, field, limit, scale)
  local key_field = "_" .. field .. "_key"
  local lines_field = "_" .. field .. "_lines"
  local key = math.floor(limit / (scale or 1))
  if record[key_field] ~= key or not record[lines_field] then
    record[lines_field] = wrap_text(font, record[field] or "", limit / (scale or 1))
    record[key_field] = key
  end
  return record[lines_field]
end

--- 一条记录占多高。用的就是**真正会被画出来的行数**。
local function measure_record(body, record, inner_w, scale)
  local limit = inner_w - 14
  local why = ensure_lines(body, record, "why", limit, scale)
  local lines = math.max(1, #why)
  if record.numbers_text then
    lines = lines + #ensure_lines(body, record, "numbers_text", limit, scale)
  end
  local line_h = body:getHeight() * scale
  return line_h * lines + line_h * 0.5 + 5
end

--- 标题栏多高。跟着字体算，而不是写死——字号一改，写死的高度就会把字挤在一起。
local function header_height(body, head, head_scale)
  local top_line = head:getHeight() * head_scale
  local sub_line = body:getHeight()
  return 6 + top_line * 0.98 + sub_line * 1.12 + 6
end

local function draw_header(body, head, head_scale, x, y, w, record, hh)
  local frame = (record and record.frame) or {}
  local bg = game_colour("RED", { 0.85, 0.25, 0.20, 1 })
  love.graphics.setColor(with_alpha(bg, 0.95))
  love.graphics.rectangle("fill", x, y, w, hh, 6, 6)

  local gold = game_colour("GOLD", { 1.00, 0.85, 0.45, 1 })
  local text_light = (G and G.C and G.C.UI and G.C.UI.TEXT_LIGHT) or { 1, 1, 1, 1 }

  love.graphics.setFont(head)
  local left = x + PAD
  local line1 = string.format("底注 %s    第 %s 回合",
    tostring(frame.ante or "?"), tostring(frame.round or "?"))
  shadow_print(head, line1, left, y + 5, text_light, head_scale)
  if frame.money ~= nil then
    local money = "$" .. tostring(frame.money)
    local mw = head:getWidth(money) * head_scale
    shadow_print(head, money, x + w - PAD - mw, y + 5, gold, head_scale)
  end

  local parts = {}
  if frame.need ~= nil then table.insert(parts, "还差 " .. tostring(frame.need)) end
  if frame.got ~= nil then table.insert(parts, "已得 " .. tostring(frame.got)) end
  if frame.hands ~= nil then table.insert(parts, "出牌 " .. tostring(frame.hands)) end
  if frame.discards ~= nil then table.insert(parts, "弃牌 " .. tostring(frame.discards)) end
  love.graphics.setFont(body)
  shadow_print(body, table.concat(parts, "   "), left,
    y + 5 + head:getHeight() * head_scale * 0.98,
    with_alpha(text_light, 0.9), 1)
end

--- 画整个面板。在 love.draw 之后调用（窗口像素坐标系）。
local function draw()
  if not BB_JOURNAL.visible then return end
  if not (G and G.STAGE == G.STAGES.RUN) then return end   -- 只在牌局里显示

  local sw, sh = love.graphics.getWidth(), love.graphics.getHeight()
  local body, head, body_scale, head_scale = get_fonts()
  log_metrics(body, head, body_scale)
  local header_h = header_height(body, head, head_scale)

  -- 面板摆在"游戏内容右边缘之外"的那条空白里。
  --
  -- 游戏现在以为窗口宽 `sw - reserve`，内容（宽 content）在逻辑宽度里居中，
  -- 所以内容右边缘落在 ((sw-reserve)+content)/2。面板就从这个位置往右铺满，
  -- 一点都不压到游戏。腾不出空间时（reserve 太小）退回覆盖显示。
  local reserve = BB_JOURNAL.reserve_now or 0
  local content = content_width()
  local content_right = ((sw - reserve) + content) * 0.5
  local avail = sw - content_right - MARGIN * 2

  local panel_w
  if avail >= 240 then
    panel_w = math.min(avail, 560)                      -- 正好填满腾出来的空白
  else
    panel_w = math.max(300, math.min(400, math.floor(sw * 0.19)))
  end
  local panel_h = sh - MARGIN * 2
  local x = sw - panel_w - MARGIN
  local y = MARGIN

  love.graphics.setShader()
  love.graphics.origin()
  love.graphics.setColor(1, 1, 1, 1)
  love.graphics.setLineWidth(1)

  local border = game_colour("GOLD", { 0.95, 0.83, 0.55, 1 })
  love.graphics.setColor(with_alpha(border, 0.70))
  love.graphics.rectangle("fill", x - 3, y - 3, panel_w + 6, panel_h + 6, 10, 10)
  -- 已经腾出真正的空白时不必再透出游戏，用不透明底把日志衬得更清楚
  local alpha = (avail >= 240) and 0.97 or 0.90
  love.graphics.setColor(0.035, 0.030, 0.045, alpha)
  love.graphics.rectangle("fill", x, y, panel_w, panel_h, 8, 8)
  love.graphics.setColor(with_alpha(border, 0.28))
  love.graphics.rectangle("line", x + 1.5, y + 1.5, panel_w - 3, panel_h - 3, 7, 7)

  draw_header(body, head, head_scale, x, y, panel_w,
              BB_JOURNAL.records[#BB_JOURNAL.records], header_h)
  love.graphics.setFont(body)

  local inner_x = x + PAD
  local inner_w = panel_w - PAD * 2
  local top = y + header_h + 6
  local bottom = y + panel_h - PAD
  local line_h = body:getHeight() * body_scale

  if #BB_JOURNAL.records == 0 then
    shadow_print(body, "等待机器人决策…", inner_x, top + 2,
      with_alpha({ 1, 1, 1, 1 }, 0.5), body_scale)
    shadow_print(body, "（F8 隐藏面板）", inner_x, top + line_h + 2,
      with_alpha({ 1, 1, 1, 1 }, 0.35), body_scale)
    return
  end

  -- 从最新一条往回量高度，找出"装得下的最老一条"。这样记录多的时候
  -- 最老的会从顶部滚出去、最新的始终可见。
  local heights, total = {}, 0
  local first = #BB_JOURNAL.records
  for i = #BB_JOURNAL.records, 1, -1 do
    local h = measure_record(body, BB_JOURNAL.records[i], inner_w, body_scale)
    if total + h > (bottom - top) then break end
    heights[i] = h
    total = total + h
    first = i
  end

  -- **从上往下**排（用户反馈：初始内容应该从顶部开始生成）。
  -- 记录少的时候整块贴顶；装满之后最老的从顶部滚出，最新的仍在底部。
  local cursor = top
  for i = first, #BB_JOURNAL.records do
    local record = BB_JOURNAL.records[i]
    local kind = record.kind or "other"
    local accent = kind_colour(kind)
    local h = heights[i] or measure_record(body, record, inner_w, body_scale)
    local limit = inner_w - 14

    -- 左侧一道类别色条（比整行铺色更克制，也不会盖掉游戏本身的信息）
    love.graphics.setColor(with_alpha(accent, 0.85))
    love.graphics.rectangle("fill", inner_x, cursor + 1, 3,
      h - line_h * 0.5 - 2, 1, 1)

    local tx = inner_x + 9
    local ty = cursor + 2

    -- 第一行：时间 + 类别徽标
    local badge = KIND_LABEL[kind] or kind
    shadow_print(body, record.t or "", tx, ty,
      with_alpha({ 1, 1, 1, 1 }, 0.55), body_scale)
    local tw = body:getWidth(record.t or "") * body_scale
    shadow_print(body, badge, tx + tw + 8, ty, accent, body_scale)
    ty = ty + line_h

    -- 理由：用**自己折好的行**逐行画，和量高度用的是同一份数据
    local why_lines = ensure_lines(body, record, "why", limit, body_scale)
    if #why_lines == 0 and record.detail and record.detail ~= "" then
      why_lines = { record.detail }
    end
    for _, line in ipairs(why_lines) do
      shadow_print(body, line, tx, ty, { 1, 1, 1, 1 }, body_scale)
      ty = ty + line_h
    end

    -- 数字：暗一档，扫一眼就够
    local num_lines = ensure_lines(body, record, "numbers_text", limit, body_scale)
    for _, line in ipairs(num_lines) do
      shadow_print(body, line, tx, ty, with_alpha(accent, 0.85), body_scale * 0.92)
      ty = ty + line_h * 0.92
    end

    cursor = cursor + h
  end
end

-- ------------------------------------------------- 计算并腾出空间

--- 算出这次 resize 能安全让出多少宽度。
---
--- "安全"有两条界线：
---   * **不能把游戏挤小**：让出 r 之后游戏按 `w - r` 布局，一旦 `(w-r)/h` 小于
---     原始宽高比，游戏就改走"按宽度充满"分支、`TILESCALE` 变小 → 整个游戏缩水。
---     所以 r 不能超过 `w - natural_content`。
---   * **内容两侧各留 min_side**，保证不会被画布切掉。
---
--- 两条都满足时，游戏尺寸完全不变，只是两侧空白变多——右侧那条空白正好给面板。
local function compute_reserve(w, h)
  local content = natural_content(w, h)
  local no_shrink = w - content
  local safe = w - content - 2 * BB_JOURNAL.min_side
  if safe < 0 then safe = 0 end
  if no_shrink < 0 then no_shrink = 0 end
  local limit = math.min(safe, no_shrink)
  return math.min(BB_JOURNAL.reserve_w, limit)
end

--- 把窗口加宽到"内容 + 两侧留边 + 面板"够用的尺寸（上限是屏幕宽度）。
---
--- 加宽只影响游戏两侧空白，游戏本身不变大也不变小。
local function ensure_window()
  if not BB_JOURNAL.auto_widen then return end
  local ok, err = pcall(function()
    local _, _, flags = love.window.getMode()
    if flags.fullscreen then
      sendInfoMessage("决策日志：全屏模式，跳过自动加宽", "BB.JOURNAL")
      return
    end
    local w, h = love.graphics.getDimensions()
    local need = natural_content(w, h) + 2 * BB_JOURNAL.min_side + BB_JOURNAL.reserve_w
    -- getDesktopDimensions 返回**两个数**（宽, 高），不是表
    local screen_w = love.window.getDesktopDimensions()
    local target = math.min(screen_w, math.max(w, math.ceil(need)))
    if target <= w + 2 then
      sendInfoMessage(string.format(
        "决策日志：窗口已够宽（%d ≥ %d），不用加宽", w, math.ceil(need)), "BB.JOURNAL")
      return
    end
    love.window.setMode(target, h, {
      fullscreen = flags.fullscreen,
      fullscreentype = flags.fullscreentype,
      vsync = flags.vsync,
      msaa = flags.msaa,
      resizable = flags.resizable,
      borderless = flags.borderless,
      centered = flags.centered,
      display = flags.display,
    })
    sendInfoMessage(string.format(
      "决策日志：窗口 %dx%d → %dx%d（为面板腾出 %dpx）",
      w, h, target, h, BB_JOURNAL.reserve_w), "BB.JOURNAL")
  end)
  if not ok then
    sendInfoMessage("决策日志：自动加宽失败，面板会退化为覆盖显示：" .. tostring(err),
      "BB.JOURNAL")
  end
end

-- ------------------------------------------------------------------ 安装

function BB_JOURNAL.setup()
  -- 让游戏以为窗口比实际窄 `reserve` 像素：它会把内容排在左侧、画布也只建那么宽，
  -- 右侧因此空出一条真正的空白。
  --
  -- 注意两点：
  --   * 这里**不**覆盖 love.graphics.getWidth()，所以面板自己看到的仍是真实窗口
  --     尺寸，能正确地把面板摆在那条空白里；
  --   * 钩子本身只是换个函数，是安全的；真正动窗口要等第一帧（见下）。
  local game_resize = love.resize
  love.resize = function(w, h)
    local reserve = compute_reserve(w, h)
    BB_JOURNAL.reserve_now = reserve
    if not BB_JOURNAL._logged_reserve then
      BB_JOURNAL._logged_reserve = true
      sendInfoMessage(string.format(
        "决策日志：窗口 %dx%d，自然内容宽 %.0f，让出 %dpx 给面板",
        w, h, natural_content(w, h), reserve), "BB.JOURNAL")
    end
    return game_resize(w - reserve, h)
  end

  local love_draw = love.draw
  love.draw = function()
    -- 动窗口必须等第一帧：mod 是在 LÖVE 初始化**之前**加载的，那时
    -- love.window / love.graphics 还不能用——实测在 setup 里直接调用会把游戏
    -- 崩掉，连启动都完不成（Lovely 日志只到 "Initialization complete"）。
    if not BB_JOURNAL._applied then
      BB_JOURNAL._applied = true
      pcall(ensure_window)
      -- 加宽会触发一次 resize 事件（走上面的钩子）；这里再按当前尺寸主动应用
      -- 一次，以防窗口本来就不需要加宽、从而没有 resize 事件。
      pcall(function() love.resize(love.graphics.getDimensions()) end)
    end

    love_draw()
    -- 面板画在游戏之后。先复位变换/着色器/颜色：游戏的 draw 可能留下了
    -- 自己的画布与缩放，不复位的话面板会跟着被缩放、跑到画面外。
    local draw_ok, draw_err = pcall(draw)
    if not draw_ok then
      -- 面板绝不能把游戏搞崩：出错就自己关掉
      BB_JOURNAL.visible = false
      sendInfoMessage("决策日志面板出错，已自动关闭：" .. tostring(draw_err),
        "BB.JOURNAL")
    end
  end

  local love_keypressed = love.keypressed
  love.keypressed = function(key, ...)
    if key == BB_JOURNAL.toggle_key then
      BB_JOURNAL.toggle()
      return                              -- 吃掉这个键，不传给游戏
    end
    return love_keypressed(key, ...)
  end
end

return BB_JOURNAL
