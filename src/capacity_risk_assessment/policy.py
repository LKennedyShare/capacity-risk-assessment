"""Tier policy: decides what happens to an app's traffic at a given PTU utilisation.

Each tier has a ladder of utilisation thresholds and actions, with hysteresis so
decisions do not flap around a boundary. Critical apps always stay on PTU.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_HYSTERESIS = 0.05


class Tier(StrEnum):
    CRITICAL = "critical"
    STANDARD = "standard"
    BATCH = "batch"
    EXPERIMENTAL = "experimental"


class Action(StrEnum):
    ALLOW_PTU = "allow_ptu"
    SPILL_PAYGO = "spill_paygo"
    QUEUE = "queue"
    BLOCK = "block"
    DOWNGRADE_MODEL = "downgrade_model"


class Ladder(BaseModel):
    """Actions by utilisation band.

    ``thresholds`` are ascending enter points in (0, 1). ``actions[i]`` applies
    below ``thresholds[i]`` and ``actions[i + 1]`` applies at or above it.
    A band is left downward only when utilisation falls below its threshold
    minus ``hysteresis``, so decisions do not flap around a boundary.
    """

    model_config = ConfigDict(frozen=True)

    thresholds: tuple[float, ...] = ()
    actions: tuple[Action, ...]
    hysteresis: float = Field(default=DEFAULT_HYSTERESIS, ge=0.0, lt=1.0)

    @model_validator(mode="after")
    def _check_shape(self) -> Ladder:
        if len(self.actions) != len(self.thresholds) + 1:
            raise ValueError("actions must have one more entry than thresholds")
        if any(not 0.0 < t < 1.0 for t in self.thresholds):
            raise ValueError("thresholds must lie strictly between 0 and 1")
        if list(self.thresholds) != sorted(set(self.thresholds)):
            raise ValueError("thresholds must be strictly ascending")
        if self.thresholds:
            gaps = [b - a for a, b in zip(self.thresholds, self.thresholds[1:])]
            smallest = min([self.thresholds[0], *gaps])
            if self.hysteresis >= smallest:
                raise ValueError("hysteresis must be smaller than the smallest band gap")
        return self


DEFAULT_LADDERS: dict[Tier, Ladder] = {
    Tier.CRITICAL: Ladder(actions=(Action.ALLOW_PTU,)),
    Tier.STANDARD: Ladder(thresholds=(0.75,), actions=(Action.ALLOW_PTU, Action.SPILL_PAYGO)),
    Tier.BATCH: Ladder(
        thresholds=(0.50, 0.90),
        actions=(Action.ALLOW_PTU, Action.QUEUE, Action.BLOCK),
    ),
    Tier.EXPERIMENTAL: Ladder(thresholds=(0.75,), actions=(Action.ALLOW_PTU, Action.BLOCK)),
}


class Override(BaseModel):
    """Manual override. Beats the ladder, the fail-safe and spend caps."""

    model_config = ConfigDict(frozen=True)

    action: Action
    reason: str = Field(min_length=1)
    expires_at: datetime | None = None


class App(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    tier: Tier
    ladder: Ladder | None = None
    override: Override | None = None

    @model_validator(mode="after")
    def _critical_stays_on_ptu(self) -> App:
        if self.tier is Tier.CRITICAL and self.ladder is not None:
            if any(action is not Action.ALLOW_PTU for action in self.ladder.actions):
                raise ValueError("critical apps must stay on PTU in every band")
        return self

    def effective_ladder(self) -> Ladder:
        return self.ladder if self.ladder is not None else DEFAULT_LADDERS[self.tier]


class Utilization(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: float = Field(ge=0.0)
    stale: bool = False


class SpendState(BaseModel):
    model_config = ConfigDict(frozen=True)

    cap_breached: bool = False


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Action
    band: int | None
    reason: str
    overridden: bool = False


def band_for(ladder: Ladder, utilization: float, prior_band: int | None) -> int:
    """Return the band index for a utilisation value, with hysteresis."""
    thresholds = ladder.thresholds
    if prior_band is None:
        return sum(1 for t in thresholds if utilization >= t)
    band = min(max(prior_band, 0), len(thresholds))
    while band < len(thresholds) and utilization >= thresholds[band]:
        band += 1
    while band > 0 and utilization < thresholds[band - 1] - ladder.hysteresis:
        band -= 1
    return band


def evaluate(
    app: App,
    utilization: Utilization | None,
    spend: SpendState,
    now: datetime,
    prior_band: int | None = None,
) -> Decision:
    """Decide the action for one request from ``app``."""
    override = app.override
    if override is not None and (override.expires_at is None or now < override.expires_at):
        return Decision(
            action=override.action,
            band=prior_band,
            reason=f"manual override: {override.reason}",
            overridden=True,
        )

    if utilization is None or utilization.stale:
        if app.tier is Tier.CRITICAL:
            return Decision(
                action=Action.ALLOW_PTU,
                band=None,
                reason="utilisation signal stale: critical app keeps PTU",
            )
        action = Action.BLOCK if spend.cap_breached else Action.SPILL_PAYGO
        suffix = " and spend cap reached, so blocked" if spend.cap_breached else ""
        return Decision(
            action=action,
            band=None,
            reason=f"utilisation signal stale: non-critical app to paygo{suffix}",
        )

    ladder = app.effective_ladder()
    band = band_for(ladder, utilization.value, prior_band)
    action = ladder.actions[band]
    reason = f"{app.tier} at {utilization.value:.0%} utilisation, band {band}: {action}"

    if spend.cap_breached and action is Action.SPILL_PAYGO:
        return Decision(
            action=Action.BLOCK,
            band=band,
            reason="spend cap reached: paygo spill blocked",
        )
    return Decision(action=action, band=band, reason=reason)
