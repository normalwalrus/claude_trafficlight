"""Incremental token accounting from a Claude Code transcript.

Claude Code hands each hook a ``transcript_path`` pointing at the session's
JSONL log. Re-parsing that file on every hook would be far too slow - they grow
into megabytes - so this keeps a tiny state file per session and only reads the
bytes appended since the last hook fired.

What we pull out of each ``assistant`` line's ``message.usage``:

    input_tokens                 fresh prompt tokens
    cache_creation_input_tokens  tokens written into the prompt cache
    cache_read_input_tokens      tokens served from the cache
    output_tokens                what the model produced

The *context* currently in play is the sum of the three input figures on the
most recent assistant turn. The *session totals* are those figures summed over
every turn. Cache reads are reported separately because they dwarf everything
else and would make a single "tokens used" number meaningless.

Nothing here raises: a missing, truncated, rotated or malformed transcript
returns whatever was learned so far.
"""

import json
import os
import tempfile

STATE_DIR = os.path.join(tempfile.gettempdir(), "claude-trafficlight", "usage")

# Most models run a 200k window; the 1M variants do not announce themselves in
# the transcript, so the limit is inferred from what we actually observe and
# can be pinned with CLAUDE_LIGHT_CONTEXT_LIMIT.
DEFAULT_LIMIT = 200_000
LARGE_LIMIT = 1_000_000
UPGRADE_AT = 190_000  # observed context above this means it cannot be a 200k run

# Never spend more than this on one hook invocation; a session that somehow
# jumped ahead by more than this resumes from the end and says so.
MAX_READ_BYTES = 8 * 1024 * 1024

EMPTY = {
    "context_tokens": 0,
    "context_limit": 0,
    "tokens_in": 0,
    "tokens_out": 0,
    "tokens_cache_write": 0,
    "tokens_cache_read": 0,
    "turns": 0,
    "turn_marks": 0,
    "agents_turn": -1,
    "model": "",
    "model_id": "",
    "model_name": "",
    "agents": 0,
    "approx": False,
    # What the session is about, both written by Claude Code itself: a title
    # it generates for the conversation, and the last thing you typed. Neither
    # is reliable alone - the title is generated early and goes stale, and the
    # last prompt is often a fragment like "carry on" - so the panel shows both.
    "title": "",
    "prompt": "",
}

# Enough to fill two lines on a 320px panel, with the rest cut.
TEXT_LIMIT = 220


def _state_path(session_id):
    from claude_light_hook import cache_key  # same sanitising rules

    return os.path.join(STATE_DIR, cache_key(session_id) + ".json")


def _load(session_id):
    try:
        with open(_state_path(session_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            merged = dict(EMPTY)
            merged.update({k: v for k, v in data.items() if k in EMPTY or k == "offset"})
            return merged
    except Exception:
        pass
    state = dict(EMPTY)
    state["offset"] = 0
    return state


def _save(session_id, state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = _state_path(session_id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, _state_path(session_id))
    except Exception:
        pass


def forget(session_id):
    """Drop the accounting state for a finished session."""
    try:
        os.remove(_state_path(session_id))
    except OSError:
        pass


def _limit_for(observed_max, model_id, fallback_model=""):
    override = os.environ.get("CLAUDE_LIGHT_CONTEXT_LIMIT", "").strip().lower()
    if override:
        try:
            if override.endswith("k"):
                return int(float(override[:-1]) * 1_000)
            if override.endswith("m"):
                return int(float(override[:-1]) * 1_000_000)
            return int(override)
        except ValueError:
            pass
    for name in (model_id, fallback_model):
        if "[1m]" in (name or "").lower():
            return LARGE_LIMIT
    # No marker seen (yet): fall back to what we have actually observed. A run
    # that has already exceeded a 200k window cannot be a 200k window.
    return LARGE_LIMIT if observed_max > UPGRADE_AT else DEFAULT_LIMIT


# How many completed turns an agent count stays trustworthy for.
#
# Measured in `turn_marks`, not `turns`: `turns` counts assistant *messages*
# and a single turn contains dozens of them, so comparing against it declared
# every live count stale within seconds of it being written.
AGENTS_FRESH_TURNS = 2


def _finalise(state):
    """Stamp the inferred limit and return only the public fields."""
    state["context_limit"] = _limit_for(
        state["context_tokens"], state.get("model_id", ""), state["model"]
    )
    result = {k: state[k] for k in EMPTY}
    seen = state.get("agents_turn", -1)
    if seen < 0 or state["turn_marks"] - seen > AGENTS_FRESH_TURNS:
        result["agents"] = 0        # too old to mean anything
    return result


def _apply_line(line, state):
    try:
        entry = json.loads(line)
    except Exception:
        return
    if not isinstance(entry, dict):
        return

    # A "turn_duration" system line is written once at the end of every turn,
    # which makes it the only reliable turn counter in here.
    turn_end = (entry.get("type") == "system"
                and entry.get("subtype") == "turn_duration")
    if turn_end:
        state["turn_marks"] += 1

    # Background agents: Claude Code reports the live count on the turn footer,
    # and omits the field entirely rather than writing a 0 when none are
    # pending - so a footer without it is a positive statement that there are
    # none, and anything older than a couple of turns is not worth trusting.
    count = entry.get("pendingBackgroundAgentCount")
    if isinstance(count, int) and count >= 0:
        state["agents"] = count
        state["agents_turn"] = state["turn_marks"]
    elif turn_end:
        state["agents"] = 0
        state["agents_turn"] = state["turn_marks"]

    # Claude Code records both of these itself, once per turn, so there is no
    # need to reconstruct them from the user messages - which would mean
    # telling real prompts apart from tool results, sidechains and meta lines.
    kind = entry.get("type")
    if kind == "ai-title":
        title = entry.get("aiTitle")
        if isinstance(title, str) and title.strip():
            state["title"] = title.strip()[:TEXT_LIMIT]
        return
    if kind == "last-prompt":
        prompt = entry.get("lastPrompt")
        if isinstance(prompt, str) and prompt.strip():
            state["prompt"] = " ".join(prompt.split())[:TEXT_LIMIT]
        return

    # The authoritative model id lives on a "model" attachment line. The
    # assistant lines carry a marketing-ish id ("claude-opus-5") that drops the
    # [1m] suffix, so only this one can tell a 1M session from a 200k one.
    if entry.get("type") == "attachment":
        attachment = entry.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") == "model":
            identity = attachment.get("identity")
            if isinstance(identity, dict):
                model_id = identity.get("modelId")
                if isinstance(model_id, str) and model_id:
                    state["model_id"] = model_id
                name = identity.get("marketingName")
                if isinstance(name, str) and name:
                    state["model_name"] = name
        return

    if entry.get("type") != "assistant":
        return
    message = entry.get("message")
    if not isinstance(message, dict):
        return

    model = message.get("model")
    if isinstance(model, str) and model:
        state["model"] = model

    usage = message.get("usage")
    if not isinstance(usage, dict):
        return

    def num(key):
        value = usage.get(key, 0)
        return value if isinstance(value, int) and value >= 0 else 0

    fresh = num("input_tokens")
    cache_write = num("cache_creation_input_tokens")
    cache_read = num("cache_read_input_tokens")
    out = num("output_tokens")

    state["tokens_in"] += fresh
    state["tokens_cache_write"] += cache_write
    state["tokens_cache_read"] += cache_read
    state["tokens_out"] += out
    state["turns"] += 1

    # A sidechain turn is a subagent's, running against its own context - it
    # must not overwrite the main thread's context reading.
    if not entry.get("isSidechain"):
        state["context_tokens"] = fresh + cache_write + cache_read


def scan(transcript_path, session_id):
    """Update and return the usage figures for one session."""
    state = _load(session_id)

    if not transcript_path or not os.path.exists(transcript_path):
        return _finalise(state)

    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return _finalise(state)

    offset = state.get("offset", 0)
    if not isinstance(offset, int) or offset < 0 or offset > size:
        # Truncated, rotated, or a resumed session pointing at a fresh file.
        offset = 0
        for key in EMPTY:
            state[key] = EMPTY[key]

    if size - offset > MAX_READ_BYTES:
        offset = size - MAX_READ_BYTES
        state["approx"] = True

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            data = fh.read()
            # Only consume up to the last complete line; a hook can fire while
            # Claude is midway through writing one.
            cut = data.rfind("\n")
            if cut == -1:
                consumed = 0
            else:
                consumed = cut + 1
                for line in data[:cut].splitlines():
                    line = line.strip()
                    if line:
                        _apply_line(line, state)
            # seek/read work in characters, tell() gives a byte offset.
            state["offset"] = offset + len(
                data[:consumed].encode("utf-8", "replace")
            )
    except Exception:
        pass

    result = _finalise(state)
    _save(session_id, state)
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python transcript.py <transcript.jsonl> [session_id]")
        raise SystemExit(2)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    result = scan(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "cli-test")
    for key in sorted(result):
        value = result[key]
        print("%-20s %s" % (key, f"{value:,}" if isinstance(value, int) else value))
