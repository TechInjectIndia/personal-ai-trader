"""
Sleeve halt — narrower than kill-switch.

When the intraday sleeve breaches its daily-loss cap mid-session:
  1. Cancel all open intraday orders on Zerodha.
  2. Square off all open MIS positions.
  3. Block new intraday entries until next session.
  4. Write audit event; notify owner.
  5. Passive sleeve continues normally.

PRD reference: §11 sleeve_halt module, §12.3 intraday hard limits.
"""


def halt_intraday_sleeve(reason: str) -> None:
    """TODO: implement Phase 7. Calls into broker.zerodha to cancel + square off."""
    raise NotImplementedError("sleeve_halt.halt_intraday_sleeve — Phase 7")
