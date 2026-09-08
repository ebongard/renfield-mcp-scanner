"""`renfield-mcp-scanner-scan` — read-only preflight.

Every sibling ships one. It touches no paper and pushes nothing: it verifies the
scanner is reachable, the registry parses, and every target's token is actually
resolvable, then exits 1 if anything is wrong so a launch script can gate on it.
"""

from __future__ import annotations

import asyncio
import sys

from .config import MissingTokenError, load_config
from . import sane


async def _run() -> int:
    problems = 0
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 - preflight reports, never crashes
        print(f"config: FAILED — {exc}")
        return 1
    print(f"config: {len(config.targets)} target(s), staging {config.staging_dir}")

    for target in config.targets:
        try:
            target.token()
            print(f"  target {target.id}: token OK ({target.base_url})")
        except MissingTokenError as exc:
            print(f"  target {target.id}: {exc}")
            problems += 1

    try:
        device = await sane.find_device(config.device)
        sensors = await sane.read_sensors(device)
        print(f"scanner: {device}")
        print(f"  ready={sensors.ready} cover_open={sensors.cover_open} "
              f"error_code={sensors.error_code}")
        if not sensors.ready:
            problems += 1
    except sane.ScannerError as exc:
        print(f"scanner: FAILED — {exc}")
        problems += 1

    print("preflight: " + ("OK" if problems == 0 else f"{problems} problem(s)"))
    return 1 if problems else 0


def main() -> None:
    sys.exit(asyncio.run(_run()))
