import { describe, expect, it } from "vitest";
import { providerCreateBody, providerEditBody, providerFormError, providerTestMessage, type ProviderForm, type ProviderEditForm } from "./providerForms";

const form: ProviderForm = {
  name: "Example", type: "kubevirt", region: "us-east-1", gcpProjectId: "project", serviceAccountJson: "{}",
  azureTenantId: "tenant", azureClientId: "client", azureClientSecret: "example", azureSubscriptionId: "subscription", azureLocation: "",
  apiUrl: "https://cluster.example.com", token: "example", namespace: "operator", verifySsl: true,
  cacheNamespace: "", projectPrefix: "", pkgRepoUrl: "", pkgRepoUsername: "", pkgRepoPassword: "",
  accessKey: "example", secretKey: "example", s3Bucket: "images", useCustomEndpoint: false, endpointUrl: "",
};
const edit: ProviderEditForm = {
  type: "kubevirt", name: "", region: "", accessKey: "", secretKey: "", endpointUrl: "",
  cacheNamespace: "", projectPrefix: "", pkgRepoUrl: "", pkgRepoUsername: "", pkgRepoPassword: "",
};

describe("provider creation", () => {
  it.each(["gcp", "azure", "ocpvirt", "kubevirt", "aws", "s3", "s3_readonly"])("accepts complete %s credentials", type => {
    expect(providerFormError({ ...form, type })).toBe("");
  });
  it.each([
    ["gcp", "gcpProjectId", "GCP Project ID"], ["gcp", "serviceAccountJson", "Service Account JSON"],
    ["azure", "azureTenantId", "Tenant ID"], ["azure", "azureClientId", "Client ID"],
    ["azure", "azureClientSecret", "Client Secret"], ["azure", "azureSubscriptionId", "Subscription ID"],
    ["ocpvirt", "apiUrl", "API URL"], ["kubevirt", "token", "Token"],
    ["aws", "accessKey", "All fields"], ["s3", "secretKey", "All fields"],
    ["kubevirt", "name", "Name"],
  ])("rejects missing %s %s", (type, field, message) => {
    expect(providerFormError({ ...form, type, [field]: "  " })).toContain(message);
  });
  it("creates cluster providers with explicit TLS verification and optional namespaces", () => {
    expect(providerCreateBody({ ...form, cacheNamespace: "cache", projectPrefix: "lab-", verifySsl: false })).toEqual({
      name: "Example", type: "kubevirt", api_url: form.apiUrl, token: form.token,
      namespace: "operator", verify_ssl: false, cache_namespace: "cache", project_prefix: "lab-",
    });
    expect(providerCreateBody(form)).not.toHaveProperty("cache_namespace");
  });
  it("only includes package credentials when an OCP package repository is configured", () => {
    const configured = { ...form, type: "ocpvirt", pkgRepoUrl: "https://packages.example.com", pkgRepoUsername: "reader", pkgRepoPassword: "example" };
    expect(providerCreateBody(configured)).toMatchObject({ pkg_repo_url: configured.pkgRepoUrl, pkg_repo_username: "reader", pkg_repo_password: "example" });
    expect(providerCreateBody({ ...configured, pkgRepoUrl: "" })).not.toHaveProperty("pkg_repo_password");
    expect(providerCreateBody({ ...configured, type: "kubevirt" })).not.toHaveProperty("pkg_repo_url");
  });
  it("uses cloud-specific credentials without leaking credentials for another provider", () => {
    expect(providerCreateBody({ ...form, type: "gcp" })).toEqual({ name: "Example", type: "gcp", default_region: "us-east-1", gcp_project_id: "project", service_account_json: "{}" });
    const azure = providerCreateBody({ ...form, type: "azure" });
    expect(azure.azure_location).toBe("us-east-1");
    expect(azure).not.toHaveProperty("token");
    expect(providerCreateBody({ ...form, type: "azure", azureLocation: "eastus" }).azure_location).toBe("eastus");
  });
  it.each(["s3", "s3_readonly"])("includes %s bucket and an enabled custom endpoint", type => {
    expect(providerCreateBody({ ...form, type, useCustomEndpoint: true, endpointUrl: "https://s3.example.com" })).toMatchObject({ bucket: "images", endpoint_url: "https://s3.example.com" });
  });
  it("does not send a disabled endpoint or S3 fields to AWS", () => {
    const body = providerCreateBody({ ...form, type: "aws", endpointUrl: "https://s3.example.com" });
    expect(body).not.toHaveProperty("endpoint_url");
    expect(body).not.toHaveProperty("bucket");
  });
});

describe("provider edits", () => {
  it("preserves stored credentials when fields are blank", () => {
    expect(providerEditBody(edit)).toEqual({});
    expect(providerEditBody({ ...edit, type: "aws" })).toEqual({ endpoint_url: "" });
    expect(providerEditBody({ ...edit, type: "aws", endpointUrl: undefined })).toEqual({});
  });
  it("maps only entered cluster fields", () => {
    expect(providerEditBody({ ...edit, name: "Renamed", region: "operator", accessKey: form.apiUrl, secretKey: "example", cacheNamespace: "cache", projectPrefix: "lab-" })).toEqual({ name: "Renamed", namespace: "operator", api_url: form.apiUrl, token: "example", cache_namespace: "cache", project_prefix: "lab-" });
  });
  it("allows OCP package fields to be cleared but preserves a blank package password", () => {
    expect(providerEditBody({ ...edit, type: "ocpvirt" })).toEqual({ pkg_repo_url: "", pkg_repo_username: "" });
    expect(providerEditBody({ ...edit, type: "ocpvirt", pkgRepoPassword: "example" })).toHaveProperty("pkg_repo_password", "example");
  });
  it("maps cloud region and entered replacement credentials", () => {
    expect(providerEditBody({ ...edit, type: "aws", region: "us-west-2", accessKey: "new-example", secretKey: "new-example", endpointUrl: undefined })).toEqual({ default_region: "us-west-2", access_key_id: "new-example", secret_access_key: "new-example" });
  });
});

describe("provider diagnostics", () => {
  it("prefers the server message, then bucket, then provider details", () => {
    expect(providerTestMessage({ message: "Ready", bucket: "images" })).toBe("Ready");
    expect(providerTestMessage({ bucket: "images" })).toBe("OK — Bucket: images");
    expect(providerTestMessage({ nodes: 0, namespace: "lab" })).toBe("OK — lab namespace, 0 nodes");
    expect(providerTestMessage({ account: "account" })).toBe("OK — Account: account");
  });
  it("reports namespace details and no warning for an installed operator", () => {
    expect(providerTestMessage({ operator: "running", nodes: 3, crds_installed: true, namespaces: { cache: "images" } })).toBe("OK — 3 nodes, operator: running, CRDs: installed, cache namespace: images");
  });
  it("prioritizes permissions warnings over installation advice", () => {
    const message = providerTestMessage({ operator: "not installed", crds: "no permission", crds_installed: false });
    expect(message).toContain("update the ClusterRole");
    expect(message).not.toContain("click Install Operator");
    expect(providerTestMessage({ operator: "not installed" })).toContain("click Install Operator");
    expect(providerTestMessage({ operator: "running", crds_installed: false })).toContain("CRDs: missing");
  });
});
