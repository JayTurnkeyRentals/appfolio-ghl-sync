"""Diagnostic: inspect what the local state DB knows about a specific guest
card and/or dedup key, so we can figure out why a note wasn't created for it.

Usage:
    python3 inspect_guest_card.py ./data/state.db --guest-card-id 123456
    python3 inspect_guest_card.py ./data/state.db --email jane@example.com
    python3 inspect_guest_card.py ./data/state.db --phone 5551234567

You can pass more than one flag at once.
"""
import argparse
import re
import sqlite3
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db_path")
    parser.add_argument("--guest-card-id", help="AppFolio guest_card_id")
    parser.add_argument("--email", help="Person's email address")
    parser.add_argument("--phone", help="Person's phone number (any format)")
    args = parser.parse_args()

    if not (args.guest_card_id or args.email or args.phone):
        print("Provide at least one of --guest-card-id, --email, --phone")
        return 1

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row

    if args.guest_card_id:
        print(f"\n=== synced_guest_cards WHERE guest_card_id = '{args.guest_card_id}' ===")
        row = conn.execute(
            "SELECT * FROM synced_guest_cards WHERE guest_card_id = ?",
            (str(args.guest_card_id),),
        ).fetchone()
        if row:
            print(dict(row))
        else:
            print("No row found -- this guest card has never been marked synced. "
                  "It SHOULD be picked up as a brand-new card on the next run "
                  "(check watermark / fetch date range if it still isn't).")

    dedup_keys = []
    if args.email:
        dedup_keys.append(f"email:{args.email.strip().lower()}")
    if args.phone:
        digits = re.sub(r"\D", "", args.phone)
        if digits:
            dedup_keys.append(f"phone:{digits}")

    for key in dedup_keys:
        print(f"\n=== contact_keys WHERE dedup_key = '{key}' ===")
        row = conn.execute(
            "SELECT * FROM contact_keys WHERE dedup_key = ?", (key,)
        ).fetchone()
        if row:
            print(dict(row))
            contact_id = row["ghl_contact_id"]
            print(f"\n=== all synced_guest_cards rows pointing at contact_id = '{contact_id}' ===")
            rows = conn.execute(
                "SELECT guest_card_id, ghl_note_id, last_synced_at FROM synced_guest_cards "
                "WHERE ghl_contact_id = ? ORDER BY last_synced_at",
                (contact_id,),
            ).fetchall()
            for r in rows:
                print(dict(r))
        else:
            print("No contact_keys row found for this dedup_key.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
