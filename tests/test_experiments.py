"""#4 — autonomous flag-experiment decision logic (pure)."""

from __future__ import annotations

from decimal import Decimal

from helm.experiments import ArmStats, decide_ab


def test_keep_on_when_treatment_beats_baseline():
    # ON exp/trade 10 > OFF 5 → keep
    assert decide_ab(ArmStats(10, Decimal("100")), ArmStats(10, Decimal("50"))) == "keep_on"


def test_revert_when_treatment_ties_or_loses():
    assert decide_ab(ArmStats(10, Decimal("50")), ArmStats(10, Decimal("50"))) == "revert_off"
    assert decide_ab(ArmStats(10, Decimal("10")), ArmStats(10, Decimal("80"))) == "revert_off"


def test_revert_when_either_arm_has_no_evidence():
    assert decide_ab(ArmStats(0, Decimal("0")), ArmStats(10, Decimal("50"))) == "revert_off"
    assert decide_ab(ArmStats(10, Decimal("50")), ArmStats(0, Decimal("0"))) == "revert_off"


def test_expectancy_is_net_over_n():
    assert ArmStats(4, Decimal("20")).expectancy == Decimal("5")
    assert ArmStats(0, Decimal("0")).expectancy == Decimal("0")
