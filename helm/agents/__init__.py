"""Self-improvement agent layer.

Three cooperating agents:
  * `helm.agents.pm`        — weekly product-manager review
  * `helm.agents.engineer`  — typed mutator that applies one task per run
  * `helm.agents.tester`    — reactive verifier; reverts broken releases

Shared plumbing lives in `helm.agents.base`. See AGENTS/*.md for the role
charters.
"""
