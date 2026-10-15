"""Add a checked /shopscope HTTP entry to the existing Nginx virtual host."""

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE = Path('/etc/nginx/sites-available/sub2api')
SNIPPET = Path('/etc/nginx/snippets/tiktok-shop-monitor-public.conf')
INCLUDE = '    include /etc/nginx/snippets/tiktok-shop-monitor-public.conf;'
MARKER = '    server_name 34.20.217.151;'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read(path, *, backend=False):
    origin = 'http://127.0.0.1:8000' if backend else 'http://127.0.0.1'
    request = urllib.request.Request(origin + path, headers={'Host': '34.20.217.151'})
    with opener.open(request, timeout=30) as response:
        return response.read()


def replace(path, content, mode=0o644):
    descriptor, temporary = tempfile.mkstemp(prefix='.shopscope-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Nginx配置检查或重载未完成；未输出原始配置。')


def homepage_digest(document):
    # The existing Sub2API generates a fresh CSP nonce for every response.
    # Ignore only that attribute; keep all scripts, URLs and page text in the hash.
    normalized, count = re.subn(rb'''(?i)nonce\s*=\s*(["'])[^"']*\1''', b'nonce="DYNAMIC"', document)
    if count != 1:
        raise ValueError('原站点的动态字段与已核实响应不同。')
    return hashlib.sha256(normalized).hexdigest()


def main():
    if SITE.is_symlink() or SITE.resolve().parent != Path('/etc/nginx/sites-available'):
        raise ValueError('现有站点配置路径不符合预期。')
    original, original_mode = SITE.read_bytes(), stat.S_IMODE(SITE.stat().st_mode)
    source = original.decode('utf-8')
    if source.count(MARKER) != 1 or 'proxy_pass http://127.0.0.1:8080;' not in source:
        raise ValueError('现有HTTP入口与已核实的Sub2API配置不同。')
    old_snippet = SNIPPET.read_bytes() if SNIPPET.exists() else None
    if old_snippet is not None and not old_snippet.startswith(b'# Managed for tiktok-shop-monitor public entry.'):
        raise ValueError('项目代理片段位置已有其他配置。')
    baseline = homepage_digest(read('/'))
    if homepage_digest(read('/')) != baseline:
        raise ValueError('原站点除CSP nonce外的首页内容仍在变化。')
    schema = json.loads(read('/openapi.json', backend=True))
    if '/api/v1/products' not in schema.get('paths', {}):
        raise ValueError('后端接口结构不属于本项目。')
    schema['servers'] = [{'url': '/shopscope'}]
    replace(ROOT / 'deployment/public-openapi.json', (json.dumps(schema, ensure_ascii=False) + '\n').encode())
    snippet = (ROOT / 'deployment/nginx-shopscope.conf').read_bytes()
    candidate = source if INCLUDE in source else source.replace(MARKER, MARKER + '\n' + INCLUDE, 1)
    directory = ROOT / 'data/samples/deployment/public-entry'
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = directory / f'sub2api-{stamp}.conf'
    backup.write_bytes(original)
    os.chmod(backup, 0o600)
    if SITE.read_bytes() != original:
        raise ValueError('现有配置在部署期间发生变化。')
    try:
        replace(SNIPPET, snippet)
        replace(SITE, candidate.encode('utf-8'), original_mode)
        command('nginx', '-t')
        command('nginx', '-s', 'reload')
        for attempt in range(20):
            try:
                document = read('/shopscope/')
                if b'ShopScope' in document:
                    break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError('公网路径的本机代理检查未通过。')
        if homepage_digest(read('/')) != baseline:
            raise RuntimeError('原站点首页内容与部署前不一致。')
        live = json.loads(read('/shopscope/health/live'))
        capabilities = json.loads(read('/shopscope/api/v1/capabilities'))
        public_schema = json.loads(read('/shopscope/openapi.json'))
        docs = read('/shopscope/docs')
        if live.get('status') != 'ok' or capabilities.get('platform') != 'tiktok_shop':
            raise RuntimeError('项目健康或接口路由检查未通过。')
        if public_schema.get('servers') != [{'url': '/shopscope'}] or b'/shopscope/openapi.json' not in docs:
            raise RuntimeError('公开接口文档的路径不正确。')
    except BaseException:
        replace(SITE, original, original_mode)
        if old_snippet is None:
            SNIPPET.unlink(missing_ok=True)
        else:
            replace(SNIPPET, old_snippet)
        command('nginx', '-t')
        command('nginx', '-s', 'reload')
        raise
    record = {'public_url': 'http://34.20.217.151/shopscope/#products',
              'public_port': 80, 'additional_public_ports': [], 'nginx_reloaded': True,
              'existing_homepage_content_unchanged': True, 'existing_homepage_sha256': baseline,
              'homepage_hash_excludes': 'per-response CSP nonce attribute only',
              'site_backup': str(backup), 'project_api_proxy': '127.0.0.1:8000',
              'verified_at': datetime.now(timezone.utc).isoformat()}
    (directory / 'public-entry.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
