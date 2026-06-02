"""site_smart_export.py — Smart tiered export of site_leads with valid email contacts.

Reads site_leads + site_contacts from Firestore, keeps only contacts with valid emails,
scores and groups leads into 5 tiers based on page count, platform and sector fit for
BlueSearch (SEO/search visibility services), then exports to Excel.

Tier 1 — Enterprise  : >10,000 pages
Tier 2 — Hot         : 500-10,000 pages + WordPress/WooCommerce OR priority sectors
Tier 3 — Good        : 100-500 pages, any platform
Tier 4 — Warm        :  50-100 pages
Tier 5 — Cold        : <50 pages

Bonus signals that can bump a site up one tier:
  - WordPress/WooCommerce platform detected
  - Sector is ecommerce, technology, or media
  - 3+ valid email contacts found

Usage:
    python app\\site_smart_export.py --countries UK
    python app\\site_smart_export.py --countries UK IN --out exports\\smart_uk_in.xlsx
    python app\\site_smart_export.py --countries NO --min-pages 50
"""
from __future__ import annotations

import threading as _threading

# Guards firebase_admin.initialize_app against concurrent init
_local_fb_lock = _threading.Lock()
import argparse
import importlib.util
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Email validation
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r'^[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+(?:\.[a-zA-Z0-9\-]+)+$')
_EMAIL_BAD = re.compile(
    r'(noreply|no-reply|donotreply|example|localhost|invalid|test@|dummy|placeholder)',
    re.IGNORECASE
)


def _valid_email(email: str) -> bool:
    if not email or '@' not in email:
        return False
    local = email.split('@')[0]
    if not _EMAIL_RE.match(email):
        return False
    if _EMAIL_BAD.search(email):
        return False
    if len(local) >= 16 and re.fullmatch(r'[0-9a-f\-]+', local):
        return False
    return True


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

HOT_SECTORS   = {'ecommerce', 'technology', 'media', 'company', 'finance'}
HOT_PLATFORMS = {'wordpress', 'woocommerce', 'elementor', 'divi'}


def _score_lead(lead: dict, email_count: int) -> tuple[int, str]:
    """Return (tier 1-6, tier_label) for a lead."""
    pages = int(lead.get('page_count') or 0)
    platform = (lead.get('ai_platform') or lead.get('platform') or '').lower()
    sector   = (lead.get('ai_sector') or '').lower()

    is_wp     = any(p in platform for p in HOT_PLATFORMS)
    is_hot_s  = sector in HOT_SECTORS
    many_contacts = email_count >= 3

    # Base tier from page count — two enterprise tiers
    if pages > 100_000:
        tier = 1   # Ultra Enterprise
    elif pages > 10_000:
        tier = 2   # Enterprise
    elif pages >= 500:
        tier = 3   # Hot
    elif pages >= 100:
        tier = 4   # Good
    elif pages >= 50:
        tier = 5   # Warm
    else:
        tier = 6   # Cold

    # Bonus signals bump up one tier (enterprise tiers are immune — already top)
    bonuses = sum([is_wp, is_hot_s, many_contacts])
    if bonuses >= 2 and tier > 2:
        tier -= 1
    elif bonuses == 1 and tier > 3:
        tier -= 1

    labels = {
        1: '1 - Ultra Enterprise',
        2: '2 - Enterprise',
        3: '3 - Hot',
        4: '4 - Good',
        5: '5 - Warm',
        6: '6 - Cold',
    }
    return tier, labels[tier]


# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------

def _load_secrets():
    p = Path(__file__).parent.parent / 'blueboot_secrets.py'
    if not p.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location('blueboot_secrets', p)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, 'fireBaseAdminKey', None)
    except Exception:
        return None


def _init_firestore(fb_key):
    import firebase_admin
    from firebase_admin import firestore
    import firebase_admin.credentials as creds
    c = creds.Certificate(fb_key) if fb_key else creds.Certificate(
        os.getenv('FIREBASE_CREDENTIALS', 'config/serviceAccountKey.json'))
    with _local_fb_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
    return firestore.client()


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_data(db, countries: list[str] | None, min_pages: int,
               outreach_priority: int | None = None) -> list[dict]:
    """Load contacts via collectionGroup, then batch-fetch parent leads. Fast."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    from concurrent.futures import ThreadPoolExecutor

    PAGE_SIZE    = 500
    LEAD_BATCH   = 500   # Firestore in() limit
    WORKERS      = 20

    print('[smart-export] Step 1: streaming site_contacts collectionGroup…', flush=True)

    # ── Step 1: collect all valid contacts + their lead IDs ─────────────
    cg = db.collection_group('site_contacts')

    contact_map: dict[str, list[dict]] = {}   # lead_id → [contact, ...]
    last_doc = None
    total_contacts = 0

    while True:
        q = cg.order_by('__name__').limit(PAGE_SIZE)
        if last_doc:
            q = q.start_after(last_doc)
        page = list(q.stream())
        if not page:
            break
        last_doc = page[-1]

        for doc in page:
            c = doc.to_dict() or {}
            email = (c.get('email') or '').strip()
            if not _valid_email(email):
                continue
            # parent path: site_leads/{lead_id}/site_contacts/{contact_id}
            parts = doc.reference.path.split('/')
            if len(parts) >= 4:
                lead_id = parts[1]   # second segment
                contact_map.setdefault(lead_id, []).append(c)
                total_contacts += 1

        print(f'[smart-export]   {sum(len(v) for v in contact_map.values())} valid contacts '
              f'in {len(contact_map)} leads so far…', flush=True)
        if len(page) < PAGE_SIZE:
            break

    print(f'[smart-export] Step 1 done: {total_contacts} valid contacts in {len(contact_map)} leads', flush=True)

    # ── Step 2: batch-fetch parent lead docs ────────────────────────────
    print('[smart-export] Step 2: batch-fetching parent site_leads…', flush=True)
    lead_ids = list(contact_map.keys())
    leads_col = db.collection('site_leads')
    lead_data: dict[str, dict] = {}

    def _fetch_batch(batch_ids):
        refs = [leads_col.document(lid) for lid in batch_ids]
        return db.get_all(refs)

    batches = [lead_ids[i:i+LEAD_BATCH] for i in range(0, len(lead_ids), LEAD_BATCH)]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_fetch_batch, b) for b in batches]
        for fut in futures:
            for doc in fut.result():
                if doc.exists:
                    lead_data[doc.id] = doc.to_dict() or {}

    print(f'[smart-export] Step 2 done: {len(lead_data)} lead docs fetched', flush=True)

    # ── Step 3: merge + filter ───────────────────────────────────────────
    print('[smart-export] Step 3: merging and filtering…', flush=True)
    rows: list[dict] = []
    skipped = 0

    for lead_id, contacts in contact_map.items():
        lead = lead_data.get(lead_id)
        if not lead:
            skipped += 1
            continue

        if lead.get('country') == '*':
            skipped += 1
            continue

        if countries:
            c = (lead.get('ai_country') or lead.get('country') or '').upper()
            if c not in countries:
                skipped += 1
                continue

        pages = int(lead.get('page_count') or 0)
        if pages < min_pages:
            skipped += 1
            continue

        # Outreach priority filter on contacts
        if outreach_priority is not None:
            contacts = [c for c in contacts
                        if c.get("outreach_priority") is None
                        or int(c.get("outreach_priority", 4)) <= outreach_priority]
            if not contacts:
                skipped += 1
                continue

        tier, tier_label = _score_lead(lead, len(contacts))

        base = {
            'tier':         tier,
            'tier_label':   tier_label,
            'domain':       lead.get('domain', ''),
            'website':      lead.get('website', ''),
            'country':      (lead.get('ai_country') or lead.get('country') or '').upper(),
            'pages':        pages,
            'platform':     lead.get('ai_platform') or lead.get('platform') or '',
            'sector':       lead.get('ai_sector') or '',
            'company_type': lead.get('ai_company_type') or '',
            'summary':      (lead.get('ai_summary') or '')[:120],
            'confidence':   lead.get('ai_confidence') or 0,
            'location':     lead.get('location') or '',
            'email_count':  len(contacts),
            'sitemap_type': lead.get('sitemap_type') or '',
        }

        for contact in contacts:
            rows.append({
                **base,
                'email':              contact.get('email', ''),
                'name':               contact.get('name', ''),
                'title':              contact.get('title', '') or contact.get('occupation', ''),
                'phone':              contact.get('phone', ''),
                'linkedin':           contact.get('linkedin', ''),
                'found_on':           contact.get('found_on', ''),
                'email_type':         contact.get('email_type', ''),
                'contact_type':       contact.get('contact_type', ''),
                'outreach_priority':  contact.get('outreach_priority', ''),
            })

    print(f'[smart-export] {len(rows)} contact rows in '
          f'{len(set(r["domain"] for r in rows))} sites  ({skipped} leads skipped)', flush=True)
    return rows

# ---------------------------------------------------------------------------
# Excel builder
# ---------------------------------------------------------------------------

TIER_COLORS = {
    1: '7030A0',  # Purple    — Ultra Enterprise (>100k pages)
    2: 'C00000',  # Dark red  — Enterprise       (>10k pages)
    3: 'FF0000',  # Red       — Hot
    4: 'FF9900',  # Orange    — Good
    5: 'FFFF00',  # Yellow    — Warm
    6: 'D9D9D9',  # Grey      — Cold
}
TIER_TEXT = {1: 'FFFFFF', 2: 'FFFFFF', 3: 'FFFFFF', 4: '000000', 5: '000000', 6: '000000'}


def _build_excel(rows: list[dict], out_path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    # ── Sheet 1: All Contacts sorted by tier → pages desc ──────────────────
    ws = wb.active
    ws.title = 'Contacts'

    COLS = [
        ('Tier',        'tier_label',   18),
        ('Domain',      'domain',       28),
        ('Email',       'email',        30),
        ('Name',        'name',         22),
        ('Title',       'title',        22),
        ('Phone',       'phone',        16),
        ('LinkedIn',    'linkedin',     30),
        ('Country',     'country',       8),
        ('Pages',       'pages',        10),
        ('Platform',    'platform',     16),
        ('Sector',      'sector',       16),
        ('Location',    'location',     25),
        ('Summary',     'summary',      50),
        ('Confidence',  'confidence',    9),
        ('Emails on site','email_count', 12),
        ('Found on',    'found_on',     30),
        ('Email Type',  'email_type',   12),
        ('Contact Role','contact_type', 16),
        ('Outreach P',  'outreach_priority', 9),
    ]

    HDR_FILL  = PatternFill('solid', start_color='1F497D')
    HDR_FONT  = Font(name='Arial', bold=True, color='FFFFFF', size=10)
    DATA_FONT = Font(name='Arial', size=10)
    WRAP      = Alignment(wrap_text=True, vertical='top')
    NOWRAP    = Alignment(vertical='top')
    THIN      = Side(style='thin', color='CCCCCC')
    BORDER    = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

    # Header
    for ci, (hdr, _, w) in enumerate(COLS, 1):
        cell = ws.cell(row=1, column=ci, value=hdr)
        cell.font   = HDR_FONT
        cell.fill   = HDR_FILL
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = 'A2'

    # Sort: tier asc, pages desc
    sorted_rows = sorted(rows, key=lambda r: (r['tier'], -r['pages']))

    for ri, row in enumerate(sorted_rows, 2):
        tier = row['tier']
        bg   = TIER_COLORS.get(tier, 'FFFFFF')
        fg   = TIER_TEXT.get(tier, '000000')
        tier_fill = PatternFill('solid', start_color=bg)
        tier_font = Font(name='Arial', size=10, color=fg, bold=tier <= 2)

        for ci, (_, key, _) in enumerate(COLS, 1):
            val  = row.get(key, '')
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.border = BORDER
            if ci == 1:   # Tier column — coloured
                cell.fill  = tier_fill
                cell.font  = tier_font
                cell.alignment = Alignment(horizontal='center', vertical='top')
            elif key in ('summary', 'linkedin', 'found_on'):
                cell.font      = DATA_FONT
                cell.alignment = WRAP
            else:
                cell.font      = DATA_FONT
                cell.alignment = NOWRAP

        ws.row_dimensions[ri].height = 18

    ws.auto_filter.ref = f'A1:{get_column_letter(len(COLS))}1'

    # ── Sheet 2: Summary ─────────────────────────────────────────────────
    ws2 = wb.create_sheet('Summary')
    ws2.column_dimensions['A'].width = 22
    ws2.column_dimensions['B'].width = 12
    ws2.column_dimensions['C'].width = 20

    def _hdr2(cell, val):
        cell.value = val
        cell.font  = Font(name='Arial', bold=True, size=11)
        cell.fill  = PatternFill('solid', start_color='1F497D')
        cell.font  = Font(name='Arial', bold=True, color='FFFFFF', size=10)

    # By tier
    _hdr2(ws2.cell(1, 1), 'Tier')
    _hdr2(ws2.cell(1, 2), 'Sites')
    _hdr2(ws2.cell(1, 3), 'Contacts (emails)')

    from collections import Counter
    tier_sites    = Counter(r['domain'] + '|' + str(r['tier']) for r in rows)
    tier_s: dict[int, int] = {}
    tier_c: dict[int, int] = Counter(r['tier'] for r in rows)
    for k in tier_sites:
        t = int(k.split('|')[1])
        tier_s[t] = tier_s.get(t, 0) + 1

    tier_labels = {1:'1-Ultra Enterprise', 2:'2-Enterprise', 3:'3-Hot', 4:'4-Good', 5:'5-Warm', 6:'6-Cold'}
    for ri, t in enumerate(sorted(tier_labels), 2):  # tiers 1-6
        bg = TIER_COLORS.get(t, 'FFFFFF')
        fg = TIER_TEXT.get(t, '000000')
        for ci, val in enumerate([tier_labels[t], tier_s.get(t, 0), tier_c.get(t, 0)], 1):
            cell = ws2.cell(ri, ci, val)
            cell.fill = PatternFill('solid', start_color=bg)
            cell.font = Font(name='Arial', size=10, color=fg)
            cell.border = BORDER
    ws2.cell(9, 1, 'TOTAL').font = Font(name='Arial', bold=True)
    ws2.cell(9, 2, len(set(r['domain'] for r in rows))).font = Font(name='Arial', bold=True)
    ws2.cell(9, 3, len(rows)).font = Font(name='Arial', bold=True)

    # By sector
    ws2.cell(10, 1).value = 'By Sector'
    ws2.cell(10, 1).font  = Font(name='Arial', bold=True, size=11)
    sector_c = Counter(r['sector'] or 'unknown' for r in rows)
    for ri, (sec, cnt) in enumerate(sector_c.most_common(15), 11):
        ws2.cell(ri, 1, sec)
        ws2.cell(ri, 2, cnt)

    # By platform
    ws2.cell(10, 3).value = 'By Platform'
    ws2.cell(10, 3).font  = Font(name='Arial', bold=True, size=11)
    plat_c = Counter((r.get('platform') or 'unknown').lower() for r in rows)
    for ri, (plat, cnt) in enumerate(plat_c.most_common(10), 11):
        ws2.cell(ri, 3, plat)
        ws2.cell(ri, 4, cnt)
    ws2.column_dimensions['D'].width = 10

    # ── Sheet 3: By Platform ─────────────────────────────────────────────
    ws3 = wb.create_sheet('WordPress vs Others')
    wp_rows    = [r for r in rows if any(p in (r.get('platform') or '').lower() for p in HOT_PLATFORMS)]
    other_rows = [r for r in rows if r not in wp_rows]

    _hdr2(ws3.cell(1, 1), 'Group')
    _hdr2(ws3.cell(1, 2), 'Sites')
    _hdr2(ws3.cell(1, 3), 'Contacts')
    _hdr2(ws3.cell(1, 4), 'Avg Pages')

    for ri, (label, grp) in enumerate([('WordPress/WooCommerce', wp_rows), ('Other Platforms', other_rows)], 2):
        sites = len(set(r['domain'] for r in grp))
        avg_p = int(sum(r['pages'] for r in grp) / len(grp)) if grp else 0
        for ci, val in enumerate([label, sites, len(grp), avg_p], 1):
            ws3.cell(ri, ci, val).font = Font(name='Arial', size=10)
    ws3.column_dimensions['A'].width = 25
    for col in ['B', 'C', 'D']:
        ws3.column_dimensions[col].width = 12

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    print(f'[smart-export] Saved → {out_path}', flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description='Tiered BlueSearch prospect export from site_leads')
    p.add_argument('--countries', nargs='+', default=None, metavar='CC',
                   help='Country codes e.g. UK IN NO (comma or space separated)')
    p.add_argument('--min-pages', type=int, default=0, metavar='N',
                   help='Minimum page count (default 0)')
    p.add_argument('--outreach-priority', type=int, default=None, metavar='N',
                   help='Only include contacts with outreach_priority <= N (1=best only, 2=top two, etc.)')
    p.add_argument('--out', default=None, metavar='PATH',
                   help='Output .xlsx path (default: exports/smart_<countries>_<ts>.xlsx)')
    args = p.parse_args(argv)

    countries = None
    if args.countries:
        raw = []
        for t in args.countries:
            raw.extend(c.strip().upper() for c in t.split(',') if c.strip())
        countries = raw or None

    fb_key = _load_secrets()
    db     = _init_firestore(fb_key)

    rows = _load_data(db, countries, args.min_pages, args.outreach_priority)
    if not rows:
        print('[smart-export] No contacts found matching filters.')
        return

    if not args.out:
        ts  = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')
        cc  = '_'.join(countries) if countries else 'all'
        out = Path('exports') / f'site_prospects_{cc}_{ts}.xlsx'
    else:
        out = Path(args.out)

    _build_excel(rows, out)

    # Tier summary
    from collections import Counter
    tc = Counter(r['tier_label'] for r in rows)
    print('\n[smart-export] Tier breakdown:')
    for label, count in sorted(tc.items()):
        print(f'  {label}: {count} contacts')


if __name__ == '__main__':
    main()
