"""Entrypoint: run one AppFolio -> GoHighLevel vacancy sync cycle.

Standalone from the guest card sync (sync.py) -- run on its own schedule.

Usage:
    python3 vacancy_sync_main.py
"""
import logging
import sys

sys.path.insert(0, "/opt/appfolio-ghl-sync")  # adjust if your app lives elsewhere

from app.config import load_config
from app.vacancy_sync import run_vacancy_sync


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    try:
        config = load_config()
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    try:
        run_vacancy_sync(config)
    except Exception:
        logger.exception("Vacancy sync run failed")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
