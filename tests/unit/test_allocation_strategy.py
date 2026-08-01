"""Unit tests for spot allocation strategy handling (#84).

Two defects are covered here.

First, ``spot_fleet.py`` and the ``detached.py`` bastion script both hardcoded
``lowestPrice``, silently ignoring the ``spot_allocation_strategy`` the caller
configured and picking the pools with the least spare capacity -- the most
interruption-prone choice available.

Second, the two fleet APIs spell the same enum differently and each rejects the
other's spelling. Verified against real EC2 in us-east-1 with a request anchored
on a malformed AMI so no fleet could be created::

    RequestSpotFleet, AllocationStrategy="price-capacity-optimized"
        -> InvalidParameterValue: ... failed to satisfy constraint
    RequestSpotFleet, AllocationStrategy="priceCapacityOptimized"
        -> InvalidParameterValue: Invalid value 'ami-notavalidamiid' for amiId
           (i.e. the strategy was accepted; the AMI was the only complaint)
    CreateFleet,      AllocationStrategy="priceCapacityOptimized"
        -> InvalidParameter: AllocationStrategy must be "lowest-price", ...

So the kebab-case value the provider documents has to be converted at the
RequestSpotFleet boundary. Passing it through unconverted would break every
spot fleet request.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025-2026 Scott Friedman and Project Contributors
"""

import ast
from unittest.mock import MagicMock

import pytest

from parsl_ephemeral_aws.constants import (
    DEFAULT_SPOT_ALLOCATION_STRATEGY,
    EC2_FLEET_ALLOCATION_STRATEGIES,
    EC2_FLEET_DEFAULT_ALLOCATION_STRATEGY,
    SPOT_FLEET_ALLOCATION_STRATEGIES,
    SPOT_FLEET_DEFAULT_ALLOCATION_STRATEGY,
)
from parsl_ephemeral_aws.utils.aws import normalize_spot_fleet_allocation_strategy


pytestmark = pytest.mark.unit


class TestNormalizeSpotFleetAllocationStrategy:
    """Kebab-case in, the camelCase spelling RequestSpotFleet accepts out."""

    @pytest.mark.parametrize(
        "kebab,camel",
        [
            ("price-capacity-optimized", "priceCapacityOptimized"),
            ("capacity-optimized", "capacityOptimized"),
            ("capacity-optimized-prioritized", "capacityOptimizedPrioritized"),
            ("lowest-price", "lowestPrice"),
            ("diversified", "diversified"),
        ],
    )
    def test_converts_every_documented_strategy(self, kebab, camel):
        assert normalize_spot_fleet_allocation_strategy(kebab) == camel

    @pytest.mark.parametrize("camel", sorted(SPOT_FLEET_ALLOCATION_STRATEGIES))
    def test_camel_case_passes_through(self, camel):
        """A caller who supplied the API-native spelling is not punished."""
        assert normalize_spot_fleet_allocation_strategy(camel) == camel

    def test_every_ec2_fleet_value_has_a_spot_fleet_equivalent(self):
        """The two enums must cover the same set of strategies."""
        converted = {
            normalize_spot_fleet_allocation_strategy(s)
            for s in EC2_FLEET_ALLOCATION_STRATEGIES
        }
        assert converted == set(SPOT_FLEET_ALLOCATION_STRATEGIES)

    def test_rejects_an_unknown_strategy(self):
        """Better here than several seconds and one IAM role later, from EC2."""
        with pytest.raises(ValueError, match="Unsupported spot allocation strategy"):
            normalize_spot_fleet_allocation_strategy("cheapest-possible")

    def test_error_lists_the_accepted_values(self):
        with pytest.raises(ValueError) as excinfo:
            normalize_spot_fleet_allocation_strategy("nonsense")

        assert "priceCapacityOptimized" in str(excinfo.value)

    def test_rejects_a_non_string(self):
        """A MagicMock reaching ``.split()`` unpacks to nothing.

        The resulting "not enough values to unpack" names neither the argument
        nor the caller, which is exactly what a mock-heavy test suite hands in.
        """
        with pytest.raises(ValueError, match="must be a string"):
            normalize_spot_fleet_allocation_strategy(MagicMock())

        with pytest.raises(ValueError, match="must be a string"):
            normalize_spot_fleet_allocation_strategy(None)


class TestAllocationStrategyConstants:
    """The defaults must be valid for the API each one is used with."""

    def test_default_is_price_capacity_optimized(self):
        """AWS's current recommendation, replacing capacity-optimized."""
        assert DEFAULT_SPOT_ALLOCATION_STRATEGY == "price-capacity-optimized"

    def test_user_facing_default_is_valid_for_ec2_fleet(self):
        assert DEFAULT_SPOT_ALLOCATION_STRATEGY in EC2_FLEET_ALLOCATION_STRATEGIES

    def test_spot_fleet_default_is_camel_case(self):
        """RequestSpotFleet rejects the kebab-case spelling outright."""
        assert (
            SPOT_FLEET_DEFAULT_ALLOCATION_STRATEGY in SPOT_FLEET_ALLOCATION_STRATEGIES
        )
        assert "-" not in SPOT_FLEET_DEFAULT_ALLOCATION_STRATEGY

    def test_ec2_fleet_default_is_kebab_case(self):
        """CreateFleet rejects the camelCase spelling outright."""
        assert EC2_FLEET_DEFAULT_ALLOCATION_STRATEGY in EC2_FLEET_ALLOCATION_STRATEGIES
        assert "-" in EC2_FLEET_DEFAULT_ALLOCATION_STRATEGY

    def test_the_two_defaults_are_the_same_strategy(self):
        """Differently spelled, but the same behaviour from AWS."""
        assert (
            normalize_spot_fleet_allocation_strategy(
                EC2_FLEET_DEFAULT_ALLOCATION_STRATEGY
            )
            == SPOT_FLEET_DEFAULT_ALLOCATION_STRATEGY
        )

    def test_no_default_is_lowest_price(self):
        """lowestPrice concentrates on the shallowest pools; that was the bug."""
        assert "lowest" not in DEFAULT_SPOT_ALLOCATION_STRATEGY.lower()
        assert "lowest" not in SPOT_FLEET_DEFAULT_ALLOCATION_STRATEGY.lower()
        assert "lowest" not in EC2_FLEET_DEFAULT_ALLOCATION_STRATEGY.lower()


class TestNoHardcodedLowestPriceRemains:
    """Guard against the hardcoded value being reintroduced.

    Both call sites bypassed the configured strategy entirely, so a value-level
    assertion in a mode test would not have caught it -- the constant was never
    read. Checking the source is the check that matches the defect.
    """

    def test_spot_fleet_manager_does_not_hardcode_a_strategy(self):
        import inspect

        from parsl_ephemeral_aws.compute import spot_fleet

        source = inspect.getsource(spot_fleet)
        assert '"AllocationStrategy": "lowestPrice"' not in source

    def test_bastion_script_strategy_is_injected(self):
        """The bastion runs standalone and cannot import from this package.

        The strategy therefore has to be substituted in as a literal, and the
        generated script must not carry the hardcoded lowestPrice.
        """
        import inspect

        from parsl_ephemeral_aws.modes import detached

        source = inspect.getsource(detached)
        assert "'AllocationStrategy': 'lowestPrice'" not in source
        assert "ALLOCATION_STRATEGY = " in source


class TestBastionScriptInjection:
    """The generated bastion script must carry the configured strategy.

    The source-level guard above proves the hardcoded value is gone; this proves
    the replacement actually substitutes. A substitution whose search string
    drifts from the template fails silently -- ``str.replace`` finding nothing is
    not an error -- so the generated text is what has to be asserted on.

    The spelling asserted on is kebab-case, and the *whole point* of these two
    tests is which one. The bastion called ``RequestSpotFleet`` when they were
    written, which takes camelCase; it now calls ``CreateFleet`` (#86), which
    rejects camelCase outright with ``InvalidParameter``. So the conversion these
    tests used to demand would break every fleet the bastion launches -- and it
    would break it *on the bastion*, in a standalone script on a remote instance,
    where the error surfaces only in that instance's log.
    """

    def _script(self, **kwargs):
        from parsl_ephemeral_aws.modes.detached import DetachedMode

        mode = DetachedMode(
            provider_id="test-provider",
            session=MagicMock(),
            state_store=MagicMock(),
            workflow_id="test-workflow",
            instance_type="t3.small",
            image_id="ami-12345678",
            region="us-east-1",
            vpc_id="vpc-12345",
            subnet_id="subnet-12345",
            security_group_id="sg-12345",
            **kwargs,
        )
        return mode._get_bastion_manager_script()

    def _injected_value(self, script):
        for line in script.splitlines():
            if line.startswith("ALLOCATION_STRATEGY = "):
                return ast.literal_eval(
                    line.removeprefix("ALLOCATION_STRATEGY = ").split("  #")[0]
                )
        raise AssertionError("script defines no ALLOCATION_STRATEGY constant")

    def test_configured_strategy_is_injected_as_kebab_case(self):
        """The value must reach the script in the spelling CreateFleet takes."""
        script = self._script(spot_allocation_strategy="capacity-optimized")

        assert self._injected_value(script) == "capacity-optimized"

    def test_default_strategy_is_injected(self):
        assert (
            self._injected_value(self._script())
            == EC2_FLEET_DEFAULT_ALLOCATION_STRATEGY
        )

    def test_a_camel_case_strategy_is_converted_for_the_bastion(self):
        """A caller may supply either spelling; only one works on the bastion.

        ``normalize_ec2_fleet_allocation_strategy`` runs at the substitution, so a
        caller who passed the ``RequestSpotFleet`` spelling still gets a script
        that ``CreateFleet`` accepts. Without it the value would travel verbatim
        and fail on the instance rather than here.
        """
        script = self._script(spot_allocation_strategy="capacityOptimized")

        assert self._injected_value(script) == "capacity-optimized"

    def test_generated_script_is_valid_python(self):
        """The substitution must not break the script the bastion has to run."""
        ast.parse(self._script(spot_allocation_strategy="diversified"))

    def test_script_contains_no_hardcoded_lowest_price(self):
        script = self._script()

        assert "'AllocationStrategy': 'lowestPrice'" not in script
        assert "lowestPrice" not in script

    def test_the_script_calls_create_fleet_and_not_the_legacy_api(self):
        """The strategy's spelling only makes sense against one of the two APIs.

        Pinning which API the script calls is what makes the kebab-case
        assertions above meaningful rather than arbitrary -- if the bastion went
        back to ``RequestSpotFleet``, they would be asserting the spelling that
        breaks it.
        """
        script = self._script()

        assert "ec2.create_fleet(" in script
        assert "request_spot_fleet" not in script
        assert "cancel_spot_fleet_requests" not in script
