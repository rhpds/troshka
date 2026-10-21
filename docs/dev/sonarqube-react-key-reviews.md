# S6479 review of snapshot 6

All locations below refer to the immutable scan source in `/private/tmp/troshka-sonar-snapshot-round6/`, with IDs from `/private/tmp/troshka-main-snapshot6-issues.json`. These are individually reviewed decisions, not false-positive classifications. Accepted findings retain their rules for other call sites.

## Fixed (14)

- b9064e87-7905-491b-9f7a-9e74ded69f2c: PropertiesPanel.tsx:2385, route-managed forward: UI row identity shared with editable forwards.
- e97a906d-96c7-429a-95b3-8baf9522cdbd: PropertiesPanel.tsx:2325, showroom-managed forward: same stable UI row identity.
- bac853c9-2006-4c89-9ad3-e6fa7f705d65: PropertiesPanel.tsx:2469, editable forward: WeakMap identity survives deleting another rule and explicit saved/picker replacements. Native listeners replaced by React event handlers using current saved values. Regression checks unsaved values, DOM identity and correct saved rule after deleting the preceding row.
- 2b59de81-27ad-4eb0-ae17-5504e7ccaa46: RunWorkloadModal.tsx:247: ephemeral requirement rows receive UUIDs when added, excluded from API payload. Regression checks DOM identity and focus after removing the preceding row.
- d0cb056e-3f13-4510-87eb-9219ac1ee9ae: PropertiesPanel.tsx:3024: generated read-only proxy hosts keyed by hostname.
- be5a863a-25fe-4418-8fb1-97938ee6f4e7: PropertiesPanel.tsx:2228: generated cluster DNS records keyed by hostname within cluster.
- 370321db-4719-4888-89f3-e916f12adcea: admin/hosts/page.tsx:1203: warning keyed by mount and message, allowing separate non-storage warnings for a mount.
- d8afa6d9-018e-4d31-97d1-7111a36b6cd3: Palette.tsx:1100: password rows carry and use VM node ID, independent of mutable VM name or password.
- 22adf5f5-e5f0-4fab-a5f9-295e79b06542: NetworkNode.tsx:468: frontend port display uses join instead of rendering a list of spans.
- ec2e6617-e13d-41fe-ae0e-9a2fc0acc5de: PropertiesPanel.tsx:3700: live boot device rows keyed by boot device type.
- 3dd626dd-5bda-4b92-bf01-50388a0b2385: console/page.tsx:612: fixed macro buttons keyed by unique macro label.
- beb03d07-df4a-4358-8762-c1f077fd5a7e: PropertiesPanel.tsx:1422: boot-order rows keyed by storage/network device ID.
- 91473d2f-ed1e-43ad-9068-0dee3a56e40c: PropertiesPanel.tsx:2167: DHCP errors keyed by distinct validation message.
- 2bd0fb44-3455-4533-808c-006067badb37: PropertiesPanel.tsx:2603: forwarding errors keyed by distinct validation message.

## Accepted individually (18)

- 6488cc5c-e125-479c-be1d-23442cb78f26 — PropertiesPanel.tsx:5077, user DNS records. The name, record type and address are controlled values updated by array position. Blank and duplicate in-progress names are supported; name-based keys would remount the focused editor on every keystroke. Records have no immutable ID. Accept positional editor identity; deletion may shift focus, but displayed/saved values stay controlled.
- 9478754b-0861-47d4-9638-754f05e3a839 — PropertiesPanel.tsx:677, role disk specifications. `displayDisks` consists of size/bus/bootable specifications, with all controls controlled and all fields editable. Equal disk specifications are valid. No disk identifier exists in this role template schema. Accept positional identity rather than introducing a persisted schema solely for React keys.
- aee78dd1-3c3b-4327-be43-b8054c64143f — PropertiesPanel.tsx:3039, editable proxy host strings. Input `value={h}` is controlled and mutation targets `hi`; empty strings and duplicate in-progress strings have no identity. Hostname keys would drop focus during typing. Accept positional editor identity.
- d3088347-39d3-48b1-b5d2-097c881fe520 — PropertiesPanel.tsx:4569, pod container details. Persisted containers have no immutable ID; names and all input fields are editable controlled values. Explicit tradeoff: removing a preceding container can transfer the native details expanded/collapsed state to the next container. Data and saved field values still follow controlled props. Accept this low-impact expansion-state shift; do not claim it is a false positive or fully controlled details state.
- 961fdb3b-7988-4b46-a56c-91515ab3ec51 — PropertiesPanel.tsx:4428, init container details. Same specific tradeoff independently applies: no persistent identity, editable name and controlled data fields, native expanded state can move after preceding deletion. Accept the expansion-state-only risk rather than remounting on every name keystroke or changing topology schema.
- 7f17f2e7-a5fb-4a82-9a7f-9be30c6d6b95 — PropertiesPanel.tsx:4469, init-container environment entries. Controlled key/value pair, no immutable ID, empty/duplicate keys while editing. Accept positional key to preserve typing; deletion updates the controlled values.
- 9184db98-142b-49f6-8fe1-99083f5bff47 — PropertiesPanel.tsx:4505, init-container mounts. Editable controlled paths and mount options have no ID; duplicate/blank paths can occur while editing. Accept positional identity, avoiding remount on path edits.
- 16b8ffe0-b04a-485b-9ed3-378e2fabb73c — PropertiesPanel.tsx:4703, pod-container environment entries. Controlled editable key/value fields, no independent ID. Accept positional key rather than mutable environment-name identity.
- 48a5f2f8-e7a6-4662-9481-4f6126054305 — PropertiesPanel.tsx:4739, pod-container ports. Controlled editable host/container port and protocol values; partial or duplicate rows are possible and no stable ID exists. Accept positional editor identity.
- f1fe884f-ddb6-4e2f-9a6c-7c6b55978e51 — PropertiesPanel.tsx:4777, pod-container mounts. Controlled editable mount fields, no immutable ID. Accept positional identity to preserve path editing without introducing topology IDs.
- 0c2d7e50-19d4-4d35-99a1-58db489bde9d — PropertiesPanel.tsx:4238, single-container environment entries. Both key/value controls follow array state; names are mutable and blank/duplicate keys are permitted during editing. Accept positional identity; name-based keys would interrupt typing.
- d2ad7b1f-aaf3-45fb-a9f1-9f7af05752aa — PropertiesPanel.tsx:4282, single-container ports. Controlled editable values, no ID and possible duplicate/partial tuples. Accept positional identity rather than mutable tuple keys.
- 5def2d43-0f45-4c07-89be-05697db98519 — PropertiesPanel.tsx:5183, load-balancer frontends. Editable controlled bind port and backend fields, no independent ID. Accept positional identity because port-based keys would remount a focused port input on each digit.
- 99d494bd-2623-4917-8796-5b2fae131399 — PropertiesPanel.tsx:5251, load-balancer DNS templates. Controlled editable record name/type/value fields without immutable identity. Accept positional identity; template-name keys would remount on typing and collide for blank rows.
- eabcf629-5b37-4c2c-b4d8-535f926aad33 — PropertiesPanel.tsx:2250, static routes. Controlled destination and next-hop fields; neither is immutable or unique during editing. Accept positional identity instead of remounting on route text changes.
- a7eab3bc-79d3-481c-9a4f-d0aac1f3990e — NetworkNode.tsx:233, forwarding display table. Read-only rendered external/internal mapping text and links, no per-row input or React component state. Forward rules have no immutable identifier and duplicates are representable. Accept positional presentation rows; current values are rendered each pass.
- 49c92ccd-0eb5-4b65-bf60-a3038f62026e — Palette.tsx:605, project description split into plain text divs. Rows are presentation-only children of the single description editor trigger; repeated text fragments are valid. No row input or local state exists. Accept positional text layout.
- 8107806d-c64b-460f-920a-37dcbddf28b5 — BulkDeployModal.tsx:169, generated name preview. `previewNames` is a bounded three-name plus ellipsis positional preview, with no per-row interaction/state. A template without `{n}` legitimately produces repeated names while editing, so name keys would collide. Accept positional preview identity.
