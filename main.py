"""Entrypoint: run one AppFolio -> GoHighLevel guest card sync cycle.

Intended to be invoked periodically by a systemd timer (see deploy/).
"""
import logging
import sys

from app.config import load_config
from app.sync import run_sync


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
        run_sync(config)
    except Exception:
        logger.exception("Sync run failed")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
