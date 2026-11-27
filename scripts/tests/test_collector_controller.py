import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    return importlib.import_module('collector_controller')


def test_controller_rejects_external_admin_mode(runtime):
    with pytest.raises(ValueError):
        runtime.validate_plan({'applications': [{'namespace': 'troshka'}]}, 'troshka', 'troshka')


def test_controller_rejects_other_application(runtime):
    entry = {'mode': 'operator', 'namespace': 'other', 'application': 'troshka'}
    with pytest.raises(ValueError):
        runtime.validate_plan({'applications': [entry]}, 'troshka', 'troshka')


def test_controller_fixes_local_transport_and_requires_one_application(runtime):
    entry = {'mode': 'operator', 'namespace': 'troshka', 'application': 'troshka'}
    result = runtime.validate_plan({'applications': [entry]}, 'troshka', 'troshka')
    assert result['applications'][0]['kubeconfig'] == '@incluster'
    with pytest.raises(ValueError):
        runtime.validate_plan({'applications': [entry, entry]}, 'troshka', 'troshka')


def test_adapter_rejects_unknown_mutations(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    adapter = importlib.import_module('collector_kubernetes')
    with pytest.raises(ValueError):
        adapter.execute(MagicMock(), ('delete', 'pod', 'anything'), None, 30)


def test_exec_error_is_not_returned_as_success(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    adapter = importlib.import_module('collector_kubernetes')
    result = MagicMock(returncode=1)
    monkeypatch.setattr(adapter.subprocess, 'run', lambda *a, **kw: result)
    with pytest.raises(ValueError):
        adapter.exec_pod('config', ('exec', 'pod'), None, 30)


def test_exec_preserves_create_only_permission_and_bounds_runtime(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    adapter = importlib.import_module('collector_kubernetes')
    run = MagicMock(return_value=MagicMock(returncode=0, stdout='ok'))
    monkeypatch.setattr(adapter.subprocess, 'run', run)
    assert adapter.exec_pod('private-config', ('exec', 'pod'), 'input', 30) == 'ok'
    assert run.call_args.kwargs['env']['KUBECTL_REMOTE_COMMAND_WEBSOCKETS'] == 'false'
    assert run.call_args.kwargs['timeout'] == 30
    assert run.call_args.kwargs['input'] == 'input'
    assert '--kubeconfig=private-config' in run.call_args.args[0]


def test_native_request_works_with_installed_client_and_releases_response(monkeypatch):
    from kubernetes import client
    from urllib3 import HTTPResponse

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    adapter = importlib.import_module('collector_kubernetes')
    with client.ApiClient(client.Configuration()) as api:
        response = HTTPResponse(body=b'{"metadata":{"name":"published"}}', status=200,
                                preload_content=False)
        monkeypatch.setattr(api, 'request', lambda *a, **kw: response)
        assert adapter.request(api, 'GET', 'secret', 'published', 'app')['metadata']['name'] == 'published'
        assert response.closed


def test_status_uses_merge_patch_without_credentials(runtime, monkeypatch):
    adapter = importlib.import_module('collector_kubernetes')
    api = MagicMock()
    api.__enter__.return_value = api
    monkeypatch.setattr(adapter, 'api_for', lambda _: api)
    monkeypatch.setenv('POD_NAMESPACE', 'app')
    result = runtime.publish_status({'status': 'healthy', 'token': 'must-not-publish'})
    kwargs = api.call_api.call_args.kwargs
    assert kwargs['header_params']['Content-Type'] == 'application/merge-patch+json'
    assert 'must-not-publish' not in str(kwargs)
    assert result['ready'] is True
