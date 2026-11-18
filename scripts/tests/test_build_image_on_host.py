"""Run the native build helper without network, registry, or database access."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "build-image-on-host.sh"
AUTH_BYTES = b'{\n  "auths": {"example.invalid": {"auth": "synthetic+/=="}}\n}\n\n'


def executable(path, source):
    path.write_text(source)
    path.chmod(0o755)


@pytest.fixture
def run_build(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
    context = tmp_path / "context"
    context.mkdir()
    (context / "Containerfile").write_text("FROM scratch\n")
    auth = tmp_path / "local-auth.json"
    auth.write_bytes(AUTH_BYTES)
    remote = tmp_path / "remote"
    remote.mkdir()
    record = tmp_path / "push.json"
    commands = tmp_path / "commands.jsonl"
    executable(
        scripts / "host-ssh.sh",
        """#!/usr/bin/env python3
import json, os, pathlib, shlex, subprocess, sys, tarfile
assert sys.argv[1] == 'test-host'
command = sys.argv[2].replace('/tmp/troshka-build-', os.environ['TEST_REMOTE'] + '/troshka-build-')
with open(os.environ['TEST_COMMANDS'], 'a') as log:
    log.write(json.dumps(command) + '\\n')
# Linux tar preserves AppleDouble entries as ordinary files. Avoid macOS tar's
# automatic metadata unpacking so the fake host exposes the same build context.
if '&& tar xzf -' in command:
    destination = pathlib.Path(shlex.split(command)[-1])
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=sys.stdin.buffer, mode='r|gz') as archive:
        archive.extractall(destination)
    sys.exit(0)
sys.exit(subprocess.call(['/bin/sh', '-c', command]))
""",
    )
    executable(
        scripts / "sudo",
        """#!/usr/bin/env python3
import os, sys
os.execvp(sys.argv[1], sys.argv[1:])
""",
    )
    executable(
        scripts / "podman",
        """#!/usr/bin/env python3
import json, os, pathlib, stat, sys
args = sys.argv[1:]
if args[0] == 'build':
    pathlib.Path(os.environ['TEST_CONTEXT_RECORD']).write_text(json.dumps({
        str(path): path.read_bytes().hex()
        for path in pathlib.Path('.').rglob('*') if path.is_file()
    }))
    sys.exit(0)
if args[:2] == ['image', 'inspect']:
    print('built synthetic (amd64)')
    sys.exit(0)
assert args[0] == 'push'
assert args[1] == '--authfile'
assert args[3] == 'example.invalid/test:candidate'
auth = pathlib.Path(args[2])
pathlib.Path(os.environ['TEST_RECORD']).write_text(json.dumps({
    'bytes': list(auth.read_bytes()),
    'mode': stat.S_IMODE(auth.stat().st_mode),
    'path': str(auth),
}))
sys.exit(int(os.environ['TEST_PUSH_EXIT']))
""",
    )
    executable(
        scripts / "base64",
        """#!/usr/bin/env python3
import base64, os, pathlib, sys
if '-d' in sys.argv:
    if os.environ['TEST_DECODE_FAIL'] == '1':
        print('base64: invalid input', file=sys.stderr)
        sys.exit(7)
    sys.stdout.buffer.write(base64.b64decode(pathlib.Path(sys.argv[-1]).read_bytes()))
else:
    sys.stdout.buffer.write(base64.b64encode(sys.stdin.buffer.read()))
""",
    )

    def run(push_exit=0, decode_fail=False, context_setup=None):
        if context_setup is not None:
            context_setup(context)
        env = {
            **os.environ,
            "PATH": str(scripts) + os.pathsep + os.environ["PATH"],
            "REGISTRY_AUTH_FILE": str(auth),
            "TEST_REMOTE": str(remote),
            "TEST_RECORD": str(record),
            "TEST_COMMANDS": str(commands),
            "TEST_CONTEXT_RECORD": str(remote / "context.json"),
            "TEST_PUSH_EXIT": str(push_exit),
            "TEST_DECODE_FAIL": "1" if decode_fail else "0",
        }
        result = subprocess.run(
            ["bash", str(scripts / SCRIPT.name), "--host", "test-host",
             "--context", str(context), "--tag", "example.invalid/test:candidate", "--push"],
            env=env, capture_output=True, text=True, timeout=15, umask=0o022,
        )
        pushed = json.loads(record.read_text()) if record.exists() else None
        return result, pushed, remote, commands.read_text()

    return run


def assert_no_auth_residue(remote):
    assert not list(remote.rglob(".auth*"))


def test_push_preserves_auth_bytes_uses_private_mode_and_removes_auth(run_build):
    result, pushed, remote, commands = run_build()
    assert result.returncode == 0, result.stderr
    assert bytes(pushed["bytes"]) == AUTH_BYTES
    assert pushed["mode"] == 0o600
    assert not Path(pushed["path"]).exists()
    assert AUTH_BYTES.decode() not in commands
    assert ">> Done:" in result.stdout
    assert_no_auth_residue(remote)


def test_build_context_preserves_source_and_excludes_existing_appledouble(run_build):
    source = b'revision = "real-migration"\n'

    def setup(context):
        migrations = context / "app" / "alembic" / "versions"
        migrations.mkdir(parents=True)
        (migrations / "migration.py").write_bytes(source)
        (migrations / "._bad.py").write_bytes(b"\x00AppleDouble metadata\x00")
        (context / "._Containerfile").write_bytes(b"\x00root metadata\x00")

    result, _, remote, _ = run_build(context_setup=setup)
    assert result.returncode == 0, result.stdout + result.stderr
    files = json.loads((remote / "context.json").read_text())
    assert files == {
        "Containerfile": b"FROM scratch\n".hex(),
        "app/alembic/versions/migration.py": source.hex(),
    }


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS copyfile metadata")
def test_build_context_omits_synthesized_macos_appledouble(run_build):
    source = b'revision = "xattr-migration"\n'
    xattr = shutil.which("xattr")
    if xattr is None:
        pytest.skip("xattr utility unavailable")

    def setup(context):
        path = context / "migration.py"
        path.write_bytes(source)
        result = subprocess.run(
            [xattr, "-w", "com.troshka.archive-test", "metadata payload", str(path)],
            capture_output=True, text=True,
        )
        if result.returncode:
            pytest.skip(f"filesystem does not support test xattr: {result.stderr}")
        assert subprocess.check_output(
            [xattr, "-p", "com.troshka.archive-test", str(path)], text=True,
        ).strip() == "metadata payload"

    result, _, remote, _ = run_build(context_setup=setup)
    assert result.returncode == 0, result.stdout + result.stderr
    files = json.loads((remote / "context.json").read_text())
    assert files == {
        "Containerfile": b"FROM scratch\n".hex(),
        "migration.py": source.hex(),
    }


def test_failed_push_exits_nonzero_without_done_or_auth_residue(run_build):
    result, pushed, remote, _ = run_build(push_exit=41)
    assert result.returncode == 41, result.stdout + result.stderr
    assert pushed is not None
    assert ">> Done:" not in result.stdout
    assert_no_auth_residue(remote)


def test_raw_auth_transfer_does_not_depend_on_remote_base64_decoder(run_build):
    result, pushed, remote, _ = run_build(decode_fail=True)
    assert result.returncode == 0, result.stderr
    assert pushed is not None
    assert bytes(pushed["bytes"]) == AUTH_BYTES
    assert "invalid input" not in result.stderr
    assert_no_auth_residue(remote)
