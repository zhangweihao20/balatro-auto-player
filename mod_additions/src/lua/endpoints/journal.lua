-- src/lua/endpoints/journal.lua
--
-- 把机器人这一步的决策交给屏幕上的日志面板。
--
-- 这是本项目对 balatrobot mod 的**附加**端点，不改动 mod 原有逻辑。
-- 仓库里的对应副本在 mod_additions/src/lua/endpoints/journal.lua。
--
-- 参数按"已经排好版的字符串 + 一个数字字典"给，而不是让 Lua 去解析结构：
-- 格式化留在 Python 侧（那里有全部上下文），Lua 只负责画。

---@class Request.Endpoint.Journal.Params
---@field kind string?        play / discard / buy / sell / reroll / use / pack / blind / flow
---@field action string?      RPC 方法名，例如 play
---@field why string?         为什么这样做（一句话）
---@field detail string?      做了什么（例如 "出牌 [0, 1, 2]"）
---@field numbers table?      当时的数字，例如 {概率="26%", 期望="1240"}
---@field frame table?        局面快照，例如 {ante=5, round=14, money=21, need=6528}
---@field step integer?       第几步
---@field clear boolean?      先清空面板（每局开始时用）

---@type Endpoint
return {

  name = "journal",

  description = "把一步决策写到屏幕上的决策日志面板",

  schema = {
    kind = { type = "string", required = false, description = "动作类别" },
    action = { type = "string", required = false, description = "RPC 方法名" },
    why = { type = "string", required = false, description = "为什么这样做" },
    detail = { type = "string", required = false, description = "做了什么" },
    numbers = { type = "table", required = false, description = "当时的数字" },
    frame = { type = "table", required = false, description = "局面快照" },
    step = { type = "integer", required = false, description = "第几步" },
    clear = { type = "boolean", required = false, description = "先清空面板" },
  },

  -- 任何状态下都能记录（MENU 里也可以先清空）
  requires_state = nil,

  ---@param args Request.Endpoint.Journal.Params
  ---@param send_response fun(response: Response.Endpoint)
  execute = function(args, send_response)
    if BB_JOURNAL == nil then
      send_response({
        message = "决策日志面板没有加载（检查 balatrobot.lua 是否加载了 src/lua/ui/journal.lua）",
        name = BB_ERROR_NAMES.INTERNAL_ERROR,
      })
      return
    end

    if args.clear then
      BB_JOURNAL.clear()
    end

    -- 注意 Lua 里**空字符串是真值**：`if args.action then` 对 `action = ""` 也成立。
    -- 所以必须显式判空，否则"只清空面板"的调用会插进一条空记录。
    local function has_text(value)
      return type(value) == "string" and value ~= ""
    end

    if has_text(args.why) or has_text(args.detail) or has_text(args.action) then
      BB_JOURNAL.add({
        kind = args.kind,
        action = args.action,
        why = args.why,
        detail = args.detail,
        numbers = args.numbers,
        frame = args.frame,
        step = args.step,
      })
    end

    send_response({
      success = true,
      count = #BB_JOURNAL.records,
    })
  end,
}
