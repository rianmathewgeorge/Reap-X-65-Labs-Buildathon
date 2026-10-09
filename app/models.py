from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class RequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=1000)


class CheckoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: StrictBool


class PolicyTestOutcomeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: Literal["COMPLETED", "FAILED"]


class RestrictedIntent(BaseModel):
    """The optional model can choose search wording only, never spending authority."""

    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=3, max_length=120)
    quantity: StrictInt


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    rules: list[dict[str, Any]]

    @property
    def reasons(self) -> list[str]:
        return [rule["rule"] for rule in self.rules if rule["outcome"] != "PASS"]
