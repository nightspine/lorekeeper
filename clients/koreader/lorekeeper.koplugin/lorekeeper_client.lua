-- HTTP client for the Lorekeeper read-only protocol (SPEC section 8).
-- Every function that touches the network says in one line what it sends.
-- Blocking calls: run them from a scheduled callback while an "Asking Lorekeeper" message
-- is shown (see main.lua), never from a paint or input handler.

local ltn12 = require("ltn12")
local socket = require("socket")
local socketutil = require("socketutil")
local logger = require("logger")

local http = require("socket.http")

local json
do
    local ok, rapidjson = pcall(require, "rapidjson")
    if ok then
        json = { decode = rapidjson.decode, null = rapidjson.null }
    else
        json = { decode = require("json").decode }
    end
end

local BLOCK_TIMEOUT, TOTAL_TIMEOUT = 10, 30

-- Route templates. %s is the url-encoded index id. The test suite checks these against the
-- server's routes (src/lorekeeper/web/protocol.py).
local ROUTE_DISCOVERY = "/.well-known/lorekeeper"
local ROUTE_LOOKUP = "/v0/lookup"
local ROUTE_VIEW = "/v0/indexes/%s/view"
local ROUTE_ENTITIES = "/v0/indexes/%s/entities"

local Client = {}
Client.__index = Client

local function urlencode(s)
    return (tostring(s):gsub("[^%w%-_%.~]", function(c) return string.format("%%%02X", c:byte()) end))
end

-- Query string from an ordered list of {name, value} pairs; nil values are skipped.
local function qs(pairs_list)
    local out = {}
    for _i, kv in ipairs(pairs_list) do
        if kv[2] ~= nil then out[#out + 1] = kv[1] .. "=" .. urlencode(kv[2]) end
    end
    return #out > 0 and ("?" .. table.concat(out, "&")) or ""
end

-- JSON null decodes to a lightuserdata that is truthy in Lua; treat it as absent.
local function scrub(value)
    if json.null and value == json.null then return nil end
    if type(value) == "table" then
        for k, v in pairs(value) do value[k] = scrub(v) end
    end
    return value
end

local function decode(body)
    if type(body) ~= "string" or body == "" then return nil end
    local ok, value = pcall(json.decode, body)
    if not ok or type(value) ~= "table" then return nil end
    return scrub(value)
end

function Client:new(server, token)
    return setmetatable({ server = (server or ""):gsub("/+$", ""), token = token }, Client)
end

-- Performs one GET. Returns the decoded JSON table, or nil, kind, detail where kind is one of
-- "config", "network", "auth" (401), "notfound" (404), "edition" (409), "bad" (400), "http".
function Client:get(path)
    if self.server == "" or not self.server:lower():match("^https?://") then
        return nil, "config", "no server address set"
    end
    local url = self.server .. path
    local headers = { ["Accept"] = "application/json" }
    if self.token and self.token ~= "" then headers["Authorization"] = "Bearer " .. self.token end
    local sink = {}
    local request = { url = url, method = "GET", headers = headers, sink = ltn12.sink.table(sink) }
    local requester = http.request
    if url:lower():match("^https://") then
        local ok, https = pcall(require, "ssl.https")
        if not ok then return nil, "network", "this device cannot do https" end
        requester = https.request
    end
    socketutil:set_timeout(BLOCK_TIMEOUT, TOTAL_TIMEOUT)
    local ok, result, code, _headers, status = pcall(requester, request)
    socketutil:reset_timeout()
    if not ok then return nil, "network", tostring(result) end
    -- table-form requests return 1, code, headers, status; a transport failure returns nil, err
    if type(code) ~= "number" then
        logger.dbg("Lorekeeper: network error:", status or code or result)
        return nil, "network", tostring(code or status or "no answer")
    end
    local body = decode(table.concat(sink))
    if code >= 200 and code < 300 then
        if not body then return nil, "http", "the server sent something that is not JSON" end
        return body
    end
    local detail = body and (body.detail or body.title) or nil
    local kind = ({ [400] = "bad", [401] = "auth", [404] = "notfound", [409] = "edition" })[code] or "http"
    return nil, kind, detail or ("HTTP " .. tostring(code))
end

-- Sends nothing but the request line: GET /.well-known/lorekeeper (public, no token).
function Client:discovery()
    local saved = self.token
    self.token = nil
    local res, kind, detail = self:get(ROUTE_DISCOVERY)
    self.token = saved
    if res and not res.lorekeeper then return nil, "http", "that address does not look like a Lorekeeper server" end
    return res, kind, detail
end

-- Sends the book's file hash (sha256) or an ISBN from its metadata. Never a position.
-- GET /v0/lookup?fileHash=...  or  GET /v0/lookup?isbn=...
function Client:lookup(file_hash, isbn)
    return self:get(ROUTE_LOOKUP .. qs({ { "fileHash", file_hash }, { "isbn", isbn } }))
end

-- Sends the reader's position as a CREngine XPointer plus the edition's file hash.
-- GET /v0/indexes/{id}/view?xpointer=...&fileHash=...   (finished mode: the default)
-- `inclusive` is sent only when given (the selection fallback needs the other mode).
function Client:view(index_id, xpointer, file_hash, inclusive)
    return self:get(ROUTE_VIEW:format(urlencode(index_id)) .. qs({
        { "xpointer", xpointer }, { "fileHash", file_hash },
        { "inclusive", inclusive and "true" or nil },
    }))
end

-- Sends the selected text, the XPointer of the selection start and the file hash.
-- GET /v0/indexes/{id}/entities?name=...&xpointer=...&fileHash=...   (inclusive by default)
function Client:entities(index_id, name, xpointer, file_hash)
    return self:get(ROUTE_ENTITIES:format(urlencode(index_id)) .. qs({
        { "name", name }, { "xpointer", xpointer }, { "fileHash", file_hash },
    }))
end

return Client
