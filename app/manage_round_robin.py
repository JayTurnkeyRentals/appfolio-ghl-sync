"""Interactive helper for managing the round-robin leasing agent roster.

Run this whenever you need to add/remove agents, reorder the rotation, or
turn round-robin assignment on/off. sync.py and full_resync.py both read
this same configuration automatically on their next run -- no other changes
needed.

Usage:
    python3 manage_round_robin.py
"""
import sys

sys.path.insert(0, "/opt/appfolio-ghl-sync")  # adjust if your app lives elsewhere

from app.config import load_config
from app.ghl_client import GHLApiError, GHLClient
from app.state_store import StateStore
from app import mapper


def print_status(state: StateStore) -> None:
    enabled = state.is_round_robin_enabled()
    agents = state.all_round_robin_agents()
    last = state.get_last_round_robin_agent()

    print(f"\nRound robin: {'ENABLED' if enabled else 'DISABLED'}")
    if not agents:
        print("No agents configured yet.")
    else:
        print("Rotation order:")
        for row in agents:
            marker = " (inactive)" if not row["active"] else ""
            last_marker = "  <- last assigned" if row["name"] == last else ""
            print(f"  {row['sort_order'] + 1}. {row['name']}{marker}{last_marker}")


def fetch_ghl_user_names(config) -> list[str]:
    """Best-effort fetch of GHL user names, to help catch typos when adding
    agents. Returns an empty list if this fails -- not fatal, just less
    convenient (you can still type a name manually)."""
    try:
        ghl = GHLClient(config)
        users = ghl.list_users()
        names = []
        for u in users:
            name = u.get("name") or f"{u.get('firstName', '')} {u.get('lastName', '')}".strip()
            if name:
                names.append(name)
        return names
    except GHLApiError as e:
        print(f"(Could not fetch GHL users to validate against: {e})")
        return []
    except Exception as e:
        print(f"(Could not fetch GHL users to validate against: {e})")
        return []


def add_agent(state: StateStore, ghl_user_names: list[str]) -> None:
    if ghl_user_names:
        print("\nGHL users found:")
        for i, name in enumerate(ghl_user_names, 1):
            print(f"  {i}. {name}")
        raw = input("Enter a number from the list above, or type a name manually: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(ghl_user_names):
            name = ghl_user_names[int(raw) - 1]
        else:
            name = raw
    else:
        name = input("Agent name (must match their name in GHL exactly): ").strip()

    if not name:
        print("No name entered, nothing added.")
        return

    # Warn (don't block) if the name doesn't look like it'll match a GHL user later.
    if ghl_user_names:
        normalized_input = mapper.normalize_user_name(name)
        matches = any(mapper.normalize_user_name(n) == normalized_input for n in ghl_user_names)
        if not matches:
            confirm = input(
                f"'{name}' doesn't match any GHL user name found. Add anyway? "
                "It won't be assignable until the name matches. (y/N): "
            ).strip().lower()
            if confirm != "y":
                print("Cancelled.")
                return

    state.add_round_robin_agent(name)
    print(f"Added/reactivated '{name}' in the rotation.")


def remove_agent(state: StateStore) -> None:
    agents = state.list_round_robin_agents(active_only=False)
    if not agents:
        print("No agents to remove.")
        return
    for i, name in enumerate(agents, 1):
        print(f"  {i}. {name}")
    raw = input("Enter the number to remove (deletes entirely, including their rotation history): ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(agents):
        name = agents[int(raw) - 1]
        state.remove_round_robin_agent(name)
        print(f"Removed '{name}'.")
    else:
        print("Invalid selection.")


def toggle_active(state: StateStore) -> None:
    agents = state.all_round_robin_agents()
    if not agents:
        print("No agents configured.")
        return
    for i, row in enumerate(agents, 1):
        status = "active" if row["active"] else "inactive"
        print(f"  {i}. {row['name']} ({status})")
    raw = input("Enter a number to toggle active/inactive (pauses them without losing their position): ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(agents):
        row = agents[int(raw) - 1]
        new_active = not bool(row["active"])
        state.set_round_robin_agent_active(row["name"], new_active)
        print(f"'{row['name']}' is now {'active' if new_active else 'inactive'}.")
    else:
        print("Invalid selection.")


def reorder_agents(state: StateStore) -> None:
    agents = state.list_round_robin_agents(active_only=False)
    if len(agents) < 2:
        print("Need at least 2 agents to reorder.")
        return
    print("Current order:")
    for i, name in enumerate(agents, 1):
        print(f"  {i}. {name}")
    raw = input(
        "Enter the new order as a comma-separated list of numbers "
        f"(e.g. for {len(agents)} agents: {','.join(str(i) for i in range(len(agents), 0, -1))}): "
    ).strip()
    try:
        indices = [int(x.strip()) for x in raw.split(",")]
        if sorted(indices) != list(range(1, len(agents) + 1)):
            raise ValueError
        new_order = [agents[i - 1] for i in indices]
    except (ValueError, IndexError):
        print("Invalid input -- must include every number exactly once.")
        return
    state.reorder_round_robin_agents(new_order)
    print("Rotation order updated.")


def toggle_round_robin(state: StateStore) -> None:
    enabled = state.is_round_robin_enabled()
    new_value = not enabled
    if new_value and not state.list_round_robin_agents():
        print("Warning: enabling round robin with no active agents configured -- "
              "new contacts will be created unassigned until you add some.")
    state.set_round_robin_enabled(new_value)
    print(f"Round robin is now {'ENABLED' if new_value else 'DISABLED'}.")


def main() -> int:
    config = load_config()
    state = StateStore(config.state_db_path)
    ghl_user_names = fetch_ghl_user_names(config)

    while True:
        print_status(state)
        print(
            "\n1. Add agent\n"
            "2. Remove agent\n"
            "3. Toggle agent active/inactive\n"
            "4. Reorder rotation\n"
            "5. Enable/disable round robin\n"
            "6. Refresh GHL user list\n"
            "0. Exit"
        )
        choice = input("Choose an option: ").strip()

        if choice == "1":
            add_agent(state, ghl_user_names)
        elif choice == "2":
            remove_agent(state)
        elif choice == "3":
            toggle_active(state)
        elif choice == "4":
            reorder_agents(state)
        elif choice == "5":
            toggle_round_robin(state)
        elif choice == "6":
            ghl_user_names = fetch_ghl_user_names(config)
        elif choice == "0":
            print("Done.")
            return 0
        else:
            print("Invalid choice.")


if __name__ == "__main__":
    sys.exit(main())
