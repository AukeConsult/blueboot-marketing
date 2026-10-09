"""Invariant: every campaign_contact points (lead_id) at a campaign_leads doc of the SAME campaign.

ensure_site_links(db, campaign_id) checks every contact of a campaign and repairs the link:
  1. lead_id already names a site of this campaign          -> nothing to do
  2. else match by the contact's website / domain / e-mail domain against the campaign's sites
  3. else create the site in this campaign (from the contact's own data) and link to it
The old value is kept in `lead_id_old`.  Returns counts.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

CAMPAIGNS = "campaigns"
LEADS = "campaign_leads"
CONTACTS = "campaign_contacts"

FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
             "icloud.com", "me.com", "proton.me", "protonmail.com", "aol.com", "msn.com",
             "online.no", "hotmail.no", "live.no", "telenor.no", "gmx.com", "gmx.net", "web.de"}


def host_of(u: str) -> str:
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return re.split(r"[/?#]", u, maxsplit=1)[0]


def lead_id_for_host(host: str) -> str:
    return re.sub(r"[.\-]", "_", host)


def _email_host(email: str) -> str:
    h = (email or "").split("@")[-1].strip().lower() if "@" in (email or "") else ""
    return "" if h in FREE_MAIL else h


def ensure_site_links(db, campaign_id: str, dry_run: bool = False) -> dict:
    camp = db.collection(CAMPAIGNS).document(campaign_id)
    leads_col, contacts_col = camp.collection(LEADS), camp.collection(CONTACTS)

    lead_ids, by_host = set(), {}
    for snap in leads_col.stream():
        d = snap.to_dict() or {}
        lead_ids.add(snap.id)
        for h in (host_of(d.get("website", "")), host_of(d.get("domain", ""))):
            if h:
                by_host.setdefault(h, snap.id)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = {"links_ok": 0, "links_fixed": 0, "sites_created": 0}
    batch, pending = db.batch(), 0

    def flush(force=False):
        nonlocal batch, pending
        if pending and (force or pending >= 400):
            batch.commit()
            batch, pending = db.batch(), 0

    for snap in contacts_col.stream():
        d = snap.to_dict() or {}
        if (d.get("lead_id") or "") in lead_ids:
            stats["links_ok"] += 1
            continue
        h = host_of(d.get("website", "")) or host_of(d.get("domain", "")) or _email_host(d.get("email", ""))
        target = by_host.get(h) or by_host.get(_email_host(d.get("email", "")))
        if not target:
            if h:
                target = lead_id_for_host(h)
                site = {"lead_id": target, "campaign_id": campaign_id, "website": "https://" + h,
                        "domain": h, "company": d.get("company", ""), "country": d.get("country", ""),
                        "status": "pending", "created_at": now, "source": "site-link-repair"}
            else:      # free-mail address and no website: its own site, keyed by the address
                target = "mail_" + re.sub(r"[^a-z0-9]+", "_", (d.get("email") or snap.id).lower()).strip("_")
                site = {"lead_id": target, "campaign_id": campaign_id, "website": "", "domain": "",
                        "company": d.get("company", ""), "country": d.get("country", ""),
                        "status": "pending", "created_at": now, "source": "site-link-repair"}
            stats["sites_created"] += 1
            lead_ids.add(target)
            if h:
                by_host[h] = target
            if not dry_run:
                batch.set(leads_col.document(target), site, merge=True)
                pending += 1
        upd = {"lead_id": target}
        if d.get("lead_id"):
            upd["lead_id_old"] = d["lead_id"]
        stats["links_fixed"] += 1
        if not dry_run:
            batch.update(snap.reference, upd)
            pending += 1
            flush()
    if not dry_run:
        flush(force=True)
    print(f"[site-link] {campaign_id}: {stats['links_ok']} ok, {stats['links_fixed']} re-linked, "
          f"{stats['sites_created']} site(s) created", flush=True)
    return stats
