#!/usr/bin/env python3
"""Heuristic public-tree check; print locations, never suspect values."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_NAMES = {
    "auth.json", "config.toml", ".codex-global-state.json", "route_desktop_metadata.json",
    "route_history.jsonl", "restore-paths.json", ".env",
}
PRIVATE_SUFFIXES = {".sqlite", ".db", ".log", ".jsonl", ".pem", ".key", ".p12", ".pfx", ".zip"}
PRIVATE_DIRS = {"work", "outputs", "backup", "backups", "sessions", ".codex"}
SKIP_DIRS = {".git", "__pycache__"}
RULES = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "api-key": re.compile(r"\bsk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{20,}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "aws-access-id": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "bearer-token": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{20,}"),
    "jwt-token": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "literal-password": re.compile(r'''(?i)["']?(?:api_key|apiKey|password|access_token|refresh_token)["']?\s*[:=]\s*["'][^"'\n]{12,}["']'''),
}
UUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I)
DEMO_UUID = re.compile(r"00000000-0000-4000-8000-00000000000[1-4]", re.I)
USER_PATH = re.compile(r"/(?:Users|home)/([^/\s\"'<>]+)")
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def findings(root=ROOT):
    errors = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            errors.append((str(relative), 0, "symlink"))
            continue
        if not path.is_file():
            continue
        scanned += 1
        if (path.name in PRIVATE_NAMES or path.name.startswith(".env.") and path.name != ".env.example"
                or path.suffix in PRIVATE_SUFFIXES or any(part in PRIVATE_DIRS for part in relative.parts[:-1])
                or re.search(r"\.(?:sqlite|db)-", path.name)):
            errors.append((str(relative), 0, "private-runtime-file"))
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append((str(relative), 0, "unreviewed-binary"))
            continue
        for rule, expression in RULES.items():
            for match in expression.finditer(content):
                errors.append((str(relative), content.count("\n", 0, match.start()) + 1, rule))
        for match in UUID.finditer(content):
            if not DEMO_UUID.fullmatch(match[0]):
                errors.append((str(relative), content.count("\n", 0, match.start()) + 1, "non-demo-uuid"))
        for match in USER_PATH.finditer(content):
            if match[1] != "example":
                errors.append((str(relative), content.count("\n", 0, match.start()) + 1, "personal-home-path"))
        for match in EMAIL.finditer(content):
            if match[0].rsplit("@", 1)[-1].lower() not in {"example.com", "example.org", "example.net"}:
                errors.append((str(relative), content.count("\n", 0, match.start()) + 1, "non-demo-email"))
    return scanned, errors


if __name__ == "__main__":
    scanned, errors = findings()
    for path, line, rule in errors:
        print(f"{path}:{line}: {rule}")
    print(f"Scanned {scanned} files; {len(errors)} findings.")
    sys.exit(bool(errors))
