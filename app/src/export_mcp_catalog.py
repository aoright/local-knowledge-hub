#!/usr/bin/env python3
"""Export the MCP catalog used by Antigravity's local tool cache."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import knowledge_hub as kh


DEFAULT_TARGETS = [
    Path.home() / ".gemini/antigravity/mcp/local-knowledge",
]


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def export(target: Path) -> dict[str, object]:
    expected: set[str] = set()
    for tool in kh.mcp_tools():
        name = f"{tool['name']}.json"
        payload = {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["inputSchema"],
        }
        atomic_write(
            target / name,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        expected.add(name)
    atomic_write(target / "instructions.md", kh.MCP_INSTRUCTIONS)
    expected.add("instructions.md")
    return {"target": str(target), "files": len(expected), "expected": sorted(expected)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="*", type=Path, default=DEFAULT_TARGETS)
    args = parser.parse_args()
    print(json.dumps([export(target) for target in args.targets], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
