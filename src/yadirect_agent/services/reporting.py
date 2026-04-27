"""Reporting service: joins Direct cost/clicks with Metrika conversions.

Design:

- One source of truth for the join — the Metrika ``/stat/v1/data``
  endpoint. Metrika ingests Direct cost/click data via the Direct↔
  Metrika integration (``ym:ad:directCost``, ``ym:s:visits``), so
  fetching everything from Metrika gives a consistent snapshot
  without parsing Direct's TSV reports.
- A few-hour data lag is acceptable for first-look / health-check
  use cases (M15.5). Sub-hour freshness will need Direct's
  ``fetch_report`` (TSV, async polling) — out of scope for M6 basic.
- ``cpa_rub`` and ``cr_pct`` are computed here, not by callers, so
  the divide-by-zero contract is enforced in one place. None means
  "undefined / not applicable" — never 0, never infinity. A downstream
  rule-based check that wants to filter "burning campaigns" must
  treat ``cpa_rub is None and cost_rub > 0`` as the signal, not
  ``cpa_rub > some_threshold``.
"""

from __future__ import annotations

from typing import Self

import structlog

from ..clients.metrika import MetrikaService
from ..config import Settings
from ..exceptions import ConfigError
from ..models.metrika import CampaignPerformance, DateRange

_log = structlog.get_logger(component="services.reporting")

# Metrika metric identifiers we use. Centralised so a typo crashes
# imports rather than silently returning zeros (the agent worst case).
_METRIC_VISITS = "ym:s:visits"
_METRIC_DIRECT_COST = "ym:ad:directCost"
# Goal-specific conversion metric is composed at call time:
#   f"ym:s:goal{goal_id}conversions"


def _compute_cpa(cost_rub: float, conversions: int) -> float | None:
    """Cost per action. None when undefined.

    Undefined means: zero conversions (would be div-by-zero) OR zero
    cost (no spend yet, so "what did each conversion cost" has no
    economic meaning).
    """
    if conversions <= 0 or cost_rub <= 0:
        return None
    return cost_rub / conversions


def _compute_cr_pct(clicks: int, conversions: int) -> float | None:
    """Conversion rate as a percentage. None when undefined.

    Undefined when there were no clicks — without traffic, the
    conversion rate has no meaning.
    """
    if clicks <= 0:
        return None
    return conversions / clicks * 100.0


class ReportingService:
    """Thin facade over MetrikaService that produces joined DTOs.

    Currently stateless: ``__aenter__`` / ``__aexit__`` are noops and
    each method opens its own short-lived ``MetrikaService`` block.
    The async-context shape is preserved for symmetry with
    ``CampaignService`` / ``BiddingService`` / ``MetrikaService``,
    and as a place to put per-session state if/when caching is
    introduced (auditor M6 LOW-9). If/when that happens, anything
    cached in ``__aenter__`` MUST be invalidated in ``__aexit__`` —
    the M14 agency-mode rollout will reuse this class across multiple
    accounts and silent state-leak between accounts would be a
    cross-tenant data leak, not a perf bug.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def _require_counter_id(self) -> int:
        """Resolve the configured counter or fail with a clear message."""
        cid = self._settings.yandex_metrika_counter_id
        if cid is None:
            msg = (
                "Metrika counter_id is not configured — set "
                "YANDEX_METRIKA_COUNTER_ID in your .env or pass "
                "yandex_metrika_counter_id to Settings explicitly."
            )
            raise ConfigError(msg)
        return cid

    async def campaign_performance(
        self,
        *,
        campaign_id: int,
        campaign_name: str,
        date_range: DateRange,
        goal_id: int | None = None,
    ) -> CampaignPerformance:
        """One campaign's clicks / cost / conversions over the window.

        Returns a zero-filled ``CampaignPerformance`` when Metrika has
        no rows for the campaign in the window (new, paused, or no
        traffic). ``cpa_rub`` and ``cr_pct`` are None whenever they
        would be undefined (zero conversions, zero clicks, or zero
        cost — see ``_compute_cpa`` / ``_compute_cr_pct``).

        ``goal_id=None`` means we don't fetch any conversion metric;
        ``conversions`` is 0 in the result and ``cpa_rub`` is None.
        Callers that need conversions must pass an explicit goal_id
        (typically the user's primary commerce goal, looked up via
        ``MetrikaService.get_goals``).
        """
        counter_id = self._require_counter_id()

        metrics = [_METRIC_VISITS, _METRIC_DIRECT_COST]
        if goal_id is not None:
            metrics.append(f"ym:s:goal{goal_id}conversions")

        # Filter to this single campaign — without it, ym:ad:directCost
        # would be account-wide, not per-campaign. Same for visits.
        filters = f"ym:ad:directCampaignID=={campaign_id}"

        async with MetrikaService(self._settings) as mc:
            rows = await mc.get_report(
                counter_id=counter_id,
                metrics=metrics,
                dimensions=[],
                date_range=date_range,
                filters=filters,
            )

        if not rows:
            return CampaignPerformance(
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                date_range=date_range,
                clicks=0,
                cost_rub=0.0,
                conversions=0,
                cpa_rub=None,
                cr_pct=None,
            )

        # Filtered query returns at most one aggregated row.
        row = rows[0]
        m = row.metrics
        clicks = int(m[0]) if len(m) > 0 else 0
        cost_rub = float(m[1]) if len(m) > 1 else 0.0
        conversions = int(m[2]) if len(m) > 2 and goal_id is not None else 0

        return CampaignPerformance(
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            date_range=date_range,
            clicks=clicks,
            cost_rub=cost_rub,
            conversions=conversions,
            cpa_rub=_compute_cpa(cost_rub, conversions),
            cr_pct=_compute_cr_pct(clicks, conversions),
        )

    async def account_overview(
        self,
        *,
        date_range: DateRange,
        goal_id: int | None = None,
    ) -> list[CampaignPerformance]:
        """All campaigns with traffic in the window, one row each.

        Powers M15.5's rule-based account health check — without
        this, the agent can't surface "campaigns burning money
        without converting" or rank campaigns by efficiency.

        Groups by ``ym:ad:directCampaignID`` (numeric, the join key
        — by-name would conflate same-named promo cycles).

        No filter is applied; the caller gets every campaign that
        had at least one visit in the window. Brand-new or paused
        campaigns with zero traffic in the window simply won't appear.

        Rows with malformed dimensions (missing id, non-int id) are
        skipped rather than crashing the whole overview — defensive
        against unexpected wire shapes. Logged via structlog at
        warning level so the operator can see when Metrika returns
        unexpected data, but the agent loop is not interrupted.
        """
        counter_id = self._require_counter_id()

        metrics = [_METRIC_VISITS, _METRIC_DIRECT_COST]
        if goal_id is not None:
            metrics.append(f"ym:s:goal{goal_id}conversions")

        async with MetrikaService(self._settings) as mc:
            rows = await mc.get_report(
                counter_id=counter_id,
                metrics=metrics,
                dimensions=["ym:ad:directCampaignID"],
                date_range=date_range,
                filters=None,
            )

        results: list[CampaignPerformance] = []
        for row in rows:
            if not row.dimensions:
                _log.warning(
                    "metrika.row.dimensions_missing",
                    counter_id=counter_id,
                    metrics=row.metrics,
                )
                continue
            dim = row.dimensions[0]
            raw_id = dim.get("id")
            # Metrika sometimes returns id as int, sometimes as
            # numeric string — accept both, reject anything else.
            # Bool is filtered explicitly because ``isinstance(True, int)``
            # is True in Python; we don't want True/False as a campaign id.
            # For strings, ``int()`` in a try/except is more robust than
            # ``str.isdigit()`` which accepts non-ASCII digit codepoints
            # (U+00B2, Arabic-Indic digits, etc.) but then crashes on
            # the actual ``int()`` call. (auditor M6 MEDIUM-4.)
            campaign_id: int
            if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                campaign_id = raw_id
            elif isinstance(raw_id, str):
                try:
                    campaign_id = int(raw_id)
                except ValueError:
                    _log.warning(
                        "metrika.row.dimension_id_invalid",
                        raw_id=raw_id,
                        counter_id=counter_id,
                    )
                    continue
            else:
                _log.warning(
                    "metrika.row.dimension_id_invalid",
                    raw_id=raw_id,
                    raw_id_type=type(raw_id).__name__,
                    counter_id=counter_id,
                )
                continue
            campaign_name = str(dim.get("name") or f"campaign_{campaign_id}")

            m = row.metrics
            clicks = int(m[0]) if len(m) > 0 else 0
            cost_rub = float(m[1]) if len(m) > 1 else 0.0
            conversions = int(m[2]) if len(m) > 2 and goal_id is not None else 0

            results.append(
                CampaignPerformance(
                    campaign_id=campaign_id,
                    campaign_name=campaign_name,
                    date_range=date_range,
                    clicks=clicks,
                    cost_rub=cost_rub,
                    conversions=conversions,
                    cpa_rub=_compute_cpa(cost_rub, conversions),
                    cr_pct=_compute_cr_pct(clicks, conversions),
                ),
            )
        return results


# Re-export so monkeypatch in tests can target a stable name.
__all__ = ["MetrikaService", "ReportingService"]
