"""One-time migration: find guest cards whose ghl_note_id was misattributed by
the old (buggy) unit/date substring-matching duplicate detector, and reset
their ghl_note_id to NULL so the next sync run's repair pass creates a
correct, dedicated note for them.

Detection logic: a real `create_note` call produces a note_id used by exactly
one guest_card_id. If a single ghl_note_id appears against MORE than one
guest_card_id in synced_guest_cards, every row after the first (by
last_synced_at) was assigned that note_id via the false-positive duplicate
check -- i.e. its own note was never created.

Usage:
    python3 repair_misattributed_notes.py /path/to/state.db          # dry run (default)
    python3 repair_misattributed_notes.py /path/to/state.db --apply  # actually reset rows

Always run without --apply first and review the printed list before applying.
"""
import argparse
import sqlite3
import sys


def find_affected(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Returns rows to be reset: all but the earliest-synced row for each
    ghl_note_id that is shared across more than one guest_card_id."""
    shared_note_ids = [
        row["ghl_note_id"]
        for row in conn.execute(
            """
            SELECT ghl_note_id
            FROM synced_guest_cards
            WHERE ghl_note_id IS NOT NULL
            GROUP BY ghl_note_id
            HAVING COUNT(DISTINCT guest_card_id) > 1
            """
        ).fetchall()
    ]

    affected: list[sqlite3.Row] = []
    for note_id in shared_note_ids:
        rows = conn.execute(
            """
            SELECT guest_card_id, ghl_contact_id, ghl_note_id, last_synced_at
            FROM synced_guest_cards
            WHERE ghl_note_id = ?
            ORDER BY last_synced_at ASC
            """,
            (note_id,),
        ).fetchall()
        # Keep the first (earliest) as the legitimate owner of that note;
        # every subsequent guest card was misattributed.
        affected.extend(rows[1:])

    return affected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db_path", help="Path to state.db")
    parser.add_argument("--apply", action="store_true", help="Actually reset the affected rows (default is dry run)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row

    affected = find_affected(conn)

    if not affected:
        print("No misattributed rows found. Nothing to do.")
        return 0

    print(f"Found {len(affected)} guest card(s) with misattributed notes:\n")
    for row in affected:
        print(
            f"  guest_card_id={row['guest_card_id']}  "
            f"contact_id={row['ghl_contact_id']}  "
            f"was pointing at note_id={row['ghl_note_id']}  "
            f"last_synced_at={row['last_synced_at']}"
        )

    if not args.apply:
        print("\nDry run only -- no changes made. Re-run with --apply to reset these rows.")
        conn.close()
        return 0

    ids = [row["guest_card_id"] for row in affected]
    conn.executemany(
        "UPDATE synced_guest_cards SET ghl_note_id = NULL WHERE guest_card_id = ?",
        [(i,) for i in ids],
    )
    conn.commit()
    conn.close()
    print(f"\nReset ghl_note_id to NULL for {len(affected)} row(s). "
          "The next sync run's repair pass will create correct notes for them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
