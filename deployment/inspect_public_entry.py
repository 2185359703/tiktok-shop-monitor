"""Read the existing HTTP virtual host without exposing sensitive directives."""

import re
from pathlib import Path


path = Path('/etc/nginx/sites-available/sub2api')
source = path.read_text(encoding='utf-8')
for line in source.splitlines():
    if re.search(r'authorization|password|secret|api[_-]?key|token', line, re.I):
        print('    # sensitive directive omitted')
    else:
        print(line)
