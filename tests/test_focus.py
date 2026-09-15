"""Window-picking tests.

Regression cover for the "Focus window opens the wrong project" bug.

One Code.exe owns every VS Code window on the machine, so neither the pid nor
the handle captured at SessionStart identifies a session's window: the pid
resolves to whichever window is topmost now, and the handle to whichever was
topmost when the session started. Only the window title, matched against the
session's cwd, actually says which window belongs to which session.

pick_window is pure, so these run everywhere - no real windows needed.
"""

import os

from harness import Skip, eq, ok

import winutil as W

# The window set that produced the bug, in the shape VS Code titles them:
# "<open file> - <workspace folder> - Visual Studio Code".
VSCODE_WINDOWS = [
    (69142, "Welcome - trafficlight - Visual Studio Code"),
    (69324, "train_model.ipynb - ml_pipeline - Visual Studio Code"),
    (131236, "README.md - travel_planner - Visual Studio Code"),
    (329392, ".env - billing_service - Visual Studio Code"),
]

BASE = "C:/Users/someone/projects/"


def test_each_project_picks_its_own_window():
    """The reported bug: clicking one project opened a different one."""
    expected = {
        "trafficlight": 69142,
        "ml_pipeline": 69324,
        "travel_planner": 131236,
        "billing_service": 329392,
    }
    for project, hwnd in expected.items():
        got, confident = W.pick_window(VSCODE_WINDOWS, BASE + project)
        eq(got, hwnd, "wrong window for " + project)
        ok(confident, "should be a confident match for " + project)


def test_a_backslash_path_works_the_same():
    got, confident = W.pick_window(
        VSCODE_WINDOWS, r"C:\Users\someone\projects\billing_service"
    )
    eq(got, 329392, "backslash path")
    ok(confident, "confident")


def test_a_subdirectory_falls_back_to_the_project_folder():
    """An editor window is titled after the workspace root, not the cwd."""
    got, confident = W.pick_window(
        VSCODE_WINDOWS, BASE + "travel_planner/backend/src"
    )
    eq(got, 131236, "should match the project folder via a parent segment")
    ok(confident, "confident")


def test_generic_path_segments_are_never_matched_on():
    """'projects' and 'Desktop' appear in every path; matching them would make
    the first window win for every session."""
    terms = W.cwd_terms(BASE + "trafficlight")
    for junk in ("projects", "desktop", "users", "src", "c:"):
        ok(junk not in terms, "%r must not be a matching term: %r" % (junk, terms))
    eq(terms[0], "trafficlight", "most specific segment comes first")


def test_the_deepest_matching_segment_wins():
    windows = [
        (1, "something - workspaces - Visual Studio Code"),
        (2, "other - trafficlight - Visual Studio Code"),
    ]
    got, _c = W.pick_window(windows, "C:/Users/someone/workspaces/trafficlight")
    eq(got, 2, "the project folder beats an ancestor folder")


def test_a_single_window_needs_no_matching():
    got, confident = W.pick_window([(42, "Some Terminal")], BASE + "whatever")
    eq(got, 42, "the only candidate")
    ok(confident, "no ambiguity to resolve")


def test_no_title_match_reports_itself_as_unconfident():
    """So focus_session knows to prefer the captured handle instead."""
    got, confident = W.pick_window(VSCODE_WINDOWS, BASE + "unopened_project")
    ok(not confident, "must not claim a match it did not make")
    eq(got, VSCODE_WINDOWS[0][0], "falls back to the first window")


def test_an_empty_cwd_is_unconfident_when_ambiguous():
    got, confident = W.pick_window(VSCODE_WINDOWS, "")
    ok(not confident, "no cwd means no basis to choose")
    ok(got, "still returns something to raise")


def test_no_candidates_is_inert():
    eq(W.pick_window([], BASE + "x"), (0, False), "nothing to pick")
    eq(W.pick_window(None, BASE + "x"), (0, False), "None is tolerated")


def test_cwd_terms_handles_junk():
    for junk in (None, "", "/", "\\", "C:", "  "):
        got = W.cwd_terms(junk)
        ok(isinstance(got, list), "cwd_terms(%r) should return a list" % junk)


def test_scoring_is_case_insensitive():
    windows = [(1, "X - OTHER - Visual Studio Code"),
               (2, "X - My-API - Visual Studio Code")]
    got, confident = W.pick_window(windows, "/home/me/code/my-api")
    eq(got, 2, "case-insensitive match")
    ok(confident, "confident")


# --- against the real desktop ------------------------------------------------


def test_every_real_window_resolves_to_its_own_title():
    """If this box has a process owning several windows, prove we can tell them
    apart by the folder each one is showing."""
    if os.name != "nt":
        raise Skip("windows-only")
    groups = [ws for ws in W.windows_by_pid_all().values() if len(ws) > 1]
    if not groups:
        raise Skip("no multi-window process on this machine")

    checked = 0
    for windows in groups:
        for hwnd, title in windows:
            # Build a plausible cwd from the window's own title: the folder name
            # a VS Code window shows is the middle dash-separated segment.
            parts = [p.strip() for p in title.split(" - ")]
            if len(parts) < 3:
                continue
            folder = parts[-2]
            if len(folder) < 3:
                continue
            got, confident = W.pick_window(windows, "/x/y/" + folder)
            if not confident:
                continue
            eq(got, hwnd, "title %r should resolve to its own window" % title)
            checked += 1
    if not checked:
        raise Skip("no window titles in a recognisable folder form")
