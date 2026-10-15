#!/usr/bin/env python3
"""Collect rebuilt gateway binaries and Ceph-built shared-library closure.

System libraries are deliberately validated against the candidate's pinned
S4 base before adding any replacements. This avoids mixing libc releases.
"""
import hashlib
import json
import pathlib
import re
import shutil
import subprocess

root = pathlib.Path('/work/src/build')
out = pathlib.Path('/work/candidate/runtime')
manifest = {}
for directory in ('usr/bin', 'usr/lib64'):
    (out / directory).mkdir(parents=True, exist_ok=True)

def copy(source, relative):
    destination = out / relative
    shutil.copy2(source.resolve(), destination)
    manifest[str(relative)] = hashlib.sha256(destination.read_bytes()).hexdigest()

for source_name, dest_name in (('rgw-standalone', 'radosgw'), ('rgw-standalone-admin', 'radosgw-admin')):
    binary = root / 'bin' / source_name
    copy(binary, pathlib.Path('usr/bin') / dest_name)
    output = subprocess.check_output(['ldd', str(binary)], text=True)
    print(output)
    if 'not found' in output:
        raise SystemExit('Builder binary has unresolved runtime dependencies')
    for match in re.finditer(r'^\s*(\S+) => (/\S+)', output, re.MULTILINE):
        name, path = match.groups()
        library = pathlib.Path(path)
        if library.is_relative_to(root):
            copy(library, pathlib.Path('usr/lib64') / name)
# LTTng probes are loaded dynamically and therefore do not appear in ldd.
for library in (root / 'lib').glob('librgw_op_tp.so*'):
    copy(library, pathlib.Path('usr/lib64') / library.name)
(out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
shutil.copy2('/work/Containerfile.candidate', out.parent / 'Containerfile')
print(json.dumps(manifest, indent=2))
