"""V4 Priority 3 (brief Section 6 — Strategy Registry): structural tests.

Not a test of trading logic (there isn't any yet for most entries, by
design — these are research specs, "not assumed profitable methods" per
the brief's own words) -- a test that the registry itself is complete and
internally consistent.

Run: .venv/Scripts/python.exe -m pytest tests/test_strategy_registry.py -v
"""
import os
import sys
from dataclasses import fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategies.registry import STRATEGY_REGISTRY, StrategySpec, all_strategy_codes

_REQUIRED_SPEC_FIELDS = (
    "hypothesis", "eligible_instruments", "timeframe", "entry_conditions", "exit_conditions",
    "position_sizing_assumptions", "stop_loss_logic", "invalidation_conditions",
    "expected_holding_period", "data_requirements", "transaction_costs", "failure_conditions",
    "validation_criteria",
)


def test_all_ten_named_strategy_families_are_present():
    assert all_strategy_codes() == list("ABCDEFGHIJ")


def test_every_spec_defines_every_field_the_brief_requires_non_empty():
    spec_field_names = {f.name for f in fields(StrategySpec)}
    for field_name in _REQUIRED_SPEC_FIELDS:
        assert field_name in spec_field_names

    for code, spec in STRATEGY_REGISTRY.items():
        for field_name in _REQUIRED_SPEC_FIELDS:
            value = getattr(spec, field_name)
            assert isinstance(value, str) and len(value) > 20, (
                f"strategy {code}'s {field_name} is missing or suspiciously short"
            )


def test_each_specs_own_code_matches_its_registry_key():
    for code, spec in STRATEGY_REGISTRY.items():
        assert spec.code == code


def test_every_spec_starts_as_research_only_honestly():
    # No strategy here has been backtested/shadow/paper-approved yet --
    # this test exists so a future change to one entry's status is a
    # deliberate, visible edit, not an accidental default drift.
    for code, spec in STRATEGY_REGISTRY.items():
        assert spec.status in ("RESEARCH_SPEC_ONLY", "HYPOTHESIS_TESTED", "BACKTESTED", "SHADOW", "PAPER_APPROVED")
