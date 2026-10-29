-- Lorekeeper for KOReader: a thin client for a self-hosted Lorekeeper server.
--
-- The server holds pre-built, spoiler-gated indexes. This plugin asks it two questions about the
-- book you are reading ("who is this?" and "story so far") and shows only what the server returns
-- for your current position. There is no AI here and no text model on the device.
--
-- SPOILER SAFETY (SPEC 3.7, 7.4, 8.4):
--   * Nothing is displayed that the server did not return for the position sent with the request.
--   * A view or entity answer is never cached and never reused after the position changes. Every
--     action asks again. Only position-independent facts are cached: server capabilities, and
--     which index belongs to this file.
--   * "Story so far" sends the XPointer of the START of the current page (document:getXPointer()).
--     In the default "finished" mode the server counts the block holding that XPointer as not yet
--     finished, so a page that begins mid-paragraph can only under-reveal.
--   * "Who is this?" sends the XPointer of the selection's own start (pos0), not the page start,
--     and relies on the server's inclusive default for that route: the reader is looking at it.
--     If KOReader gives no usable pos0, the page-start XPointer is sent instead, which is earlier
--     or equal, so it can only under-reveal.
--   * Positions are never derived from percentages or page numbers (SPEC 3.7).

local DataStorage = require("datastorage")
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local LuaSettings = require("luasettings")
local NetworkMgr = require("ui/network/manager")
local TextViewer = require("ui/widget/textviewer")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")
local _ = require("gettext")

local Client = require("lorekeeper_client")
local fmt = require("lorekeeper_format")

local Lorekeeper = WidgetContainer:extend{
    name = "lorekeeper",
    is_doc_only = false,
}

local SETTINGS_FILE = "lorekeeper.lua"
local HASH_CHUNK = 1024 * 1024

-- ---------------------------------------------------------------------------------------------
-- setup

function Lorekeeper:init()
    self.settings = LuaSettings:open(DataStorage:getSettingsDir() .. "/" .. SETTINGS_FILE)
    self.hashes = self.settings:readSetting("file_hashes", {}) -- path -> {size, mtime, hash}
    self.told = self.settings:readSetting("told_no_index", {})  -- file hash -> true
    self.caps = nil  -- discovery result for the current server (position-independent)
    self.book = nil  -- { hash, index_id, title, match } for the open book
    self.ui.menu:registerToMainMenu(self)
    self:registerHighlight()
end

-- "Who is this?" next to the dictionary in the selection menu.
function Lorekeeper:registerHighlight()
    if self.highlight_registered or not (self.ui and self.ui.highlight) then return end
    self.highlight_registered = true
    self.ui.highlight:addToHighlightDialog("12_lorekeeper", function(this)
        return {
            text = _("Who is this?"),
            callback = function()
                local sel = this.selected_text or {}
                local text, pos0 = sel.text, sel.pos0
                this:onClose()
                self:whoIsThis(text, pos0)
            end,
        }
    end)
end

function Lorekeeper:onReaderReady()
    self:registerHighlight()
    self.book = nil
    if not self:readable() or self:serverAddress() == "" then return end
    -- Quietly look the book up once, only if already online (never switch Wi-Fi on unasked).
    UIManager:scheduleIn(2, function()
        if self.ui and self.ui.document and NetworkMgr:isConnected() then
            self:findBook(true, function() end)
        end
    end)
end

function Lorekeeper:onCloseDocument()
    self.book = nil
    self.settings:flush()
end

-- Only reflowable EPUBs (CREngine) have the XPointers the protocol understands.
function Lorekeeper:readable()
    local doc = self.ui and self.ui.document
    return doc ~= nil and self.ui.rolling ~= nil and tostring(doc.file):lower():match("%.epub$") ~= nil
end

-- ---------------------------------------------------------------------------------------------
-- settings

function Lorekeeper:serverAddress()
    return self.settings:readSetting("server", "")
end

function Lorekeeper:token()
    return self.settings:readSetting("token", "")
end

function Lorekeeper:client()
    return Client:new(self:serverAddress(), self:token())
end

local function normalize_address(text)
    local s = fmt.trim(text or "")
    if s == "" or s == "http://" then return "" end
    if not s:lower():match("^https?://") then s = "http://" .. s end
    return (s:gsub("/+$", ""))
end

function Lorekeeper:askText(title, current, hint, on_save)
    local dialog
    dialog = InputDialog:new{
        title = title,
        input = current,
        input_hint = hint,
        buttons = {{
            { text = _("Cancel"), callback = function() UIManager:close(dialog) end },
            { text = _("Save"), is_enter_default = true, callback = function()
                local value = dialog:getInputText()
                UIManager:close(dialog)
                on_save(value)
            end },
        }},
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function Lorekeeper:setServer(touchmenu)
    local current = self:serverAddress()
    self:askText(_("Lorekeeper server address"), current ~= "" and current or "http://",
        "http://192.168.1.10:8765", function(value)
            self.settings:saveSetting("server", normalize_address(value))
            self.settings:flush()
            self.caps, self.book = nil, nil
            if touchmenu then touchmenu:updateItems() end
        end)
end

function Lorekeeper:setToken(touchmenu)
    self:askText(_("API token (leave empty for none)"), self:token(), "", function(value)
        self.settings:saveSetting("token", fmt.trim(value or ""))
        self.settings:flush()
        self.caps = nil
        if touchmenu then touchmenu:updateItems() end
    end)
end

function Lorekeeper:addToMainMenu(menu_items)
    local in_book = self:readable()
    local items = {
        {
            text_func = function()
                local a = self:serverAddress()
                return _("Server address: ") .. (a ~= "" and a or _("not set"))
            end,
            keep_menu_open = true,
            callback = function(touchmenu) self:setServer(touchmenu) end,
        },
        {
            text_func = function()
                return _("API token: ") .. (self:token() ~= "" and _("set") or _("none"))
            end,
            keep_menu_open = true,
            callback = function(touchmenu) self:setToken(touchmenu) end,
        },
        {
            text = _("Spoiler-safe only"),
            keep_menu_open = true,
            callback = function()
                UIManager:show(InfoMessage:new{ text = _(
                    "Lorekeeper only shows what the server returns for the page you are on. " ..
                    "Your reading position is sent to your own server with each question and is not stored by this plugin.") })
            end,
        },
    }
    if in_book then
        table.insert(items, 1, {
            text = _("Story so far"),
            callback = function() self:storySoFar() end,
        })
        table.insert(items, 4, {
            text = _("Look up this book again"),
            keep_menu_open = true,
            callback = function()
                self.book, self.caps = nil, nil
                self:findBook(false, function(book)
                    if book then
                        UIManager:show(InfoMessage:new{ text = _("Index found: ") .. (book.title or book.index_id) })
                    end
                end)
            end,
        })
    end
    menu_items.lorekeeper = {
        text = _("Lorekeeper"),
        sorting_hint = "more_tools",
        sub_item_table = items,
    }
end

-- ---------------------------------------------------------------------------------------------
-- running network work without a silent freeze

-- Shows "Asking Lorekeeper..." while `work` (blocking, may call the network) runs after the
-- screen has repainted, then calls done(...) with whatever work returned.
function Lorekeeper:ask(work, done, label)
    NetworkMgr:runWhenConnected(function()
        local msg = InfoMessage:new{ text = label or _("Asking Lorekeeper…"), dismissable = false }
        UIManager:show(msg)
        UIManager:forceRePaint()
        UIManager:scheduleIn(0.2, function()
            local results = { pcall(work) }
            UIManager:close(msg)
            if not results[1] then
                logger.warn("Lorekeeper: error:", results[2])
                UIManager:show(InfoMessage:new{ text = _("Lorekeeper hit an unexpected error.") })
                return
            end
            done(unpack(results, 2, 4))
        end)
    end)
end

local PROBLEM_TEXT = {
    config = _("Set the Lorekeeper server address in the plugin menu first."),
    network = _("Could not reach the Lorekeeper server."),
    auth = _("The server needs an API token. Set it in the Lorekeeper menu."),
    notfound = _("The server has no index for this book any more."),
    edition = _("This server doesn't hold your edition of this book, so it can't tell where you are."),
    bad = _("The server did not accept the question."),
    http = _("The Lorekeeper server answered with an error."),
}

function Lorekeeper:showProblem(kind, detail)
    local text = PROBLEM_TEXT[kind] or PROBLEM_TEXT.http
    if (kind == "bad" or kind == "http" or kind == "network") and detail and detail ~= "" then
        text = text .. "\n" .. fmt.truncate(tostring(detail), 160)
    end
    UIManager:show(InfoMessage:new{ text = text })
end

-- ---------------------------------------------------------------------------------------------
-- identifying the book (position-independent, so it may be cached)

-- sha256 of the EPUB file, cached by path, size and modification time.
function Lorekeeper:fileHash()
    local path = self.ui.document.file
    local attr = lfs.attributes(path)
    if not attr then return nil end
    local cached = self.hashes[path]
    if cached and cached.size == attr.size and cached.mtime == attr.modification then
        return cached.hash
    end
    local ok, sha2 = pcall(require, "ffi/sha2")
    if not ok or not sha2.sha256 then return nil end
    local f = io.open(path, "rb")
    if not f then return nil end
    local hash
    local streamed = pcall(function()
        local append = sha2.sha256() -- no argument: returns a feeder; call it with nil for the digest
        while true do
            local chunk = f:read(HASH_CHUNK)
            if not chunk then break end
            append(chunk)
        end
        hash = append()
    end)
    f:close()
    if not streamed or type(hash) ~= "string" then
        local g = io.open(path, "rb")
        if not g then return nil end
        local data = g:read("*a")
        g:close()
        hash = sha2.sha256(data)
    end
    hash = type(hash) == "string" and hash:lower() or nil
    if not (hash and hash:match("^%x+$") and #hash == 64) then return nil end
    self.hashes[path] = { size = attr.size, mtime = attr.modification, hash = hash }
    self.settings:flush()
    return hash
end

-- ISBNs from the document's metadata, when KOReader exposes any.
function Lorekeeper:isbns()
    local doc = self.ui.document
    local props = (self.ui.doc_props) or (doc.getProps and doc:getProps()) or {}
    return fmt.parse_isbns(props.identifiers)
end

-- Capabilities of the server, asked once per server address and kept. Sends GET /.well-known/lorekeeper.
function Lorekeeper:capabilities()
    if self.caps then return self.caps end
    local res, kind, detail = self:client():discovery()
    if not res then return nil, kind, detail end
    local has = {}
    for _i, c in ipairs(res.capabilities or {}) do has[c] = true end
    self.caps = { has = has, bearer = res.auth and res.auth.type == "bearer", name = res.name }
    return self.caps
end

-- Finds the index for the open book and remembers it in self.book. `quiet` suppresses error
-- popups (used for the automatic lookup on open). done(book) gets nil when there is none.
function Lorekeeper:findBook(quiet, done)
    if self.book then return done(self.book) end
    if not self:readable() then
        if not quiet then UIManager:show(InfoMessage:new{ text = _("Lorekeeper works with EPUB books.") }) end
        return done(nil)
    end
    if self:serverAddress() == "" then
        if not quiet then self:showProblem("config") end
        return done(nil)
    end
    local function work()
        local caps, kind, detail = self:capabilities()
        if not caps then return nil, kind, detail end
        if caps.bearer and self:token() == "" then return nil, "auth" end
        if not (caps.has.lookup and caps.has.view and caps.has.entities) then
            return nil, "http", "this server does not offer lookup, view and entities"
        end
        if not caps.has["view-cfi"] then
            return nil, "http", "this server cannot map KOReader positions (no view-cfi)"
        end
        local hash = self:fileHash()
        if not hash then return nil, "http", "could not compute the file hash" end
        local res, k, d = self:client():lookup(hash, nil)
        if res and #(res.results or {}) == 0 then
            for _i, isbn in ipairs(self:isbns()) do
                res, k, d = self:client():lookup(hash, isbn)
                if not res or #(res.results or {}) > 0 then break end
            end
        end
        if not res then return nil, k, d end
        local best = (res.results or {})[1]
        if not best then return false, hash end
        return { hash = hash, index_id = best.id, title = best.title, match = best.match }
    end
    -- work() returns a book table, or nil plus a problem kind and detail, or false plus the file
    -- hash when the server has no index for the book.
    local function finish(book, kind, detail)
        if book == nil then
            if not quiet then self:showProblem(kind, detail) end
            return done(nil)
        end
        if book == false then
            -- No index: say so once per book (file hash), never repeatedly.
            local hash = kind
            if not self.told[hash] or not quiet then
                self.told[hash] = true
                self.settings:flush()
                UIManager:show(InfoMessage:new{ text = _("No Lorekeeper index for this book on ") .. self:serverAddress() })
            end
            return done(nil)
        end
        self.book = book
        done(book)
    end
    if quiet then
        local ok, book, kind, detail = pcall(work)
        if ok then finish(book, kind, detail) else done(nil) end
    else
        self:ask(work, finish)
    end
end

-- The XPointer of the start of the current page (see the spoiler notes at the top).
function Lorekeeper:pageXPointer()
    local ok, xp = pcall(function() return self.ui.document:getXPointer() end)
    if ok and type(xp) == "string" and xp:sub(1, 1) == "/" then return xp end
    return nil
end

-- ---------------------------------------------------------------------------------------------
-- the two actions

-- "Who is this?": selected text + the selection's own XPointer.
function Lorekeeper:whoIsThis(text, pos0)
    local name = fmt.clean_selection(text)
    if not name then
        UIManager:show(InfoMessage:new{ text = _("Select a name, a few words at most.") })
        return
    end
    local xp = (type(pos0) == "string" and pos0:sub(1, 1) == "/") and pos0 or self:pageXPointer()
    if not xp then
        UIManager:show(InfoMessage:new{ text = _("Lorekeeper could not tell where you are in the book.") })
        return
    end
    self:findBook(false, function(book)
        if not book then return end
        local function work()
            local client = self:client()
            -- GET entities?name&xpointer&fileHash; retried once without a trailing 's.
            local res, kind, detail = client:entities(book.index_id, name, xp, book.hash)
            local shown = name
            local alt = fmt.without_possessive(name)
            if res and #(res.entities or {}) == 0 and alt then
                res, kind, detail = client:entities(book.index_id, alt, xp, book.hash)
                shown = alt
            end
            if not res then return nil, kind, detail end
            local names = {}
            local wants_names = false
            for _i, e in ipairs(res.entities or {}) do
                if #(e.relations or {}) > 0 then wants_names = true end
            end
            if wants_names then
                -- GET view?xpointer&fileHash&inclusive=true at the same position, only to turn the
                -- entity ids in relations into names the reader may already see.
                local view = client:view(book.index_id, xp, book.hash, true)
                names = fmt.name_map(view)
            end
            return res, shown, names
        end
        -- work() gives res, shown, names; on failure nil, problem kind, detail
        self:ask(work, function(res, shown, names)
            if not res then return self:showProblem(shown, names) end
            if #(res.entities or {}) == 0 then
                UIManager:show(InfoMessage:new{ text = fmt.nothing_known(name) })
                return
            end
            UIManager:show(TextViewer:new{
                title = res.entities[1].name,
                text = fmt.entities_text(res.entities, names),
            })
        end)
    end)
end

-- "Story so far": the view at the start of the current page, finished mode.
function Lorekeeper:storySoFar()
    local xp = self:pageXPointer()
    if not xp then
        UIManager:show(InfoMessage:new{ text = _("Lorekeeper could not tell where you are in the book.") })
        return
    end
    self:findBook(false, function(book)
        if not book then return end
        -- GET view?xpointer&fileHash (finished mode); the answer is shown once and not kept.
        local function work() return self:client():view(book.index_id, xp, book.hash) end
        self:ask(work, function(view, kind, detail)
            if not view then return self:showProblem(kind, detail) end
            local viewer
            viewer = TextViewer:new{
                title = _("Story so far"),
                text = fmt.story(view),
                buttons_table = {{
                    {
                        text = _("Cast"),
                        callback = function()
                            -- Same answer as above (same page); no new request, nothing newer.
                            UIManager:show(TextViewer:new{ title = _("Cast"), text = fmt.cast(view) })
                        end,
                    },
                }},
            }
            UIManager:show(viewer)
        end)
    end)
end

return Lorekeeper
