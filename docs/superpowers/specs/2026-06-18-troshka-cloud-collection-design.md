# troshka.cloud Ansible Collection — Design Spec

## Problem

The current `agnosticd.cloud_provider_troshka` collection grew organically:
- Wrong namespace (tied to agnosticd, not standalone)
- One monolith module (`troshka_api`) doing everything
- 7 roles that are just thin wrappers around API calls
- Mixed `ansible.builtin.uri` and module calls
- Roles contain logic that belongs in modules (polling, waiting)

## Goal

Replace with a clean `troshka.cloud` collection following Ansible best practices:
- One module per resource type (project, pattern, portal)
- Modules handle state management (`state: present/absent`)
- No roles — modules are called directly from playbooks
- Connection plugin for exec API access
- Shared `module_utils` for the API client

## Collection Structure

```
troshka.cloud/
├── galaxy.yml                    # namespace: troshka, name: cloud
├── plugins/
│   ├── modules/
│   │   ├── project.py            # Create, get, delete, start, stop projects
│   │   ├── project_deploy.py     # Deploy from template or pattern, wait for ready
│   │   ├── project_info.py       # Get project details, topology, VM states
│   │   ├── pattern.py            # Capture, get, delete patterns
│   │   ├── pattern_info.py       # List/search patterns
│   │   ├── portal_token.py       # Create portal access tokens
│   │   └── vm_info.py            # VM readiness check, exec commands
│   ├── module_utils/
│   │   └── troshka_api.py        # Shared API client (keep existing)
│   ├── connection/
│   │   └── troshka.py            # Connection plugin for exec API (keep existing)
│   └── inventory/
│       └── troshka.py            # Dynamic inventory plugin (keep existing)
├── meta/
│   └── runtime.yml
├── README.md
└── tests/
```

## Modules

### troshka.cloud.project

Manage Troshka projects (create, delete, start, stop).

```yaml
# Create project from template YAML
- troshka.cloud.project:
    api_url: "{{ troshka_api_url }}"
    api_key: "{{ troshka_api_key }}"
    name: "{{ guid }}"
    state: present
    template_yaml: "{{ template }}"
    common_password: "{{ common_password }}"
    ssh_pub_key: "{{ ssh_provision_pubkey_content }}"
    auto_install_ocp: false

# Delete project
- troshka.cloud.project:
    api_url: "{{ troshka_api_url }}"
    api_key: "{{ troshka_api_key }}"
    project_id: "{{ troshka_project_id }}"
    state: absent

# Start project
- troshka.cloud.project:
    api_url: "{{ troshka_api_url }}"
    api_key: "{{ troshka_api_key }}"
    project_id: "{{ troshka_project_id }}"
    state: started

# Stop project
- troshka.cloud.project:
    api_url: "{{ troshka_api_url }}"
    api_key: "{{ troshka_api_key }}"
    project_id: "{{ troshka_project_id }}"
    state: stopped
```

Returns: `project_id`, `state`, `name`, `portal_url`

### troshka.cloud.project_deploy

Deploy a project and optionally wait for completion.

```yaml
# Deploy from pattern
- troshka.cloud.project_deploy:
    api_url: "{{ troshka_api_url }}"
    api_key: "{{ troshka_api_key }}"
    source: pattern
    pattern_name: "5G RAN Lab v4.20"
    name: "{{ guid }}"

# Deploy (trigger on existing project)
- troshka.cloud.project_deploy:
    api_url: "{{ troshka_api_url }}"
    api_key: "{{ troshka_api_key }}"
    project_id: "{{ troshka_project_id }}"
```

Returns: `project_id`, `state`

Note: Don't use `wait` inside the module — use Ansible-level retries with `project_info` to poll. This keeps AAP2 heartbeats alive.

### troshka.cloud.project_info

Get project details — state, topology, OCP status.

```yaml
- troshka.cloud.project_info:
    api_url: "{{ troshka_api_url }}"
    api_key: "{{ troshka_api_key }}"
    project_id: "{{ troshka_project_id }}"
  register: project

# Use in retry loops for waiting:
- troshka.cloud.project_info:
    api_url: ...
    project_id: ...
  register: _deploy_status
  until: _deploy_status.state in ['active', 'error']
  retries: 240
  delay: 15
```

Returns: `state`, `name`, `ocp_status`, `topology`, `deploy_error`

### troshka.cloud.pattern

Capture patterns from projects.

```yaml
# Capture
- troshka.cloud.pattern:
    api_url: "{{ troshka_api_url }}"
    api_key: "{{ troshka_api_key }}"
    state: present
    name: "5G RAN Lab v4.20"
    source_project_id: "{{ troshka_project_id }}"
    visibility: private

# Delete
- troshka.cloud.pattern:
    api_url: "{{ troshka_api_url }}"
    api_key: "{{ troshka_api_key }}"
    pattern_id: "{{ pattern_id }}"
    state: absent
```

Returns: `pattern_id`, `state`, `name`

### troshka.cloud.pattern_info

List and search patterns.

```yaml
- troshka.cloud.pattern_info:
    api_url: "{{ troshka_api_url }}"
    api_key: "{{ troshka_api_key }}"
    name: "5G RAN Lab v4.20"
  register: patterns

# Use in retry loop for capture wait:
- troshka.cloud.pattern_info:
    api_url: ...
    pattern_id: "{{ pattern_id }}"
  register: _capture_status
  until: _capture_status.state in ['available', 'error']
  retries: 120
  delay: 15
```

Returns: `pattern_id`, `state`, `name`, `patterns` (list for search)

### troshka.cloud.portal_token

Create portal access tokens.

```yaml
- troshka.cloud.portal_token:
    api_url: "{{ troshka_api_url }}"
    api_key: "{{ troshka_api_key }}"
    project_id: "{{ troshka_project_id }}"
    access_level: console
  register: portal
```

Returns: `portal_url`, `token`

### troshka.cloud.vm_info

Check VM readiness, get VM states.

```yaml
# Check if VM is reachable
- troshka.cloud.vm_info:
    api_url: "{{ troshka_api_url }}"
    api_key: "{{ troshka_api_key }}"
    project_id: "{{ troshka_project_id }}"
    vm_id: "{{ bastion_vm_id }}"
  register: vm
  until: vm.ready
  retries: 30
  delay: 10
```

Returns: `ready`, `vm_id`, `state`

## Connection Plugin

Keep `troshka.cloud.troshka` — same as current but:
- Reads SSH private key from `ansible_ssh_private_key_file`, passes to exec API
- Falls back to `ansible_password` if no key
- Never logs keys or passwords

## What Gets Removed

All roles are removed. Callers use modules directly:

| Old Role | New Module Call |
|----------|---------------|
| `deploy` | `project` (create) + `project_deploy` + `project_info` (wait) + `portal_token` |
| `destroy` | `project` (state: absent) |
| `capture` | `pattern` (state: present) + `pattern_info` (wait) |
| `lifecycle` | `project` (state: started/stopped) + `project_info` (wait) |
| `portal_token` | `portal_token` |
| `create_inventory` | `project_info` + `add_host` in playbook |
| `wait_for_vm` | `vm_info` with `until/retries` |

## Cloud Provider Changes (agnosticd-v2)

`infrastructure_deployment.yml` calls modules directly instead of roles:

```yaml
- name: Step 001.0 - Create SSH Key
  hosts: localhost
  tasks:
    - ansible.builtin.include_role:
        name: infra_create_ssh_provision_key

- name: Step 001.1 - Deploy Infrastructure
  hosts: localhost
  tasks:
    - troshka.cloud.project:
        api_url: "{{ troshka_api_url }}"
        api_key: "{{ troshka_api_key }}"
        name: "{{ guid }}"
        state: present
        template_yaml: "{{ _template }}"
        common_password: "{{ common_password }}"
        ssh_pub_key: "{{ ssh_provision_pubkey_content }}"
        auto_install_ocp: "{{ auto_install_ocp }}"
      register: _project

    - ansible.builtin.set_fact:
        troshka_project_id: "{{ _project.project_id }}"

    - troshka.cloud.project_deploy:
        api_url: "{{ troshka_api_url }}"
        api_key: "{{ troshka_api_key }}"
        project_id: "{{ troshka_project_id }}"

    - troshka.cloud.project_info:
        api_url: "{{ troshka_api_url }}"
        api_key: "{{ troshka_api_key }}"
        project_id: "{{ troshka_project_id }}"
      register: _deploy
      until: _deploy.state in ['active', 'error']
      retries: 240
      delay: 15

- name: Step 001.2 - Create Inventory
  hosts: localhost
  tasks:
    - troshka.cloud.project_info:
        api_url: "{{ troshka_api_url }}"
        api_key: "{{ troshka_api_key }}"
        project_id: "{{ troshka_project_id }}"
      register: _project

    # Build inventory from topology
    - ansible.builtin.add_host:
        name: "{{ item.data.name }}"
        ansible_connection: troshka.cloud.troshka
        ...
      loop: "{{ _project.topology.nodes | selectattr('type', 'eq', 'vmNode') }}"

- name: Step 001.3 - Wait for Bastion
  hosts: localhost
  tasks:
    - troshka.cloud.vm_info:
        api_url: "{{ troshka_api_url }}"
        api_key: "{{ troshka_api_key }}"
        project_id: "{{ troshka_project_id }}"
        vm_id: "{{ troshka_bastion_vm_id }}"
      register: _bastion
      until: _bastion.ready
      retries: 30
      delay: 10
```

## Design Principles

1. **Modules are stateless** — each call is independent, no internal polling
2. **Waiting happens in Ansible** — `until/retries/delay` keeps AAP2 alive
3. **No roles** — modules are the interface, playbooks are the orchestration
4. **FQCNs everywhere** — `troshka.cloud.project`, not short names
5. **`module_utils` for shared code** — API client stays in one place
6. **`_info` suffix for read-only** — follows Ansible naming convention
7. **`state` parameter** — follows Ansible state machine convention

## Migration

1. Create new repo `troshka-cloud-collection` (or rename existing)
2. Update `galaxy.yml` to `namespace: troshka, name: cloud`
3. Create individual modules wrapping `troshka_api.py`
4. Delete all roles
5. Update agnosticd-v2 cloud provider + configs to use new FQCNs
6. Update connection plugin transport to `troshka.cloud.troshka`
7. Test with `test-agnosticd-template.sh`
