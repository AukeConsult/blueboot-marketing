# functions-crm/smart_mail/outreach_status_lib.py
"""Read-only status of the outreach send loop.

Answers, WITHOUT sending anything, the same questions send_outreach() answers
when it runs:

  * which campaigns are ready to send, and which are blocked (and why)
  * how many contacts are waiting for the intro mail / a due reminder
  * how many of those are held back (not confirmed, site excluded, not due)
  * the send budget per sending account (hour/day limits, reservations)
  * how many mails the next run would actually send

It reuses the selection rules of outreach_mail_select.read_outreach() and the
budget rules of outreach_sender._claim_send_budget(); it never writes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from google.cloud.firestore_v1.base_query import FieldFilter

from .outreach_mail_select import (
    ContactRow,
    CampaignMail,
    _campaign_status,
    _load_account,
    _next_step_due,
    _prepare_mail_sequence,
)

_RESERVATION_TTL_SECONDS = 7200
_INTRO_OK = {"ready", "active"}      # same as read_outreach
_FOLLOWUP_OK = {"active"}


def _account_problem(account) -> str:
    """Same checks as outreach_sender._account_ready()."""
    if account.account_type == "gmail":
        if account.access_token or (account.client_id and account.client_secret and account.refresh_token):
            return ""
        return "Gmail account is missing OAuth settings"
    if not account.host:
        return "mail account has no SMTP host"
    if not account.username:
        return "mail account has no username"
    if not account.password:
        return "mail account has no password"
    return ""


def load_limits(db) -> dict:
    from .config import MAX_SENDS_PER_HOUR, MAX_SENDS_PER_DAY, BOUNCE_RATE_PAUSE_THRESHOLD
    out = {"max_per_hour": MAX_SENDS_PER_HOUR, "max_per_day": MAX_SENDS_PER_DAY,
           "bounce_threshold": BOUNCE_RATE_PAUSE_THRESHOLD, "source": "defaults(env/config)"}
    try:
        doc = db.collection("settings").document("send_limits").get()
        if doc.exists:
            d = doc.to_dict() or {}
            out.update(
                max_per_hour=int(d.get("max_sends_per_hour", out["max_per_hour"])),
                max_per_day=int(d.get("max_sends_per_day", out["max_per_day"])),
                bounce_threshold=float(d.get("bounce_rate_pause_threshold", out["bounce_threshold"])),
                source="Firestore(settings/send_limits)",
            )
    except Exception:
        pass
    return out


def _count(db, account: str, since_iso: str) -> int:
    docs = (db.collection("outreach_sent")
            .where(filter=FieldFilter("sender_account", "==", account))
            .where(filter=FieldFilter("sent_at", ">=", since_iso))
            .stream())
    return sum(1 for _ in docs)


def account_budget(db, account: str, limits: dict, now: datetime | None = None) -> dict:
    """Budget a run would get for this account right now (read-only)."""
    now = now or datetime.now(timezone.utc)
    sent_hour = _count(db, account, (now - timedelta(hours=1)).isoformat())
    sent_day = _count(db, account, (now - timedelta(days=1)).isoformat())
    stale_cutoff = (now - timedelta(seconds=_RESERVATION_TTL_SECONDS)).isoformat()
    try:
        reserved = sum(
            (d.to_dict() or {}).get("budget_claimed", 0)
            for d in db.collection("send_run_reservations")
            .where(filter=FieldFilter("account", "==", account))
            .where(filter=FieldFilter("status", "==", "active"))
            .where(filter=FieldFilter("started_at", ">=", stale_cutoff))
            .stream()
        )
    except Exception:
        reserved = 0
    rem_hour = max(0, limits["max_per_hour"] - sent_hour - reserved)
    rem_day = max(0, limits["max_per_day"] - sent_day - reserved)
    return {
        "account": account,
        "sent_last_hour": sent_hour, "sent_last_day": sent_day, "reserved": reserved,
        "max_per_hour": limits["max_per_hour"], "max_per_day": limits["max_per_day"],
        "remaining_hour": rem_hour, "remaining_day": rem_day,
        "budget": min(rem_hour, rem_day),
    }


def _campaign_status_row(db, cdoc, now, account_cache) -> dict:
    d = cdoc.to_dict() or {}
    cid = cdoc.id
    status = _campaign_status(d.get("status"))
    sender = (d.get("outreach_email_account") or d.get("sender_account") or "").strip().lower()
    seq = _prepare_mail_sequence(d.get("mail_sequence") or [])
    require = bool(d.get("require_send_confirmation", False))

    blockers: list[str] = []
    if status not in _INTRO_OK:
        blockers.append(f"campaign status is '{status}' (needs ready or active)")
    if not seq:
        blockers.append("no Intro mail step")
    account = None
    if not sender:
        blockers.append("no mail account on the campaign")
    else:
        if sender not in account_cache:
            account_cache[sender] = _load_account(db, sender)
        account = account_cache[sender]
        if account is None:
            blockers.append(f"mail account '{sender}' not found")
        else:
            problem = _account_problem(account)
            if problem:
                blockers.append(problem)

    row = {
        "campaign_id": cid, "name": d.get("name") or cid, "status": status,
        "sender": sender, "require_send_confirmation": require,
        "has_intro_step": bool(seq), "steps": len(seq),
        "ready": not blockers, "blockers": blockers,
        "followup_enabled": status in _FOLLOWUP_OK and bool(seq) and account is not None,
        "pending": None, "intro_waiting": 0, "intro_not_confirmed": 0,
        "intro_site_blocked": 0, "intro_to_send": 0,
        "followup_waiting": 0, "followup_due": 0, "followup_not_due": 0,
        "mailed": 0, "excluded": 0, "total": 0,
    }
    if status not in _INTRO_OK:
        return row                       # draft / on hold / canceled: the sender ignores it, no need to read contacts

    # Site-lead statuses (contacts of a non-pending/active site are skipped by the sender).
    lead_status: dict[str, str] = {}
    try:
        for ldoc in (db.collection("campaigns").document(cid).collection("campaign_leads")
                     .select(["status"]).stream()):
            lead_status[ldoc.id] = str((ldoc.to_dict() or {}).get("status", "")).strip().lower()
    except Exception:
        pass

    camp = CampaignMail(campaign_id=cid, campaign_name=row["name"], status=status,
                        subject_template="", body_html="", sender_email=sender, mail_sequence=seq)
    pending = 0
    for cd in (db.collection("campaigns").document(cid).collection("campaign_contacts")
               .select(["status", "mail_sent", "send_confirmation", "lead_id"]).stream()):
        c = cd.to_dict() or {}
        row["total"] += 1
        st = str(c.get("status", "")).strip().lower()
        sent = c.get("mail_sent") or []
        if not isinstance(sent, list):
            sent = []
        if sent:
            row["mailed"] += 1
        if st == "excluded":
            row["excluded"] += 1
        if st != "pending":
            continue
        pending += 1
        lid = str(c.get("lead_id") or "").strip()
        site_blocked = bool(lid and lid in lead_status and lead_status[lid] not in ("pending", "active"))
        if not sent:                                  # intro candidate
            row["intro_waiting"] += 1
            if require and not c.get("send_confirmation"):
                row["intro_not_confirmed"] += 1
            elif site_blocked:
                row["intro_site_blocked"] += 1
            else:
                row["intro_to_send"] += 1
        else:                                         # reminder candidate
            row["followup_waiting"] += 1
            if site_blocked:
                row["followup_not_due"] += 1
                continue
            cr = ContactRow(contact_doc_id=cd.id, campaign_id=cid, email="", contact_name="",
                            company="", domain="", country="", status=st, mail_sent=sent)
            if _next_step_due(cr, camp, now):
                row["followup_due"] += 1
            else:
                row["followup_not_due"] += 1
    row["pending"] = pending
    # What the next run could pick up from this campaign (0 when blocked).
    row["intro_sendable"] = row["intro_to_send"] if row["ready"] else 0
    row["followup_sendable"] = row["followup_due"] if (row["ready"] and row["followup_enabled"]) else 0
    return row


def compute_outreach_status(db, campaign_ids: list[str] | None = None) -> dict:
    now = datetime.now(timezone.utc)
    limits = load_limits(db)
    account_cache: dict = {}
    wanted = {c for c in (campaign_ids or []) if c}

    cdocs = [c for c in db.collection("campaigns").stream() if not wanted or c.id in wanted]
    # Resolve each distinct mail account once, up front (threads then only read the cache).
    for cdoc in cdocs:
        d = cdoc.to_dict() or {}
        sender = (d.get("outreach_email_account") or d.get("sender_account") or "").strip().lower()
        if sender and sender not in account_cache:
            account_cache[sender] = _load_account(db, sender)

    def one(cdoc):
        try:
            return _campaign_status_row(db, cdoc, now, account_cache)
        except Exception as ex:                    # one bad campaign must not hide the rest
            return {"campaign_id": cdoc.id, "name": cdoc.id, "status": "?",
                    "ready": False, "blockers": [f"status check failed: {ex}"]}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        campaigns = list(pool.map(one, cdocs))
    for c in campaigns:
        c.setdefault("intro_sendable", 0)
        c.setdefault("followup_sendable", 0)

    # Per account: budget + what is waiting for it.
    accounts: dict[str, dict] = {}
    for c in campaigns:
        s = c.get("sender") or ""
        if not s:
            continue
        a = accounts.setdefault(s, {"account": s, "campaigns": 0, "ready_campaigns": 0,
                                    "intro_sendable": 0, "followup_sendable": 0})
        a["campaigns"] += 1
        if c.get("ready"):
            a["ready_campaigns"] += 1
        a["intro_sendable"] += c.get("intro_sendable", 0)
        a["followup_sendable"] += c.get("followup_sendable", 0)
    for s, a in accounts.items():
        acc = account_cache.get(s)
        a["configured"] = acc is not None
        a["problem"] = ("account not found" if acc is None else _account_problem(acc))
        a.update({k: v for k, v in account_budget(db, s, limits, now).items() if k != "account"})
        a["waiting"] = a["intro_sendable"] + a["followup_sendable"]
        a["next_run_sends"] = 0 if (a["problem"] or acc is None) else min(a["budget"], a["waiting"])
        a["left_over"] = max(0, a["waiting"] - a["next_run_sends"])

    totals = {
        "campaigns": len(campaigns),
        "ready": sum(1 for c in campaigns if c.get("ready")),
        "blocked": sum(1 for c in campaigns if not c.get("ready")),
        "intro_waiting": sum(c.get("intro_waiting", 0) for c in campaigns),
        "intro_not_confirmed": sum(c.get("intro_not_confirmed", 0) for c in campaigns),
        "intro_sendable": sum(c.get("intro_sendable", 0) for c in campaigns),
        "followup_due": sum(c.get("followup_sendable", 0) for c in campaigns),
        "next_run_sends": sum(a["next_run_sends"] for a in accounts.values()),
        "left_over": sum(a["left_over"] for a in accounts.values()),
    }
    campaigns.sort(key=lambda c: (not c.get("ready"), -(c.get("intro_sendable", 0) + c.get("followup_sendable", 0)),
                                  c.get("campaign_id", "")))
    return {"status": "ok", "calculated_at": now.isoformat(), "limits": limits,
            "totals": totals, "campaigns": campaigns,
            "accounts": sorted(accounts.values(), key=lambda a: a["account"])}
