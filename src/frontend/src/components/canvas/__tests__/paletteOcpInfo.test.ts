import { describe, it, expect } from "vitest";
import { buildPaletteOcpInfo } from "@/components/canvas/paletteOcpInfo";

describe("buildPaletteOcpInfo", () => {
  it("is hidden when there are no cluster nodes", () => {
    const info = buildPaletteOcpInfo(
      [{ id: "vm1", type: "vmNode", data: { name: "bastion" } }],
      [],
    );
    expect(info.ocpPresent).toBe(false);
    expect(info.clusters).toEqual([]);
    expect(info.showroomUrl).toBeNull();
  });

  it("lists each cluster with API/console URLs and member credentials", () => {
    const info = buildPaletteOcpInfo(
      [
        {
          id: "cluster-ocp",
          type: "clusterNode",
          data: { clusterId: "ocp", name: "ocp" },
        },
        {
          id: "cp-0",
          type: "vmNode",
          data: {
            clusterId: "ocp",
            name: "cp-0",
            ocpKubeadminPassword: "kubeadmin-test",
            ocpKubeconfig: "apiVersion: v1\nkind: Config\n",
          },
        },
      ],
      [{ id: "ocp", name: "ocp", baseDomain: "lab.local" }],
    );
    expect(info.ocpPresent).toBe(true);
    expect(info.clusters).toHaveLength(1);
    expect(info.clusters[0]).toMatchObject({
      id: "ocp",
      name: "ocp",
      apiUrl: "https://api.ocp.lab.local:6443",
      consoleUrl: "https://console-openshift-console.apps.ocp.lab.local",
      consoleViaShowroom: false,
      kubeadminPassword: "kubeadmin-test",
      kubeconfig: "apiVersion: v1\nkind: Config\n",
      kubeconfigVmName: "cp-0",
    });
  });

  it("prefers the showroom app-proxy console URL when showroom is deployed", () => {
    const info = buildPaletteOcpInfo(
      [
        {
          id: "cluster-ocp",
          type: "clusterNode",
          data: { clusterId: "ocp", name: "ocp" },
        },
        {
          id: "showroom",
          type: "containerNode",
          data: {
            isShowroom: true,
            name: "showroom",
            showroomTabs: [
              {
                id: "t1",
                clusterId: "ocp",
                type: "proxy",
                proxyHosts: [
                  "console-openshift-console.apps.ocp.local",
                  "oauth-openshift.apps.ocp.local",
                ],
              },
            ],
          },
        },
        {
          id: "gw",
          type: "networkNode",
          data: {
            subtype: "gateway",
            externalEndpoints: [
              {
                vmName: "showroom",
                hostname: "showroom-troshka-1e559f8a.apps.ocpv06.dal10.infra.demo.redhat.com",
                port: 443,
              },
            ],
          },
        },
      ],
      [{ id: "ocp", name: "ocp", baseDomain: "local" }],
      {
        projectId: "6fcf0e3e-aaaa-bbbb",
        deployed: {
          _showroom_url: "https://showroom-troshka-1e559f8a.apps.ocpv06.dal10.infra.demo.redhat.com",
          _showroom_access_token: "tok",
          nodes: [],
        },
      },
    );
    expect(info.showroomUrl).toContain("token=tok");
    expect(info.clusters[0].consoleViaShowroom).toBe(true);
    expect(info.clusters[0].consoleUrl).toBe(
      "https://tpf-6fcf0e3e-con-ocp-troshka-1e559f8a.apps.ocpv06.dal10.infra.demo.redhat.com/?token=tok",
    );
  });

  it("still shows the section for a draft cluster with empty credentials", () => {
    const info = buildPaletteOcpInfo(
      [
        {
          id: "cluster-ocp",
          type: "clusterNode",
          data: { clusterId: "ocp", name: "ocp" },
        },
      ],
      [{ id: "ocp", name: "ocp", baseDomain: "local" }],
    );
    expect(info.ocpPresent).toBe(true);
    expect(info.clusters[0].kubeadminPassword).toBe("");
    expect(info.clusters[0].kubeconfig).toBe("");
  });
});
