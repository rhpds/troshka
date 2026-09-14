import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import RunWorkloadModal from "@/components/canvas/RunWorkloadModal";
import { useCanvasStore } from "@/stores/canvasStore";

function okJson(data: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(data),
  } as Response);
}

function seedStore(clusters: Array<{ id: string; name: string }>, vmNodes: Array<{ id: string; name: string }>) {
  useCanvasStore.setState({
    nodes: vmNodes.map((vm) => ({
      id: vm.id,
      type: "vmNode",
      position: { x: 0, y: 0 },
      data: { name: vm.name },
    })),
    edges: [],
    clusters: clusters.map((c) => ({
      id: c.id,
      name: c.name,
      nodeId: `node-${c.id}`,
      type: "ocp",
      controlPlane: 1,
      workers: 0,
    })),
    selectedNodeId: null,
    projectState: "draft",
    deployedNodeData: {},
    deployedEdgeKey: "",
    deployedExternalIps: "[]",
    deployedClusters: "[]",
    externalIps: [],
  } as never);
}

describe("RunWorkloadModal", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  describe("cluster mode multi-select", () => {
    it("fan-outs to multiple POSTs when multiple clusters selected", async () => {
      seedStore(
        [
          { id: "c1", name: "hub" },
          { id: "c2", name: "sno1" },
        ],
        [],
      );

      const postBodies: any[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === "string" ? input : input.toString();
          if (url.endsWith("/workloads") && init?.method === "POST") {
            postBodies.push(JSON.parse(init!.body as string));
            return okJson({ id: `run-${postBodies.length}`, status: "pending" }, 202);
          }
          return okJson({});
        }),
      );

      const onLaunched = vi.fn();
      render(<RunWorkloadModal projectId="p1" onClose={() => {}} onLaunched={onLaunched} />);

      // Fill role FQCN
      fireEvent.change(screen.getByPlaceholderText(/role/i), {
        target: { value: "agnosticd.core.test" },
      });

      // Select both clusters
      fireEvent.click(screen.getByLabelText("hub"));
      fireEvent.click(screen.getByLabelText("sno1"));

      fireEvent.click(screen.getByRole("button", { name: /launch/i }));

      await waitFor(() => expect(onLaunched).toHaveBeenCalledWith(["run-1", "run-2"]));
      expect(postBodies).toHaveLength(2);
      expect(postBodies[0].target_map).toEqual({ mode: "cluster", cluster_id: "c1" });
      expect(postBodies[1].target_map).toEqual({ mode: "cluster", cluster_id: "c2" });
    });

    it("selects all clusters when 'All' checkbox clicked", async () => {
      seedStore(
        [
          { id: "c1", name: "hub" },
          { id: "c2", name: "sno1" },
          { id: "c3", name: "sno2" },
        ],
        [],
      );

      const postBodies: any[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === "string" ? input : input.toString();
          if (url.endsWith("/workloads") && init?.method === "POST") {
            postBodies.push(JSON.parse(init!.body as string));
            return okJson({ id: `run-${postBodies.length}` }, 202);
          }
          return okJson({});
        }),
      );

      const onLaunched = vi.fn();
      render(<RunWorkloadModal projectId="p1" onClose={() => {}} onLaunched={onLaunched} />);

      fireEvent.change(screen.getByPlaceholderText(/role/i), { target: { value: "test.role" } });

      // Click "All" checkbox
      fireEvent.click(screen.getByLabelText("All"));

      fireEvent.click(screen.getByRole("button", { name: /launch/i }));

      await waitFor(() => expect(onLaunched).toHaveBeenCalled());
      expect(postBodies).toHaveLength(3);
    });

    it("defaults to selecting single cluster when only one exists", async () => {
      seedStore([{ id: "c1", name: "solo" }], []);
      render(<RunWorkloadModal projectId="p1" onClose={() => {}} onLaunched={() => {}} />);
      await waitFor(() => {
        const checkbox = screen.getByLabelText("solo") as HTMLInputElement;
        expect(checkbox.checked).toBe(true);
      });
    });
  });

  describe("VMs mode multi-select", () => {
    it("sends ONE POST with vm_names array when VMs selected", async () => {
      seedStore(
        [],
        [
          { id: "vm-1", name: "web1" },
          { id: "vm-2", name: "web2" },
          { id: "vm-3", name: "db1" },
        ],
      );

      let postBody: any = null;
      vi.stubGlobal(
        "fetch",
        vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === "string" ? input : input.toString();
          if (url.endsWith("/workloads") && init?.method === "POST") {
            postBody = JSON.parse(init!.body as string);
            return okJson({ id: "run-vm-1" }, 202);
          }
          return okJson({});
        }),
      );

      const onLaunched = vi.fn();
      render(<RunWorkloadModal projectId="p1" onClose={() => {}} onLaunched={onLaunched} />);

      fireEvent.change(screen.getByPlaceholderText(/role/i), { target: { value: "test.role" } });

      // Switch to VMs mode
      fireEvent.click(screen.getByLabelText("VMs"));

      // Select two VMs
      fireEvent.click(screen.getByLabelText("web1"));
      fireEvent.click(screen.getByLabelText("db1"));

      fireEvent.click(screen.getByRole("button", { name: /launch/i }));

      await waitFor(() => expect(onLaunched).toHaveBeenCalledWith(["run-vm-1"]));
      expect(postBody.target_map).toEqual({ mode: "vms", vm_names: ["web1", "db1"] });
    });

    it("selects all VMs when 'All' checkbox clicked in VMs mode", async () => {
      seedStore(
        [],
        [
          { id: "vm-1", name: "app1" },
          { id: "vm-2", name: "app2" },
        ],
      );

      let postBody: any = null;
      vi.stubGlobal(
        "fetch",
        vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === "string" ? input : input.toString();
          if (url.endsWith("/workloads") && init?.method === "POST") {
            postBody = JSON.parse(init!.body as string);
            return okJson({ id: "run-all-vms" }, 202);
          }
          return okJson({});
        }),
      );

      const onLaunched = vi.fn();
      render(<RunWorkloadModal projectId="p1" onClose={() => {}} onLaunched={onLaunched} />);

      fireEvent.change(screen.getByPlaceholderText(/role/i), { target: { value: "test.role" } });
      fireEvent.click(screen.getByLabelText("VMs"));
      fireEvent.click(screen.getByLabelText("All"));

      fireEvent.click(screen.getByRole("button", { name: /launch/i }));

      await waitFor(() => expect(onLaunched).toHaveBeenCalled());
      expect(postBody.target_map.vm_names).toEqual(["app1", "app2"]);
    });
  });

  describe("extra variables", () => {
    it("includes extra_vars_text in POST body when textarea filled", async () => {
      seedStore([{ id: "c1", name: "hub" }], []);

      let postBody: any = null;
      vi.stubGlobal(
        "fetch",
        vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === "string" ? input : input.toString();
          if (url.endsWith("/workloads") && init?.method === "POST") {
            postBody = JSON.parse(init!.body as string);
            return okJson({ id: "run-with-vars" }, 202);
          }
          return okJson({});
        }),
      );

      const onLaunched = vi.fn();
      render(
        <RunWorkloadModal
          projectId="p1"
          onClose={() => {}}
          onLaunched={onLaunched}
          initialClusterIds={["c1"]}
        />,
      );

      fireEvent.change(screen.getByPlaceholderText(/role/i), { target: { value: "test.role" } });

      // Expand extra vars
      fireEvent.click(screen.getByRole("button", { name: /extra variables/i }));

      // Type YAML
      const textarea = screen.getByPlaceholderText(/key: value/i);
      fireEvent.change(textarea, { target: { value: "foo: bar\nbaz: 42" } });

      fireEvent.click(screen.getByRole("button", { name: /launch/i }));

      await waitFor(() => expect(onLaunched).toHaveBeenCalled());
      expect(postBody.extra_vars_text).toBe("foo: bar\nbaz: 42");
    });
  });

  describe("pre-seed props", () => {
    it("pre-seeds VMs mode with selected VMs", () => {
      seedStore(
        [],
        [
          { id: "vm-1", name: "web1" },
          { id: "vm-2", name: "web2" },
        ],
      );

      render(
        <RunWorkloadModal
          projectId="p1"
          onClose={() => {}}
          onLaunched={() => {}}
          initialMode="vms"
          initialVmNames={["web1"]}
        />,
      );

      // VMs mode should be active
      const vmsRadio = screen.getByLabelText("VMs") as HTMLInputElement;
      expect(vmsRadio.checked).toBe(true);

      // web1 should be checked
      const web1 = screen.getByLabelText("web1") as HTMLInputElement;
      expect(web1.checked).toBe(true);

      const web2 = screen.getByLabelText("web2") as HTMLInputElement;
      expect(web2.checked).toBe(false);
    });

    it("pre-seeds cluster mode with selected clusters", () => {
      seedStore(
        [
          { id: "c1", name: "hub" },
          { id: "c2", name: "sno1" },
        ],
        [],
      );

      render(
        <RunWorkloadModal
          projectId="p1"
          onClose={() => {}}
          onLaunched={() => {}}
          initialMode="cluster"
          initialClusterIds={["c2"]}
        />,
      );

      const clusterRadio = screen.getByLabelText("Cluster") as HTMLInputElement;
      expect(clusterRadio.checked).toBe(true);

      const sno1 = screen.getByLabelText("sno1") as HTMLInputElement;
      expect(sno1.checked).toBe(true);

      const hub = screen.getByLabelText("hub") as HTMLInputElement;
      expect(hub.checked).toBe(false);
    });
  });

  describe("validation", () => {
    it("disables Launch when no role_fqcn", () => {
      seedStore([{ id: "c1", name: "hub" }], []);
      render(<RunWorkloadModal projectId="p1" onClose={() => {}} onLaunched={() => {}} />);
      fireEvent.click(screen.getByLabelText("hub"));
      expect(screen.getByRole("button", { name: /launch/i })).toBeDisabled();
    });

    it("disables Launch when cluster mode but no clusters selected", () => {
      seedStore([{ id: "c1", name: "hub" }], []);
      render(
        <RunWorkloadModal
          projectId="p1"
          onClose={() => {}}
          onLaunched={() => {}}
          initialClusterIds={[]}
        />,
      );
      fireEvent.change(screen.getByPlaceholderText(/role/i), { target: { value: "test.role" } });
      // Don't select any cluster
      expect(screen.getByRole("button", { name: /launch/i })).toBeDisabled();
    });

    it("disables Launch when VMs mode but no VMs selected", () => {
      seedStore([], [{ id: "vm-1", name: "web1" }]);
      render(<RunWorkloadModal projectId="p1" onClose={() => {}} onLaunched={() => {}} />);
      fireEvent.change(screen.getByPlaceholderText(/role/i), { target: { value: "test.role" } });
      fireEvent.click(screen.getByLabelText("VMs"));
      // Don't select any VM
      expect(screen.getByRole("button", { name: /launch/i })).toBeDisabled();
    });
  });

  describe("requirements and EE image passthrough", () => {
    it("includes requirements_content when collections added", async () => {
      seedStore([{ id: "c1", name: "hub" }], []);

      let postBody: any = null;
      vi.stubGlobal(
        "fetch",
        vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === "string" ? input : input.toString();
          if (url.endsWith("/workloads") && init?.method === "POST") {
            postBody = JSON.parse(init!.body as string);
            return okJson({ id: "run-req" }, 202);
          }
          return okJson({});
        }),
      );

      const onLaunched = vi.fn();
      render(
        <RunWorkloadModal
          projectId="p1"
          onClose={() => {}}
          onLaunched={onLaunched}
          initialClusterIds={["c1"]}
        />,
      );

      fireEvent.change(screen.getByPlaceholderText(/role/i), { target: { value: "test.role" } });

      // Add requirement
      fireEvent.click(screen.getByRole("button", { name: /add requirement/i }));
      const gitInput = screen.getByPlaceholderText("git URL");
      fireEvent.change(gitInput, { target: { value: "https://github.com/redhat-cop/agnosticd" } });
      const versionInput = screen.getByPlaceholderText("version");
      fireEvent.change(versionInput, { target: { value: "development" } });

      fireEvent.click(screen.getByRole("button", { name: /launch/i }));

      await waitFor(() => expect(onLaunched).toHaveBeenCalled());
      expect(postBody.requirements_content).toEqual({
        collections: [{ name: "https://github.com/redhat-cop/agnosticd", type: "git", version: "development" }],
      });
    });

    it("includes ee_image when set", async () => {
      seedStore([{ id: "c1", name: "hub" }], []);

      let postBody: any = null;
      vi.stubGlobal(
        "fetch",
        vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === "string" ? input : input.toString();
          if (url.endsWith("/workloads") && init?.method === "POST") {
            postBody = JSON.parse(init!.body as string);
            return okJson({ id: "run-ee" }, 202);
          }
          return okJson({});
        }),
      );

      const onLaunched = vi.fn();
      render(
        <RunWorkloadModal
          projectId="p1"
          onClose={() => {}}
          onLaunched={onLaunched}
          initialClusterIds={["c1"]}
        />,
      );

      fireEvent.change(screen.getByPlaceholderText(/role/i), { target: { value: "test.role" } });
      fireEvent.change(screen.getByPlaceholderText(/defaults to server config/i), {
        target: { value: "quay.io/custom/ee:latest" },
      });

      fireEvent.click(screen.getByRole("button", { name: /launch/i }));

      await waitFor(() => expect(onLaunched).toHaveBeenCalled());
      expect(postBody.ee_image).toBe("quay.io/custom/ee:latest");
    });
  });
});
