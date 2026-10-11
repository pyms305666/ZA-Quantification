"""正式发布前检查；不构建、不签名、不读取现场配置或启动服务。"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def signing_config(path):
    if not path.is_file():
        raise ValueError('Release signing configuration missing; debug remains available')
    values = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line and not line.startswith(('#','!')):
            key, separator, value = line.partition('=')
            if separator:
                values[key.strip()] = value.strip()
    required = ('storeFile','storePassword','keyAlias','keyPassword','certificateSha256')
    if any(not values.get(key) for key in required):
        raise ValueError('Release signing configuration incomplete')
    store = Path(values['storeFile'])
    if not store.is_absolute():
        store = path.parent / 'app' / store  # 与 app/build.gradle 的 file() 相同。
    if not store.is_file():
        raise ValueError('Release keystore missing')
    fingerprint = values['certificateSha256'].replace(':','').lower()
    if not re.fullmatch('[0-9a-f]{64}', fingerprint):
        raise ValueError('Production certificate SHA-256 required')
    return fingerprint


def release_identity():
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    gradle = (ROOT / 'mobile-app/app/build.gradle').read_text(encoding='utf-8')
    code = re.search(r'\bversionCode\s+([1-9][0-9]*)\b', gradle)
    name = re.search(r'\bversionName\s+"([^"]+)"', gradle)
    phone = (ROOT / 'mobile-app/app/src/main/python/VERSION').read_text(encoding='utf-8').strip()
    if not code or not name or name[1] != version or phone != version:
        raise ValueError('Source release versions differ')
    return version, code[1]


def validate_artifact(path, expected_hash, fingerprint, apksigner, aapt, run=subprocess.check_output):
    if not re.fullmatch('[0-9a-fA-F]{64}', expected_hash or ''):
        raise ValueError('Expected artifact SHA-256 required')
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_hash.lower():
        raise ValueError('Artifact SHA-256 mismatch')
    signature = run([str(apksigner),'verify','--verbose','--print-certs',str(path)], text=True, encoding='utf-8', errors='replace', stderr=subprocess.STDOUT)
    certificates = re.findall(r'certificate SHA-256 digest:\s*([0-9a-fA-F:]+)', signature)
    if not certificates or any(value.replace(':','').lower() != fingerprint for value in certificates):
        raise ValueError('Artifact production signature mismatch')
    if 'android debug' in signature.lower():
        raise ValueError('Debug signature forbidden for formal release')
    # Windows aapt 不接受含中文的绝对路径，使用工作目录和文件名；
    # 本地化标签可能使用系统编码，门禁只解析 ASCII 身份字段。
    aapt_path = Path(aapt)
    aapt_command = str(aapt_path.resolve()) if aapt_path.is_file() else str(aapt)
    badging = run([aapt_command,'dump','badging',path.name], cwd=str(path.resolve().parent),
                  text=True, encoding='utf-8', errors='replace', stderr=subprocess.STDOUT)
    version, code = release_identity()
    if not re.search(r"package: name='com\.zaquant\.mobile' versionCode='" + code + "' versionName='" + re.escape(version) + "'", badging):
        raise ValueError('Artifact package/version mismatch')
    with zipfile.ZipFile(str(path)) as archive:
        abis = {name.split('/')[1] for name in archive.namelist() if name.startswith('lib/') and name.endswith('.so')}
    if abis != {'arm64-v8a', 'x86_64'}:
        raise ValueError('Artifact ABI mismatch')
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--properties', type=Path, default=ROOT / 'mobile-app/keystore.properties')
    parser.add_argument('--artifact', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--apksigner', type=Path)
    parser.add_argument('--aapt', type=Path)
    args = parser.parse_args()
    try:
        fingerprint = signing_config(args.properties)
        if args.artifact:
            if not args.apksigner or not args.aapt:
                raise ValueError('Artifact verification needs apksigner and aapt')
            actual = validate_artifact(args.artifact, args.sha256, fingerprint, args.apksigner, args.aapt)
            print('PASS: verified release artifact SHA-256 ' + actual)
        else:
            print('PASS: signing configuration present; artifact not built or verified')
        return 0
    except ValueError as error:
        print('FAIL: ' + str(error) + '; no release permitted')
        return 1
    except (OSError, subprocess.CalledProcessError, zipfile.BadZipFile):
        # SDK/密码错误的原始输出不回显，避免密码或凭据进入诊断。
        print('FAIL: release signing, identity or artifact verification failed; no release permitted')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
