"""Check the public HTTP entry from an external machine, without SSH forwarding."""

import hashlib
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'http://34.20.217.151'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
paths = ('/shopscope', '/shopscope/health/ready', '/shopscope/api/v1/capabilities',
         '/shopscope/openapi.json', '/shopscope/docs',
         '/shopscope/api/v1/analytics/products?page_size=2', '/')


def read(path):
    with opener.open(ORIGIN + path, timeout=30) as response:
        return path, {'status': response.status, 'final_url': response.url,
                      'body': response.read()}


with ThreadPoolExecutor(max_workers=3) as pool:
    responses = dict(pool.map(read, paths))
assert all(record['status'] == 200 for record in responses.values())
assert responses['/shopscope']['final_url'] == ORIGIN + '/shopscope/'
assert b'ShopScope' in responses['/shopscope']['body']
capabilities = json.loads(responses['/shopscope/api/v1/capabilities']['body'])
assert capabilities['platform'] == 'tiktok_shop'
schema = json.loads(responses['/shopscope/openapi.json']['body'])
assert schema['servers'] == [{'url': '/shopscope'}]
assert '/api/v1/products' in schema['paths']
assert b'/shopscope/openapi.json' in responses['/shopscope/docs']['body']
products = json.loads(responses['/shopscope/api/v1/analytics/products?page_size=2']['body'])
assert products['items'] and products['total'] > 0
homepage, count = re.subn(rb'''(?i)nonce\s*=\s*(["'])[^"']*\1''', b'nonce="DYNAMIC"', responses['/']['body'])
server_evidence = json.loads((ROOT / 'data/samples/deployment/public-entry.json').read_text())
assert count == 1 and hashlib.sha256(homepage).hexdigest() == server_evidence['existing_homepage_sha256']
result = {'public_url': ORIGIN + '/shopscope/#products', 'public_port': 80,
          'external_direct_http': True, 'ssh_forwarding_used': False,
          'system_proxy_used': False, 'all_responses_ok': True,
          'checked_paths': list(paths), 'trailing_slash_redirect': True,
          'openapi_server_prefix': '/shopscope', 'product_total': products['total'],
          'sample_products': len(products['items']), 'existing_root_homepage_unchanged': True,
          'verified_at': datetime.now(timezone.utc).isoformat()}
output = ROOT / 'data/samples/deployment/public-api-check.json'
output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
