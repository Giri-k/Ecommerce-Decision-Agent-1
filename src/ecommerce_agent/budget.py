"""Calculate token-cap cost estimates; this module does not enforce spending.

The output cap must include all billed output tokens, including reasoning tokens.
Estimates use uncached input rates and exclude development usage, hosting,
embeddings, other provider charges, and any budget reserve.
"""

from dataclasses import dataclass
from decimal import Decimal


def _validate_count(name: str, value: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer, not a boolean or other type")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")


def _validate_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")


@dataclass(frozen=True)
class TokenRates:
    input_per_million_usd: Decimal
    output_per_million_usd: Decimal

    def __post_init__(self) -> None:
        _validate_decimal("input_per_million_usd", self.input_per_million_usd)
        _validate_decimal("output_per_million_usd", self.output_per_million_usd)


@dataclass(frozen=True)
class InvestigationLimits:
    input_tokens: int = 18_000
    output_tokens: int = 3_000

    def __post_init__(self) -> None:
        _validate_count("input_tokens", self.input_tokens)
        _validate_count("output_tokens", self.output_tokens)


@dataclass(frozen=True)
class UsageEstimate:
    investigations: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal

    def __post_init__(self) -> None:
        _validate_count("investigations", self.investigations)
        _validate_count("input_tokens", self.input_tokens)
        _validate_count("output_tokens", self.output_tokens)
        _validate_decimal("cost_usd", self.cost_usd)


# GPT-5 nano standard uncached input and output rates, in USD per million tokens.
DEFAULT_TOKEN_RATES = TokenRates(Decimal("0.05"), Decimal("0.40"))
DEFAULT_INVESTIGATION_LIMITS = InvestigationLimits()
_TOKENS_PER_MILLION = Decimal(1_000_000)
LLM_POLICY_ARMS = ("single_agent", "multi_agent")
CORE_EPISODES_PER_POLICY = 90  # Thirty scenarios and three matched replicates.
REVIEWS_PER_EPISODE = 4


def estimate_usage(
    investigations: int,
    rates: TokenRates = DEFAULT_TOKEN_RATES,
    limits: InvestigationLimits = DEFAULT_INVESTIGATION_LIMITS,
) -> UsageEstimate:
    """Estimate usage at each investigation's complete input and output caps."""
    _validate_count("investigations", investigations)
    if not isinstance(rates, TokenRates):
        raise TypeError("rates must be TokenRates")
    if not isinstance(limits, InvestigationLimits):
        raise TypeError("limits must be InvestigationLimits")

    input_tokens = investigations * limits.input_tokens
    output_tokens = investigations * limits.output_tokens
    cost_usd = (
        Decimal(input_tokens) * rates.input_per_million_usd
        + Decimal(output_tokens) * rates.output_per_million_usd
    ) / _TOKENS_PER_MILLION
    return UsageEstimate(investigations, input_tokens, output_tokens, cost_usd)


def evaluation_estimates(
    rates: TokenRates = DEFAULT_TOKEN_RATES,
    limits: InvestigationLimits = DEFAULT_INVESTIGATION_LIMITS,
) -> dict[str, UsageEstimate]:
    """Return combined single-agent and multi-agent token-cap estimates.

    Each policy episode has four reviews. A multi-agent investigation shares
    one whole-team token cap, rather than receiving a cap per specialist.
    Reduced/full sensitivity scales are alternatives, additional to core.
    The fixed numerical arm has no LLM token usage.
    """
    return {
        name: estimate_usage(
            episodes_per_policy * REVIEWS_PER_EPISODE * len(LLM_POLICY_ARMS),
            rates=rates,
            limits=limits,
        )
        for name, episodes_per_policy in (
            ("core", CORE_EPISODES_PER_POLICY),
            ("reduced_sensitivity", 96),
            ("full_sensitivity", 240),
        )
    }
