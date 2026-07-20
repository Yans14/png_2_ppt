#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_TRACKED_SUFFIXES = {
    ".pptx",
    ".pptm",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".zip",
}
REQUIRED_IGNORES = {
    ".env",
    ".env.*",
    "*.pptx",
    "*.pptm",
    "benchmarks/cache/",
    "benchmarks/results/",
    "artifacts/",
    "out/",
}
SECRET_PATTERNS = {
    "OpenAI API key": re.compile(r"\bsk-(?!example|test|your)[A-Za-z0-9_-]{20,}"),
    "GitHub token": re.compile(r"\bgh[opsu]_[A-Za-z0-9]{30,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def repository_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode() for item in completed.stdout.split(b"\0") if item]


def main() -> int:
    failures: list[str] = []
    files = repository_files()
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        if path.suffix.casefold() in FORBIDDEN_TRACKED_SUFFIXES:
            failures.append(f"binary/client artifact is tracked: {relative}")
        if relative == ".env" or (relative.startswith(".env.") and relative != ".env.example"):
            failures.append(f"environment secret file is tracked: {relative}")
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                failures.append(f"possible {label} in {relative}")

    ignore_lines = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    for required in sorted(REQUIRED_IGNORES - ignore_lines):
        failures.append(f"required .gitignore rule is missing: {required}")

    if failures:
        print("Public repository safety checks failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"Public repository safety checks passed for {len(files)} repository files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
