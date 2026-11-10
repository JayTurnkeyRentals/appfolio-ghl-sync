"""Reset local sync state after deleting all contacts in GHL.

The state.db is only a CACHE of what should already exist in GHL. If you
delete contacts directly in GHL without touching state.db, the sync will
see its old records (guest_card_id -> contact_id, inquiry -> note_id) and
assume everything is already synced -- it won't even check whether the
contact/note still exists remotely, so it silently does nothing.

This clears the three tables that record "already done" state:
    contact_keys       (guest_card_id/email/phone -> ghl_contact_id)
    synced_inquiries    (inquiry -> ghl_note_id)
    synced_guest_cards  (legacy table, pre-migration)

It does NOT touch sync_state (the watermark) -- see --reset-watermark if you
also want to reset that (usually not needed; the fetch window is currently
hardcoded to a rolling 5 days regardless of watermark).

Usage:
    python3 reset_state.py ./data/state.db                    # dry run
    python3 reset_state.py ./data/state.db --apply             # actually clear
    python3 reset_state.py ./data/state.db --apply --reset-watermark
"""
import argparse
import sqlite3
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db_path")
    parser.add_argument("--apply", action="store_true", help="Actually clear the tables (default is dry run)")
    parser.add_argument("--reset-watermark", action="store_true", help="Also clear the stored watermark")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row

    tables = ["contact_keys", "synced_inquiries", "synced_guest_cards"]
    counts = {}
    for t in tables:
        try:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[t] = 0  # table doesn't exist yet, nothing to clear

    print("Current row counts:")
    for t, c in counts.items():
        print(f"  {t}: {c}")

    if not args.apply:
        print("\nDry run only -- no changes made. Re-run with --apply to clear these tables.")
        conn.close()
        return 0

    for t in tables:
        conn.execute(f"DELETE FROM {t}")
    if args.reset_watermark:
        conn.execute("DELETE FROM sync_state WHERE key = 'last_received_on_to'")
        print("Watermark cleared.")

    conn.commit()
    conn.close()
    print("\nCleared. The next sync run will treat all fetched guest cards as brand new.")
    print("Reminder: the normal run only fetches the last 5 days of inquiries -- "
          "use full_resync.py instead if you need to pull back further than that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
