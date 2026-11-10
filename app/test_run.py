"""Read-only smoke test: verifies config loads and both AppFolio and GHL APIs
are reachable, without creating/updating/syncing any records. Run this after
deploying to a new environment to catch credential/config issues early.

Usage:
    python3 test_run.py
"""
import logging
import sys
from datetime import date, timedelta

sys.path.insert(0, "/opt/appfolio-ghl-sync")  # adjust if your app lives elsewhere

from app.config import load_config
from app.appfolio_client import AppFolioClient
from app.ghl_client import GHLApiError, GHLClient


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)
    failures: list[str] = []

    try:
        config = load_config()
        logger.info(
            "[OK] Config loaded (database=%s, location=%s)",
            config.appfolio_database, config.ghl_location_id,
        )
    except RuntimeError as e:
        logger.error("[FAIL] Config: %s", e)
        return 1

    appfolio = AppFolioClient(config)

    try:
        today = date.today().strftime("%Y-%m-%d")
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        cards = appfolio.fetch_guest_cards(yesterday, today)
        logger.info("[OK] AppFolio guest card report reachable (%d row(s) in last day)", len(cards))
    except Exception as e:
        logger.error("[FAIL] AppFolio guest card report: %s", e)
        failures.append("appfolio_guest_cards")

    try:
        vacancies = appfolio.fetch_vacancies()
        logger.info("[OK] AppFolio vacancy report reachable (%d qualifying unit(s))", len(vacancies))
    except Exception as e:
        logger.error("[FAIL] AppFolio vacancy report: %s", e)
        failures.append("appfolio_vacancies")

    ghl = GHLClient(config)

    try:
        users = ghl.list_users()
        logger.info("[OK] GHL reachable (%d user(s) found)", len(users))
    except GHLApiError as e:
        logger.error("[FAIL] GHL users: %s", e)
        failures.append("ghl_users")

    if config.ghl_property_object_key:
        try:
            schema = ghl.get_custom_object_schema(config.ghl_property_object_key)
            if schema.get("key") != config.ghl_property_object_key:
                raise RuntimeError(
                    f"Schema key mismatch: expected {config.ghl_property_object_key!r}, "
                    f"received {schema.get('key')!r}"
                )
            logger.info(
                "[OK] GHL custom object schema '%s' exists in location %s",
                config.ghl_property_object_key, config.ghl_location_id,
            )
            records = ghl.search_custom_object_records(config.ghl_property_object_key, page_limit=1)
            logger.info(
                "[OK] GHL custom object '%s' reachable (search returned %d record(s))",
                config.ghl_property_object_key, len(records),
            )
        except (GHLApiError, RuntimeError) as e:
            logger.error("[FAIL] GHL custom object '%s': %s", config.ghl_property_object_key, e)
            failures.append("ghl_custom_object")
    else:
        logger.warning("[SKIP] GHL_PROPERTY_OBJECT_KEY not set; skipping custom object check")

    if failures:
        logger.error("Test run FAILED: %s", ", ".join(failures))
        return 1

    logger.info("Test run PASSED: all checks succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
