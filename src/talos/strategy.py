"""Strategy seam — per-side sizing dispatch for the standard pipeline.

Today: standard strategy (uses ledger.unit_size) and DRIP (uses
DripConfig.per_side_contract_cap). Future strategies plug in by extending
``per_side_max_ahead`` with a new config branch.
"""

from __future__ import annotations

from talos.drip import DripConfig
from talos.position_ledger import PositionLedger, Side


def per_side_max_ahead(
    ledger: PositionLedger,
    side: Side,
    drip_config: DripConfig | None,
) -> int:
    """Strategy-aware 'allowed resting' for one side, pre-catch-up.

    DRIP events return their absolute resting cap (drip_size × max_drips).
    Non-DRIP events return the standard 'room left in the current unit',
    falling back to a full unit when filled_in_unit == 0.

    Boundary behavior: when ``filled_in_unit % unit_size == 0`` (the unit
    is exactly complete), this returns ``unit_size`` — the next unit's
    worth of resting room opens immediately.  This intentionally differs
    from ``PositionLedger.unit_remaining(side)``, which returns 0 in the
    same state because it answers a different question ("how many more
    contracts to complete THIS unit?").  Callers wanting the
    unit-completion semantics should use ``unit_remaining`` directly.

    The catch-up exception (max(this, fill_gap)) lives in the call sites
    so it stays uniform across strategies.
    """
    if drip_config is not None:
        return drip_config.per_side_contract_cap

    filled_in_unit = ledger.filled_count(side) % ledger.unit_size
    return ledger.unit_size - filled_in_unit
