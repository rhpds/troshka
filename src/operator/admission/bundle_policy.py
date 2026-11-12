"""API-local policies; exact controller identities are an explicit trust boundary.

CEL cannot look up owner ancestry or protect VAP/binding configuration itself.
Target API type checking and effective RBAC remain mandatory deployment gates.
"""

import json

NAME = "troshka-quarantine"
ROLES = (
    "deployment",
    "replicaset",
    "scheduler",
    "pod_status",
    "endpoints",
    "endpointslice",
    "garbage_collection",
    "disruption",
)


def literal(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def rule(group, resources, operations, *, scope="Namespaced"):
    return {
        "apiGroups": [group],
        "apiVersions": ["*"],
        "resources": resources,
        "operations": operations,
        "scope": scope,
    }


def policy(name, rules, condition, expression, variables=()):
    spec = {
        "failurePolicy": "Fail",
        "matchConstraints": {"resourceRules": rules},
        "matchConditions": [{"name": "exact-scope", "expression": condition}],
        "validations": [
            {"expression": expression, "message": "quarantine_control_denied"}
        ],
    }
    if variables:
        spec["variables"] = [
            {"name": key, "expression": value} for key, value in variables
        ]
    return [
        {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicy",
            "metadata": {"name": name},
            "spec": spec,
        },
        {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": name},
            "spec": {"policyName": name, "validationActions": ["Deny"]},
        },
    ]


def lifetime_policy(entries, namespace, namespace_uid, bootstrap):
    identities = {entry.namespace_name: entry.namespace_uid for entry in entries}
    identities[namespace] = namespace_uid
    return policy(
        NAME + "-lifetimes",
        [rule("", ["namespaces", "namespaces/*"], ["*"], scope="Cluster")],
        f"request.name in {literal(sorted(identities))}",
        'request.operation != "CREATE" && '
        'request.operation in ["UPDATE", "DELETE"] && '
        "oldObject != null && oldObject.metadata.uid == variables.uid && "
        '(request.operation == "DELETE" || '
        "(object != null && object.metadata.uid == variables.uid)) && "
        f"(request.name != {literal(namespace)} || "
        '(request.operation != "DELETE" && '
        f"request.userInfo.username in {literal(bootstrap)}))",
        [("uid", f"{literal(identities)}[request.name]")],
    )


def _owner(api, kind, name_expression):
    return (
        "has(variables.target.metadata.ownerReferences) && "
        "variables.target.metadata.ownerReferences.exists(o, "
        f"o.apiVersion == {literal(api)} && o.kind == {literal(kind)} && "
        f'{name_expression} && has(o.uid) && o.uid != "" && '
        "has(o.controller) && o.controller)"
    )


def _named(prefix):
    pattern = literal(prefix + "-[a-z0-9]+(-[a-z0-9]+)?")
    return (
        "((has(variables.target.metadata.name) && "
        f'variables.target.metadata.name.matches("^" + {pattern} + "$")) || '
        '(request.operation == "CREATE" && has(variables.target.metadata.generateName) && '
        f'variables.target.metadata.generateName.matches("^" + {pattern} + "-$")))'
    )


def _class(group, resource, subresource, operations):
    # AdmissionRequest omits the optional subResource key for root requests.
    subresource_match = (
        f"(has(request.subResource) && request.subResource == {literal(subresource)})"
        if subresource
        else '(!has(request.subResource) || request.subResource == "")'
    )
    return (
        f"request.resource.group == {literal(group)} && "
        'request.resource.version == "v1" && '
        f"request.resource.resource == {literal(resource)} && "
        f"{subresource_match} && "
        f"request.operation in {literal(operations)}"
    )


def _status(group, resource, shape, metadata="statusMetadataUnchanged"):
    return (
        f"({_class(group, resource, 'status', ['UPDATE'])} && {shape} && "
        f"variables.{metadata} && object.spec == oldObject.spec)"
    )


def _status_metadata_unchanged(*, annotations=True):
    """Compare ObjectMeta except API resourceVersion/managedFields bookkeeping.

    These are all other persisted v1.30 ObjectMeta fields (clusterName was
    removed). The API schema handles unknown metadata, not this field list.
    Target API CEL type checking remains a required deployment gate.
    """
    fields = (
        "name",
        "generateName",
        "namespace",
        "selfLink",
        "uid",
        "generation",
        "creationTimestamp",
        "deletionTimestamp",
        "deletionGracePeriodSeconds",
        "labels",
        "annotations",
        "ownerReferences",
        "finalizers",
    )
    return " && ".join(
        f"(has(object.metadata.{field}) == has(oldObject.metadata.{field}) && "
        f"(!has(object.metadata.{field}) || "
        f"object.metadata.{field} == oldObject.metadata.{field}))"
        for field in fields
        if annotations or field != "annotations"
    )


def _status_annotations_unchanged(allowed):
    """Only explicit controller-owned annotations may differ on status writes."""
    keys = literal(allowed)
    return (
        "variables.statusNonAnnotationMetadataUnchanged && "
        f"variables.oldAnnotations.all(k, k in {keys} || "
        "(k in variables.newAnnotations && "
        "variables.oldAnnotations[k] == variables.newAnnotations[k])) && "
        f"variables.newAnnotations.all(k, k in {keys} || "
        "k in variables.oldAnnotations)"
    )


def _identity(role, users, expressions):
    return (
        f"(request.userInfo.username in {literal(users[role])} && ("
        + " || ".join(expressions)
        + "))"
    )


def _controller_rules(users, scheduler_nodes):
    rs = "variables.replicaSet"
    pod = "variables.pod"
    endpoint = f"variables.target.metadata.name == {literal(NAME)}"
    rules = {
        "deployment": [
            f"({_class('apps', 'replicasets', '', ['CREATE', 'UPDATE', 'DELETE'])} && {rs})",
            _status(
                "apps", "deployments", endpoint, "deploymentStatusMetadataUnchanged"
            ),
        ],
        "replicaset": [
            f"({_class('', 'pods', '', ['CREATE', 'DELETE'])} && {pod})",
            _status("apps", "replicasets", rs),
        ],
        "scheduler": [
            (
                f"({_class('', 'pods', 'binding', ['CREATE'])} && "
                f'{_named(NAME)} && variables.target.target.kind == "Node" && '
                f"variables.target.target.name in {literal(scheduler_nodes)})"
            )
        ],
        "pod_status": [_status("", "pods", pod, "podStatusMetadataUnchanged")],
        "endpoints": [
            f"({_class('', 'endpoints', '', ['CREATE', 'UPDATE', 'DELETE'])} && {endpoint})"
        ],
        "endpointslice": [
            f"({_class('discovery.k8s.io', 'endpointslices', '', ['CREATE', 'UPDATE', 'DELETE'])} && variables.slice)"
        ],
        "garbage_collection": [
            f"({_class('apps', 'replicasets', '', ['DELETE'])} && {rs})",
            f"({_class('', 'pods', '', ['DELETE'])} && {pod})",
            f"({_class('discovery.k8s.io', 'endpointslices', '', ['DELETE'])} && variables.slice)",
        ],
        "disruption": [_status("policy", "poddisruptionbudgets", endpoint)],
    }
    return [_identity(role, users, rules[role]) for role in ROLES]


def _pod_shape(path, image, config_name, tls_secret):
    """Require the tokenless workload shape, not just a forgeable label/owner."""
    container = path + ".spec.containers[0]"
    clauses = [
        f"has({path}.metadata.labels)",
        f'{path}.metadata.labels["app.kubernetes.io/name"] == {literal(NAME)}',
        f"{path}.spec.serviceAccountName == {literal(NAME)}",
        f"has({path}.spec.automountServiceAccountToken)",
        f"!{path}.spec.automountServiceAccountToken",
        f"size({path}.spec.containers) == 1",
        f"{container}.image == {literal(image)}",
        f'{container}.command == ["python3", "-m", "admission.server"]',
        f'{container}.args == ["--config", "/etc/quarantine/enrollments.json", "--tls-cert", "/etc/tls/tls.crt", "--tls-key", "/etc/tls/tls.key"]',
        f"!{container}.securityContext.allowPrivilegeEscalation",
        f"{container}.securityContext.readOnlyRootFilesystem",
        f'{container}.securityContext.capabilities.drop == ["ALL"]',
        f"(!has({container}.securityContext.privileged) || !{container}.securityContext.privileged)",
        f"(!has({container}.securityContext.capabilities.add) || size({container}.securityContext.capabilities.add) == 0)",
        f"{path}.spec.securityContext.runAsNonRoot",
        f'{path}.spec.securityContext.seccompProfile.type == "RuntimeDefault"',
        f"size({path}.spec.volumes) == 2",
        f'{path}.spec.volumes.exists(v, v.name == "config" && has(v.configMap) && v.configMap.name == {literal(config_name)})',
        f'{path}.spec.volumes.exists(v, v.name == "tls" && has(v.secret) && v.secret.secretName == {literal(tls_secret)})',
    ]
    clauses.extend(
        f"(!has({path}.spec.{field}) || !{path}.spec.{field})"
        for field in ("hostNetwork", "hostPID", "hostIPC")
    )
    clauses.extend(
        f"!has({path}.spec.{field})"
        for field in ("initContainers", "ephemeralContainers")
    )
    return " && ".join(clauses)


def control_policy(
    namespace, tls_secret, config_name, image, bootstrap, users, scheduler_nodes
):
    roots = [
        ("", "secrets", tls_secret),
        ("", "configmaps", config_name),
        ("", "serviceaccounts", NAME),
        ("", "services", NAME),
        ("apps", "deployments", NAME),
        ("policy", "poddisruptionbudgets", NAME),
    ]
    root_expressions = [
        f"({_class(group, resource, '', ['CREATE', 'UPDATE', 'DELETE'])} && "
        f"request.name == {literal(name)})"
        for group, resource, name in roots
    ]
    trusted = (
        f"(request.userInfo.username in {literal(bootstrap)} && ("
        + " || ".join(root_expressions)
        + "))"
    )
    variables = [
        ("target", 'request.operation == "DELETE" ? oldObject : object'),
        ("statusMetadataUnchanged", _status_metadata_unchanged()),
        (
            "statusNonAnnotationMetadataUnchanged",
            _status_metadata_unchanged(annotations=False),
        ),
        (
            "oldAnnotations",
            "has(oldObject.metadata.annotations) ? oldObject.metadata.annotations : {}",
        ),
        (
            "newAnnotations",
            "has(object.metadata.annotations) ? object.metadata.annotations : {}",
        ),
        (
            "deploymentStatusMetadataUnchanged",
            _status_annotations_unchanged(["deployment.kubernetes.io/revision"]),
        ),
        (
            "podStatusMetadataUnchanged",
            _status_annotations_unchanged(
                ["k8s.ovn.org/pod-networks", "k8s.v1.cni.cncf.io/network-status"]
            ),
        ),
        (
            "replicaSet",
            f"{_named(NAME)} && {_owner('apps/v1', 'Deployment', 'o.name == ' + literal(NAME))} && "
            + _pod_shape(
                "variables.target.spec.template", image, config_name, tls_secret
            ),
        ),
        (
            "pod",
            f"{_named(NAME)} && {_owner('apps/v1', 'ReplicaSet', 'o.name.matches(' + literal('^' + NAME + '-[a-z0-9]+$') + ')')} && "
            + _pod_shape("variables.target", image, config_name, tls_secret),
        ),
        (
            "slice",
            "has(variables.target.metadata.labels) && "
            f'variables.target.metadata.labels["kubernetes.io/service-name"] == {literal(NAME)} && '
            + _owner("v1", "Service", "o.name == " + literal(NAME)),
        ),
    ]
    expression = (
        'variables.target != null && (request.operation == "CREATE" || '
        '(oldObject != null && has(oldObject.metadata.uid) && oldObject.metadata.uid != "" && '
        "variables.target.metadata.uid == oldObject.metadata.uid)) && ("
        + trusted
        + " || "
        + " || ".join(_controller_rules(users, scheduler_nodes))
        + ")"
    )
    return policy(
        NAME + "-control",
        [rule("*", ["*/*"], ["*"])],
        f"request.namespace == {literal(namespace)}",
        expression,
        variables,
    )


def webhook_protection(bootstrap):
    return policy(
        NAME + "-webhook",
        [
            rule(
                "admissionregistration.k8s.io",
                ["validatingwebhookconfigurations"],
                ["*"],
                scope="Cluster",
            )
        ],
        f"request.name == {literal(NAME)}",
        f"request.userInfo.username in {literal(bootstrap)}",
    )
