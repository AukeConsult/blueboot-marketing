"""
Tests _sortlist_urls_from_json directly against the live page.
  python debug_sortlist.py
"""
import json, sys, requests, traceback
sys.path.insert(0, "app")

from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
URL = "https://www.sortlist.com/web-design/norway-no"

r = requests.get(URL, headers={"User-Agent": UA, "Accept-Language": "en;q=0.8"}, timeout=20)
print(f"HTTP {r.status_code}  ({len(r.text)} bytes)")

soup = BeautifulSoup(r.text, "html.parser")
script = soup.find("script", id="__NEXT_DATA__")
if not script or not script.string:
    print("ERROR: No __NEXT_DATA__ script tag found")
    sys.exit(1)

print(f"__NEXT_DATA__ script: {len(script.string)} chars")

try:
    data = json.loads(script.string)
    print("JSON parse: OK")
except Exception as e:
    print(f"JSON parse FAILED: {e}")
    sys.exit(1)

# Call the actual function from lead_agent
try:
    from lead_agent import _sortlist_urls_from_json
    found = _sortlist_urls_from_json(data, set())
    print(f"\n_sortlist_urls_from_json returned {len(found)} URLs:")
    for u in found:
        print(f"  {u}")
except Exception as e:
    print(f"\n_sortlist_urls_from_json RAISED an exception:")
    traceback.print_exc()
