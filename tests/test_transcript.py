"""Transcript / token-accounting tests.

Synthetic JSONL is written to a temp file and scanned, so nothing here depends
on a real Claude session existing.
"""

import json
import os
import tempfile

from harness import eq, ok

import transcript as T


def _write(lines, path=None):
    path = path or os.path.join(
        tempfile.mkdtemp(prefix="clt-transcript-"), "t.jsonl"
    )
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return path


def _append(path, lines):
    with open(path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _assistant(inp=0, read=0, write=0, out=0, sidechain=False, model="claude-opus-5"):
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write,
                "output_tokens": out,
            },
        },
    }


def _model_attachment(model_id):
    return {
        "type": "attachment",
        "attachment": {
            "type": "model",
            "identity": {"modelId": model_id, "marketingName": "Test Model"},
        },
    }


def _fresh(name):
    T.forget(name)
    return name


def test_totals_accumulate_across_turns():
    path = _write([
        _assistant(inp=10, read=100, write=5, out=50),
        _assistant(inp=20, read=200, write=7, out=60),
    ])
    r = T.scan(path, _fresh("acc"))
    eq(r["tokens_in"], 30, "input")
    eq(r["tokens_cache_read"], 300, "cache read")
    eq(r["tokens_cache_write"], 12, "cache write")
    eq(r["tokens_out"], 110, "output")
    eq(r["turns"], 2, "turns")


def test_context_is_the_latest_turn_not_the_sum():
    path = _write([
        _assistant(inp=10, read=100, write=5),
        _assistant(inp=1, read=900, write=9),
    ])
    r = T.scan(path, _fresh("ctx"))
    eq(r["context_tokens"], 910, "context is the most recent turn only")


def test_sidechain_turns_do_not_clobber_the_context_reading():
    """A subagent runs against its own context; the panel reports the main one."""
    path = _write([
        _assistant(inp=1, read=5000, write=0),
        _assistant(inp=1, read=42, write=0, sidechain=True),
    ])
    r = T.scan(path, _fresh("side"))
    eq(r["context_tokens"], 5001, "main-thread context preserved")
    eq(r["turns"], 2, "the subagent's tokens still count towards totals")


def test_model_attachment_sets_a_1m_limit():
    """message.model drops the [1m] suffix, so only the attachment can tell us."""
    plain = _write([_assistant(inp=1, read=1000)])
    eq(T.scan(plain, _fresh("lim-a"))["context_limit"], 200_000, "default limit")

    large = _write([
        _model_attachment("claude-opus-5[1m]"),
        _assistant(inp=1, read=1000),
    ])
    r = T.scan(large, _fresh("lim-b"))
    eq(r["context_limit"], 1_000_000, "1m limit")
    eq(r["model_id"], "claude-opus-5[1m]", "model id")
    eq(r["model_name"], "Test Model", "marketing name")


def test_observed_context_above_200k_upgrades_the_limit():
    path = _write([_assistant(inp=1, read=195_000)])
    eq(T.scan(path, _fresh("lim-c"))["context_limit"], 1_000_000,
       "a run past 200k cannot be a 200k window")


def test_env_override_wins():
    path = _write([_assistant(inp=1, read=10)])
    os.environ["CLAUDE_LIGHT_CONTEXT_LIMIT"] = "500k"
    try:
        eq(T.scan(path, _fresh("lim-d"))["context_limit"], 500_000, "override")
    finally:
        del os.environ["CLAUDE_LIGHT_CONTEXT_LIMIT"]


def test_second_scan_reads_only_the_new_bytes():
    path = _write([_assistant(out=10)])
    sid = _fresh("incr")
    eq(T.scan(path, sid)["tokens_out"], 10, "first scan")
    _append(path, [_assistant(out=5)])
    r = T.scan(path, sid)
    eq(r["tokens_out"], 15, "incremental scan adds, does not double count")
    eq(r["turns"], 2, "turns")
    eq(T.scan(path, sid)["tokens_out"], 15, "re-scanning with no new bytes is a no-op")


def test_a_truncated_transcript_resets_instead_of_double_counting():
    path = _write([_assistant(out=100), _assistant(out=100)])
    sid = _fresh("trunc")
    eq(T.scan(path, sid)["tokens_out"], 200, "before")
    _write([_assistant(out=7)], path)  # rewritten shorter: offset now past EOF
    eq(T.scan(path, sid)["tokens_out"], 7, "state rebuilt from scratch")


def test_a_half_written_final_line_is_not_consumed():
    """A hook can fire while Claude is midway through writing a line."""
    path = _write([_assistant(out=10)])
    sid = _fresh("partial")
    T.scan(path, sid)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","message":{"usage":{"output_toke')
    eq(T.scan(path, sid)["tokens_out"], 10, "partial line ignored")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('ns": 5}}}\n')
    eq(T.scan(path, sid)["tokens_out"], 15, "consumed once it is complete")


def _turn_end(agents=None):
    """The per-turn footer Claude Code writes; it carries the agent count only
    when something is actually pending."""
    line = {"type": "system", "subtype": "turn_duration", "durationMs": 1}
    if agents is not None:
        line["pendingBackgroundAgentCount"] = agents
    return line


def test_background_agent_count_is_tracked():
    path = _write([
        {"type": "system", "pendingBackgroundAgentCount": 2},
        _assistant(out=1),
    ])
    eq(T.scan(path, _fresh("agents"))["agents"], 2, "agent count")


def test_a_live_agent_count_survives_a_long_turn():
    """The count goes stale in *turns*, and one turn is dozens of assistant
    messages - a real transcript here ran 28 of them between two turn footers.
    Measuring staleness against those messages reported every live agent as
    gone within seconds, which silently disabled both the badge and the chime
    gate that stops a session announcing it has finished while an agent runs."""
    path = _write([_turn_end(1)] + [_assistant(out=1) for _ in range(30)])
    eq(T.scan(path, _fresh("agents-long"))["agents"], 1,
       "an agent pending at the last turn boundary is still pending")


def test_a_turn_that_reports_no_agents_clears_the_count():
    """Claude Code omits the field rather than writing a 0, so a turn footer
    without it is a positive statement that none are pending."""
    path = _write([_turn_end(2), _assistant(out=1)])
    sid = _fresh("agents-clear")
    eq(T.scan(path, sid)["agents"], 2, "pending")
    _append(path, [_assistant(out=1), _turn_end(), _assistant(out=1)])
    eq(T.scan(path, sid)["agents"], 0, "the next turn says there are none left")


def test_an_agent_count_goes_stale_after_a_few_turns():
    path = _write([_turn_end(3), _assistant(out=1)])
    sid = _fresh("agents-stale")
    eq(T.scan(path, sid)["agents"], 3, "fresh")
    # Turn footers that carry no count at all - an older build that simply
    # stopped reporting. The reading must not be trusted forever.
    for turn in range(T.AGENTS_FRESH_TURNS + 1):
        _append(path, [{"type": "system", "subtype": "turn_duration"},
                       _assistant(out=1)])
    eq(T.scan(path, sid)["agents"], 0, "too old to mean anything")


def test_the_agent_count_survives_an_incremental_scan():
    path = _write([_turn_end(1), _assistant(out=1)])
    sid = _fresh("agents-incr")
    eq(T.scan(path, sid)["agents"], 1, "first scan")
    _append(path, [_assistant(out=1)])
    eq(T.scan(path, sid)["agents"], 1,
       "a later scan with no new footer keeps the last reading")


def test_garbage_and_missing_files_never_raise():
    eq(T.scan("", _fresh("g1"))["turns"], 0, "empty path")
    eq(T.scan(None, _fresh("g2"))["turns"], 0, "none path")
    eq(T.scan("/no/such/file.jsonl", _fresh("g3"))["turns"], 0, "missing file")

    path = os.path.join(tempfile.mkdtemp(prefix="clt-garbage-"), "g.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("not json\n[]\n{}\n" + json.dumps(_assistant(out=3)) + "\n{brok")
    r = T.scan(path, _fresh("g4"))
    eq(r["tokens_out"], 3, "the one good line still counts")


def test_wrong_typed_usage_values_are_ignored():
    path = _write([{
        "type": "assistant",
        "message": {"usage": {"output_tokens": "lots", "input_tokens": -5,
                              "cache_read_input_tokens": None}},
    }])
    r = T.scan(path, _fresh("types"))
    eq(r["tokens_out"], 0, "string token count ignored")
    eq(r["tokens_in"], 0, "negative token count ignored")


def test_forget_clears_state():
    path = _write([_assistant(out=9)])
    sid = _fresh("forget")
    eq(T.scan(path, sid)["tokens_out"], 9, "counted")
    T.forget(sid)
    eq(T.scan(path, sid)["tokens_out"], 9, "recounted from scratch after forget")


def test_session_ids_with_illegal_filename_characters_are_safe():
    path = _write([_assistant(out=4)])
    for sid in ("a/b", "c:d", "e*f", "../escape", ""):
        r = T.scan(path, _fresh(sid))
        eq(r["tokens_out"], 4, "scan works for session id %r" % sid)
    ok(os.path.isdir(T.STATE_DIR), "state dir exists")
    for name in os.listdir(T.STATE_DIR):
        ok(os.sep not in name and "/" not in name, "state file %r stays flat" % name)


# --- what the session is about -----------------------------------------------


def test_the_title_and_last_prompt_are_read():
    """Claude Code writes both itself, once per turn, so there is no need to
    reconstruct them from the user messages - which would mean telling real
    prompts apart from tool results, sidechains and meta lines."""
    name = _fresh("about-1")
    path = _write([
        {"type": "ai-title", "aiTitle": "Ghost rows on the panel"},
        _assistant(inp=5, out=2),
        {"type": "last-prompt", "lastPrompt": "fix the ghost row bug"},
    ])
    r = T.scan(path, name)
    eq(r["title"], "Ghost rows on the panel")
    eq(r["prompt"], "fix the ghost row bug")
    T.forget(name)


def test_the_latest_of_each_wins():
    name = _fresh("about-2")
    path = _write([
        {"type": "ai-title", "aiTitle": "Repository overview"},
        {"type": "last-prompt", "lastPrompt": "first thing"},
        {"type": "ai-title", "aiTitle": "Fixing the map"},
        {"type": "last-prompt", "lastPrompt": "second thing"},
    ])
    r = T.scan(path, name)
    eq(r["title"], "Fixing the map")
    eq(r["prompt"], "second thing")
    T.forget(name)


def test_they_survive_an_incremental_scan():
    """Only new bytes are read on each hook, so anything learned earlier has
    to come back from the state file rather than being re-read."""
    name = _fresh("about-3")
    path = _write([
        {"type": "ai-title", "aiTitle": "Ghost rows on the panel"},
        {"type": "last-prompt", "lastPrompt": "fix the ghost row bug"},
    ])
    eq(T.scan(path, name)["title"], "Ghost rows on the panel")
    _append(path, [_assistant(inp=1, out=1)])
    again = T.scan(path, name)
    eq(again["title"], "Ghost rows on the panel", "the title must not be lost")
    eq(again["prompt"], "fix the ghost row bug", "nor the prompt")
    T.forget(name)


def test_a_multi_line_prompt_becomes_one_line():
    name = _fresh("about-4")
    path = _write([
        {"type": "last-prompt",
         "lastPrompt": "do this\n\n  and   then\tthat  "},
    ])
    eq(T.scan(path, name)["prompt"], "do this and then that")
    T.forget(name)


def test_a_very_long_prompt_is_cut():
    name = _fresh("about-5")
    path = _write([{"type": "last-prompt", "lastPrompt": "x" * 5000}])
    eq(len(T.scan(path, name)["prompt"]), T.TEXT_LIMIT)
    T.forget(name)


def test_junk_titles_and_prompts_are_ignored():
    name = _fresh("about-6")
    path = _write([
        {"type": "ai-title", "aiTitle": "Real title"},
        {"type": "last-prompt", "lastPrompt": "real prompt"},
        {"type": "ai-title", "aiTitle": 12},
        {"type": "ai-title", "aiTitle": "   "},
        {"type": "last-prompt", "lastPrompt": None},
        {"type": "last-prompt"},
    ])
    r = T.scan(path, name)
    eq(r["title"], "Real title", "a junk title must not blank a good one")
    eq(r["prompt"], "real prompt")
    T.forget(name)


def test_a_session_with_neither_reports_empty_strings():
    name = _fresh("about-7")
    path = _write([_assistant(inp=1, out=1)])
    r = T.scan(path, name)
    eq(r["title"], "")
    eq(r["prompt"], "")
    T.forget(name)
