/** Lightweight import checks; the backend validates the complete template schema. */
export function templateImportError(value: unknown): string | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return "Template must be a YAML mapping";
  }
  const template = value as Record<string, unknown>;
  if (!template.networks || (!template.vms && !template.ocp)) {
    return "Template must contain 'networks' and either 'vms' or 'ocp' sections";
  }
  return null;
}

export const TEMPLATE_YAML_EXAMPLE = `networks:
  cluster:
    cidr: 10.0.0.0/24
    dhcp: true
ocp:
  - name: ocp
    type: sno
    nodes:
      cp-0:
        role: control-plane
        nics:
          - network: cluster
            ip: 10.0.0.10`;
