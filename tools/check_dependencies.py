"""检查精确锁和指定环境元数据；不会安装、升级或导入模型。"""
import argparse
from importlib import metadata
import json
from pathlib import Path
import platform
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LOCKS = ROOT / 'requirements-locks'
ENVIRONMENTS = ('desktop', 'test', 'android', 'research', 'kronos', 'desktop-build')


def read_lock(path):
    versions = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith(('#', '--extra-index-url ')):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^\s;]+)', line)
        if not match:
            raise ValueError('Unpinned dependency in ' + path.name)
        key = match[1].lower().replace('_', '-')
        if key in versions:
            raise ValueError('Duplicate dependency: ' + key)
        versions[key] = match[2]
    if not versions:
        raise ValueError('Empty dependency lock')
    return versions


def check_locks():
    errors = []
    for name in ENVIRONMENTS:
        path = LOCKS / (name + '.txt')
        versions = read_lock(path)
        source = json.loads(path.with_suffix('.provenance.json').read_text(encoding='utf-8'))
        if versions != source['packages']:
            errors.append('Lock/provenance mismatch: ' + path.name)
    package = json.loads((ROOT / 'electron/package.json').read_text(encoding='utf-8'))
    lock = json.loads((ROOT / 'electron/package-lock.json').read_text(encoding='utf-8'))
    for name, version in package['devDependencies'].items():
        if version != lock['packages']['node_modules/' + name]['version'] or version != lock['packages']['']['devDependencies'][name]:
            errors.append('Node dependency is not locked: ' + name)
    return errors


def check_environment(name, distribution_path=None):
    expected = read_lock(LOCKS / (name + '.txt'))
    provenance = json.loads((LOCKS / (name + '.provenance.json')).read_text(encoding='utf-8'))
    distributions = metadata.distributions(path=[str(distribution_path)]) if distribution_path else metadata.distributions()
    actual = {d.metadata['Name'].lower().replace('_', '-'):d.version for d in distributions}
    errors = ['Dependency mismatch: ' + key for key, version in expected.items() if actual.get(key) != version]
    if not distribution_path and platform.python_version() != provenance['python']:
        errors.append('Python runtime differs from recorded environment')
    if name in ('desktop', 'test'):
        script = "console.log(JSON.stringify({node:process.versions.node,packages:Object.fromEntries(['electron','playwright-core'].map(n=>[n,require(n+'/package.json').version]))}))"
        node = json.loads(subprocess.check_output(['node','-e',script],cwd=str(ROOT/'electron'),text=True,encoding='utf-8'))
        runtime = json.loads((LOCKS/'runtime.json').read_text(encoding='utf-8'))
        package = json.loads((ROOT/'electron/package.json').read_text(encoding='utf-8'))
        if node['node'] != runtime['node'] or node['packages'] != package['devDependencies']:
            errors.append('Node runtime/dependency environment differs from locks')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=['desktop', 'test', 'research', 'kronos', 'desktop-build', 'android'])
    parser.add_argument('--distribution-path', type=Path, help='Read-only cached target metadata, e.g. Android common')
    args = parser.parse_args()
    try:
        errors = check_locks()
        if args.environment:
            errors += check_environment(args.environment, args.distribution_path)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        errors = [str(error)]
    for error in errors:
        print(error)
    if not errors:
        print('PASS: exact dependency locks' + (' / ' + args.environment if args.environment else ''))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
