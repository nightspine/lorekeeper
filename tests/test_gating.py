"""Spoiler properties asserted directly, independent of the generated conformance views."""

import json

import pytest

from lorekeeper.gating import Position, view


def _ent(v, eid):
    return next((e for e in v["entities"] if e["id"] == eid), None)


def _facet_ids(v, eid):
    e = _ent(v, eid)
    return [f["id"] for f in e["facets"]] if e else []


def test_nothing_before_first_anchor(scarlet, series):
    v = view([scarlet], Position("study-in-scarlet", None), series)
    assert v["entities"] == [] and v["events"] == [] and v["recap"] is None


def test_entity_hidden_until_named(scarlet, series):
    v = view([scarlet], Position("study-in-scarlet", "ch1.para12"), series)
    assert _ent(v, "holmes") is None  # described by Stamford, not yet named
    v = view([scarlet], Position("study-in-scarlet", "ch1.para14"), series)
    assert _ent(v, "holmes")["name"] == "Sherlock Holmes"


def test_reveal_is_inclusive_of_the_anchor(scarlet, series):
    assert "sis.holmes.13" not in _facet_ids(view([scarlet], Position("study-in-scarlet", "ch2.para21"), series), "holmes")
    assert "sis.holmes.13" in _facet_ids(view([scarlet], Position("study-in-scarlet", "ch2.para24"), series), "holmes")


def test_replaced_facet_moves_to_history(scarlet, series):
    before = view([scarlet], Position("study-in-scarlet", "ch2.para24"), series)
    assert "sis.holmes.8" in _facet_ids(before, "holmes")
    after = view([scarlet], Position("study-in-scarlet", "ch2.para28"), series)
    holmes = _ent(after, "holmes")
    assert "sis.holmes.8" not in [f["id"] for f in holmes["facets"]]
    assert [f["id"] for f in holmes["history"]] == ["sis.holmes.p1", "sis.holmes.8"]   # the first profile, then the replaced fact


def test_display_name_follows_latest_primary(scarlet, series):
    v = view([scarlet], Position("study-in-scarlet", "ch1.para60"), series)
    assert _ent(v, "baker-street")["name"] == "Baker Street"
    v = view([scarlet], Position("study-in-scarlet", "ch2.para01"), series)
    e = _ent(v, "baker-street")
    assert e["name"] == "221B Baker Street" and e["aliases"] == ["Baker Street"]


def test_later_book_never_leaks(scarlet, four, series):
    v = view([scarlet, four], Position("study-in-scarlet", "ch2.para51"), series)
    assert all(f["book"] == "study-in-scarlet" for e in v["entities"] for f in e["facets"])
    assert _ent(v, "mary-morstan") is None


def test_earlier_book_merges_when_finished(scarlet, four, series):
    v = view([scarlet, four], Position("sign-of-four", "ch1.para01"), series)
    assert _ent(v, "watson")["name"] == "John H. Watson"
    assert "sis.holmes.17" in _facet_ids(v, "holmes")


def test_unfinished_earlier_book_is_hidden(scarlet, four, series):
    v = view([scarlet, four], Position("sign-of-four", "ch1.para17"), series, finished=set())
    assert all(f["book"] == "sign-of-four" for e in v["entities"] for f in e["facets"])
    assert _ent(v, "watson")["name"] == "Watson"


def test_cross_book_replacement(scarlet, four, series):
    v = view([scarlet, four], Position("sign-of-four", "ch1.para17"), series)
    assert "sis.rel.watson-holmes.2" in [r["id"] for r in v["relationHistory"]]
    assert "sof.rel.watson-holmes" in [r["id"] for r in v["relations"]]


def test_carried_used_only_without_origin_index(scarlet, four, series):
    alone = view([four], Position("sign-of-four", "ch1.para17"), series)
    seen = lambda v: [f["id"] for f in _ent(v, "holmes")["facets"] + _ent(v, "holmes")["history"]]  # noqa: E731
    assert "sof.carried.holmes.1" in seen(alone)    # a profile by now superseded by book 2's own
    both = view([scarlet, four], Position("sign-of-four", "ch1.para17"), series)
    assert "sof.carried.holmes.1" not in seen(both)


def test_relation_needs_both_endpoints(scarlet, series):
    v = view([scarlet], Position("study-in-scarlet", "ch1.para05"), series)
    ids = {e["id"] for e in v["entities"]}
    assert all(r["src"] in ids and r["dst"] in ids for r in v["relations"])


def test_coverage_exhausted(scarlet, series):
    assert not view([scarlet], Position("study-in-scarlet", "ch2.para50"), series)["coverage"]["exhausted"]
    assert view([scarlet], Position("study-in-scarlet", "ch2.para51"), series)["coverage"]["exhausted"]


def test_unknown_anchor_rejected(scarlet, series):
    with pytest.raises(ValueError):
        view([scarlet], Position("study-in-scarlet", "nope"), series)


def _profile(v, eid):
    p = _ent(v, eid)["profile"]
    return p and p["id"]


def test_profile_follows_position(scarlet, series):
    at = lambda a: view([scarlet], Position("study-in-scarlet", a), series)  # noqa: E731
    assert _profile(at("ch1.para14"), "holmes") is None          # named, not yet described
    assert _profile(at("ch1.para18"), "holmes") == "sis.holmes.p1"
    later = at("ch2.para28")
    assert _profile(later, "holmes") == "sis.holmes.p2"
    holmes = _ent(later, "holmes")
    assert "sis.holmes.p1" in [f["id"] for f in holmes["history"]]
    assert not any(f["id"].startswith("sis.holmes.p") for f in holmes["facets"])   # profiles never list as facts
    assert _profile(at("ch2.para24"), "holmes") == "sis.holmes.p1"   # the later profile can't leak early


def test_profile_inherited_from_earlier_book(scarlet, four, series):
    start = view([scarlet, four], Position("sign-of-four", "ch1.para05"), series)
    assert _ent(start, "holmes")["profile"]["book"] == "study-in-scarlet"
    assert _profile(start, "watson") == "sis.watson.p1"
    on = view([scarlet, four], Position("sign-of-four", "ch1.para12"), series)
    assert _profile(on, "holmes") == "sof.holmes.p1"              # book 2 overrides
    assert _profile(on, "watson") == "sis.watson.p1"              # still inherited
    # Not finished book 1: nothing of it is inherited.
    unread = view([scarlet, four], Position("sign-of-four", "ch1.para05"), series, finished=set())
    assert _profile(unread, "holmes") is None


def test_profile_inherited_from_carried_entry(four, series):
    alone = view([four], Position("sign-of-four", "ch1.para05"), series)
    assert _profile(alone, "holmes") == "sof.carried.holmes.1"
    assert _profile(view([four], Position("sign-of-four", "ch1.para12"), series), "holmes") == "sof.holmes.p1"


def test_the_view_carries_an_event_kind_only_when_the_index_has_one(scarlet, series):
    v = view([scarlet], Position("study-in-scarlet", "ch2.para51"), series)
    kinds = {ev["id"]: ev.get("kind") for ev in v["events"]}
    assert kinds["sis.ev.meeting"] == "turn" and kinds["sis.ev.sergeant"] == "minor"
    assert "kind" not in next(ev for ev in v["events"] if ev["id"] == "sis.ev.maiwand")


def test_an_event_setting_appears_only_with_its_event(scarlet, series):
    # Spoiler property: no setting text and no place link for an event past the position.
    early = view([scarlet], Position("study-in-scarlet", "ch1.para05"), series)
    assert [ev["id"] for ev in early["events"]] == ["sis.ev.maiwand", "sis.ev.criterion"]
    assert {ev["id"]: ev["setting"] for ev in early["events"]}["sis.ev.criterion"] == \
        {"text": "the Criterion Bar", "place": "criterion-bar"}
    blob = repr(early)
    for later in ("lofty chamber", "221B Baker Street", "breakfast table"):
        assert later not in blob
    late = view([scarlet], Position("study-in-scarlet", "ch2.para24"), series)
    by_id = {ev["id"]: ev for ev in late["events"]}
    assert by_id["sis.ev.article"]["setting"] == {"text": "the breakfast table"}   # no place: no link
    assert all(ev["setting"].get("place") in {e["id"] for e in late["entities"]}
               for ev in late["events"] if ev.get("setting", {}).get("place"))


def test_a_place_link_to_an_entity_not_in_the_view_is_dropped_but_the_text_stays(scarlet, series):
    import copy
    ix = copy.deepcopy(scarlet)
    ix["events"][1]["setting"] = {"text": "the Criterion Bar", "place": "not-in-the-view"}
    v = view([ix], Position("study-in-scarlet", "ch1.para05"), series)
    assert v["events"][1]["setting"] == {"text": "the Criterion Bar"}


def test_the_view_carries_book_kind_only_when_the_index_has_one(scarlet, series):
    pos = Position("study-in-scarlet", "ch2.para51")
    assert "bookKind" not in view([scarlet], pos, series)
    v = view([{**scarlet, "bookKind": "self-help"}], pos, series)
    assert v["bookKind"] == "self-help"


def test_the_view_takes_book_kind_from_the_current_book_only(scarlet, four, series):
    v = view([{**scarlet, "bookKind": "non-fiction"}, four], Position("sign-of-four", "ch1.para12"), series)
    assert "bookKind" not in v


def _with_settings(scarlet):
    import copy
    ix = copy.deepcopy(scarlet)
    for ev in ix["events"]:
        ev.pop("setting", None)  # independent of the example's own settings
    for ev in ix["events"]:
        if ev["id"] == "sis.ev.meeting":
            ev["setting"] = {"text": "a laboratory", "place": "baker-street"}
        if ev["id"] == "sis.ev.maiwand":
            ev["setting"] = {"text": "the battlefield", "place": "maiwand"}
    return ix


def test_the_view_copies_an_event_setting_and_links_only_visible_places(scarlet, series):
    ix = _with_settings(scarlet)
    # baker-street is introduced at ch1.para52, after the meeting event (ch1.para35)
    v = view([ix], Position("study-in-scarlet", "ch1.para35"), series)
    evs = {e["id"]: e for e in v["events"]}
    assert evs["sis.ev.maiwand"]["setting"] == {"text": "the battlefield", "place": "maiwand"}
    assert evs["sis.ev.meeting"]["setting"] == {"text": "a laboratory"}
    v = view([ix], Position("study-in-scarlet", "ch1.para52"), series)
    evs = {e["id"]: e for e in v["events"]}
    assert evs["sis.ev.meeting"]["setting"] == {"text": "a laboratory", "place": "baker-street"}
    assert "setting" not in next(e for e in v["events"] if e["id"] == "sis.ev.criterion")


def test_no_setting_or_place_link_for_a_future_event(scarlet, series):
    ix = _with_settings(scarlet)
    v = view([ix], Position("study-in-scarlet", "ch1.para05"), series)
    assert all(e["id"] != "sis.ev.meeting" for e in v["events"])
    assert "a laboratory" not in json.dumps(v)
    assert "baker-street" not in json.dumps(v)
