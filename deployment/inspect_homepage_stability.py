"""Check dynamic HTML fields without printing response bodies or header values."""

import hashlib
import json
import re
import urllib.request
from difflib import SequenceMatcher


opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read(port):
    request = urllib.request.Request(f'http://127.0.0.1:{port}/', headers={'Host': '34.20.217.151'})
    with opener.open(request, timeout=15) as response:
        return response.read().decode('utf-8'), response.status


for port in (80, 8080):
    first, status1 = read(port)
    second, status2 = read(port)
    differences = []
    for operation, start1, end1, start2, end2 in SequenceMatcher(None, first, second, autojunk=False).get_opcodes():
        if operation == 'equal':
            continue
        context = first[max(0, start1 - 100):end1 + 100].lower()
        differences.append({'operation': operation, 'old_characters': end1 - start1,
                            'new_characters': end2 - start2,
                            'near_nonce': 'nonce' in context, 'near_csrf': 'csrf' in context})
    normalize = lambda value: re.sub(r'''(?i)nonce\s*=\s*(["'])[^"']*\1''', 'nonce="DYNAMIC"', value)
    print(json.dumps({'port': port, 'statuses': [status1, status2], 'same_html': first == second,
                      'same_after_nonce_normalization': normalize(first) == normalize(second),
                      'nonce_attributes': len(re.findall(r'(?i)nonce\s*=', first)),
                      'differences': differences[:20], 'bytes': [len(first), len(second)],
                      'normalized_sha256': hashlib.sha256(normalize(first).encode()).hexdigest()}))
