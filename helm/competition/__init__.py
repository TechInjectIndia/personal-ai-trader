"""Competition league — multiple AI agents trading isolated wallets.

Each competitor is a row in `competitors` with its own `competitor_wallets`
cash pool. Freestyle competitors (autonomy_level='freestyle') emit their own
OPEN/CLOSE/HOLD trade-intent actions from a market snapshot; those actions are
validated by the per-competitor risk gate and routed into the competitor's own
wallet. The incumbent 'house-claude' keeps its existing strategy→decider→
paper_execute cron pipeline untouched — this package never touches that path.
"""
