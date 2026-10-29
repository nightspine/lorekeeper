-- Pure text helpers for the Lorekeeper plugin. No KOReader modules are required here,
-- so this file can be exercised with a plain Lua interpreter.
--
-- Spoiler rule (SPEC 7.4): everything formatted here comes from a server response for the
-- reader's current position. Nothing is added, counted or hinted at beyond what was returned.

local M = {}

local BULLET = "\226\128\162 " -- "• "

-- Quote and dash characters (UTF-8) that are trimmed from the edges of a selection.
local EDGE = {
    "\226\128\156", "\226\128\157", "\226\128\152", "\226\128\153", -- curly double and single quotes
    "\226\128\148", "\226\128\147", "\226\128\166",                 -- em dash, en dash, ellipsis
    "\194\171", "\194\187",                                         -- guillemets
}

local MAX_NAME_BYTES = 80

local function trim(s)
    return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end
M.trim = trim

local function strip_edges(s)
    local changed = true
    while changed do
        changed = false
        -- ASCII punctuation at both edges, then the typographic characters in EDGE.
        local t = trim(s):gsub("^%p+", "")
        t = t:gsub("%p+$", "")
        for _i, e in ipairs(EDGE) do
            if t:sub(1, #e) == e then t = t:sub(#e + 1) end
            if #t >= #e and t:sub(-#e) == e then t = t:sub(1, #t - #e) end
        end
        t = trim(t)
        if t ~= s then s = t; changed = true end
    end
    return s
end

-- A selection cleaned for a name lookup: trimmed, edge punctuation removed.
-- Returns nil when it is empty or too long to be a name.
function M.clean_selection(text)
    if type(text) ~= "string" then return nil end
    local s = strip_edges((text:gsub("%s+", " ")))
    if s == "" or #s > MAX_NAME_BYTES then return nil end
    return s
end

-- "Holmes's" -> "Holmes" (also with the typographic apostrophe). nil when there is no possessive.
function M.without_possessive(name)
    local base = name:match("^(.-)'s$") or name:match("^(.-)\226\128\153s$")
    if base and base ~= "" then return base end
    return nil
end

-- Cut to at most `max` bytes without splitting a UTF-8 character, adding an ellipsis.
function M.truncate(s, max)
    if #s <= max then return s end
    local cut = max
    while cut > 0 do
        local b = s:byte(cut + 1)
        if not b or b < 0x80 or b >= 0xC0 then break end
        cut = cut - 1
    end
    return trim(s:sub(1, cut)) .. "\226\128\166"
end

-- ISBN-10 and ISBN-13 values found in a metadata "identifiers" string (newline, comma or
-- semicolon separated, possibly "urn:isbn:" or "isbn:" prefixed). Digits only, no hyphens.
function M.parse_isbns(identifiers)
    local out, seen = {}, {}
    if type(identifiers) ~= "string" then return out end
    for token in identifiers:gmatch("[^\n,;%s]+") do
        local t = token:lower():gsub("^urn:", ""):gsub("^isbn:?", "")
        local digits = t:gsub("%-", "")
        local ok13 = #digits == 13 and digits:match("^97[89]%d+$") ~= nil
        local ok10 = #digits == 10 and digits:match("^%d%d%d%d%d%d%d%d%d[%dx]$") ~= nil
        if (ok13 or ok10) and not seen[digits] then
            seen[digits] = true
            out[#out + 1] = digits:upper()
        end
    end
    return out
end

local TYPE_LABEL = {
    person = "Person", creature = "Creature", place = "Place", group = "Group",
    object = "Object", concept = "Concept", term = "Term",
}

function M.type_label(t)
    return TYPE_LABEL[t] or "Entry"
end

local function is_person_like(t)
    return t == "person" or t == "creature"
end

-- Names by entity id, from a view's entities list.
function M.name_map(view)
    local names = {}
    if type(view) == "table" and type(view.entities) == "table" then
        for _i, e in ipairs(view.entities) do
            if e.id and e.name then names[e.id] = e.name end
        end
    end
    return names
end

-- "Gabriel de Leon killed Forever King". A relation whose other end has no name in `names`
-- is left out: an id is never shown.
function M.relation_sentence(r, names)
    local src, dst = names[r.src], names[r.dst]
    if not (src and dst and r.label and r.label ~= "") then return nil end
    return src .. " " .. r.label .. " " .. dst
end

-- One entity from the entities route, as plain text. `names` maps ids to names for relations.
function M.entity(e, names)
    local lines = { e.name .. "  (" .. M.type_label(e.type):lower() .. ")" }
    if type(e.aliases) == "table" and #e.aliases > 0 then
        lines[#lines + 1] = "Also: " .. table.concat(e.aliases, ", ")
    end
    local seen = {}
    if type(e.profile) == "table" and e.profile.text then
        lines[#lines + 1] = ""
        lines[#lines + 1] = e.profile.text
        seen[e.profile.id or e.profile.text] = true
        seen[e.profile.text] = true
    end
    local bullets = {}
    for _i, f in ipairs(e.facets or {}) do
        if not (seen[f.id] or seen[f.text]) then
            bullets[#bullets + 1] = BULLET .. f.text .. (f.basis == "inferred" and " (inferred)" or "")
        end
    end
    if #bullets > 0 then
        lines[#lines + 1] = ""
        for _i, b in ipairs(bullets) do lines[#lines + 1] = b end
    end
    local rels = {}
    for _i, r in ipairs(e.relations or {}) do
        local s = M.relation_sentence(r, names or {})
        if s then rels[#rels + 1] = BULLET .. s end
    end
    if #rels > 0 then
        lines[#lines + 1] = ""
        lines[#lines + 1] = "Connections"
        for _i, s in ipairs(rels) do lines[#lines + 1] = s end
    end
    return table.concat(lines, "\n")
end

-- The whole answer to "Who is this?", several matches separated by a rule.
function M.entities_text(entities, names)
    local parts = {}
    for _i, e in ipairs(entities) do parts[#parts + 1] = M.entity(e, names) end
    parts[#parts + 1] = "\nAs of your current page."
    return table.concat(parts, "\n\n----\n\n")
end

function M.nothing_known(name)
    return "Nothing known about \226\128\156" .. name .. "\226\128\157 yet at this point in the book."
end

-- Number of characters (people and creatures) in a view.
function M.character_count(view)
    local n = 0
    for _i, e in ipairs(view.entities or {}) do
        if is_person_like(e.type) then n = n + 1 end
    end
    return n
end

-- The story so far: the latest recap, else the visible chapter summaries, then the count line.
function M.story(view)
    local parts = {}
    if type(view.recap) == "table" and view.recap.text and view.recap.text ~= "" then
        parts[#parts + 1] = view.recap.text
    else
        for _i, c in ipairs(view.chapters or {}) do
            if c.text and c.text ~= "" then
                parts[#parts + 1] = (c.title and c.title ~= "" and (c.title .. "\n") or "") .. c.text
            end
        end
    end
    if #parts == 0 then parts[1] = "Nothing to recap yet at this point in the book." end
    local n = M.character_count(view)
    parts[#parts + 1] = string.format("Characters known so far: %d.\nAs of your current page.", n)
    return table.concat(parts, "\n\n")
end

-- The cast: one line per visible entry, people and creatures first.
function M.cast(view)
    local rank = { person = 1, creature = 2, group = 3, place = 4, object = 5, concept = 6, term = 7 }
    local list = {}
    for _i, e in ipairs(view.entities or {}) do list[#list + 1] = e end
    table.sort(list, function(a, b)
        local ra, rb = rank[a.type] or 9, rank[b.type] or 9
        if ra ~= rb then return ra < rb end
        return a.name < b.name
    end)
    if #list == 0 then return "Nobody known yet at this point in the book." end
    local lines = {}
    for _i, e in ipairs(list) do
        local line = (e.profile and e.profile.text) or (e.facets and e.facets[1] and e.facets[1].text) or ""
        line = M.truncate((line:gsub("%s+", " ")), 120)
        lines[#lines + 1] = BULLET .. e.name .. " (" .. M.type_label(e.type):lower() .. ")"
            .. (line ~= "" and (": " .. line) or "")
    end
    lines[#lines + 1] = "\nAs of your current page."
    return table.concat(lines, "\n")
end

return M
