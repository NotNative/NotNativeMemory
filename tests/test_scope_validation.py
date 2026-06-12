"""
Unit test for server._validate_writable_scope and source normalization.

Runs without a live DB or the MCP SDK — loads just the validator block
out of server.py via regex + exec so the test is independent of
dotenv / mcp imports that would require the production environment.
"""
import os
import re
import sys
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SERVER_PATH = os.path.join(ROOT, "server.py")


def _load_helpers():
    src = open(SERVER_PATH, "r", encoding="utf-8").read()
    pattern = re.compile(
        r"_GLOBAL_SCOPE = .*?(?=^@mcp\.tool)",
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(src)
    if not match:
        raise RuntimeError("could not locate _validate_writable_scope block")
    ns = {"Optional": Optional}
    exec(match.group(0), ns)
    return ns["_validate_writable_scope"], ns["_normalize_source"]


def run():
    validate, normalize_source = _load_helpers()

    cases = [
        # Accepted scopes
        ("_global", True),
        ("_domain_python", True),
        ("_domain_docker", True),
        ("_domain_a", True),
        ("/home/user/proj", True),
        ("/", True),
        ("//server/share", True),
        ("\\\\server\\share", True),
        ("C:/Users/foo", True),
        ("C:\\Users\\foo", True),
        ("D:/Projects/example-project", True),

        # Rejected scopes
        ("", False),
        ("   ", False),
        ("_domain_", False),          # empty domain name
        ("general", False),           # the historical silent fallback
        ("scratch", False),
        ("my-project", False),
        ("./relative", False),
        ("..", False),
        ("_globa", False),            # typo guard
        ("_domain", False),           # missing trailing underscore+name
    ]

    failed = 0
    for inp, expected_ok in cases:
        err = validate(inp)
        got_ok = err is None
        status = "PASS" if got_ok == expected_ok else "FAIL"
        if status == "FAIL":
            failed += 1
            print(
                f"  FAIL  {inp!r}: got err={err!r}, expected ok={expected_ok}"
            )
        else:
            print(f"  PASS  {inp!r}")

    source_cases = [
        (None, None, False),
        ("user-stated", "user-stated", False),
        ("USER-STATED", "user-stated", True),
        ("user", "user-stated", True),
        ("human", "user-stated", True),
        ("tool", "tool-result", True),
        ("tool_call", "tool-result", True),
        ("assistant", "model-inferred", True),
        ("inferred", "model-inferred", True),
    ]
    for inp, expected, expect_warning in source_cases:
        got, warning, err = normalize_source(inp)
        ok = got == expected and err is None and bool(warning) == expect_warning
        if not ok:
            failed += 1
            print(
                f"  FAIL  source {inp!r}: got={got!r}, "
                f"warning={warning!r}, err={err!r}"
            )
        else:
            print(f"  PASS  source {inp!r}")

    for bad in ("user-said", "gospel", 42):
        got, warning, err = normalize_source(bad)
        ok = got is None and warning is None and err
        if not ok:
            failed += 1
            print(
                f"  FAIL  bad source {bad!r}: got={got!r}, "
                f"warning={warning!r}, err={err!r}"
            )
        else:
            print(f"  PASS  bad source {bad!r}")

    print("---")
    total = len(cases) + len(source_cases) + 3
    print(f"{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
