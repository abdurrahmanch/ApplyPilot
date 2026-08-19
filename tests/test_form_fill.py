"""Tests for the deterministic form filler (ARCHITECTURE §5).

The point of the module is that filling a known field involves no reasoning, so
these tests assert on the plan rather than on a browser. Everything here runs
without Chrome, which is the design working as intended: if planning needed a
live page, it would not be deterministic.
"""

from __future__ import annotations

import json

import pytest

from applypilot.apply import form_fill


PROFILE = {
    "personal": {
        "full_name": "Test Candidate",
        "email": "test@example.com",
        "phone": "555-0100",
        "linkedin_url": "https://linkedin.com/in/test",
        "github_url": "https://github.com/test",
        "city": "Chicago",
    },
}


def _ctx(**over):
    ctx = {"profile": PROFILE, "files": {"resume": "/tmp/r.pdf"}, "conn": None}
    ctx.update(over)
    return ctx


# ---------------------------------------------------------------------------
# Source resolution — the whole "no LLM" claim rests on this being a lookup
# ---------------------------------------------------------------------------

def test_profile_value_resolves():
    assert form_fill.resolve("profile.personal.email", _ctx()) == "test@example.com"


def test_missing_profile_key_resolves_to_none_not_a_guess():
    assert form_fill.resolve("profile.personal.nonexistent", _ctx()) is None


def test_const_resolves():
    assert form_fill.resolve("const.Yes", _ctx()) == "Yes"


def test_file_resolves():
    assert form_fill.resolve("file.resume", _ctx()) == "/tmp/r.pdf"
    assert form_fill.resolve("file.cover_letter", _ctx()) is None


def test_first_and_last_name_are_derived_from_full_name():
    """profile.json stores only `full_name`; most ATSes want the parts."""
    assert form_fill.resolve("profile.personal.first_name", _ctx()) == "Test"
    assert form_fill.resolve("profile.personal.last_name", _ctx()) == "Candidate"


def test_list_valued_profile_keys_never_leak_into_a_form_field():
    ctx = _ctx(profile={"skills": {"languages": ["Java", "TypeScript"]}})
    assert form_fill.resolve("profile.skills.languages", ctx) is None


def test_question_source_reads_the_bank(tmp_db):
    from applypilot.questions import add_question

    conn = tmp_db()
    add_question(conn, "Will you require sponsorship?", "No", category="sponsorship")
    assert form_fill.resolve("question.sponsorship", _ctx(conn=conn)) == "No"


def test_question_source_without_a_connection_is_none_not_an_error():
    assert form_fill.resolve("question.sponsorship", _ctx(conn=None)) is None


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_unmapped_ats_plans_nothing_and_says_so():
    """An unmapped family must fall straight through to the existing agent
    path — the filler is a fast lane alongside it, not a replacement."""
    result = form_fill.plan("some-bespoke-career-site", _ctx())
    assert result["has_map"] is False
    assert result["actions"] == []
    assert form_fill.prompt_block(result) == ""


def test_greenhouse_plans_the_fields_it_can_resolve():
    result = form_fill.plan("greenhouse", _ctx())
    assert result["has_map"] is True
    filled = {a["field"] for a in result["actions"]}
    assert {"first_name", "last_name", "email", "phone"} <= filled
    assert "resume" in filled


def test_unresolvable_fields_are_reported_not_invented():
    """A mapped field with no stored answer goes to the agent. It must never
    be filled with an empty string or a guess."""
    ctx = _ctx(profile={"personal": {"email": "a@b.c"}}, files={})
    result = form_fill.plan("greenhouse", ctx)
    values = [a["value"] for a in result["actions"]]
    assert all(v for v in values), "no empty values may reach the form"
    assert "phone" in result["unresolved"]
    assert "resume" in result["unresolved"]


def test_every_shipped_map_loads_and_is_well_formed():
    maps = form_fill.available_maps()
    assert {"greenhouse", "ashby", "workday"} <= set(maps)
    for ats in maps:
        spec = form_fill.load_map(ats)
        assert spec is not None, ats
        for name, field in spec["fields"].items():
            assert field.get("selector"), f"{ats}.{name} has no selector"
            assert "." in field.get("source", ""), f"{ats}.{name} has a bad source"


def test_workday_is_mapped_because_it_is_the_largest_family():
    """24% of the queue. Greenhouse and Lever together are under 8%, so a
    rollout that starts with them optimises the wrong end (ARCHITECTURE §5)."""
    assert form_fill.load_map("workday") is not None


# ---------------------------------------------------------------------------
# Script generation — the safety property lives here
# ---------------------------------------------------------------------------

def test_script_probes_before_writing():
    """This is what makes an unverified map safe: a selector that does not
    match is reported, never forced onto some other element."""
    script = form_fill.render_fill_script(
        [{"field": "email", "selector": "#email", "value": "a@b.c", "kind": "text"}])
    assert "querySelector" in script
    assert "missing.push" in script


def test_script_dispatches_events_so_react_keeps_the_value():
    script = form_fill.render_fill_script(
        [{"field": "email", "selector": "#email", "value": "a@b.c", "kind": "text"}])
    assert "'input'" in script and "'change'" in script
    assert "getOwnPropertyDescriptor" in script


def test_file_inputs_are_excluded_from_the_script_but_still_reported():
    """A script cannot attach a file without a user gesture. Uploads stay with
    the agent's upload tool, but must not silently vanish from the plan."""
    actions = [
        {"field": "email", "selector": "#email", "value": "a@b.c", "kind": "text"},
        {"field": "resume", "selector": "#resume", "value": "/tmp/r.pdf", "kind": "file"},
    ]
    script = form_fill.render_fill_script(actions)
    plan_json = script.split("const plan = ")[1].split(";\n")[0]
    assert [s["field"] for s in json.loads(plan_json)] == ["email"]
    assert "/tmp/r.pdf" in script

    block = form_fill.prompt_block(
        {"has_map": True, "ats": "greenhouse", "actions": actions, "unresolved": []})
    assert "/tmp/r.pdf" in block


def test_values_are_json_escaped_not_string_concatenated():
    """A quote or newline in a profile value must not break out of the script."""
    script = form_fill.render_fill_script([
        {"field": "x", "selector": "#x", "value": 'he said "hi"\n</script>',
         "kind": "text"}])
    plan_json = script.split("const plan = ")[1].split(";\n")[0]
    assert json.loads(plan_json)[0]["value"] == 'he said "hi"\n</script>'


# ---------------------------------------------------------------------------
# The prompt block
# ---------------------------------------------------------------------------

def test_prompt_block_tells_the_agent_not_to_revisit_filled_fields():
    block = form_fill.prompt_block(form_fill.plan("greenhouse", _ctx()))
    assert "browser_evaluate" in block
    assert "ONE" in block
    assert "Never re-enter a field" in block


def test_prompt_block_scopes_the_agent_to_unresolved_fields_only():
    ctx = _ctx(profile={"personal": {"full_name": "A B", "email": "a@b.c"}})
    block = form_fill.prompt_block(form_fill.plan("greenhouse", ctx))
    assert "reason about only these" in block


# ---------------------------------------------------------------------------
# Fallback ledger — rule 2: a repeated miss is a map bug
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    return tmp_path / "fill_fallbacks.json"


def test_fallbacks_accumulate_per_field(ledger):
    form_fill.record_fallbacks("greenhouse", ["veteran_status"])
    assert form_fill.map_bugs() == {}, "one miss is not yet a bug"

    form_fill.record_fallbacks("greenhouse", ["veteran_status"])
    assert form_fill.map_bugs() == {"greenhouse": ["veteran_status"]}


def test_recording_nothing_is_harmless(ledger):
    assert form_fill.record_fallbacks("greenhouse", []) == {}
    assert form_fill.record_fallbacks(None, ["x"]) == {}
    assert form_fill.map_bugs() == {}


def test_a_corrupt_ledger_never_breaks_an_application(ledger):
    ledger.write_text("{ not json")
    form_fill.record_fallbacks("greenhouse", ["phone"])
    assert form_fill.map_bugs() == {}
