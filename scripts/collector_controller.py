"""Central collector publisher loop, authenticated by its projected identity."""

import argparse
import copy
import fcntl
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from collector_provision import require

# The container invokes this file by path; make its sibling app package visible.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def validate_plan(plan, namespace, application):
    result = copy.deepcopy(plan)
    entries = result.get('applications', [])
    require(len(entries) == 1, 'one local application required')
    entry = entries[0]
    require(entry.get('mode') == 'operator' and entry.get('namespace') == namespace
            and entry.get('application') == application, 'publisher scope mismatch')
    entry['kubeconfig'] = '@incluster'
    return result


def publish_status(result):
    from collector_kubernetes import api_for

    # Never persist exception text or the plan: either could carry credentials.
    status = {'status': result['status'], 'observed_at': int(time.time()),
              'ready': result['status'] in {'healthy', 'updated'}}
    with api_for('@incluster') as api:
        name = os.environ.get('COLLECTOR_MAINTENANCE_STATUS_CONFIGMAP', 'troshka-collector-maintenance-status')
        response = api.call_api(
            '/api/v1/namespaces/' + os.environ['POD_NAMESPACE'] + '/configmaps/' + name, 'PATCH',
            body={'data': {'status.json': json.dumps(status)}},
            header_params={'Content-Type': 'application/merge-patch+json'},
            auth_settings=['BearerToken'], _return_http_data_only=True, _preload_content=False,
            _request_timeout=(5, 30))
        response.close()
        response.release_conn()
    return status


def cycle(plan_path, state):
    from collector_maintenance import run

    plan = validate_plan(json.loads(plan_path.read_text()), os.environ['POD_NAMESPACE'],
                         os.environ['COLLECTOR_MAINTENANCE_APPLICATION'])
    return run(plan, state, apply=True)


def main():
    from collector_kubernetes import install

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--interval', type=int, default=300)
    args = parser.parse_args()
    require(30 <= args.interval <= 3600, 'interval must be 30..3600 seconds')
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    install()
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    with (args.state_dir / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not stopped.is_set():
            try:
                result = cycle(args.plan, args.state_dir)
            except Exception:  # noqa: BLE001 -- provider exceptions may contain credentials
                result = {'status': 'blocked'}
            try:
                status = publish_status(result)
            except Exception:  # noqa: BLE001 -- never emit provider response bodies
                status = {'status': 'status_publication_failed', 'ready': False}
            print(json.dumps(status), flush=True)
            stopped.wait(args.interval)


if __name__ == '__main__':
    main()
