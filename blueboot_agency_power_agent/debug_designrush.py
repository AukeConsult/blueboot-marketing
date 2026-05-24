"""
Tests catalog_links_designrush directly against the live page.
  python debug_designrush.py
"""
import sys
sys.path.insert(0, "app")

from lead_agent import catalog_links_designrush

URL = "https://www.designrush.com/agency/website-design-development/se?page=1"

found = catalog_links_designrush(URL, blocklist=set())
print(f"\ncatalog_links_designrush returned {len(found)} URLs:")
for u in found[:20]:
    print(f"  {u}")
