"""
Kill-switch — full system halt.

Single user action. Within 10 seconds:
  1. Stops all strategies (set state = MANUAL_HOLD).
  2. Cancels all open orders on Zerodha (kite.cancel_order for each).
  3. Cancels all open orders on IBKR (ib.cancelOrder for each).
  4. Squares off any open MIS positions.
  5. Writes audit event; notifies owner.
  6. Owner must explicitly re-arm before autonomous trading resumes.

PRD reference: §12.5 Kill-Switch sequence. The four-step cancel + square-off
flow is what dry-run G4 proves works.
"""

import logging

logger = logging.getLogger(__name__)


def kill(reason: str = "user-initiated") -> None:
    """TODO: implement Phase 3.

    Skeleton (uses broker adapters that will exist in helm/brokers/):

      from helm.brokers import zerodha, ibkr
      from helm.orchestrator import audit, notify, state

      state.set_manual_hold(reason=reason)
      logger.warning("KILL-SWITCH engaged: %s", reason)

      # Defense in depth: stop strategies AND cancel directly.
      try:
          zerodha.cancel_all_orders()
          zerodha.square_off_all_mis()
      except Exception as exc:
          logger.exception("zerodha cancel/square-off failed: %s", exc)

      try:
          ibkr.cancel_all_orders()
      except Exception as exc:
          logger.exception("ibkr cancel failed: %s", exc)

      audit.record("kill_switch", {"reason": reason})
      notify.alert(
          subject="Helm KILL-SWITCH engaged",
          body=f"Reason: {reason}. All trading halted. Owner re-arm required.",
      )
    """
    raise NotImplementedError("kill_switch.kill — Phase 3")
