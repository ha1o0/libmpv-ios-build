#!/usr/bin/env python3
"""Validate and publish immutable iOS-only libmpv release payloads (stdlib only)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile

REPOSITORY = 'ha1o0/libmpv-ios-build'
TARGET = 'mk-out-archive-xcframeworks-ios-universal-video-full'
MANIFEST = 'mobile-libmpv-manifest.json'
FRAMEWORKS = frozenset('Ass Avcodec Avfilter Avformat Avutil Dav1d Freetype Fribidi Harfbuzz Mbedcrypto Mbedtls Mbedx509 Mpv Png16 Swresample Swscale Uchardet Xml2'.split())
TAG = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')


def validate_identity(tag, commit, request_id=''):
    if not TAG.fullmatch(tag) or len(tag) > 100:
        raise ValueError('release_tag must be vMAJOR.MINOR.PATCH with an optional safe prerelease suffix')
    if not SHA.fullmatch(commit):
        raise ValueError('source_commit must be the full dispatched commit SHA')
    if request_id and not re.fullmatch(r'[A-Za-z0-9-]{1,64}', request_id):
        raise ValueError('request_id must be empty or 1–64 letters, digits, and hyphens')


def prefix(tag):
    return f'libmpv-xcframeworks_{tag}_ios-universal-video-full'


def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def run(args, **kwargs):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, **kwargs).strip()


def checked_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or '..' in path.parts or '\\' in name:
        raise ValueError(f'Unsafe archive path: {name!r}')
    return path


def unpack_archive(archive, destination, tag):
    root_name = prefix(tag)
    with tarfile.open(archive, 'r:gz') as handle:
        seen = set()
        for member in handle:
            relative = checked_path(member.name)
            if relative.parts[0] != root_name or member.name in seen:
                raise ValueError('Archive root or duplicate entry mismatch')
            seen.add(member.name)
            if not (member.isdir() or member.isfile()):
                raise ValueError('Release frameworks must contain regular files/directories, not links')
            output = destination.joinpath(*relative.parts)
            if member.isdir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with handle.extractfile(member) as source, output.open('wb') as target:
                    shutil.copyfileobj(source, target)
                output.chmod(member.mode & 0o777)
    return destination / root_name


def validate_zip(zip_path, framework, extracted):
    expected = {
        p.relative_to(extracted).as_posix(): digest(p)
        for p in (extracted / f'{framework}.xcframework').rglob('*') if p.is_file()
    }
    actual = {}
    with zipfile.ZipFile(zip_path) as archive:
        for item in archive.infolist():
            path = checked_path(item.filename)
            if path.parts[0] != f'{framework}.xcframework':
                raise ValueError(f'{zip_path.name}: unexpected framework root')
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('Framework ZIP must not contain symlinks')
            if item.is_dir():
                continue
            if item.filename in actual:
                raise ValueError('Duplicate ZIP entry')
            actual[item.filename] = hashlib.sha256(archive.read(item)).hexdigest()
    if actual != expected or not actual:
        raise ValueError(f'{zip_path.name}: ZIP differs from the verified archive framework')


def framework_slices(root):
    if {p.name for p in root.iterdir()} != {f'{name}.xcframework' for name in FRAMEWORKS}:
        raise ValueError('Archive contains unexpected top-level entries')
    found = {p.stem for p in root.glob('*.xcframework') if p.is_dir()}
    if found != FRAMEWORKS:
        raise ValueError(f'Wrong framework set: missing={sorted(FRAMEWORKS-found)}, extra={sorted(found-FRAMEWORKS)}')
    result = {}
    for framework in sorted(FRAMEWORKS):
        directory = root / f'{framework}.xcframework'
        with (directory / 'Info.plist').open('rb') as stream:
            data = plistlib.load(stream)
        libraries = data.get('AvailableLibraries', [])
        if len(libraries) != 2:
            raise ValueError(f'{framework}: expected one device and one universal simulator library')
        variants = {}
        for item in libraries:
            variant = item.get('SupportedPlatformVariant', 'device')
            expected = {'device': {'arm64'}, 'simulator': {'arm64', 'x86_64'}}
            if (item.get('SupportedPlatform') != 'ios' or variant not in expected
                    or set(item.get('SupportedArchitectures', [])) != expected[variant]
                    or variant in variants):
                raise ValueError(f'{framework}: invalid platform/architecture slice metadata')
            identifier = checked_path(item['LibraryIdentifier'])
            if len(identifier.parts) != 1 or item['LibraryPath'] != f'{framework}.framework':
                raise ValueError(f'{framework}: invalid library path')
            binary = directory / str(identifier) / item['LibraryPath'] / framework
            if not binary.is_file() or binary.stat().st_size < 32:
                raise ValueError(f'{framework}: missing native binary')
            if framework == 'Mpv':
                for required in ('Headers/client.h', 'Headers/render.h', 'Modules/module.modulemap'):
                    if not (binary.parent / required).is_file():
                        raise ValueError(f'Mpv: missing importable API {required}')
            variants[variant] = (binary, expected[variant])
        result[framework] = variants
    return result


def dylib_commands(text):
    """Read actual load commands, excluding LC_ID_DYLIB from dependency edges."""
    identifier = None
    dependencies = []
    for block in re.split(r'Load command \d+\n', text):
        match = re.search(r'\bcmd (LC_\w+)', block)
        if not match:
            continue
        command = match[1]
        if command in ('LC_ID_DYLIB', 'LC_LOAD_DYLIB', 'LC_LOAD_WEAK_DYLIB', 'LC_REEXPORT_DYLIB', 'LC_LOAD_UPWARD_DYLIB'):
            name = re.search(r'\bname (.+?) \(offset \d+\)', block)
            if not name:
                raise ValueError('Malformed dylib load command')
            if command == 'LC_ID_DYLIB':
                identifier = name[1]
            else:
                dependencies.append(name[1])
    return identifier, dependencies


def symbols(text):
    defined, imported = set(), set()
    for line in text.splitlines():
        if 'dynamically looked up' in line:
            raise ValueError(f'Unbound flat-namespace symbol: {line.strip()}')
        symbol = re.search(r'\b(?:external|private external) (\S+)', line)
        if not symbol:
            continue
        if '(undefined)' in line:
            imported.add(symbol[1])
        else:
            defined.add(symbol[1])
    return defined, imported


def validate_native(root, command=run):
    slices = framework_slices(root)
    for variant in ('device', 'simulator'):
        for arch in (('arm64',) if variant == 'device' else ('arm64', 'x86_64')):
            exports, imports, edges = {}, {}, {}
            for framework, variants in slices.items():
                binary, architectures = variants[variant]
                actual = set(command(['xcrun', 'lipo', '-archs', str(binary)]).split())
                if actual != architectures:
                    raise ValueError(f'{framework}/{variant}: Mach-O architectures differ from Info.plist')
                load = command(['xcrun', 'otool', '-arch', arch, '-l', str(binary)])
                identity, dependencies = dylib_commands(load)
                if identity != f'@rpath/{framework}.framework/{framework}':
                    raise ValueError(f'{framework}/{variant}: invalid install name {identity!r}')
                platforms = re.findall(r'\bplatform (\S+)', load)
                expected_platform = {'2', 'IOS'} if variant == 'device' else {'7', 'IOSSIMULATOR'}
                if platforms:
                    if not set(platforms) <= expected_platform:
                        raise ValueError(f'{framework}/{variant}/{arch}: wrong Mach-O platform')
                elif 'LC_VERSION_MIN_IPHONEOS' not in load or (variant == 'simulator' and arch != 'x86_64'):
                    raise ValueError(f'{framework}/{variant}/{arch}: missing platform load command')
                local = set()
                for dependency in dependencies:
                    if dependency.startswith(('/usr/lib/', '/System/Library/Frameworks/')):
                        continue
                    match = re.fullmatch(r'@rpath/([A-Za-z0-9-]+)\.framework/\1', dependency)
                    if not match or match[1] not in FRAMEWORKS:
                        raise ValueError(f'{framework}: unbundled/non-system dependency {dependency!r}')
                    local.add(match[1])
                edges[framework] = local
                exports[framework], imports[framework] = symbols(
                    command(['xcrun', 'nm', '-arch', arch, '-m', str(binary)]))
            if not {'_mpv_client_api_version', '_mpv_create', '_mpv_initialize', '_mpv_terminate_destroy'} <= exports['Mpv']:
                raise ValueError(f'Mpv/{variant}/{arch}: required public API symbols are missing')
            # libplacebo was the source of a previous dlopen failure. Never allow
            # _pl_* references to survive unless a real reachable provider exists.
            for framework in FRAMEWORKS:
                reachable = {framework}
                pending = list(edges[framework])
                while pending:
                    dependency = pending.pop()
                    if dependency in reachable:
                        continue
                    reachable.add(dependency)
                    pending.extend(edges[dependency])
                available = set().union(*(exports[name] for name in reachable))
                missing = {s for s in imports[framework] if s.startswith('_pl_')} - available
                if missing:
                    raise ValueError(f'{framework}/{variant}/{arch}: unresolved libplacebo symbols {sorted(missing)}')
    return slices


def asset_names(tag):
    base = prefix(tag)
    return {f'{base}.tar.gz'} | {f'{base}_{name}.zip' for name in FRAMEWORKS}


def prepare(source, output, tag, commit):
    validate_identity(tag, commit)
    expected = asset_names(tag)
    actual = {p.name for p in source.iterdir() if p.is_file()}
    if actual != expected:
        raise ValueError(f'Unexpected Nix output set: missing={expected-actual}, extra={actual-expected}')
    if output.exists():
        raise ValueError('Output must be a new directory; refusing to replace files')
    with tempfile.TemporaryDirectory(prefix='libmpv-ios-verify-') as temp:
        root = unpack_archive(source / f'{prefix(tag)}.tar.gz', Path(temp), tag)
        validate_native(root)
        for framework in FRAMEWORKS:
            validate_zip(source / f'{prefix(tag)}_{framework}.zip', framework, root)
        smoke = Path(__file__).with_name('ios_load_smoke.py')
        subprocess.run(['python3', str(smoke), str(root)], check=True)
    output.mkdir(parents=True)
    assets = []
    for name in sorted(expected):
        destination = output / name
        shutil.copyfile(source / name, destination)
        asset = {'name': name, 'sha256': digest(destination), 'size': destination.stat().st_size}
        if name.endswith('.tar.gz'):
            asset['kind'] = 'ios-archive'
        else:
            asset.update(kind='ios-framework-zip', framework=name[len(prefix(tag)) + 1:-4])
        assets.append(asset)
    manifest = dict(schema_version=1, platform='ios', repository=REPOSITORY,
                    tag=tag, source_commit=commit, assets=assets,
                    workflow_run_id=int(os.environ.get('GITHUB_RUN_ID', '0')),
                    request_id=os.environ.get('REQUEST_ID', ''))
    (output / MANIFEST).write_text(json.dumps(manifest, indent=2) + '\n')


def verify_payload(directory, tag, commit):
    validate_identity(tag, commit)
    manifest = json.loads((directory / MANIFEST).read_text())
    for key, expected in dict(schema_version=1, platform='ios', repository=REPOSITORY, tag=tag, source_commit=commit).items():
        if manifest.get(key) != expected:
            raise ValueError(f'Manifest identity mismatch: {key}')
    if 'GITHUB_RUN_ID' in os.environ:
        if manifest.get('workflow_run_id') != int(os.environ['GITHUB_RUN_ID']) or manifest.get('request_id') != os.environ.get('REQUEST_ID', ''):
            raise ValueError('Manifest workflow/request provenance mismatch')
    entries = manifest.get('assets', [])
    names = [entry['name'] for entry in entries]
    if len(names) != len(set(names)) or set(names) != asset_names(tag):
        raise ValueError('Manifest assets must exactly match the complete iOS payload')
    if {p.name for p in directory.iterdir()} != set(names) | {MANIFEST}:
        raise ValueError('Unexpected publish payload entries')
    for item in entries:
        path = directory / item['name']
        is_zip = path.suffix == '.zip'
        if item.get('kind') != ('ios-framework-zip' if is_zip else 'ios-archive'):
            raise ValueError('Incorrect asset kind')
        if is_zip and item.get('framework') != path.name[len(prefix(tag)) + 1:-4]:
            raise ValueError('Incorrect framework name')
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item.get('size') or digest(path) != item.get('sha256'):
            raise ValueError(f'Asset checksum/size mismatch: {path.name}')
    return manifest


class GitHub:
    def __init__(self, token):
        if not token:
            raise ValueError('GH_TOKEN is required')
        self.token = token

    def request(self, method, path, payload=None, *, content_type='application/json', absent_ok=False):
        url = path if path.startswith('https://uploads.github.com/') else f'https://api.github.com/repos/{REPOSITORY}/{path}'
        data = json.dumps(payload).encode() if payload is not None and content_type == 'application/json' else payload
        request = Request(url, data=data, method=method, headers={
            'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': content_type,
        })
        try:
            with urlopen(request, timeout=120) as response:
                return json.load(response)
        except HTTPError as error:
            if absent_ok and error.code == 404:
                return None
            raise RuntimeError(f'GitHub {method} failed with HTTP {error.code}; no existing release was overwritten') from None


def ensure_unused(api, tag):
    if api.request('GET', f'git/ref/tags/{quote(tag, safe="")}', absent_ok=True) is not None:
        raise ValueError(f'Tag already exists: {tag}')
    if api.request('GET', f'releases/tags/{quote(tag, safe="")}', absent_ok=True) is not None:
        raise ValueError(f'Release already exists: {tag}')


def publish(api, directory, tag, commit):
    manifest = verify_payload(directory, tag, commit)
    ensure_unused(api, tag)
    # Atomic ref creation refuses a concurrently-created tag. Never force/update.
    api.request('POST', 'git/refs', {'ref': f'refs/tags/{tag}', 'sha': commit})
    release = api.request('POST', 'releases', {
        'tag_name': tag, 'target_commitish': commit, 'name': f'iOS libmpv {tag}',
        'draft': True, 'prerelease': True, 'make_latest': 'false',
        'body': f'iOS universal video/full build from `{commit}`.\n\nChecksums and provenance: `{MANIFEST}`.\nDevice arm64; simulator arm64 and x86_64.\n',
    })
    upload = release['upload_url'].split('{', 1)[0]
    expected = {}
    for name in sorted(asset_names(tag) | {MANIFEST}):
        path = directory / name
        expected[name] = {'size': path.stat().st_size, 'digest': f'sha256:{digest(path)}'}
        response = api.request('POST', f'{upload}?name={quote(name, safe="")}', path.read_bytes(), content_type='application/octet-stream')
        if (response.get('state') != 'uploaded' or response.get('name') != name
                or response.get('size') != expected[name]['size']
                or response.get('digest') != expected[name]['digest']):
            raise ValueError(f'Upload verification failed for {name}; draft retained for inspection')
    remote_assets = api.request('GET', f'releases/{release["id"]}/assets?per_page=100')
    if len(remote_assets) != len(expected) or {a['name'] for a in remote_assets} != set(expected):
        raise ValueError('Remote asset set mismatch; draft retained')
    for asset in remote_assets:
        if asset.get('state') != 'uploaded' or any(asset.get(k) != v for k, v in expected[asset['name']].items()):
            raise ValueError('Remote asset digest/size mismatch; draft retained')
    ref = api.request('GET', f'git/ref/tags/{quote(tag, safe="")}')
    if ref.get('object', {}).get('sha') != commit or ref.get('object', {}).get('type') != 'commit':
        raise ValueError('Release tag changed; draft retained')
    final = api.request('PATCH', f'releases/{release["id"]}', {'draft': False, 'prerelease': True, 'make_latest': 'false'})
    if final.get('draft') is not False or final.get('prerelease') is not True or final.get('tag_name') != tag:
        raise ValueError('GitHub did not confirm the requested prerelease state')
    print(f'Published immutable prerelease {tag} from {manifest["source_commit"]}')


def resolve_xcode(applications=Path('/Applications'), command=run):
    checked = set()
    for candidate in sorted(applications.glob('Xcode*.app')):
        candidate = candidate.resolve()
        if candidate in checked:
            continue
        checked.add(candidate)
        tool = candidate / 'Contents/Developer/usr/bin/xcodebuild'
        if not tool.is_file():
            continue
        version = command([str(tool), '-version'])
        if re.search(r'^Xcode 16\.1\s*$', version, re.M) and 'Build version 16B40' in version:
            environment = {**os.environ, 'DEVELOPER_DIR': str(candidate / 'Contents/Developer')}
            for sdk in ('iphoneos', 'iphonesimulator'):
                sdk_path = command(['xcrun', '--sdk', sdk, '--show-sdk-path'], env=environment)
                if not Path(sdk_path).is_dir():
                    raise ValueError(f'{sdk} SDK not installed')
            if '\n' in str(candidate):
                raise ValueError('Invalid Xcode path')
            return candidate
    raise ValueError('Xcode 16.1 (16B40) is required and was not found; refusing to download Xcode or change SDK versions')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['validate-inputs', 'preflight', 'resolve-xcode', 'prepare', 'verify', 'publish'])
    parser.add_argument('--source', type=Path, default=Path('result'))
    parser.add_argument('--output', type=Path, default=Path('release-assets'))
    args = parser.parse_args()
    tag, commit, request_id = (os.environ.get(name, '') for name in ('RELEASE_TAG', 'SOURCE_COMMIT', 'REQUEST_ID'))
    validate_identity(tag, commit, request_id)
    if os.environ.get('GITHUB_ACTIONS') == 'true' and not re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ID', '')):
        raise ValueError('GitHub Actions must supply its actual numeric run ID')
    if args.action == 'resolve-xcode':
        path = resolve_xcode()
        print(path)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'xcode_path={path}\n')
    elif args.action == 'prepare':
        prepare(args.source, args.output, tag, commit)
    elif args.action == 'verify':
        verify_payload(args.output, tag, commit)
    elif args.action in ('preflight', 'publish'):
        if os.environ.get('GITHUB_REPOSITORY') != REPOSITORY:
            raise ValueError(f'This release workflow belongs to {REPOSITORY}')
        api = GitHub(os.environ.get('GH_TOKEN', ''))
        if args.action == 'preflight':
            ensure_unused(api, tag)
        else:
            publish(api, args.output, tag, commit)


if __name__ == '__main__':
    main()
