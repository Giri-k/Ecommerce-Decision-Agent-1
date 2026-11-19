"""Budget arithmetic and validation, without model calls or provider access."""

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from ecommerce_agent.budget import (
    InvestigationLimits,
    TokenRates,
    estimate_usage,
    evaluation_estimates,
)


class BudgetTests(unittest.TestCase):
    def test_evaluation_matrices_have_known_token_and_cost_totals(self) -> None:
        estimates = evaluation_estimates()
        expected = {
            "core": (720, 12_960_000, 2_160_000, Decimal("1.512")),
            "reduced_sensitivity": (768, 13_824_000, 2_304_000, Decimal("1.6128")),
            "full_sensitivity": (1920, 34_560_000, 5_760_000, Decimal("4.032")),
        }
        self.assertEqual(set(estimates), set(expected))
        for name, totals in expected.items():
            with self.subTest(matrix=name):
                result = estimates[name]
                self.assertEqual(
                    (
                        result.investigations,
                        result.input_tokens,
                        result.output_tokens,
                        result.cost_usd,
                    ),
                    totals,
                )

    def test_custom_rates_and_caps_use_separate_token_categories(self) -> None:
        result = estimate_usage(
            2,
            rates=TokenRates(Decimal("1.25"), Decimal("10.00")),
            limits=InvestigationLimits(input_tokens=1_000, output_tokens=250),
        )
        self.assertEqual(result.input_tokens, 2_000)
        self.assertEqual(result.output_tokens, 500)
        self.assertEqual(result.cost_usd, Decimal("0.0075"))

    def test_zero_investigations_and_zero_caps_cost_nothing(self) -> None:
        self.assertEqual(estimate_usage(0).cost_usd, Decimal(0))
        result = estimate_usage(4, limits=InvestigationLimits(0, 0))
        self.assertEqual(result.cost_usd, Decimal(0))
        self.assertEqual(result.input_tokens, 0)
        self.assertEqual(result.output_tokens, 0)

    def test_invalid_investigation_counts_are_rejected(self) -> None:
        for value in (True, False, 1.5, Decimal(1), "1", None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                estimate_usage(value)
        with self.assertRaises(ValueError):
            estimate_usage(-1)

    def test_invalid_caps_are_rejected_for_both_categories(self) -> None:
        for name in ("input_tokens", "output_tokens"):
            for value in (True, False, 1.5, "1", None):
                with self.subTest(name=name, value=value), self.assertRaises(TypeError):
                    InvestigationLimits(**{name: value})
            with self.subTest(name=name), self.assertRaises(ValueError):
                InvestigationLimits(**{name: -1})

    def test_invalid_rates_are_rejected_for_both_categories(self) -> None:
        for name in ("input_per_million_usd", "output_per_million_usd"):
            for value in (Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")):
                rates = {
                    "input_per_million_usd": Decimal("0.05"),
                    "output_per_million_usd": Decimal("0.40"),
                    name: value,
                }
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    TokenRates(**rates)
            for value in (True, 0.05, 1, "0.05", None):
                rates = {
                    "input_per_million_usd": Decimal("0.05"),
                    "output_per_million_usd": Decimal("0.40"),
                    name: value,
                }
                with self.subTest(name=name, value=value), self.assertRaises(TypeError):
                    TokenRates(**rates)

    def test_returned_estimate_is_immutable(self) -> None:
        result = estimate_usage(1)
        with self.assertRaises(FrozenInstanceError):
            result.cost_usd = Decimal(0)


if __name__ == "__main__":
    unittest.main()
