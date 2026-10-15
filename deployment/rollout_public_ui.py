"""Install a frozen, prefix-aware UI and replace only the project's API."""

import hashlib
import json
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPLOAD = Path('/home/gcpuser')
IDENTIFIER = 'frontend-ui-public-20261015-f7c3102a'
ARCHIVE_SHA = '7c86f2e92341e228b47d477b5ff136b897fd8ebcb8cd3d8eef17f7798b72a8c4'
IMAGE = 'tiktok-shop-monitor:frontend-20261015-images-verified-c7a93e14'
COMPOSE = ['docker', 'compose', '-f', 'deployment/compose.server.yml',
           '-f', 'deployment/compose.frontend.yml', '--env-file', 'deployment.env']
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(arguments):
    completed = subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError('项目API发布命令未完成；未输出环境变量和连接信息。')
    return completed.stdout.strip()


def workers():
    return set(run([*COMPOSE, 'ps', '-q', 'worker']).split())


def ready():
    for attempt in range(45):
        try:
            with opener.open('http://127.0.0.1:8000/health/ready', timeout=3) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError('API未通过就绪检查。')


def main():
    archive = UPLOAD / f'{IDENTIFIER}.tar.gz'
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA:
        raise ValueError('前端压缩包与冻结摘要不一致。')
    destination = ROOT / 'releases' / IDENTIFIER
    if destination.exists():
        raise ValueError('发布目录已存在；不会覆盖不可变发布。')
    with tarfile.open(archive, 'r:gz') as stream:
        members = stream.getmembers()
        if any(not member.isfile() or member.name.startswith('/') or '..' in Path(member.name).parts
               for member in members):
            raise ValueError('压缩包包含预期以外的成员。')
        destination.mkdir(parents=True)
        stream.extractall(destination, filter='data')
    manifest = json.loads((destination / 'release-manifest.json').read_text())
    if manifest['release_id'] != IDENTIFIER or len(manifest['files']) != 18:
        raise ValueError('前端发布清单不符合冻结版本。')
    for record in manifest['files']:
        path = (destination / record['path']).resolve()
        if not path.is_relative_to(destination) or hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError('前端资源摘要不一致。')
    running = workers()
    if len(running) != 2:
        raise ValueError('发布前运行中的Worker数量不符合预期。')
    config = ROOT / 'deployment/compose.frontend.yml'
    previous = config.read_bytes()
    if b'frontend-ui-20261015-e6b2084d/app/frontend' not in previous:
        raise ValueError('当前前端不是已经核实的发布目录。')
    backup = ROOT / 'data/samples/deployment/compose.frontend.before-public.yml'
    backup.write_bytes(previous)
    candidate = (UPLOAD / 'compose.frontend.yml').read_bytes()
    if IDENTIFIER.encode() not in candidate:
        raise ValueError('候选配置没有引用新前端发布。')
    try:
        config.write_bytes(candidate)
        configured = json.loads(run([*COMPOSE, 'config', '--format', 'json']))['services']
        if any(configured[name]['image'] != IMAGE for name in ('api', 'worker', 'scheduler', 'collection')):
            raise ValueError('候选配置改变了已验证的基础镜像。')
        run([*COMPOSE, 'up', '-d', '--no-deps', '--no-build', 'api'])
        ready()
        with opener.open('http://127.0.0.1:8000/ui-assets/api.js', timeout=10) as response:
            record = next(item for item in manifest['files'] if item['path'] == 'app/frontend/api.js')
            if hashlib.sha256(response.read()).hexdigest() != record['sha256']:
                raise ValueError('运行中的API未提供新版本静态资源。')
        if workers() != running:
            raise ValueError('发布期间采集Worker发生变化。')
    except BaseException:
        config.write_bytes(previous)
        run([*COMPOSE, 'up', '-d', '--no-deps', '--no-build', 'api'])
        ready()
        raise
    for filename in ('nginx-shopscope.conf', 'apply_public_entry.py', 'check_frontend_release.py'):
        shutil.copyfile(UPLOAD / filename, ROOT / 'deployment' / filename)
    print(json.dumps({'release_id': IDENTIFIER, 'static_assets': 18, 'api_ready': True,
                      'worker_container_ids_unchanged': True, 'worker_count': len(running)}))


if __name__ == '__main__':
    main()
