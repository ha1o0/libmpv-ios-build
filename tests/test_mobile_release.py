import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mobile_release as release

TAG = 'v2.25.0-ios.1'
COMMIT = 'a' * 40


def create_frameworks(root):
    for name in release.FRAMEWORKS:
        directory = root / f'{name}.xcframework'
        libraries = []
        for identifier, variant, archs in [
            ('ios-arm64', None, ['arm64']),
            ('ios-arm64_x86_64-simulator', 'simulator', ['arm64', 'x86_64']),
        ]:
            libraries.append(dict(LibraryIdentifier=identifier, LibraryPath=f'{name}.framework',
                                  SupportedArchitectures=archs, SupportedPlatform='ios',
                                  **({'SupportedPlatformVariant': variant} if variant else {})))
            framework = directory / identifier / f'{name}.framework'
            framework.mkdir(parents=True)
            (framework / name).write_bytes(b'fixture Mach-O payload, not a real binary!' * 2)
            if name == 'Mpv':
                for required in ('Headers/client.h', 'Headers/render.h', 'Modules/module.modulemap'):
                    path = framework / required
                    path.parent.mkdir(exist_ok=True)
                    path.write_text('fixture API')
        (directory / 'Info.plist').write_bytes(plistlib.dumps({'AvailableLibraries': libraries}))


def mock_native(args, **kwargs):
    tool = args[1]
    binary = Path(args[-1])
    simulator = '-simulator' in str(binary)
    if tool == 'lipo':
        return 'x86_64 arm64' if simulator else 'arm64'
    if tool == 'otool':
        return f'''Load command 0
          cmd LC_ID_DYLIB
         name @rpath/{binary.name}.framework/{binary.name} (offset 24)
Load command 1
          cmd LC_BUILD_VERSION
     platform {'7' if simulator else '2'}
Load command 2
          cmd LC_LOAD_DYLIB
         name /usr/lib/libSystem.B.dylib (offset 24)
'''
    if tool == 'nm':
        return '\n'.join(f'000000001 (__TEXT,__text) external {symbol}' for symbol in (
            '_mpv_client_api_version', '_mpv_create', '_mpv_initialize', '_mpv_terminate_destroy'))
    raise AssertionError(args)


def create_payload(directory):
    directory.mkdir()
    assets = []
    for name in sorted(release.asset_names(TAG)):
        path = directory / name
        path.write_bytes(name.encode())
        item = dict(name=name, size=path.stat().st_size, sha256=release.digest(path))
        if name.endswith('.zip'):
            item.update(kind='ios-framework-zip', framework=name[len(release.prefix(TAG)) + 1:-4])
        else:
            item['kind'] = 'ios-archive'
        assets.append(item)
    manifest = dict(schema_version=1, platform='ios', repository=release.REPOSITORY,
                    tag=TAG, source_commit=COMMIT, assets=assets, workflow_run_id=12345, request_id='request-123')
    (directory / release.MANIFEST).write_text(json.dumps(manifest))
    return manifest


class FakeGitHub:
    def __init__(self, *, occupied=False, upload_failure=False, tag_race=False):
        self.calls = []
        self.occupied = occupied
        self.upload_failure = upload_failure
        self.tag_race = tag_race
        self.assets = []
        self.created_sha = None

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload))
        if method == 'GET':
            if '/assets?' in path:
                return self.assets
            if path.startswith('git/ref/') and self.created_sha:
                return {'object': {'sha': self.created_sha, 'type': 'commit'}}
            return {'id': 1} if self.occupied else None
        if path == 'git/refs':
            if self.tag_race:
                raise RuntimeError('HTTP 422 existing ref')
            self.created_sha = payload['sha']
            return {}
        if path == 'releases':
            return {'id': 17, 'upload_url': 'https://uploads.github.com/repos/ha1o0/libmpv-ios-build/releases/17/assets{?name,label}'}
        if path.startswith('https://uploads.github.com/'):
            asset = {'name': parse_qs(urlparse(path).query)['name'][0], 'state': 'failed' if self.upload_failure else 'uploaded', 'size': len(payload), 'digest': 'sha256:' + hashlib.sha256(payload).hexdigest()}
            self.assets.append(asset)
            return asset
        if method == 'PATCH':
            return {'draft': False, 'prerelease': True, 'tag_name': TAG}
        return {}


class ReleaseValidationTests(unittest.TestCase):
    def test_safe_dispatch_inputs(self):
        release.validate_identity(TAG, COMMIT, 'request-123')
        for tag in ('v1', '../v1.0.0', 'v1.0.0;echo x', 'v1.0.0\n', 'main', '--help'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.validate_identity(tag, COMMIT)
        with self.assertRaises(ValueError):
            release.validate_identity(TAG, 'main')
        with self.assertRaises(ValueError):
            release.validate_identity(TAG, COMMIT, '$(echo x)')

    def test_exact_expected_slice_set(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_frameworks(root)
            release.validate_native(root, command=mock_native)
            plist = root / 'Mpv.xcframework/Info.plist'
            data = plistlib.loads(plist.read_bytes())
            data['AvailableLibraries'][1]['SupportedArchitectures'] = ['x86_64']
            plist.write_bytes(plistlib.dumps(data))
            with self.assertRaisesRegex(ValueError, 'architecture'):
                release.validate_native(root, command=mock_native)

    def test_native_architecture_cannot_disagree_with_plist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_frameworks(root)
            def bad(args, **kwargs):
                return 'arm64' if args[1] == 'lipo' else mock_native(args)
            with self.assertRaisesRegex(ValueError, 'Mach-O architectures'):
                release.validate_native(root, command=bad)

    def test_rejects_flat_namespace_symbol(self):
        with self.assertRaisesRegex(ValueError, 'flat-namespace'):
            release.symbols('                 (undefined) external _pl_color_map_default_params (dynamically looked up)')

    def test_rejects_unresolved_libplacebo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_frameworks(root)
            def bad(args, **kwargs):
                text = mock_native(args)
                if args[1] == 'nm' and Path(args[-1]).name == 'Mpv':
                    text += '\n (undefined) external _pl_color_map_default_params (from Placebo)'
                return text
            with self.assertRaisesRegex(ValueError, 'unresolved libplacebo'):
                release.validate_native(root, command=bad)

    def test_rejects_unbundled_build_machine_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_frameworks(root)
            def bad(args, **kwargs):
                return mock_native(args).replace('/usr/lib/libSystem.B.dylib', '/opt/homebrew/lib/libplacebo.dylib')
            with self.assertRaisesRegex(ValueError, 'non-system dependency'):
                release.validate_native(root, command=bad)

    def test_rejects_macos_binary_labelled_as_ios(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_frameworks(root)
            def bad(args, **kwargs):
                return mock_native(args).replace('platform 2', 'platform 1')
            with self.assertRaisesRegex(ValueError, 'Mach-O platform'):
                release.validate_native(root, command=bad)

    def test_zip_must_match_archive_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_frameworks(root)
            archive = root / 'Mpv.zip'
            with zipfile.ZipFile(archive, 'w') as handle:
                for path in (root / 'Mpv.xcframework').rglob('*'):
                    if path.is_file():
                        handle.write(path, path.relative_to(root))
            release.validate_zip(archive, 'Mpv', root)
            (root / 'Mpv.xcframework/ios-arm64/Mpv.framework/Mpv').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'differs'):
                release.validate_zip(archive, 'Mpv', root)

    def test_archive_traversal_and_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, symbolic in [('../escape', False), (f'{release.prefix(TAG)}/link', True)]:
                archive = root / 'bad.tar.gz'
                with tarfile.open(archive, 'w:gz') as handle:
                    item = tarfile.TarInfo(name)
                    if symbolic:
                        item.type = tarfile.SYMTYPE
                        item.linkname = '/tmp/escape'
                    handle.addfile(item)
                with self.assertRaises(ValueError):
                    release.unpack_archive(archive, root / 'output', TAG)

    def test_manifest_and_payload_hashes_are_bound_to_tag_and_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'assets'
            manifest = create_payload(root)
            self.assertEqual(release.verify_payload(root, TAG, COMMIT), manifest)
            with self.assertRaisesRegex(ValueError, 'source_commit'):
                release.verify_payload(root, TAG, 'b' * 40)
            (root / manifest['assets'][0]['name']).write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'checksum/size'):
                release.verify_payload(root, TAG, COMMIT)

    def test_publish_creates_exact_ref_then_draft_then_assets_then_prerelease(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'assets'
            create_payload(root)
            api = FakeGitHub()
            release.publish(api, root, TAG, COMMIT)
            mutations = [(m, p, data) for m, p, data in api.calls if m != 'GET']
            self.assertEqual(mutations[0], ('POST', 'git/refs', {'ref': f'refs/tags/{TAG}', 'sha': COMMIT}))
            self.assertTrue(mutations[1][2]['draft'])
            self.assertEqual(mutations[-1], ('PATCH', 'releases/17', {'draft': False, 'prerelease': True, 'make_latest': 'false'}))
            self.assertEqual(sum(p.startswith('https://uploads.github.com/') for _, p, _ in mutations), 20)

    def test_existing_tag_or_release_never_mutates(self):
        api = FakeGitHub(occupied=True)
        with self.assertRaises(ValueError):
            release.ensure_unused(api, TAG)
        self.assertTrue(all(method == 'GET' for method, _, _ in api.calls))

    def test_upload_failure_leaves_draft_and_tag_without_publishing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'assets'
            create_payload(root)
            api = FakeGitHub(upload_failure=True)
            with self.assertRaisesRegex(ValueError, 'draft retained'):
                release.publish(api, root, TAG, COMMIT)
            self.assertFalse(any(method in ('PATCH', 'DELETE') for method, _, _ in api.calls))

    def test_concurrent_tag_creation_does_not_create_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'assets'
            create_payload(root)
            api = FakeGitHub(tag_race=True)
            with self.assertRaises(RuntimeError):
                release.publish(api, root, TAG, COMMIT)
            self.assertFalse(any(path == 'releases' for _, path, _ in api.calls))

    def test_workflow_identity_is_verified_for_publishing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'assets'
            create_payload(root)
            with patch.dict('os.environ', {'GITHUB_RUN_ID': '12345', 'REQUEST_ID': 'request-123'}):
                release.verify_payload(root, TAG, COMMIT)
            with patch.dict('os.environ', {'GITHUB_RUN_ID': '999', 'REQUEST_ID': 'request-123'}):
                with self.assertRaisesRegex(ValueError, 'provenance'):
                    release.verify_payload(root, TAG, COMMIT)

    def test_remote_checksum_mismatch_prevents_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'assets'
            create_payload(root)
            class Tampered(FakeGitHub):
                def request(self, method, path, payload=None, **kwargs):
                    response = super().request(method, path, payload, **kwargs)
                    if method == 'GET' and '/assets?' in path:
                        response[0]['digest'] = 'sha256:' + '0' * 64
                    return response
            api = Tampered()
            with self.assertRaisesRegex(ValueError, 'digest/size'):
                release.publish(api, root, TAG, COMMIT)
            self.assertFalse(any(method == 'PATCH' for method, _, _ in api.calls))

    def test_xcode_discovery_matches_executable_version_not_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / 'Xcode_custom.app'
            tool = app / 'Contents/Developer/usr/bin/xcodebuild'
            tool.parent.mkdir(parents=True)
            tool.touch()
            sdk = root / 'sdk'
            sdk.mkdir()
            def command(args, **kwargs):
                return 'Xcode 16.1\nBuild version 16B40' if args[-1] == '-version' else str(sdk)
            self.assertEqual(release.resolve_xcode(root, command), app.resolve())
            with self.assertRaisesRegex(ValueError, 'refusing to download'):
                release.resolve_xcode(root, lambda *_args, **_kwargs: 'Xcode 27.2\nBuild version 27B52')


if __name__ == '__main__':
    unittest.main()
