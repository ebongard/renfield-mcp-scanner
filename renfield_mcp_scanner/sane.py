"""SANE wrapper: device discovery, live sensors, and batch scanning.

Everything here is driven through the `scanimage` CLI rather than a SANE
binding, because the CLI is what is actually installed on an operator host and
its behaviour is what the measurements in pipeline.py were taken against.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("renfield-mcp-scanner.sane")

# A finished stack ENDS with this: it is the normal terminator, not a fault.
_TERMINATOR = "out of documents"
# Emitted on EVERY run when an option is quantised to the device's step size
# ("rounded value of page-height from 297 to 296.994"). Treating any
# "scanimage:" line as an error would fault every single scan.
_BENIGN = re.compile(r"out of documents|rounded value")


class ScannerError(RuntimeError):
    """A scanner fault. Carries the device's own message."""


class ScannerBusyError(ScannerError):
    """The device is already scanning."""


@dataclass
class ScanResult:
    pages: list[Path]
    log: str
    faulted: bool = False
    fault_detail: str = ""


@dataclass
class Sensors:
    """Live hardware state. Honest — read from the device, never inferred."""

    page_loaded: bool = False
    cover_open: bool = False
    power_save: bool = False
    double_feed: bool = False
    scan_button: bool = False
    error_code: int = 0
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return not self.cover_open and self.error_code == 0


async def _run(*args: str, timeout: float = 600.0) -> tuple[int, str]:
    """Run scanimage, returning (exit status, combined output)."""
    if not shutil.which("scanimage"):
        raise ScannerError("scanimage not found — install sane-backends")
    proc = await asyncio.create_subprocess_exec(
        "scanimage", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ScannerError(f"scanimage timed out after {timeout}s") from None
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def find_device(preferred: str = "") -> str:
    """Resolve the scanner device name. Empty preference -> first Fujitsu."""
    if preferred:
        return preferred
    _, out = await _run("-f", "%d%n", timeout=60)
    for line in out.splitlines():
        if "fujitsu" in line.lower():
            return line.strip()
    raise ScannerError("no Fujitsu scanner found (is the vendor software holding it?)")


async def read_sensors(device: str) -> Sensors:
    """Read the live sensor group. Cheap, and the basis of scanner_status."""
    _, out = await _run("-d", device, "-A", timeout=60)
    raw: dict[str, str] = {}
    for m in re.finditer(r"^\s+--([a-z0-9-]+)\[?=?.*?\[(\w+)\]\s*\[hardware\]", out, re.M):
        raw[m.group(1)] = m.group(2)
    yes = lambda k: raw.get(k, "no") == "yes"  # noqa: E731
    code = raw.get("error-code", "0")
    return Sensors(
        page_loaded=yes("page-loaded"), cover_open=yes("cover-open"),
        power_save=yes("power-save"), double_feed=yes("double-feed"),
        scan_button=yes("scan"),
        error_code=int(code) if code.isdigit() else 0,
        raw=raw,
    )


async def scan_batch(
    device: str, out_dir: Path, *, source: str, mode: str, resolution: int,
    page_height_mm: float, deskew: bool = False, crop: bool = False,
    skip_blank: float = 2.0, timeout: float = 1800.0,
) -> ScanResult:
    """Scan the whole feeder into out_dir as p0001.png...

    Two measured decisions are baked in as defaults:

    * ``page_height_mm`` is A4 (297), NOT SANE's US Letter default (279.364),
      which silently truncates 17.7mm off the bottom of every A4 page.
    * ``deskew`` is OFF. Scanner-side --swdeskew ESTIMATES the angle from page
      content and gets it badly wrong on a sparse back side (measured ~34
      degrees of rotation it invented). A crooked feed skews BOTH sides of a
      sheet, so a one-sided tilt is always this bug, never the paper.

    A fault mid-stack is REPORTED, never swallowed. scanimage signals a jam,
    double feed, opened cover or USB error through its exit status and still
    leaves the pages it managed to scan on disk; checking only "did any page
    come out?" files a silently truncated document, which for an archive is
    worse than a loud failure because it is found only when the page is needed.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    status, log = await _run(
        "-d", device,
        "--source", source, "--mode", mode, "--resolution", str(resolution),
        "--page-height", str(page_height_mm),
        "--swdeskew=yes" if deskew else "--swdeskew=no",
        "--swcrop=yes" if crop else "--swcrop=no",
        "--swskip", str(skip_blank),
        "--format=png", f"--batch={out_dir}/p%04d.png",
        timeout=timeout,
    )
    pages = sorted(out_dir.glob("p*.png"))
    detail = "\n".join(
        ln for ln in log.splitlines()
        if ln.startswith("scanimage:") and not _BENIGN.search(ln)
    )
    if detail:
        logger.error("scanner faulted mid-stack after %d page(s): %s", len(pages), detail)
        return ScanResult(pages=pages, log=log, faulted=True, fault_detail=detail)
    if status != 0 and _TERMINATOR not in log and not pages:
        return ScanResult(pages=pages, log=log, faulted=True,
                          fault_detail=f"scanimage exited {status}")
    return ScanResult(pages=pages, log=log)
