"""Which target does this scan belong to?

The design's invariant governs this module: a scan enters exactly ONE instance,
and only after its destination is SETTLED. There is deliberately no fallback
target — an undecidable scan is staged for a human, never guessed into whichever
instance happens to be first. A misroute across independent instances is a
cross-boundary leak, and it is not undoable: within seconds of ingest the
document has become chunks, embeddings, extracted facts and a filed copy.

Phase 1 implements L1 only (the operator declares the destination) plus the
n == 1 short-circuit. L2 (barcode separator sheets) and L3 (content
classification behind a confidence gate) slot in at the marked points without
changing this function's contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config, ScanTarget

logger = logging.getLogger("renfield-mcp-scanner.router")


@dataclass
class Routing:
    target: ScanTarget | None
    layer: str          # declared | single | separator | classified | undecided
    confidence: float   # 1.0 for deterministic layers
    reason: str = ""

    @property
    def settled(self) -> bool:
        return self.target is not None


def route(config: Config, declared: str = "") -> Routing:
    """Resolve a destination, or return an UNSETTLED routing for human review."""
    if declared:
        target = config.target_by_id(declared)
        if target is None:
            known = [t.id for t in config.targets]
            # An unknown id is refused rather than coerced: a typo must not
            # become a silent route into the wrong instance.
            return Routing(None, "undecided", 0.0,
                           f"unknown target {declared!r}; known: {known}")
        return Routing(target, "declared", 1.0, "operator declared the destination")

    single = config.single_target
    if single is not None:
        # n == 1: nothing to decide, and no routing machinery should run.
        return Routing(single, "single", 1.0, "only one target is configured")

    # L2 separator sheets and L3 classification attach here (Phases 2 and 3).
    return Routing(None, "undecided", 0.0,
                   "no destination declared and more than one target is configured")
