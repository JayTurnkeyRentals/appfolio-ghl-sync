"""Refresh existing GoHighLevel contacts from AppFolio guest-card inquiries.

Run directly or as a module:
    python /opt/appfolio-ghl-sync/app/resync.py
    python /opt/appfolio-ghl-sync/app/resync.py --apply
    python -m app.resync --days 90 --apply

Without --apply, the script fetches AppFolio rows and reports what it can
determine from local sync state without changing GHL.
"""
import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import mapper
from app.appfolio_client import AppFolioClient
from app.config import load_config
from app.ghl_client import GHLApiError, GHLClient
from app.state_store import StateStore
from app.sync import _build_user_map, resync_inquiry

logger = logging.getLogger("resync")
DATE_FORMAT = "%Y-%m-%d"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    date_range = parser.add_mutually_exclusive_group()
    date_range.add_argument("--days", type=int, help="How many days back to fetch")
    date_range.add_argument("--from", dest="from_date", help="Start date, YYYY-MM-DD")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Update contacts, assignments, and notes in GHL (default: report only)",
    )
    return parser


def _prompt_for_days() -> int:
    while True:
        raw_days = input("How many days back should the resync go? ").strip()
        try:
            days = int(raw_days)
        except ValueError:
            print("Enter a whole number of days greater than zero.")
            continue
        if days > 0:
            return days
        print("Enter a whole number of days greater than zero.")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = _parser()
    args = parser.parse_args()

    if args.from_date is not None:
        try:
            received_on_from = date.fromisoformat(args.from_date).strftime(DATE_FORMAT)
        except ValueError:
            parser.error("--from must be a valid date in YYYY-MM-DD format")
    else:
        days = args.days if args.days is not None else _prompt_for_days()
        if days < 1:
            parser.error("--days must be a positive integer")
        received_on_from = (date.today() - timedelta(days=days)).strftime(DATE_FORMAT)
    received_on_to = date.today().strftime(DATE_FORMAT)

    config = load_config()
    state = StateStore(config.state_db_path)
    appfolio = AppFolioClient(config)
    ghl = GHLClient(config) if args.apply else None

    logger.info("===== RESYNC (%s to %s) =====", received_on_from, received_on_to)
    guest_cards = appfolio.fetch_guest_cards(received_on_from, received_on_to)
    guest_cards.sort(key=lambda card: card.get(mapper.RECEIVED_ON_FIELD) or "")
    logger.info("Fetched %d inquiry row(s)", len(guest_cards))

    if not args.apply:
        known = 0
        needs_lookup = 0
        for guest_card in guest_cards:
            try:
                inquiry_key = mapper.inquiry_id(guest_card)
                gc_id = mapper.guest_card_id(guest_card)
            except ValueError:
                continue
            known_contact = (
                state.get_synced_inquiry(inquiry_key)
                or state.get_contact_by_key(f"gc:{gc_id}")
            )
            if not known_contact:
                secondary_key = mapper.contact_dedup_key(guest_card)
                known_contact = state.get_contact_by_key(secondary_key) if secondary_key else None
            if known_contact:
                known += 1
            else:
                needs_lookup += 1

        print(
            f"\nDry run: {len(guest_cards)} inquiry row(s), {known} with a local "
            f"contact mapping, {needs_lookup} requiring an email/phone lookup."
        )
        print("No contacts or notes were changed. Add --apply to perform the resync.")
        return 0

    assert ghl is not None
    user_map = _build_user_map(ghl)
    logger.info("Loaded %d GHL user(s) for AppFolio assignment matching", len(user_map))

    counts = {"updated": 0, "created_note": 0, "missing_contact": 0}
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
            outcome = resync_inquiry(state, ghl, user_map, guest_card)
            counts[outcome] += 1
            logger.info("Guest card %s, inquiry %s -> %s", gc_id, inquiry_key, outcome)
        except GHLApiError:
            logger.exception("GHL API error resyncing guest card %s, inquiry %s", gc_id, inquiry_key)
            errors += 1
        except Exception:
            logger.exception("Unexpected error resyncing guest card %s, inquiry %s", gc_id, inquiry_key)
            errors += 1

    logger.info(
        "Resync complete: %d contact/note update(s), %d note(s) created, "
        "%d contact(s) not found, %d error(s)",
        counts["updated"], counts["created_note"], counts["missing_contact"], errors,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
