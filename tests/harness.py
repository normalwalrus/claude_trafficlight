
"""Tiny zero-dependency test harness.

Collects `test_*` functions from the test modules, runs them, prints one line
each and a pass/fail summary. A test signals "not applicable here" by raising
Skip - used for the Windows-only and FastAPI-only cases.
"""

import os
import sys
import time

# Failure output can contain any character the panel draws (chevrons, bullets,
# box glyphs). On a cp1252 console that raises UnicodeEncodeError *while
# reporting a failure*, hiding the very thing the run exists to show.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "panel"))
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "server"))


class Skip(Exception):
    pass


def eq(got, want, what=""):
    if got != want:
        raise AssertionError((what or "value") + ": got %r, want %r" % (got, want))


def ok(cond, what=""):
    if not cond:
        raise AssertionError(what or "assertion failed")


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    raise AssertionError("expected %s" % exc.__name__)


def run(modules):
    passed = failed = skipped = 0
    failures = []
    for mod in modules:
        name = mod.__name__.replace("test_", "")
        print("\n== " + name + " " + "=" * max(0, 58 - len(name)))
        for attr in sorted(dir(mod)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(mod, attr)
            label = attr[5:].replace("_", " ")
            t0 = time.time()
            try:
                fn()
            except Skip as exc:
                skipped += 1
                print("  SKIP  %-52s %s" % (label, exc))
            except Exception:
                failed += 1
                failures.append((name + "." + attr, traceback.format_exc()))
                print("  FAIL  %-52s" % label)
            else:
                passed += 1
                print("  ok    %-52s %4dms" % (label, (time.time() - t0) * 1000))

    for where, tb in failures:
        print("\n" + "-" * 70 + "\nFAILED: " + where + "\n" + tb.rstrip())

    print(
        "\n%s\n%d passed, %d failed, %d skipped"
        % ("=" * 70, passed, failed, skipped)
    )
    return 1 if failed else 0
