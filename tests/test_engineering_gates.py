import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from tools import check_core_drift as drift, check_dependencies as deps, health_readonly as health, release_gate, source_guard


class GateTests(unittest.TestCase):
    def test_dynamic_new_missing_and_changed_fork(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory) / 'desktop'; phone = Path(directory) / 'phone'
            for base in (root, phone):
                base.mkdir()
                for rel in drift.TOP_LEVEL:
                    (base / rel).write_bytes(b'baseline\n')
                (base / 'tq').mkdir()
            self.assertEqual(drift.check(root, phone, {}, []), [])
            (root / 'tq/new.py').write_bytes(b'x=1\r\n')
            self.assertTrue(drift.check(root, phone, {}, []))
            (phone / 'tq/new.py').write_bytes(b'x=1\n')
            self.assertEqual(drift.check(root, phone, {}, []), [])
            (phone / 'tq/new.py').write_bytes(b'x=2\n')
            registry = {'tq/new.py':dict(reason='platform fork', desktop_sha256=drift.sha256_of(root/'tq/new.py'),android_sha256=drift.sha256_of(phone/'tq/new.py'))}
            self.assertEqual(drift.check(root, phone, registry, []), [])
            (phone / 'tq/new.py').write_bytes(b'x=3\n')
            self.assertTrue(drift.check(root, phone, registry, []))
            (phone / 'tq/new.py').unlink();(root / 'tq/new.py').unlink()
            self.assertTrue(drift.check(root, phone, {}, ['tq/new.py']))

    def test_portable_guard_checks_working_index_and_committed_baseline(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root=Path(directory)
            def git(*args):
                return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL)
            git('init','-q');(root/'.gitattributes').write_text('*.py text eol=lf\n',encoding='utf-8')
            file=root/'frozen.py';file.write_bytes(b'x=1\r\n')
            git('add','.');git('-c','user.name=Offline','-c','user.email=offline@example.invalid','-c','commit.gpgsign=false','commit','-qm','baseline')
            commit=git('rev-parse','HEAD').decode().strip();blob=git('rev-parse','HEAD:frozen.py').decode().strip()
            index={'git_baseline_commit':commit,'files':[dict(path='frozen.py',frozen=True,git_blob=blob,git_sha256=source_guard.digest(git('show','HEAD:frozen.py')))]}
            with mock.patch.object(source_guard,'ROOT',root):
                self.assertEqual(source_guard.check_git(index),[])
                file.write_bytes(b'x=2\n');self.assertTrue(source_guard.check_git(index))
                git('add','frozen.py');self.assertTrue(source_guard.check_git(index))
                git('-c','user.name=Offline','-c','user.email=offline@example.invalid','-c','commit.gpgsign=false','commit','-qm','changed')
                self.assertTrue(source_guard.check_git(index))

    def test_dependency_pins_reject_ranges_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'lock.txt'
            for text in ('requests>=2.0\n','requests==1\nrequests==2\n'):
                path.write_text(text)
                with self.assertRaises(ValueError):deps.read_lock(path)
            path.write_text('requests==2.32.3\n');self.assertEqual(deps.read_lock(path),{'requests':'2.32.3'})

    def test_readonly_health_checks_pid_heartbeat_without_exposing_other_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'status.json'
            path.write_text(json.dumps(dict(pid=123,wall_ms=100000,state='collecting',password='fake-secret')))
            before=(path.read_bytes(),path.stat().st_mtime_ns)
            result=health.inspect_status(path,now=110,probe=lambda pid:True)
            self.assertEqual(result['status'],'recent');self.assertTrue(result['pid_alive'])
            self.assertFalse(result['identity_verified']);self.assertNotIn('fake-secret',json.dumps(result))
            self.assertEqual((path.read_bytes(),path.stat().st_mtime_ns),before)
            self.assertEqual(health.inspect_status(path,now=300,probe=lambda pid:False)['status'],'stale')
            path.write_text('{}');self.assertEqual(health.inspect_status(path)['status'],'unavailable')

    def test_release_signing_missing_and_incomplete_fail_debug_has_no_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'keystore.properties'
            with self.assertRaises(ValueError):release_gate.signing_config(path)
            path.write_text('storeFile=missing.jks\n')
            with self.assertRaises(ValueError):release_gate.signing_config(path)
            (Path(directory)/'app').mkdir();(Path(directory)/'app/key.jks').write_bytes(b'fake')
            path.write_text('storeFile=key.jks\nstorePassword=fake\nkeyAlias=fake\nkeyPassword=fake\ncertificateSha256='+('a'*64)+'\n')
            self.assertEqual(release_gate.signing_config(path),'a'*64)
        gradle=(release_gate.ROOT/'mobile-app/app/build.gradle').read_text(encoding='utf-8')
        self.assertIn('throw new GradleException',gradle)
        self.assertNotIn('signingConfigs.debug',gradle)
        self.assertIn('requirements-locks/android.txt',gradle)

    def test_release_artifact_hash_certificate_identity_version_and_abi(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.apk'
            with zipfile.ZipFile(path,'w') as z:
                z.writestr('lib/arm64-v8a/fake.so',b'fake');z.writestr('lib/x86_64/fake.so',b'fake')
            sha=hashlib.sha256(path.read_bytes()).hexdigest()
            signature='Signer #1 certificate SHA-256 digest: '+('a'*64)
            version,code=release_gate.release_identity()
            badging="package: name='com.zaquant.mobile' versionCode='%s' versionName='%s'" % (code,version)
            def run(command,**kwargs):return signature if command[0]=='signer' else badging
            self.assertEqual(release_gate.validate_artifact(path,sha,'a'*64,'signer','aapt',run),sha)
            def localized_run(command,**kwargs):
                if command[0]=='signer':return signature
                self.assertEqual(command,['aapt','dump','badging',path.name])
                self.assertEqual(kwargs['cwd'],str(path.resolve().parent))
                payload=badging.encode('ascii')+b"\napplication-label:'\xc6\xda\xbb\xf5'\n"
                return subprocess.check_output([sys.executable,'-c',
                    'import sys; sys.stdout.buffer.write('+repr(payload)+')'],**kwargs)
            self.assertEqual(release_gate.validate_artifact(path,sha,'a'*64,'signer','aapt',localized_run),sha)
            sdk=Path(directory)/'sdk/aapt.exe';sdk.parent.mkdir();sdk.write_bytes(b'fake')
            def relative_sdk_run(command,**kwargs):
                if command[0]=='signer':return signature
                self.assertEqual(command[0],str(sdk.resolve()))
                return badging
            self.assertEqual(release_gate.validate_artifact(path,sha,'a'*64,'signer',
                Path(os.path.relpath(sdk)),relative_sdk_run),sha)
            for wrong_hash,wrong_cert in [('0'*64,'a'*64),(sha,'b'*64)]:
                with self.assertRaises(ValueError):release_gate.validate_artifact(path,wrong_hash,wrong_cert,'signer','aapt',run)
            badging="package: name='wrong' versionCode='%s' versionName='%s'" % (code,version)
            with self.assertRaises(ValueError):release_gate.validate_artifact(path,sha,'a'*64,'signer','aapt',run)
            badging="package: name='com.zaquant.mobile' versionCode='%s' versionName='%s'" % (int(code)+1,version)
            with self.assertRaises(ValueError):release_gate.validate_artifact(path,sha,'a'*64,'signer','aapt',run)
            badging="package: name='com.zaquant.mobile' versionCode='%s' versionName='%s'" % (code,version)
            signature+=' Android Debug'
            with self.assertRaises(ValueError):release_gate.validate_artifact(path,sha,'a'*64,'signer','aapt',run)


if __name__=='__main__':
    unittest.main()
