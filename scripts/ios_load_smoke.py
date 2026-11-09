#!/usr/bin/env python3
"""Load the actual bundled simulator frameworks and initialize libmpv, no stubs."""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile


def run(args, **kwargs):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, **kwargs).strip()


def simulator_selection(runtime_id=None):
    runtimes = json.loads(run(['xcrun', 'simctl', 'list', 'runtimes', '--json']))['runtimes']
    candidates = [r for r in runtimes if r.get('isAvailable') and
                  (r['identifier'] == runtime_id if runtime_id else
                   r['identifier'].startswith('com.apple.CoreSimulator.SimRuntime.iOS-18-'))]
    if not candidates:
        raise RuntimeError(f'An available {runtime_id or "iOS 18"} simulator runtime is required; no runtime is downloaded automatically')
    runtime = min(candidates, key=lambda r: tuple(map(int, r['version'].split('.'))))
    devices = json.loads(run(['xcrun', 'simctl', 'list', 'devicetypes', '--json']))['devicetypes']
    types = [d for d in devices if d.get('productFamily') == 'iPhone' and 'iPhone-16' in d['identifier']]
    if not types:
        raise RuntimeError('No compatible iPhone 16 simulator device type is installed')
    return runtime['identifier'], types[0]['identifier']


SOURCE = r'''
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
int main(void) {
  void *library = dlopen("@rpath/Mpv.framework/Mpv", RTLD_NOW | RTLD_LOCAL);
  if (!library) { fprintf(stderr, "dlopen: %s\n", dlerror()); return 1; }
  unsigned long (*version)(void) = dlsym(library, "mpv_client_api_version");
  void *(*create)(void) = dlsym(library, "mpv_create");
  int (*option)(void *, const char *, const char *) = dlsym(library, "mpv_set_option_string");
  int (*initialize)(void *) = dlsym(library, "mpv_initialize");
  void (*destroy)(void *) = dlsym(library, "mpv_terminate_destroy");
  if (!version || !create || !option || !initialize || !destroy) return 2;
  void *handle = create();
  if (!handle) return 3;
  if (option(handle, "config", "no") < 0 || option(handle, "vo", "null") < 0 || option(handle, "ao", "null") < 0) return 4;
  // Confirm the zcopy secondary-subtitle API, not just a loadable upstream mpv.
  if (option(handle, "secondary-sub-style", "independent") < 0 ||
      option(handle, "secondary-sub-scale", "1.25") < 0) return 6;
  int result = initialize(handle);
  destroy(handle);
  if (result < 0) { fprintf(stderr, "mpv_initialize: %d\n", result); return 5; }
  printf("LIBMPV_IOS_LOAD_OK api=%lu\n", version());
  dlclose(library);
  return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', type=Path)
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--runtime', help='Exact installed runtime ID for local validation; CI defaults to iOS 18')
    args = parser.parse_args()
    runtime, device_type = simulator_selection(args.runtime)
    if args.preflight:
        print(f'Simulator smoke runtime: {runtime}; device type: {device_type}')
        return
    if args.root is None:
        parser.error('root is required unless --preflight is used')
    root = args.root.resolve()
    with tempfile.TemporaryDirectory(prefix='mpv-ios-smoke-') as temp:
        directory = Path(temp)
        frameworks = directory / 'Frameworks'
        frameworks.mkdir()
        for xcframework in sorted(root.glob('*.xcframework')):
            metadata = plistlib.loads((xcframework / 'Info.plist').read_bytes())
            entries = [e for e in metadata['AvailableLibraries'] if e.get('SupportedPlatformVariant') == 'simulator']
            if len(entries) != 1:
                raise ValueError('Missing simulator slice')
            item = entries[0]
            shutil.copytree(xcframework / item['LibraryIdentifier'] / item['LibraryPath'], frameworks / item['LibraryPath'])
            for file in (frameworks / item['LibraryPath']).rglob('*'):
                file.chmod(file.stat().st_mode | 0o200)
            subprocess.run(['codesign', '--force', '--sign', '-', str(frameworks / item['LibraryPath'])], check=True)
        source = directory / 'smoke.c'
        source.write_text(SOURCE)
        arch = 'arm64' if platform.machine() == 'arm64' else 'x86_64'
        executable = directory / 'smoke'
        subprocess.run(['xcrun', '--sdk', 'iphonesimulator', 'clang', '-target', f'{arch}-apple-ios13.0-simulator',
                        '-isysroot', run(['xcrun', '--sdk', 'iphonesimulator', '--show-sdk-path']),
                        '-Wl,-rpath,@executable_path/Frameworks', str(source), '-o', str(executable)], check=True)
        subprocess.run(['codesign', '--force', '--sign', '-', str(executable)], check=True)
        device = run(['xcrun', 'simctl', 'create', f'libmpv-validation-{os.getpid()}', device_type, runtime])
        try:
            subprocess.run(['xcrun', 'simctl', 'boot', device], check=True, timeout=120)
            subprocess.run(['xcrun', 'simctl', 'bootstatus', device, '-b'], check=True, timeout=300)
            output = run(['xcrun', 'simctl', 'spawn', device, str(executable)], timeout=120)
            print(output)
            if 'LIBMPV_IOS_LOAD_OK api=' not in output:
                raise RuntimeError('Simulator did not confirm libmpv initialization')
        finally:
            subprocess.run(['xcrun', 'simctl', 'shutdown', device], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['xcrun', 'simctl', 'delete', device], check=False)


if __name__ == '__main__':
    main()
