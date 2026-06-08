"""
Regression checks for installer dependency preflights.

These are text-level guards because the installers are shell scripts.

Usage:
    python tests/test_installer_dependency_preflight.py
"""

import os
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    checks: list[tuple[str, bool]] = []

    windows = read("install_windows.ps1")
    linux = read("install_linux.sh")

    checks.append((
        "windows docker model download preflights Hugging Face from container",
        "function Test-HuggingFaceFromDocker" in windows
        and "Docker container cannot reach Hugging Face" in windows
        and "Test-HuggingFaceFromDocker $composeProfile" in windows,
    ))
    checks.append((
        "linux docker model download preflights Hugging Face from container",
        "check_huggingface_from_docker()" in linux
        and "Docker container cannot reach Hugging Face" in linux
        and "check_huggingface_from_docker" in linux[
            linux.index("mkdir -p models") : linux.index("docker compose --progress=plain", linux.index("mkdir -p models"))
        ],
    ))
    checks.append((
        "windows full/docker password generation does not call host python",
        'python -c "import secrets; print(secrets.token_urlsafe(24))"' not in windows
        and "New-UrlSafeToken" in windows,
    ))
    checks.append((
        "linux full/docker password generation does not call host python",
        "python3 -c \"import secrets" not in linux
        and "generate_token()" in linux,
    ))
    checks.append((
        "linux client manifest write does not require host python",
        "install_mode': 'client'" not in linux
        and '"install_mode": "client"' in linux,
    ))
    checks.append((
        "preflight guidance names DNS/proxy/firewall, not Hugging Face CLI",
        "DNS/proxy/firewall" in windows
        and "DNS/proxy/firewall" in linux
        and "huggingface cli" not in windows.lower()
        and "huggingface cli" not in linux.lower(),
    ))

    failed = 0
    for label, ok in checks:
        if ok:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    print("---")
    print(f"{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
