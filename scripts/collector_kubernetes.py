"""Kubernetes transport for the publisher, without administrator kubeconfigs.

Only the operations used by collector maintenance are supported. External
collector kubeconfigs remain static, TLS-verified credentials.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from collector_provision import require

RESOURCES = {
    'configmap': ('/api/v1', 'configmaps'),
    'secret': ('/api/v1', 'secrets'),
    'pod': ('/api/v1', 'pods'),
    'namespace': ('/api/v1', 'namespaces'),
    'deployment': ('/apis/apps/v1', 'deployments'),
    'applications.argoproj.io': ('/apis/argoproj.io/v1alpha1', 'applications'),
}


def api_for(kubeconfig):
    from kubernetes import client, config

    if kubeconfig == '@incluster':
        cfg = client.Configuration()
        config.load_incluster_config(client_configuration=cfg)
        return client.ApiClient(cfg)
    import yaml
    from collector_transport_health import selected_credentials

    with open(kubeconfig) as stream:
        value = yaml.safe_load(stream)
    selected_credentials(value)
    return config.new_client_from_config_dict(value)


def request(api, method, resource, name=None, namespace=None, body=None, patch=False):
    singular = resource if resource in RESOURCES else resource.removesuffix('s')
    require(singular in RESOURCES, 'unsupported publisher resource')
    prefix, plural = RESOURCES[singular]
    if singular == 'applications.argoproj.io':
        namespace = os.environ.get('COLLECTOR_MAINTENANCE_ARGO_NAMESPACE', namespace)
    path = prefix + (f'/namespaces/{namespace}' if namespace else '') + '/' + plural
    if name:
        path += '/' + name
    response = api.call_api(
        path, method, body=body,
        header_params={'Accept': 'application/json', 'Content-Type':
                       'application/json-patch+json' if patch else 'application/json'},
        auth_settings=['BearerToken'], _return_http_data_only=True, _preload_content=False,
        _request_timeout=(5, 30),
    )
    try:
        return json.loads(response.data)
    finally:
        response.close()
        response.release_conn()


def flag(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def exec_pod(kubeconfig, args, payload, timeout):
    # Python's websocket transport uses GET irrespective of its method argument.
    # Use pinned oc with SPDY to keep the collector's create-only exec scope.
    env = {**os.environ, 'KUBECTL_REMOTE_COMMAND_WEBSOCKETS': 'false'}
    result = subprocess.run(['oc', '--kubeconfig=' + kubeconfig, '--request-timeout=30s', *args],
                            input=payload, text=True, capture_output=True, timeout=timeout,
                            check=False, env=env)
    require(result.returncode == 0, 'publisher inspection failed')
    return result.stdout


def incluster_config():
    host = os.environ['KUBERNETES_SERVICE_HOST']
    if ':' in host:
        host = '[' + host + ']'
    server = 'https://' + host + ':' + os.environ.get('KUBERNETES_SERVICE_PORT', '443')
    root = '/var/run/secrets/kubernetes.io/serviceaccount/'
    return {'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'publisher',
            'clusters': [{'name': 'local', 'cluster': {'server': server, 'certificate-authority': root + 'ca.crt'}}],
            'users': [{'name': 'publisher', 'user': {'tokenFile': root + 'token'}}],
            'contexts': [{'name': 'publisher', 'context': {'cluster': 'local', 'user': 'publisher'}}]}


def execute(api, args, payload, timeout):
    verb = args[0]
    require(verb in {'get', 'create', 'patch'}, 'unsupported publisher operation')
    if verb == 'create':
        require(args[1:] == ('-f', '-') and payload is not None, 'unsupported creation')
        value = json.loads(payload)
        require(value['kind'] in {'ConfigMap', 'Secret'} and value.get('immutable') is True,
                'only immutable publisher inputs may be created')
        result = request(api, 'POST', value['kind'].lower(), namespace=value['metadata']['namespace'], body=value)
    else:
        name = args[2] if len(args) > 2 and not args[2].startswith('-') else None
        namespace = flag(args, '-n')
        if verb == 'patch':
            require(args[1] == 'applications.argoproj.io' and '--type=json' in args,
                    'only optimistic application patches are supported')
            result = request(api, 'PATCH', args[1], name, namespace,
                             json.loads(flag(args, '-p')), patch=True)
        else:
            result = request(api, 'GET', args[1], name, namespace)
    return json.dumps(result)


def oc(kubeconfig, *args, payload=None, timeout=90):
    if args[0] == 'exec':
        if kubeconfig == '@incluster':
            with tempfile.TemporaryDirectory(prefix='publisher-exec-') as directory:
                path = Path(directory) / 'config'
                path.write_text(json.dumps(incluster_config()))
                path.chmod(0o600)
                return exec_pod(str(path), args, payload, timeout)
        with api_for(kubeconfig):  # Validate static external TLS before invoking oc.
            return exec_pod(kubeconfig, args, payload, timeout)
    with api_for(kubeconfig) as api:
        return execute(api, args, payload, timeout)


def inspect(kubeconfig, namespace):
    from collector_fleet_preflight import PROBE

    return json.loads(oc(kubeconfig, 'exec', '-i', 'deployment/troshka-collection-worker',
                         '-n', namespace, '-c', 'worker', '--', 'python3', '-', payload=PROBE))


def install():
    import collector_maintenance
    import collector_provision

    collector_maintenance.oc = oc
    collector_provision.oc = oc
    collector_maintenance.inspect = inspect
