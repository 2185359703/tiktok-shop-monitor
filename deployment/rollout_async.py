"""Deploy one committed release into the existing project without replacing data services."""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import tempfile
import hashlib
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path('/opt/tiktok-shop-monitor')
SERVICES = ('api', 'worker', 'scheduler', 'collection')
INFRASTRUCTURE = ('mysql', 'redis', 'rabbitmq', 'detail-egress', 'prometheus')


def command(arguments, *, env=None, input=None):
    result = subprocess.run(arguments, cwd=ROOT, env=env, input=input, text=True, capture_output=True)
    if result.returncode:
        # Detailed errors stay in the private deployment directory, not stdout.
        path = ROOT / 'data/samples/deployment/async-command-error.log'
        path.write_text(result.stdout + result.stderr, encoding='utf-8')
        path.chmod(0o600)
        raise RuntimeError('Deployment command failed; inspect the private deployment log.')
    return result.stdout.strip()


def containers():
    ids = command(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=tiktok-shop-monitor']).split()
    records = json.loads(command(['docker', 'inspect', *ids]))
    return [{
        'id': r['Id'], 'name': r['Name'], 'service': r['Config']['Labels']['com.docker.compose.service'],
        'image_id': r['Image'], 'image': r['Config']['Image'], 'state': r['State']['Status'],
        'health': r['State'].get('Health', {}).get('Status'), 'oom': r['State'].get('OOMKilled', False),
        'restarts': r['RestartCount'], 'started_at': r['State']['StartedAt'],
        'compose_files': r['Config']['Labels'].get('com.docker.compose.project.config_files'),
        'mounts': [{'type': m['Type'], 'source': m.get('Source'), 'target': m['Destination']} for m in r['Mounts']],
    } for r in records]


def publish_ui(source, revision):
    """Point only the existing ShopScope static locations at this release."""
    snippet = Path('/etc/nginx/snippets/tiktok-shop-monitor-public.conf')
    if snippet.is_symlink() or not snippet.is_file():
        raise ValueError('The expected ShopScope Nginx snippet is missing.')
    original = snippet.read_text(encoding='utf-8')
    directory = source / 'app/frontend'
    if not (directory / 'index.html').is_file() or './ui-assets/' not in (directory / 'index.html').read_text(encoding='utf-8'):
        raise ValueError('The committed UI is not prefix-aware.')
    pattern = r'(?m)^(\s*(?:root|alias)\s+)/opt/tiktok-shop-monitor/releases/[^;\n]+/app/frontend(/?);'
    updated, count = re.subn(pattern, lambda m: m[1] + str(directory) + m[2] + ';', original)
    if count != 2 or 'location = /shopscope/' not in original or 'location ^~ /shopscope/ui-assets/' not in original:
        raise ValueError('The public static locations differ from the known project layout.')
    evidence = ROOT / 'data/samples/deployment'
    (evidence / f'nginx-before-{revision[:12]}.conf').write_text(original, encoding='utf-8')
    def replace(value):
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=snippet.parent, prefix='.shopscope-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(value)
        temporary.chmod(0o644)
        os.replace(temporary, snippet)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        replace(updated)
        command(['nginx', '-t'])
        command(['systemctl', 'reload', 'nginx'])
        with opener.open('http://127.0.0.1:8000/openapi.json', timeout=20) as response:
            schema = json.load(response)
        schema['servers'] = [{'url': '/shopscope'}]
        (ROOT / 'deployment/public-openapi.json').write_text(json.dumps(schema, ensure_ascii=False), encoding='utf-8')
        checks = {}
        for relative, url in (('index.html', '/shopscope/'), ('app.js', '/shopscope/ui-assets/app.js'),
                              ('api.js', '/shopscope/ui-assets/api.js'), ('product-detail.js', '/shopscope/ui-assets/product-detail.js'),
                              ('app.bundle.js', '/shopscope/ui-assets/app.bundle.js'), ('styles.bundle.css', '/shopscope/ui-assets/styles.bundle.css')):
            with opener.open('http://127.0.0.1' + url, timeout=20) as response:
                checks[relative] = hashlib.sha256(response.read()).hexdigest() == hashlib.sha256((directory / relative).read_bytes()).hexdigest()
        if not all(checks.values()):
            raise RuntimeError('Public UI bytes do not match the committed release.')
    except BaseException:
        replace(original)
        command(['nginx', '-t'])
        command(['systemctl', 'reload', 'nginx'])
        raise
    print(json.dumps({'phase': 'public_ui_ready', 'commit': revision, 'checks': checks}), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--publish-ui-only', action='store_true')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[a-f0-9]{40}', args.commit):
        raise ValueError('A full Git commit is required.')
    source = ROOT / 'releases' / args.commit
    if not source.is_dir() or not (source / 'deployment/compose.async.yml').is_file():
        raise ValueError('Committed release is missing.')
    image = 'tiktok-shop-monitor:async-' + args.commit[:12]
    info = json.loads(command(['docker', 'image', 'inspect', image]))[0]
    if info['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != args.commit:
        raise ValueError('Image revision does not match the requested commit.')
    if args.publish_ui_only:
        current = [r for r in containers() if r['service'] in SERVICES]
        if set(r['service'] for r in current) != set(SERVICES) or any(r['image_id'] != info['Id'] for r in current):
            raise ValueError('Publish UI only after all application services run this image.')
        publish_ui(source, args.commit)
        return 0
    env = {**os.environ, 'APP_IMAGE': image}
    compose = ['docker', 'compose', '-f', str(ROOT / 'deployment/compose.server.yml'),
               '-f', str(source / 'deployment/compose.async.yml'), '--env-file', str(ROOT / 'deployment.env')]
    before = containers()
    workers = [r for r in before if r['service'] == 'worker' and r['state'] == 'running']
    if len(workers) != 2:
        raise ValueError('Expected exactly two existing running workers.')
    infrastructure = {r['service']: r['id'] for r in before if r['service'] in INFRASTRUCTURE}
    if set(infrastructure) != set(INFRASTRUCTURE):
        raise ValueError('An existing infrastructure service is missing.')
    print(json.dumps({'phase': 'preflight', 'commit': args.commit, 'image': image, 'worker_count': len(workers)}), flush=True)
    if not args.execute:
        return 0
    evidence = ROOT / 'data/samples/deployment'
    evidence.mkdir(parents=True, exist_ok=True)
    with (evidence / 'async-rollout.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (evidence / f'async-before-{args.commit[:12]}.json').write_text(json.dumps(before, indent=2), encoding='utf-8')
        for module in ('scripts.migrate_shop_profiles', 'scripts.migrate_product_reviews', 'scripts.migrate_analytics'):
            output = command([*compose, 'run', '--rm', '--no-deps', '-T', 'api', 'python', '-m', module, '--execute'], env=env)
            print(json.dumps({'phase': 'migrated', 'module': module, 'summary': output[-800:]}), flush=True)
        print(json.dumps({'phase': 'draining_existing_processes'}), flush=True)
        command([*compose, 'stop', '-t', '300', 'scheduler', 'collection', 'worker'], env=env)
        print(json.dumps({'phase': 'starting_committed_release'}), flush=True)
        command([*compose, 'up', '-d', '--no-deps', '--no-build', '--scale', 'worker=2', *SERVICES], env=env)
        deadline = time.monotonic() + 120
        while True:
            try:
                with urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError('New API did not become ready; saved previous deployment state is available.')
            time.sleep(1)
        after = containers()
        apps = [r for r in after if r['service'] in SERVICES]
        if (set(r['service'] for r in apps) != set(SERVICES)
                or len([r for r in apps if r['service'] == 'worker']) != 2
                or any(r['image_id'] != info['Id'] or r['state'] != 'running' or r['oom'] for r in apps)):
            raise RuntimeError('Application image, state or worker count differs from the release.')
        if {r['service']: r['id'] for r in after if r['service'] in INFRASTRUCTURE} != infrastructure:
            raise RuntimeError('An infrastructure container changed unexpectedly.')
        api = next(r for r in apps if r['service'] == 'api')
        if any(m['target'].startswith('/opt/tiktok-shop-monitor/app/') for m in api['mounts']):
            raise RuntimeError('An old code bind mount is masking the image.')
        publish_ui(source, args.commit)
        shutil.copy2(source / 'deployment/compose.async.yml', ROOT / 'deployment/compose.async.yml')
        active = {'commit': args.commit, 'image': image, 'image_id': info['Id'], 'deployed_at': datetime.now(timezone.utc).isoformat(),
                  'compose_files': [str(ROOT / 'deployment/compose.server.yml'), str(ROOT / 'deployment/compose.async.yml')],
                  'worker_count': 2, 'infrastructure_recreated': False}
        (ROOT / 'deployment/active-release.json').write_text(json.dumps(active, indent=2) + '\n', encoding='utf-8')
        (evidence / f'async-after-{args.commit[:12]}.json').write_text(json.dumps(after, indent=2), encoding='utf-8')
        print(json.dumps({'phase': 'complete', **active}), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'phase': 'failed', 'error_type': type(error).__name__, 'message': str(error)}), flush=True)
        raise SystemExit(1)
