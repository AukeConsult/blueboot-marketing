import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import _pathsetup
from functions.config import cfg
import requests

r = requests.get(
    'https://api.search.brave.com/res/v1/web/search',
    params={'q': 'web agency stockholm', 'count': 1},
    headers={'Accept': 'application/json', 'X-Subscription-Token': cfg.BRAVE_API_KEY},
    timeout=10
)
print(f"  Status:  {r.status_code}")
if r.status_code == 200:
    results = r.json().get('web', {}).get('results', [])
    print(f"  Results: {len(results)}")
else:
    print(f"  Error:   {r.text[:100]}")
    sys.exit(1)
