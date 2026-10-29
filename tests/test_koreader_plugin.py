"""The KOReader plugin cannot run here, so these tests pin what can be checked without a device:
Lua syntax, the plugin metadata, and that every route and query parameter the plugin uses exists
in the server's protocol (src/lorekeeper/web/protocol.py)."""

import re
import shutil
import subprocess

import pytest

from .conftest import ROOT

PLUGIN = ROOT / "clients/koreader/lorekeeper.koplugin"
PROTOCOL = ROOT / "src/lorekeeper/web/protocol.py"
LUA_FILES = sorted(PLUGIN.glob("*.lua"))
LUA = shutil.which("lua") or shutil.which("luajit")
LUAC = shutil.which("luac")


def code_only(src: str) -> str:
    """Lua source without comments (block and line). Strings are kept."""
    src = re.sub(r"--\[(=*)\[.*?\]\1\]", "", src, flags=re.S)
    return re.sub(r"--[^\n]*", "", src)


def test_plugin_files_exist():
    names = {p.name for p in LUA_FILES}
    assert {"_meta.lua", "main.lua"} <= names


@pytest.mark.skipif(LUAC is None and LUA is None, reason="no Lua binary on PATH")
@pytest.mark.parametrize("path", LUA_FILES, ids=lambda p: p.name)
def test_lua_syntax(path):
    cmd = [LUAC, "-p", str(path)] if LUAC else [LUA, "-e", f"assert(loadfile({str(path)!r}))"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("path", LUA_FILES, ids=lambda p: p.name)
def test_lua_51_only(path):
    """KOReader runs LuaJIT (Lua 5.1). A newer luac accepts more than the device does."""
    code = code_only(path.read_text())
    for bad in (r"\bgoto\b", r"::\w+::", r"<const>", r"<close>", r"\butf8\.", r"\btable\.pack\b",
                r"\btable\.unpack\b", r"\bmath\.tointeger\b", r"\bstring\.pack\b"):
        assert not re.search(bad, code), f"{path.name}: {bad} is not Lua 5.1"


def test_meta_fields():
    src = (PLUGIN / "_meta.lua").read_text()
    assert re.search(r'name\s*=\s*"lorekeeper"', src)
    assert re.search(r"fullname\s*=\s*_\(", src)
    assert re.search(r"description\s*=\s*_\(", src)
    assert src.lstrip().startswith("local _ = require(\"gettext\")")
    assert re.search(r"return\s*\{", src)


# ---- the plugin against the server's protocol ------------------------------------------------

def server_routes() -> dict[str, set[str]]:
    """Route path -> query parameter names the server accepts there."""
    src = PROTOCOL.read_text()
    pos = re.search(r"class Position:.*?\) -> None:", src, re.S).group(0)
    position = set(re.findall(r"^\s+(\w+): Annotated\[", pos, re.M))
    lookup_sig = re.search(r"def lookup\((.*?)\n    \):", src, re.S).group(1)
    lookup = set(re.findall(r"^\s+(\w+): Annotated\[", lookup_sig, re.M))
    routes = {m: set() for m in re.findall(r'@router\.get\("([^"]+)"', src)}
    routes["/v0/lookup"] = lookup
    routes["/v0/indexes/{id}/view"] = position
    routes["/v0/indexes/{id}/entities"] = position | {"name"}
    return routes


def plugin_routes() -> dict[str, set[str]]:
    """Route path (with %s as {id}) -> query parameter names the plugin sends there."""
    src = code_only((PLUGIN / "lorekeeper_client.lua").read_text())
    templates = dict(re.findall(r'local (ROUTE_\w+)\s*=\s*"([^"]+)"', src))
    used: dict[str, set[str]] = {t.replace("%s", "{id}"): set() for t in templates.values()}
    for const, body in re.findall(r"(ROUTE_\w+)[^\n]*?qs\(\{(.*?)\}\)", src, re.S):
        names = re.findall(r'\{\s*"(\w+)"\s*,', body)
        used[templates[const].replace("%s", "{id}")] |= set(names)
    return used


def test_every_plugin_route_exists_on_the_server():
    server = server_routes()
    plugin = plugin_routes()
    assert plugin, "no routes found in the client"
    for route in plugin:
        assert route in server, f"plugin calls {route}, which the server does not serve"


def test_every_plugin_query_parameter_exists_on_the_server():
    server = server_routes()
    for route, params in plugin_routes().items():
        assert params <= server[route], f"{route}: server has no parameter {params - server[route]}"


def test_routes_are_only_defined_in_the_client():
    for path in LUA_FILES:
        if path.name == "lorekeeper_client.lua":
            continue
        assert not re.search(r"/v0|\.well-known", code_only(path.read_text())), \
            f"{path.name} builds a server path; routes belong in lorekeeper_client.lua"


def test_plugin_uses_the_documented_actions():
    used = plugin_routes()
    assert used["/v0/lookup"] >= {"fileHash"}
    assert used["/v0/indexes/{id}/view"] >= {"xpointer", "fileHash"}
    assert used["/v0/indexes/{id}/entities"] >= {"name", "xpointer", "fileHash"}


def test_only_xpointer_locators_are_sent():
    """SPEC 3.7: positions never come from percentages or page numbers. Only an XPointer goes out."""
    sent = set().union(*plugin_routes().values())
    assert not sent & {"progress", "spine", "word", "block", "cfi", "anchor", "percentage"}
    for path in LUA_FILES:
        code = code_only(path.read_text())
        assert not re.search(r"percent|progress|getPageCount|getCurrentPage|getLastPercent", code, re.I), \
            f"{path.name} mentions a percentage, progress or page number"


def test_views_are_not_cached():
    """Only capabilities, file hashes and the matched index are kept; never a view or entity answer."""
    main = code_only((PLUGIN / "main.lua").read_text())
    assert not re.search(r"self\.(view|views|cache|last_view)\b", main)
    assert not re.search(r'saveSetting\("(view|entities|story|cast)', main)


# ---- the pure helpers, when a Lua interpreter exists ----------------------------------------

@pytest.mark.skipif(LUA is None, reason="no Lua interpreter on PATH")
def test_format_helpers_run():
    script = r'''
package.path = ARGV_DIR .. "/?.lua;" .. package.path
local f = require("lorekeeper_format")
assert(f.clean_selection('  \226\128\156Holmes,\226\128\157 ') == "Holmes")
assert(f.clean_selection("") == nil)
assert(f.clean_selection(string.rep("a", 200)) == nil)
assert(f.without_possessive("Holmes's") == "Holmes")
assert(f.without_possessive("Holmes\226\128\153s") == "Holmes")
assert(f.without_possessive("Holmes") == nil)
local isbns = f.parse_isbns("urn:uuid:abc\nurn:isbn:978-0-14-043786-9\nisbn:0141439513\n12345")
assert(#isbns == 2 and isbns[1] == "9780140437869" and isbns[2] == "0141439513", table.concat(isbns, ","))
assert(#f.truncate(string.rep("\195\169", 100), 11) <= 14)
local names = { a = "Gabriel de Leon", b = "Forever King" }
assert(f.relation_sentence({ src = "a", dst = "b", label = "killed" }, names) == "Gabriel de Leon killed Forever King")
assert(f.relation_sentence({ src = "a", dst = "zzz", label = "killed" }, names) == nil)
local text = f.entity({ id = "a", name = "Gabriel de Leon", type = "person", aliases = {},
    profile = { id = "p", text = "A silversaint." },
    facets = { { id = "p", text = "A silversaint.", basis = "stated" }, { id = "f", text = "Wields a sword", basis = "inferred" } },
    relations = { { src = "a", dst = "b", label = "killed" } } }, names)
assert(text:find("A silversaint.", 1, true) and text:find("Wields a sword (inferred)", 1, true))
assert(text:find("Gabriel de Leon killed Forever King", 1, true))
local view = { entities = { { id = "a", name = "A", type = "person", facets = {} }, { id = "t", name = "T", type = "place", facets = {} } },
    recap = { id = "r", text = "It began.", book = false }, chapters = {} }
assert(f.character_count(view) == 1)
assert(f.story(view):find("Characters known so far: 1", 1, true))
assert(f.nothing_known("X"):find("Nothing known about", 1, true))
print("ok")
'''
    r = subprocess.run([LUA, "-e", f"ARGV_DIR={str(PLUGIN)!r}\n" + script], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr
