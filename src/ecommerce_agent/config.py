"""Versioned development settings; these are not final experiment assumptions."""

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

PositiveInt = Annotated[StrictInt, Field(gt=0)]
NonnegativeInt = Annotated[StrictInt, Field(ge=0)]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelConfig(ConfigModel):
    provider: Literal["openai"]
    model_id: str = Field(min_length=1)
    embedding_model_id: str = Field(min_length=1)
    embeddings_enabled: bool = Field(strict=True)
    monthly_budget_cents: PositiveInt
    reserve_cents: NonnegativeInt
    max_model_calls: PositiveInt
    max_input_tokens: PositiveInt
    max_output_tokens: PositiveInt
    max_tool_calls: PositiveInt
    max_compare_calls: PositiveInt
    max_wall_seconds: PositiveInt

    @model_validator(mode="after")
    def coherent_limits(self) -> "ModelConfig":
        if self.reserve_cents >= self.monthly_budget_cents:
            raise ValueError("reserve must be smaller than monthly budget")
        if self.max_compare_calls > self.max_tool_calls:
            raise ValueError("compare calls must fit within the tool-call budget")
        return self


class OperatingConfig(ConfigModel):
    currency: Literal["USD"]
    initial_cash_cents: NonnegativeInt
    order_fee_cents: NonnegativeInt
    case_pack: PositiveInt
    minimum_order_units: PositiveInt
    maximum_order_units: PositiveInt
    minimum_price_percent: PositiveInt
    maximum_price_percent: PositiveInt
    minimum_gross_margin_percent: Annotated[StrictInt, Field(ge=0, lt=100)]
    price_cooldown_days: NonnegativeInt

    @model_validator(mode="after")
    def coherent_policy(self) -> "OperatingConfig":
        if self.minimum_order_units > self.maximum_order_units:
            raise ValueError("minimum order exceeds maximum order")
        if self.minimum_order_units % self.case_pack or self.maximum_order_units % self.case_pack:
            raise ValueError("order bounds must be whole case packs")
        if not self.minimum_price_percent <= 100 <= self.maximum_price_percent:
            raise ValueError("price range must include the reference price")
        return self


class SimulationConfig(ConfigModel):
    episode_days: PositiveInt
    continuation_days: PositiveInt
    review_days: tuple[PositiveInt, ...]
    demand_dispersion: Annotated[Decimal, Field(gt=0, le=1000)] = Decimal("10")
    daily_holding_rate: Annotated[Decimal, Field(ge=0, le=1, decimal_places=3)] = Decimal("0.001")
    elasticity_min: Annotated[Decimal, Field(ge=-5, le=0)] = Decimal("-2.0")
    elasticity_max: Annotated[Decimal, Field(ge=-5, le=0)] = Decimal("-0.5")
    initial_inventory_days_min: PositiveInt = 7
    initial_inventory_days_max: PositiveInt = 21
    unit_cost_percent_min: Annotated[StrictInt, Field(gt=0, lt=100)] = 50
    unit_cost_percent_max: Annotated[StrictInt, Field(gt=0, lt=100)] = 70

    @model_validator(mode="after")
    def coherent_reviews(self) -> "SimulationConfig":
        if not self.review_days or tuple(sorted(set(self.review_days))) != self.review_days:
            raise ValueError("review days must be nonempty, unique, and increasing")
        if self.review_days[-1] > self.episode_days:
            raise ValueError("review days must fall within the decision episode")
        if self.elasticity_min > self.elasticity_max:
            raise ValueError("elasticity range is reversed")
        if self.initial_inventory_days_min > self.initial_inventory_days_max:
            raise ValueError("inventory coverage range is reversed")
        if self.unit_cost_percent_min > self.unit_cost_percent_max:
            raise ValueError("unit cost range is reversed")
        return self


class PlanningConfig(ConfigModel):
    forecast_model: Literal["seasonal_naive", "weekday_mean_4"] = "weekday_mean_4"
    rollout_count: Annotated[StrictInt, Field(ge=1, le=20)] = 20
    planning_seed: NonnegativeInt = 41017
    horizon: Annotated[StrictInt, Field(ge=1, le=14)] = 14
    central_elasticity: Annotated[Decimal, Field(ge=-5, le=0)] = Decimal("-1.0")
    sensitivity_elasticity_min: Annotated[Decimal, Field(ge=-5, le=0)] = Decimal("-2.0")
    sensitivity_elasticity_max: Annotated[Decimal, Field(ge=-5, le=0)] = Decimal("-0.5")

    @model_validator(mode="after")
    def coherent_elasticities(self) -> "PlanningConfig":
        if (
            not self.sensitivity_elasticity_min
            <= self.central_elasticity
            <= self.sensitivity_elasticity_max
        ):
            raise ValueError("central planning elasticity must lie in sensitivity range")
        return self


class ProjectConfig(ConfigModel):
    schema_version: Literal["0.1.0"]
    status: Literal["development"]
    model: ModelConfig
    operating: OperatingConfig
    simulation: SimulationConfig
    planning: PlanningConfig = Field(default_factory=PlanningConfig)

    @model_validator(mode="after")
    def initial_margin(self) -> "ProjectConfig":
        if (
            self.simulation.unit_cost_percent_max
            > 100 - self.operating.minimum_gross_margin_percent
        ):
            raise ValueError("initial costs must permit the minimum gross margin")
        return self


def load_config(path: Path = Path("config/development.yaml")) -> ProjectConfig:
    return ProjectConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
