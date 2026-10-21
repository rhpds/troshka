export interface ProviderForm {
  name: string; type: string; region: string;
  gcpProjectId: string; serviceAccountJson: string;
  azureTenantId: string; azureClientId: string; azureClientSecret: string;
  azureSubscriptionId: string; azureLocation: string;
  apiUrl: string; token: string; namespace: string; verifySsl: boolean;
  cacheNamespace: string; projectPrefix: string;
  pkgRepoUrl: string; pkgRepoUsername: string; pkgRepoPassword: string;
  accessKey: string; secretKey: string; s3Bucket: string;
  useCustomEndpoint: boolean; endpointUrl: string;
}

export function isClusterProvider(type?: string): boolean {
  return type === "ocpvirt" || type === "kubevirt";
}

export function providerFormError(form: ProviderForm): string {
  let required = [form.name, form.accessKey, form.secretKey];
  let message = "All fields are required";
  if (form.type === "gcp") {
    required = [form.name, form.gcpProjectId, form.serviceAccountJson];
    message = "Name, GCP Project ID, and Service Account JSON are required";
  } else if (form.type === "azure") {
    required = [form.name, form.azureTenantId, form.azureClientId, form.azureClientSecret, form.azureSubscriptionId];
    message = "Name, Tenant ID, Client ID, Client Secret, and Subscription ID are required";
  } else if (isClusterProvider(form.type)) {
    required = [form.name, form.apiUrl, form.token];
    message = "Name, API URL, and Token are required";
  }
  return required.some(value => !value.trim()) ? message : "";
}

function populatedFields(fields: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(fields).filter(([, value]) => !!value));
}

function clusterProviderBody(form: ProviderForm) {
  const body: Record<string, string | boolean> = {
    name: form.name, type: form.type, api_url: form.apiUrl, token: form.token,
    namespace: form.namespace, verify_ssl: form.verifySsl,
  };
  if (form.type === "kubevirt") {
    Object.assign(body, populatedFields({ cache_namespace: form.cacheNamespace, project_prefix: form.projectPrefix }));
  }
  if (form.type === "ocpvirt" && form.pkgRepoUrl) {
    Object.assign(body, populatedFields({ pkg_repo_url: form.pkgRepoUrl, pkg_repo_username: form.pkgRepoUsername, pkg_repo_password: form.pkgRepoPassword }));
  }
  return body;
}

export function providerCreateBody(form: ProviderForm): Record<string, string | boolean> {
  if (isClusterProvider(form.type)) return clusterProviderBody(form);
  const common = { name: form.name, type: form.type, default_region: form.region };
  if (form.type === "gcp") return { ...common, gcp_project_id: form.gcpProjectId, service_account_json: form.serviceAccountJson };
  if (form.type === "azure") return {
    ...common, azure_tenant_id: form.azureTenantId, azure_client_id: form.azureClientId,
    azure_client_secret: form.azureClientSecret, azure_subscription_id: form.azureSubscriptionId,
    azure_location: form.azureLocation || form.region,
  };
  const body = { ...common, access_key_id: form.accessKey, secret_access_key: form.secretKey } as Record<string, string>;
  if (form.type === "s3" || form.type === "s3_readonly") body.bucket = form.s3Bucket;
  if (form.useCustomEndpoint && form.endpointUrl) body.endpoint_url = form.endpointUrl;
  return body;
}

export interface ProviderEditForm {
  type?: string; name: string; region: string; accessKey: string; secretKey: string;
  endpointUrl: string | undefined; cacheNamespace: string; projectPrefix: string;
  pkgRepoUrl: string; pkgRepoUsername: string; pkgRepoPassword: string;
}

export function providerEditBody(form: ProviderEditForm): Record<string, string> {
  const body = populatedFields({ name: form.name });
  if (isClusterProvider(form.type)) {
    Object.assign(body, populatedFields({ api_url: form.accessKey, token: form.secretKey,
      namespace: form.region, cache_namespace: form.cacheNamespace, project_prefix: form.projectPrefix }));
    if (form.type === "ocpvirt") {
      body.pkg_repo_url = form.pkgRepoUrl;
      body.pkg_repo_username = form.pkgRepoUsername;
      Object.assign(body, populatedFields({ pkg_repo_password: form.pkgRepoPassword }));
    }
  } else {
    Object.assign(body, populatedFields({ default_region: form.region, access_key_id: form.accessKey, secret_access_key: form.secretKey }));
    if (form.endpointUrl !== undefined) body.endpoint_url = form.endpointUrl;
  }
  return body;
}

interface ProviderTestResult {
  message?: string; bucket?: string; operator?: string; crds_installed?: boolean;
  crds?: string; nodes?: number; namespace?: string; account?: string;
  namespaces?: Record<string, string>;
}

function operatorWarning(data: ProviderTestResult): string {
  if (data.crds?.includes("no permission")) return "\n⚠ Service account lacks permissions to manage CRDs — update the ClusterRole";
  if (data.operator === "not installed" || !data.crds_installed) return "\n⚠ Operator not installed — click Install Operator below";
  return "";
}

export function providerTestMessage(data: ProviderTestResult): string {
  if (data.message) return data.message;
  if (data.bucket) return `OK — Bucket: ${data.bucket}`;
  if (data.operator !== undefined) {
    const namespaces = Object.entries(data.namespaces || {}).map(([key, value]) => `${key} namespace: ${value}`).join(", ");
    const nsInfo = data.namespaces ? ", " + namespaces : "";
    const crds = data.crds || (data.crds_installed ? "installed" : "missing");
    return `OK — ${data.nodes} nodes, operator: ${data.operator}, CRDs: ${crds}${nsInfo}${operatorWarning(data)}`;
  }
  if (data.nodes !== undefined) return `OK — ${data.namespace} namespace, ${data.nodes} nodes`;
  return `OK — Account: ${data.account}`;
}
