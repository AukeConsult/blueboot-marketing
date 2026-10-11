"""
push_to_campaign_lib.py -- Move selected contacts from the contact sheet into campaigns.

Each selected sheet row (Select filled) is put into the campaign named in its Campaign column.
A row is ignored when
  * its site already sits in another campaign (campaign_leads of any other campaign), or
  * its e-mail already sits in another campaign (campaign_contacts of any other campaign).
A row that is already in its own campaign is left alone. Everything else is added through
the normal campaign import (campaign doc created as draft when missing, campaign_leads and
campaign_contacts written, nothing existing overwritten). The sheet itself is only read.
"""
from __future__ import annotations
import re
from collections import Counter, defaultdict

from crm.campaign_import_lib import (
    CAMPAIGNS_COLLECTION, lead_id_from_website, run_campaign_import,
)
from crm.contact_clean_lib import clean_email
from crm.sheets_config import CONTACT_SHEET_ID, CONTACT_TAB

# Why a row is not added ("" = it is added).
ADD, SITE_OTHER, CONTACT_OTHER, ALREADY = "", "site_in_other", "contact_in_other", "already_in_campaign"
NO_CAMPAIGN, NO_SITE, NO_EMAIL = "no_campaign", "no_site", "no_email"
REASONS = (ADD, SITE_OTHER, CONTACT_OTHER, ALREADY, NO_CAMPAIGN, NO_SITE, NO_EMAIL)


def _norm_id(x) -> str:
    """Same normalisation the campaign import applies to lead ids and campaign names."""
    return re.sub(r"[^a-z0-9_-]+", "_", str(x or "").strip().lower()).strip("_")


def _read_selected_rows(svc, tab: str, info: dict) -> list[dict]:
    """Rows of the contact sheet with Select filled, keyed by lower-cased header (spaces -> _)."""
    rows = svc.spreadsheets().values().get(
        spreadsheetId=CONTACT_SHEET_ID, range=f"{tab}!A:ZZ"
    ).execute().get("values", [])
    info.update({"tab": tab, "rows": max(len(rows) - 1, 0), "select_column": False,
                 "headers": [str(h) for h in (rows[0] if rows else [])]})
    if not rows:
        return []
    col = {h.strip().lower().replace(" ", "_"): i for i, h in enumerate(rows[0])}
    sel = col.get("select", -1)
    if sel < 0:
        return []
    info["select_column"] = True
    return [{h: (r[i].strip() if i < len(r) else "") for h, i in col.items()}
            for r in rows[1:] if sel < len(r) and r[sel].strip()]


def _campaigns_by(db, sub: str, fields: list, key_of) -> dict:
    """{key: set(campaign ids)} over campaigns/*/<sub>; key_of(doc) gives the lookup key."""
    found: dict = defaultdict(set)
    for doc in db.collection_group(sub).select(fields).stream():
        key, parent = key_of(doc), doc.reference.parent.parent
        if key and parent:
            found[key].add(parent.id)
    return found


def _plan(db, svc, contact_tab: str, campaign: str) -> dict:
    """Read-only: decide for every selected row whether it is added or why it is ignored."""
    sheet: dict = {}
    rows = _read_selected_rows(svc, contact_tab, sheet)
    sheet_campaigns = Counter((r.get("campaign") or "").strip() or "(blank)" for r in rows)
    campaign = campaign.strip()
    if campaign:
        rows = [r for r in rows if (r.get("campaign") or "").strip().lower() == campaign.lower()]

    sites_in = _campaigns_by(db, "campaign_leads", [], lambda d: _norm_id(d.id))
    emails_in = _campaigns_by(db, "campaign_contacts", ["email"],
                              lambda d: str((d.to_dict() or {}).get("email") or "").strip().lower())

    decisions = []
    for r in rows:
        target = (r.get("campaign") or "").strip()
        email = clean_email(r.get("email") or "")
        website = (r.get("website") or "").strip()
        site = _norm_id(r.get("lead_id_site")) or (_norm_id(lead_id_from_website(website)) if website else "")
        d = {"row": r, "campaign": target, "email": email, "site_id": site, "others": [], "reason": ADD}
        if not target:
            d["reason"] = NO_CAMPAIGN
        elif not site:
            d["reason"] = NO_SITE
        elif not email:
            d["reason"] = NO_EMAIL
        else:
            t = _norm_id(target)
            site_other = sorted(c for c in sites_in.get(site, ()) if _norm_id(c) != t)
            contact_other = sorted(c for c in emails_in.get(email, ()) if _norm_id(c) != t)
            if site_other:
                d["reason"], d["others"] = SITE_OTHER, site_other
            elif contact_other:
                d["reason"], d["others"] = CONTACT_OTHER, contact_other
            elif any(_norm_id(c) == t for c in emails_in.get(email, ())):
                d["reason"] = ALREADY
        decisions.append(d)
    return {"sheet": sheet, "sheet_campaigns": dict(sheet_campaigns), "campaign": campaign,
            "decisions": decisions}


def _item(d: dict) -> dict:
    r = d["row"]
    return {"name": r.get("name", ""), "email": d["email"] or r.get("email", ""),
            "website": r.get("website", ""), "site_id": d["site_id"],
            "campaign": d["campaign"], "others": d["others"]}


def preview_push(db, svc, contact_tab: str = CONTACT_TAB, campaign: str = "",
                 max_items: int = 200) -> dict:
    """Report what Push would do. Read-only: nothing is written anywhere."""
    p = _plan(db, svc, contact_tab, campaign)
    by_reason = {k: [d for d in p["decisions"] if d["reason"] == k] for k in REASONS}
    add = by_reason[ADD]
    known = {_norm_id(c.id) for c in db.collection(CAMPAIGNS_COLLECTION).select([]).stream()}
    new_campaigns = sorted({d["campaign"] for d in add if _norm_id(d["campaign"]) not in known})

    sh, warnings = p["sheet"], []
    if not sh.get("select_column"):
        warnings.append("No 'Select' column found in tab '%s' of the contact sheet. Headers seen: %s"
                        % (sh.get("tab"), ", ".join(sh.get("headers", [])) or "(none)"))
    elif not p["decisions"] and p["campaign"]:
        warnings.append("No selected row has Campaign = '%s' in the contact sheet." % p["campaign"])
    elif not p["decisions"]:
        warnings.append("The 'Select' column exists but no row has a value in it (%d data rows scanned)."
                        % sh.get("rows", 0))
    if new_campaigns:
        warnings.append("These campaigns do not exist yet and will be created as drafts: " + ", ".join(new_campaigns))

    return {
        "selected": len(p["decisions"]),
        "campaign": p["campaign"],
        "sheet_campaigns": p["sheet_campaigns"],
        "sheet": {k: v for k, v in sh.items() if k != "headers"},
        "add": {"contacts": len(add), "sites": len({(d["campaign"], d["site_id"]) for d in add}),
                "items": [_item(d) for d in add[:max_items]]},
        "ignored": {k: {"count": len(v), "items": [_item(d) for d in v[:max_items]]}
                    for k, v in by_reason.items() if k != ADD},
        "new_campaigns": new_campaigns,
        "can_push": bool(add),
        "warnings": warnings,
        "max_items": max_items,
    }


def run_push_to_campaign(db, svc, contact_tab: str = CONTACT_TAB, campaign: str = "") -> dict:
    """Add every row the analysis marks as ADD to its campaign (existing entries are never overwritten)."""
    p = _plan(db, svc, contact_tab, campaign)
    per_campaign = defaultdict(list)
    for d in p["decisions"]:
        if d["reason"] == ADD:
            per_campaign[d["campaign"]].append({**d["row"], "lead_id": d["site_id"], "email": d["email"]})

    results = {camp: run_campaign_import(db, camp, rows) for camp, rows in per_campaign.items()}
    ignored = Counter(d["reason"] for d in p["decisions"] if d["reason"] != ADD)
    summary = {
        "selected": len(p["decisions"]),
        "contacts_added": sum(r["contacts_new"] for r in results.values()),
        "sites_added": sum(r["leads_new"] for r in results.values()),
        "campaigns": results,
        "ignored": dict(ignored),
    }
    print(f"[push-to-campaign] {summary['contacts_added']} contacts / {summary['sites_added']} sites added "
          f"to {len(results)} campaign(s); ignored: {dict(ignored)}", flush=True)
    return summary
