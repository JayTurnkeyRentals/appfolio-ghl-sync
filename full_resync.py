#!/usr/bin/env python3
"""One-off backfill: re-sync guest card inquiries over a WIDER date range than
the normal 5-day rolling window main.py uses, for recovering after a bulk
GHL contact deletion (or any other gap wider than 5 days).

This intentionally imports and reuses the real per-inquiry sync function from
app.sync (_sync_inquiry / _build_user_map) rather than reimplementing the
logic, so behavior can't drift from your actual production sync path.

IMPORTANT: run reset_state.py FIRST if you deleted contacts in GHL --
otherwise this script will see the old (now-stale) local state and skip
everything as "already synced", same as a normal run would.

Usage:
    python3 full_resync.py --days 90                  # dry run, last 90 days
    python3 full_resync.py --days 90 --apply           # actually sync
    python3 full_resync.py --from 2026-01-01 --apply   # explicit start date
"""
import argparse
import logging
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, "/opt/appfolio-ghl-sync")  # adjust if your app lives elsewhere

from app.config import load_config
from app.appfolio_client import AppFolioClient
from app.ghl_client import GHLApiError, GHLClient
from app.state_store import StateStore
from app import mapper
from app.sync import _build_user_map, _sync_inquiry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("full_resync")

DATE_FORMAT = "%Y-%m-%d"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, help="How many days back to fetch (from today)")
    parser.add_argument("--from", dest="from_date", help="Explicit start date, YYYY-MM-DD (overrides --days)")
    parser.add_argument("--apply", action="store_true", help="Actually create contacts/notes (default is dry run: fetch + report counts only)")
    args = parser.parse_args()

    if not args.from_date and not args.days:
        print("Provide either --days N or --from YYYY-MM-DD")
        return 1

    received_on_from = args.from_date or (date.today() - timedelta(days=args.days)).strftime(DATE_FORMAT)
    received_on_to = date.today().strftime(DATE_FORMAT)

    config = load_config()
    state = StateStore(config.state_db_path)
    appfolio = AppFolioClient(config)
    ghl = GHLClient(config)

    logger.info("===== FULL RESYNC (%s to %s) =====", received_on_from, received_on_to)
    guest_cards = appfolio.fetch_guest_cards(received_on_from, received_on_to)
    logger.info("Fetched %d inquiry row(s) total", len(guest_cards))

    # Process oldest inquiries first, newest last – this makes it easier to
    # follow the chronological flow and ensures earlier records are handled
    # before later ones if any logic depends on order (e.g., deduplication).
    guest_cards = sorted(
        guest_cards,
        key=lambda gc: gc.get(mapper.RECEIVED_ON_FIELD) or "",
    )   # ascending = oldest to newest

    if not args.apply:
        # Dry run: just show what WOULD happen, without calling GHL.
        already_tracked = 0
        would_process = 0
        for guest_card in guest_cards:  # already sorted oldest-to-newest
            try:
                inquiry_key = mapper.inquiry_id(guest_card)
            except ValueError:
                continue
            existing = state.get_synced_inquiry(inquiry_key)
            if existing is not None and existing["ghl_note_id"] is not None:
                already_tracked += 1
            else:
                would_process += 1
        print(f"\nDry run: {would_process} inquiry row(s) would be synced, "
              f"{already_tracked} already tracked in local state.")
        print("If you deleted contacts in GHL, run reset_state.py first if 'already tracked' "
              "looks too high, then re-run this dry run to confirm it drops to 0.")
        print("Re-run with --apply to actually create contacts/notes in GHL.")
        return 0

    user_map = _build_user_map(ghl)
    logger.info("Loaded %d GHL user(s) for assignment matching", len(user_map))

    counts = {"new": 0, "grouped": 0, "repaired": 0, "duplicate": 0, "skipped": 0}
    errors = 0

    for guest_card in guest_cards:
        try:
            gc_id = mapper.guest_card_id(guest_card)
            inquiry_key = mapper.inquiry_id(guest_card)
        except ValueError:
            logger.exception("Skipping malformed inquiry row")
            errors += 1
            continue

        try:
            outcome = _sync_inquiry(state, ghl, user_map, guest_card)
            counts[outcome] += 1
            if outcome != "skipped":
                logger.info("Guest card %s, inquiry %s -> %s", gc_id, inquiry_key, outcome)
        except GHLApiError:
            logger.exception("GHL API error syncing guest card %s, inquiry %s", gc_id, inquiry_key)
            errors += 1
        except Exception:
            logger.exception("Unexpected error syncing guest card %s, inquiry %s", gc_id, inquiry_key)
            errors += 1

    logger.info(
        "Backfill complete: %d new, %d grouped, %d repaired, %d duplicate, %d skipped, %d error(s)",
        counts["new"], counts["grouped"], counts["repaired"], counts["duplicate"], counts["skipped"], errors,
    )
    # Deliberately do NOT touch the watermark here – this is a one-off backfill,
    # not a substitute for the normal rolling-window sync run.
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
