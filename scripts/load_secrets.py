#!/usr/bin/env python3
"""Decrypt secrets.enc.json (SOPS + age) and write .env file."""

import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    enc_file = repo_root / "secrets.enc.json"
    env_file = repo_root / ".env"

    if not enc_file.exists():
        print(f"Error: {enc_file} not found", file=sys.stderr)
        return 1

    result = subprocess.run(
        ["sops", "decrypt", str(enc_file)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error: sops decrypt failed:\n{result.stderr}", file=sys.stderr)
        return 1

    secrets = json.loads(result.stdout)
    lines = [f"export {k}={v}" for k, v in secrets.items() if v]

    env_file.write_text("\n".join(lines) + "\n")
    print(f"Wrote {len(lines)} keys to .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
