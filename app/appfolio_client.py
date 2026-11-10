"""Client for the AppFolio Reports API (Guest Card Inquiries report).

AppFolio's Reports API is pull-only: we POST a filter body and get back a page of
results plus an optional `next_page_url` for pagination.
"""
import logging
import re
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.config import Config

logger = logging.getLogger(__name__)


class AppFolioClient:
    def __init__(self, config: Config):
        self._config = config
        self._session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
        )
        self._session.mount("https://", HTTPAdapter(max_retries=retries))

    def fetch_guest_cards(
        self,
        received_on_from: str,
        received_on_to: str,
        columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all Guest Card Inquiries received in [received_on_from, received_on_to].

        Dates must be 'YYYY-MM-DD' strings. Follows pagination via `next_page_url`
        until exhausted. Returns the combined list of guest card result dicts.
        """
        url = f"{self._config.appfolio_reports_base_url}/{self._config.appfolio_guest_card_report}.json"
        body = {
            "property_visibility": "active",
            "guest_card_sources": ["all"],
            "guest_card_statuses": ["all"],
            "guest_card_lead_types": ["all"],
            "assigned_user": "All",
            "filter_date_range_by": "inquiry",
            "received_on_from": received_on_from,
            "received_on_to": received_on_to,
        }
        if columns:
            body["columns"] = columns

        results: list[dict[str, Any]] = []
        next_url: str | None = None

        page_count = 0
        while True:
            if next_url:
                response = self._session.post(
                    next_url,
                    auth=(self._config.appfolio_client_id, self._config.appfolio_client_secret),
                    timeout=30,
                )
            else:
                response = self._session.post(
                    url,
                    json=body,
                    auth=(self._config.appfolio_client_id, self._config.appfolio_client_secret),
                    timeout=30,
                )

            response.raise_for_status()
            payload = response.json()
            page_results = payload.get("results", [])
            results.extend(page_results)
            page_count += 1
            logger.info(
                "Fetched page %d: %d guest card(s) (running total %d)",
                page_count, len(page_results), len(results)
            )

            # --- DEBUG: log each card's ID and received_on date ---
            for card in page_results:
                gc_id = card.get("guest_card_id")
                received = card.get("received")
                logger.debug("DEBUG: AppFolio returned guest_card_id=%s, received_on=%s", gc_id, received)

            next_url = payload.get("next_page_url")
            if not next_url:
                break

        logger.info("Fetched %d guest card(s) total across %d page(s)", len(results), page_count)
        return results

    def fetch_vacancies(self, columns: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch vacant units from the Unit Vacancy Detail report, already
        filtered down to units that are actually leasable.

        NOTE: AppFolio's documented request filters have no "exclude" support
        (no property exclude-list, no tag exclude, no unit-status enum), so
        the three business rules below are applied client-side against the
        response rows rather than as request params:
          - unit status must be "Vacant-Unrented" (vacant AND still available
            to lease -- "Vacant-Rented" means already leased for a future
            move-in, which isn't a true marketing opportunity)
          - property name must not be "Property", "Properties", "HOA", or "POA"
          - tags must not include "End Management"
        Field names used for filtering are best-effort pending confirmation
        against a live sample response -- see _VACANCY_STATUS_FIELD etc. below.
        """
        url = f"{self._config.appfolio_reports_base_url}/{self._config.appfolio_vacancy_report}.json"
        body: dict[str, Any] = {
            "unit_visibility": "active",
            "level_of_detail": "detail_view",
            "bedrooms": "any",
            "bathrooms": "any",
            "non_revenue_units": "0",
        }
        # NOTE: omit "columns" entirely -- AppFolio rejects unrecognized column
        # names with 400 "Columns are invalid.", and our guessed names weren't
        # right. Docs say all columns are returned by default, so let the caller
        # opt in only once real column names are confirmed from a live response.
        if columns:
            body["columns"] = columns

        results: list[dict[str, Any]] = []
        next_url: str | None = None

        page_count = 0
        while True:
            if next_url:
                response = self._session.post(
                    next_url,
                    auth=(self._config.appfolio_client_id, self._config.appfolio_client_secret),
                    timeout=30,
                )
            else:
                response = self._session.post(
                    url,
                    json=body,
                    auth=(self._config.appfolio_client_id, self._config.appfolio_client_secret),
                    timeout=30,
                )

            if not response.ok:
                # AppFolio's 400 body usually names the offending param -- log it
                # since raise_for_status() alone discards the response body.
                logger.error("AppFolio vacancy request rejected (%d): %s", response.status_code, response.text)
            response.raise_for_status()
            payload = response.json()
            raw_results = payload.get("results", [])
            page_results = [row for row in raw_results if _passes_vacancy_filters(row)]
            results.extend(page_results)
            page_count += 1
            logger.info(
                "Fetched vacancy page %d: %d of %d raw row(s) qualified (running total %d)",
                page_count, len(page_results), len(raw_results), len(results)
            )
            if raw_results:
                _log_vacancy_filter_breakdown(raw_results)

            next_url = payload.get("next_page_url")
            if not next_url:
                break

        logger.info("Fetched %d vacant unit(s) total across %d page(s)", len(results), page_count)
        return results


# Field names confirmed via a live response on 2026-09-21: unit_status,
# property_name, and unit_tags all exist as shown in the sample row (see
# vacancy_mapper docstring).
_VACANCY_STATUS_FIELD = "unit_status"
_VACANCY_PROPERTY_NAME_FIELD = "property_name"
_VACANCY_TAGS_FIELD = "unit_tags"

_REQUIRED_UNIT_STATUS = "Vacant-Unrented"
# "Does not include" means a whole-word match (case-insensitive), e.g. "ABC
# Properties LLC" or "123 Main St HOA" should be excluded -- but a plain
# substring match on "hoa"/"poa" would false-positive on names like "Choate",
# so word boundaries are used instead.
_EXCLUDED_PROPERTY_NAME_WORDS = ("property", "properties", "hoa", "poa")
_EXCLUDED_PROPERTY_NAME_PATTERN = re.compile(
    r"\b(?:" + "|".join(_EXCLUDED_PROPERTY_NAME_WORDS) + r")\b", re.IGNORECASE
)
_EXCLUDED_TAG = "End Management"


def _passes_vacancy_filters(row: dict[str, Any]) -> bool:
    status = (row.get(_VACANCY_STATUS_FIELD) or "").strip()
    if status != _REQUIRED_UNIT_STATUS:
        return False

    property_name = (row.get(_VACANCY_PROPERTY_NAME_FIELD) or "").strip()
    if _EXCLUDED_PROPERTY_NAME_PATTERN.search(property_name):
        return False

    tags_raw = row.get(_VACANCY_TAGS_FIELD) or ""
    # Handle either a comma-separated string or an already-parsed list --
    # unconfirmed which shape the live API returns for unit_tags.
    if isinstance(tags_raw, str):
        tags = {tag.strip() for tag in tags_raw.split(",") if tag.strip()}
    else:
        tags = {str(tag).strip() for tag in tags_raw if str(tag).strip()}
    if _EXCLUDED_TAG in tags:
        return False

    return True


def _log_vacancy_filter_breakdown(raw_results: list[dict[str, Any]]) -> None:
    """One-time diagnostic: counts how many rows were excluded by each
    individual filter, to explain a qualifying count that looks too low
    (e.g. most rows might be "Vacant-Unrented" rather than "Vacant-Rented")."""
    from collections import Counter
    status_counts = Counter((row.get(_VACANCY_STATUS_FIELD) or "").strip() for row in raw_results)
    logger.info("Vacancy row unit_status breakdown (this page): %s", dict(status_counts))
