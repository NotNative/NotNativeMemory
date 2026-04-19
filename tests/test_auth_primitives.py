"""Unit tests for lib/auth.py. No DB, no HTTP."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib import auth


def run():
    failed = 0

    def check(label, condition):
        nonlocal failed
        if condition:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    # -- hash_secret / verify_secret roundtrip --------------------------------
    h1 = auth.hash_secret("correct-horse-battery-staple")
    check("hash starts with scrypt$", h1.startswith("scrypt$"))
    check(
        "hash has 6 fields",
        len(h1.split("$")) == 6,
    )
    check("correct secret verifies", auth.verify_secret("correct-horse-battery-staple", h1))
    check(
        "wrong secret rejects",
        not auth.verify_secret("wrong", h1),
    )
    check(
        "empty secret rejects",
        not auth.verify_secret("", h1),
    )

    # -- Different salts each call --------------------------------------------
    h2 = auth.hash_secret("correct-horse-battery-staple")
    check("two hashes of same password differ (salt)", h1 != h2)
    check("both hashes verify", auth.verify_secret("correct-horse-battery-staple", h2))

    # -- Malformed stored values reject without raising -----------------------
    for bad in ["", "not-a-hash", "scrypt$1$2$3", "scrypt$bad$r$p$salt$digest",
                "md5$1$1$1$c2FsdA==$ZGlnZXN0"]:
        check(f"malformed {bad!r} rejects cleanly",
              not auth.verify_secret("anything", bad))

    # -- Empty secret raises on hash --------------------------------------------
    try:
        auth.hash_secret("")
        check("hash_secret('') raises", False)
    except ValueError:
        check("hash_secret('') raises", True)

    # -- Token generation ----------------------------------------------------
    t1 = auth.generate_token()
    t2 = auth.generate_token()
    check("token has nnm_ prefix", t1.startswith("nnm_"))
    check("token is long enough", len(t1) >= 40)
    check("tokens are unique per call", t1 != t2)
    check("is_token_shaped accepts our token", auth.is_token_shaped(t1))
    check("is_token_shaped rejects nonsense",
          not auth.is_token_shaped("Bearer xyz"))
    check("is_token_shaped rejects empty", not auth.is_token_shaped(""))
    check("is_token_shaped rejects prefix only", not auth.is_token_shaped("nnm_"))

    # -- Token hash roundtrip ------------------------------------------------
    th = auth.hash_secret(t1)
    check("token hash verifies", auth.verify_secret(t1, th))
    check("other token rejects against this hash",
          not auth.verify_secret(t2, th))

    print("---")
    print(f"{len(str(failed))} passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
