"""Orchestrates one vacancy sync cycle: fetch vacant units from AppFolio and
upsert each as a record in a GHL "Property" custom object.

Unlike guest card inquiries, vacancy records are always upserted (never
skipped once synced) since fields like days_vacant change on every run.
"""
import logging
from datetime import datetime, timezone

from app import vacancy_mapper
from app.appfolio_client import AppFolioClient
from app.config import Config
from app.ghl_client import GHLApiError, GHLClient
from app.state_store import StateStore

logger = logging.getLogger(__name__)


def run_vacancy_sync(config: Config) -> None:
    if not config.ghl_property_object_key:
        logger.warning(
            "GHL_PROPERTY_OBJECT_KEY is not configured; skipping vacancy sync entirely."
        )
        return

    state = StateStore(config.state_db_path)
    appfolio = AppFolioClient(config)
    ghl = GHLClient(config)

    logger.info("===== VACANCY SYNC START =====")
    try:
        schema = ghl.get_custom_object_schema(config.ghl_property_object_key)
    except GHLApiError as e:
        logger.error(
            "GHL cannot resolve custom object '%s' for location '%s'; "
            "verify the object exists in this location and the key/token are correct: %s",
            config.ghl_property_object_key, config.ghl_location_id, e,
        )
        return
    if schema.get("key") != config.ghl_property_object_key:
        logger.error(
            "GHL custom-object preflight returned key %r, expected %r; aborting vacancy sync",
            schema.get("key"), config.ghl_property_object_key,
        )
        return

    vacancies = appfolio.fetch_vacancies()
    logger.info("Fetched %d vacant unit(s)", len(vacancies))

    logger.info("Preparing one GHL property record per unit, keyed by AppFolio unit_id")

    created = updated = unavailable = missing_records = failed = 0
    current_vacancy_keys: set[str] = set()

    for row in vacancies:
        try:
            key = vacancy_mapper.vacancy_key(row)
        except ValueError as e:
            logger.warning("Skipping vacancy row with no usable key: %s", e)
            failed += 1
            continue

        current_vacancy_keys.add(key)
        unit = vacancy_mapper.from_appfolio_row(row)
        properties = vacancy_mapper.to_ghl_custom_object_payload(unit)
        now = datetime.now(timezone.utc).isoformat()

        existing = state.get_synced_vacancy(key)
        try:
            if existing is not None:
                try:
                    ghl.update_custom_object_record(
                        config.ghl_property_object_key, existing["ghl_record_id"], properties
                    )
                    record_id = existing["ghl_record_id"]
                    updated += 1
                except GHLApiError as update_error:
                    body_text = str(update_error.body).lower()
                    stale_record = (
                        update_error.status_code == 404
                        and "record with id" in body_text
                        and "was not found" in body_text
                    )
                    if not stale_record:
                        raise
                    logger.info("Saved GHL record for unit %s is missing; recreating it", key)
                    result = ghl.create_custom_object_record(
                        config.ghl_property_object_key, properties
                    )
                    record_id = result.get("record", {}).get("id") or result.get("id")
                    if not record_id:
                        logger.error("Could not extract record id from create response: %s", result)
                        failed += 1
                        continue
                    created += 1
            else:
                result = ghl.create_custom_object_record(config.ghl_property_object_key, properties)
                record_id = result.get("record", {}).get("id") or result.get("id")
                if not record_id:
                    logger.error("Could not extract record id from create response: %s", result)
                    failed += 1
                    continue
                created += 1
        except GHLApiError as e:
            if e.status_code == 404:
                logger.error(
                    "GHL stopped resolving custom object '%s' during sync; "
                    "aborting after %d attempted unit(s). Verify the schema still exists in location '%s': %s",
                    config.ghl_property_object_key, created + updated + failed + 1,
                    config.ghl_location_id, e,
                )
                failed += 1
                break
            logger.warning("Failed to sync vacancy %s to GHL: %s", key, e)
            failed += 1
            continue

        state.upsert_synced_vacancy(key, record_id, now)

    if failed == 0:
        for existing in state.all_synced_vacancies():
            key = existing["vacancy_key"]
            if key in current_vacancy_keys:
                continue
            try:
                ghl.update_custom_object_record(
                    config.ghl_property_object_key,
                    existing["ghl_record_id"],
                    {"occupancy_status": "Unavailable"},
                )
                unavailable += 1
                logger.info("Marked unit %s unavailable; it is no longer a qualifying vacancy", key)
            except GHLApiError as e:
                body_text = str(e.body).lower()
                stale_record = (
                    e.status_code == 404
                    and "custom object record with id" in body_text
                    and "was not found" in body_text
                )
                if stale_record:
                    state.delete_synced_vacancy(key)
                    missing_records += 1
                    logger.info(
                        "Unit %s already has no GHL record; cleared its stale local mapping",
                        key,
                    )
                else:
                    logger.warning("Failed to mark unit %s unavailable: %s", key, e)
                    failed += 1
    else:
        logger.warning(
            "Skipping unavailable-unit reconciliation because %d vacancy row(s) failed; "
            "not enough confidence to mark omitted units unavailable",
            failed,
        )

    logger.info(
        "Vacancy sync complete: %d created, %d updated, %d marked unavailable, "
        "%d stale mappings cleared, %d failed (of %d fetched)",
        created, updated, unavailable, missing_records, failed, len(vacancies),
    )
    logger.info("===== VACANCY SYNC END =====")
