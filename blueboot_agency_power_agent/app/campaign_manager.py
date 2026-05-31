# app/campaign_manager.py

from pathlib import Path

from app.firestore_client import get_firestore
from app.campaign_exporter import export_campaign
from app.campaign_importer import import_campaign


# --------------------------------------------------
# Campaign Helpers
# --------------------------------------------------
def get_campaigns():
    db = get_firestore()
    campaigns = []

    for doc in db.collection("leads_extract").stream():
        data = doc.to_dict() or {}

        campaigns.append({"campaign_id": doc.id, "created_at": data.get("created_at", "")})

    campaigns.sort(key=lambda x: x["campaign_id"])

    return campaigns


def get_campaign_stats(campaign_id):
    db = get_firestore()

    campaign_ref = (db.collection("leads_extract").document(campaign_id))

    lead_count = 0
    contact_count = 0

    for lead_doc in (campaign_ref.collection("leads_extracted").stream()):
        lead_count += 1

        contacts = list(lead_doc.reference.collection("contacts_extracted").stream())
        contact_count += len(contacts)

    return {
        "lead_count": lead_count,
        "contact_count": contact_count,
    }


def choose_campaign():
    campaigns = get_campaigns()

    if not campaigns:
        print("\nNo campaigns found.\n")
        return None

    print("\nAvailable Campaigns\n")

    for idx, campaign in enumerate(campaigns, start=1):
        stats = get_campaign_stats(campaign["campaign_id"])

        print(
            f"{idx}. "
            f"{campaign['campaign_id']} "
            f"(Leads: {stats['lead_count']}, "
            f"Contacts: {stats['contact_count']})"
        )

    print()

    choice = input("Select campaign number: ").strip()

    try:
        choice = int(choice)

        return campaigns[
            choice - 1
        ]["campaign_id"]

    except Exception:
        print("\nInvalid selection.\n")
        return None


# --------------------------------------------------
# Menu Actions
# --------------------------------------------------
def action_list_campaigns():
    campaigns = get_campaigns()

    print("\nCampaigns\n")

    for campaign in campaigns:
        stats = get_campaign_stats(campaign["campaign_id"])

        print(
            f"{campaign['campaign_id']} "
            f"(Leads: {stats['lead_count']}, "
            f"Contacts: {stats['contact_count']})"
        )

    print()


def action_export_campaign():
    campaign_id = choose_campaign()

    if not campaign_id:
        return

    print(
        f"\nExporting "
        f"{campaign_id}...\n"
    )

    result = export_campaign(campaign_id=campaign_id)

    print("\nExport completed:\n")
    print(result)
    print()


def action_import_campaign():
    campaign_id = choose_campaign()

    if not campaign_id:
        return

    default_file = (
        Path("output")
        / campaign_id
        / "campaign.xlsx"
    )

    print(
        f"\nDefault file:\n"
        f"{default_file}\n"
    )

    custom_file = input(
        "Excel file "
        "(press Enter to use default): "
    ).strip()

    excel_file = (
        custom_file
        if custom_file
        else str(default_file)
    )

    dry_run_input = input("Dry run? (Y/N): ").strip().lower()

    dry_run = (dry_run_input != "n")

    print("\nImporting...\n")

    result = import_campaign(
        campaign_id=campaign_id,
        excel_file=excel_file,
        dry_run=dry_run,
    )

    print("\nImport completed:\n")
    print(result)
    print()


# --------------------------------------------------
# Main Menu
# --------------------------------------------------
def main():
    while True:
        print(
            "\n"
            "========================================\n"
            "BLUEBOOT CAMPAIGN MANAGER\n"
            "========================================\n"
            "1. List Campaigns\n"
            "2. Export Campaign\n"
            "3. Import Campaign\n"
            "4. Exit\n"
        )

        choice = input("Select option: ").strip()

        if choice == "1":
            action_list_campaigns()

        elif choice == "2":
            action_export_campaign()

        elif choice == "3":
            action_import_campaign()

        elif choice == "4":
            print("\nGoodbye.\n")
            break

        else:
            print("\nInvalid option.\n")


if __name__ == "__main__":
    main()
