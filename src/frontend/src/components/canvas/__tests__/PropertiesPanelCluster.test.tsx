import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Node } from "@xyflow/react";
import { useCanvasStore } from "@/stores/canvasStore";
import PropertiesPanel from "@/components/canvas/PropertiesPanel";

beforeEach(() => {
  useCanvasStore.setState({
    nodes: [
      {
        id: "cluster-prod",
        type: "clusterNode",
        position: { x: 0, y: 0 },
        data: {
          name: "prod",
          clusterId: "prod",
          type: "standard",
          controlPlane: 3,
          workers: 2,
          baseDomain: "ocp.local",
          apiVip: "",
          ingressVip: "",
        },
      },
    ],
    edges: [],
    selectedNodeId: "cluster-prod",
    projectState: "draft",
    clusters: [
      {
        id: "prod",
        nodeId: "cluster-prod",
        name: "prod",
        type: "standard",
        controlPlane: 3,
        workers: 2,
        baseDomain: "ocp.local",
      },
    ],
    // Not deployed — keep dirty comparison inert.
    deployedNodeData: {},
    deployedEdgeKey: "",
    deployedExternalIps: "[]",
    deployedClusters: "[]",
    externalIps: [],
  } as never);
});

describe("PropertiesPanel cluster editor", () => {
  it("displays and persists fractional memory without changing integer count inputs", () => {
    useCanvasStore.setState({ clusters: [{ ...useCanvasStore.getState().clusters[0], controlPlaneMemory: 33280, workerMemory: 12800 }] });
    render(<PropertiesPanel />);
    const cpMemory = screen.getByLabelText("Control Plane Memory (GB)");
    const workerMemory = screen.getByLabelText("Worker Memory (GB)");
    expect(cpMemory).toHaveValue(32.5);
    expect(workerMemory).toHaveValue(12.5);
    fireEvent.change(cpMemory, { target: { value: "33.25" } });
    fireEvent.change(workerMemory, { target: { value: "13.25" } });
    expect(useCanvasStore.getState().clusters[0].controlPlaneMemory).toBe(34048);
    expect(useCanvasStore.getState().clusters[0].workerMemory).toBe(13568);
    expect(cpMemory).toHaveAttribute("min", "32");
    expect(workerMemory).toHaveAttribute("min", "1");
    expect(screen.getByLabelText(/^workers$/i)).not.toHaveAttribute("step", "any");
  });

  it("shows preset OCP mount paths for control-plane and worker disks", () => {
    const cluster = useCanvasStore.getState().clusters[0];
    useCanvasStore.setState({ clusters: [{ ...cluster,
      controlPlaneDisks: [{ sizeGb: 120, bootable: true }, { sizeGb: 250, ocpMount: "/var/lib/containers" }],
      workerDisks: [{ sizeGb: 120, bootable: true }, { sizeGb: 250, ocpMount: "/var/lib/containers" }],
    }] });
    render(<PropertiesPanel />);
    const mounts = screen.getAllByRole("textbox", { name: "OCP mount path" });
    expect(mounts).toHaveLength(4);
    expect(mounts[1]).toHaveValue("/var/lib/containers");
    expect(mounts[3]).toHaveValue("/var/lib/containers");
    expect(mounts[0]).toHaveAttribute("placeholder", "/var/lib/containers");
  });

  it("persists edited role mount paths and clears blank values", () => {
    const cluster = useCanvasStore.getState().clusters[0];
    useCanvasStore.setState({ clusters: [{ ...cluster,
      controlPlaneDisks: [{ sizeGb: 120, bootable: true }, { sizeGb: 250, ocpMount: "/var/lib/containers" }],
      workerDisks: [{ sizeGb: 120, bootable: true }, { sizeGb: 250, ocpMount: "/var/lib/containers" }],
    }] });
    render(<PropertiesPanel />);
    const mounts = screen.getAllByRole("textbox", { name: "OCP mount path" });
    fireEvent.change(mounts[1], { target: { value: "/srv/containers" } });
    expect(useCanvasStore.getState().clusters[0].controlPlaneDisks![1].ocpMount).toBe("/srv/containers");
    fireEvent.change(mounts[3], { target: { value: "" } });
    expect(useCanvasStore.getState().clusters[0].workerDisks![1].ocpMount).toBeUndefined();
    const saved = JSON.parse(JSON.stringify(useCanvasStore.getState().clusters[0]));
    expect(saved.controlPlaneDisks[1].ocpMount).toBe("/srv/containers");
    expect(saved.workerDisks[1]).not.toHaveProperty("ocpMount");
  });

  it("shows, edits and clears an individual disk OCP mount path", () => {
    useCanvasStore.setState({ selectedNodeId: "disk", nodes: [{ id: "disk", type: "storageNode", position: { x: 0, y: 0 },
      data: { name: "container-storage", label: "container-storage", format: "qcow2", size: 250, bootable: false, ocpMount: "/var/lib/containers" },
    }] });
    render(<PropertiesPanel />);
    const mount = screen.getByRole("textbox", { name: "OCP mount path" });
    expect(mount).toHaveValue("/var/lib/containers");
    fireEvent.change(mount, { target: { value: "/srv/containers" } });
    expect(useCanvasStore.getState().nodes[0].data.ocpMount).toBe("/srv/containers");
    fireEvent.change(mount, { target: { value: "" } });
    expect(useCanvasStore.getState().nodes[0].data.ocpMount).toBeUndefined();
    expect(screen.getByText(/first boot.*next OpenShift install/i)).toBeInTheDocument();
  });

  it("protects role boot disks and requires clearing a mount before making a disk bootable", () => {
    const cluster = useCanvasStore.getState().clusters[0];
    useCanvasStore.setState({ clusters: [{ ...cluster, workers: 0,
      controlPlaneDisks: [{ sizeGb: 120, bootable: true }, { sizeGb: 250, ocpMount: "/var/lib/containers" }],
    }] });
    render(<PropertiesPanel />);
    const mounts = screen.getAllByRole("textbox", { name: "OCP mount path" });
    const boot = screen.getAllByRole("checkbox", { name: "Boot" });
    expect(mounts[0]).toBeDisabled();
    expect(mounts[0]).toHaveAccessibleDescription(/boot disks cannot/i);
    expect(boot[1]).toBeDisabled();
    fireEvent.change(mounts[1], { target: { value: "" } });
    expect(boot[1]).toBeEnabled();
    fireEvent.click(boot[1]);
    expect(mounts[1]).toBeDisabled();
    expect(mounts[0]).toBeEnabled();
    expect(useCanvasStore.getState().clusters[0].controlPlaneDisks![1]).toMatchObject({ bootable: true });
    expect(useCanvasStore.getState().clusters[0].controlPlaneDisks![1].ocpMount).toBeUndefined();
  });

  it.each(["bootDevices", "controller"])("protects individual disks selected by connected VM %s", (source) => {
    useCanvasStore.setState({ selectedNodeId: "root-disk", nodes: [
      { id: "vm", type: "vmNode", position: { x: 0, y: 0 }, data: {
        name: "vm", bootDevices: source === "bootDevices" ? ["root-disk"] : [],
        diskControllers: [{ id: "root", name: "disk0", bus: "virtio", bootable: source === "controller" }],
      } },
      { id: "root-disk", type: "storageNode", position: { x: 0, y: 0 }, data: { name: "root", format: "qcow2", size: 120, bootable: false } },
    ], edges: [{ id: "disk-edge", source: "root-disk", target: "vm", targetHandle: "dp-root-left" }] });
    render(<PropertiesPanel />);
    expect(screen.getByRole("textbox", { name: "OCP mount path" })).toBeDisabled();
  });

  it.each([["root-disk", true], ["data-disk", false]])("uses the first actual attached disk fallback for %s when bootDevices is empty", (selectedId, disabled) => {
    useCanvasStore.setState({ selectedNodeId: selectedId, nodes: [
      { id: "vm", type: "vmNode", position: { x: 0, y: 0 }, data: { name: "vm", bootDevices: [],
        diskControllers: [{ id: "data", name: "disk1", bus: "virtio" }, { id: "root", name: "disk0", bus: "virtio" }],
      } },
      { id: "installer", type: "storageNode", position: { x: 0, y: 0 }, data: { name: "iso", format: "iso" } },
      { id: "root-disk", type: "storageNode", position: { x: 0, y: 0 }, data: { name: "root", format: "qcow2", size: 120, bootable: false } },
      { id: "data-disk", type: "storageNode", position: { x: 0, y: 0 }, data: { name: "data", format: "qcow2", size: 250, bootable: false } },
    ], edges: [
      { id: "iso-edge", source: "installer", target: "vm", targetHandle: "dp-cdrom-left" },
      { id: "root-edge", source: "root-disk", target: "vm", targetHandle: "dp-root-left" },
      { id: "data-edge", source: "data-disk", target: "vm", targetHandle: "dp-data-left" },
    ] });
    render(<PropertiesPanel />);
    const mount = screen.getByRole("textbox", { name: "OCP mount path" });
    if (disabled) expect(mount).toBeDisabled();
    else expect(mount).toBeEnabled();
  });

  it("requires clearing an individual mount path before enabling boot", () => {
    useCanvasStore.setState({ selectedNodeId: "disk", nodes: [{ id: "disk", type: "storageNode", position: { x: 0, y: 0 },
      data: { name: "data", format: "qcow2", size: 250, bootable: false, ocpMount: "/var/lib/containers" },
    }] });
    render(<PropertiesPanel />);
    const boot = screen.getByRole("checkbox", { name: "Bootable" });
    const mount = screen.getByRole("textbox", { name: "OCP mount path" });
    expect(boot).toBeDisabled();
    expect(mount).toBeEnabled();
    fireEvent.change(mount, { target: { value: "" } });
    expect(boot).toBeEnabled();
    fireEvent.click(boot);
    expect(mount).toBeDisabled();
  });

  it.each(["cluster", "individual"])("allows repairing legacy boot-and-mount conflicts in %s disk properties", (target) => {
    if (target === "cluster") {
      useCanvasStore.setState({ clusters: [{ ...useCanvasStore.getState().clusters[0], workers: 0,
        controlPlaneDisks: [{ sizeGb: 120, bootable: true, ocpMount: "/var/lib/containers" }],
      }] });
    } else {
      useCanvasStore.setState({ selectedNodeId: "disk", nodes: [{ id: "disk", type: "storageNode", position: { x: 0, y: 0 },
        data: { name: "root", format: "qcow2", size: 120, bootable: true, ocpMount: "/var/lib/containers" },
      }] });
    }
    render(<PropertiesPanel />);
    const mount = screen.getByRole("textbox", { name: "OCP mount path" });
    const boot = screen.getByRole("checkbox", { name: target === "cluster" ? "Boot" : "Bootable" });
    expect(mount).toBeDisabled();
    expect(boot).toBeDisabled();
    const clear = screen.getByRole("button", { name: "Clear OCP mount path" });
    expect(clear).toBeEnabled();
    fireEvent.click(clear);
    expect(mount).toHaveValue("");
    expect(mount).toBeDisabled();
    expect(boot).toBeEnabled();
    expect(boot).toBeChecked();
    expect(screen.queryByRole("button", { name: "Clear OCP mount path" })).not.toBeInTheDocument();
    const storedMount = target === "cluster"
      ? useCanvasStore.getState().clusters[0].controlPlaneDisks![0].ocpMount
      : useCanvasStore.getState().nodes[0].data.ocpMount;
    expect(storedMount).toBeUndefined();
  });

  it("edits worker count into the cluster", async () => {
    render(<PropertiesPanel />);
    const workers = screen.getByLabelText(/^workers$/i);
    await userEvent.clear(workers);
    await userEvent.type(workers, "4");
    (workers as HTMLInputElement).blur();
    expect(useCanvasStore.getState().clusters[0].workers).toBe(4);
  });

  it("changing type to sno sets controlPlane to 1 (read-only, derived)", async () => {
    render(<PropertiesPanel />);
    const typeSelect = screen.getByLabelText(/cluster type/i);
    await userEvent.selectOptions(typeSelect, "sno");
    expect(useCanvasStore.getState().clusters[0].type).toBe("sno");
    expect(useCanvasStore.getState().clusters[0].controlPlane).toBe(1);
  });

  it("allows workers on an SNO cluster (SNO+workers)", async () => {
    useCanvasStore.setState({
      clusters: [
        {
          ...useCanvasStore.getState().clusters[0],
          type: "sno",
          controlPlane: 1,
          workers: 0,
        },
      ],
      nodes: [
        {
          ...useCanvasStore.getState().nodes[0],
          data: {
            ...useCanvasStore.getState().nodes[0].data,
            type: "sno",
            controlPlane: 1,
            workers: 0,
          },
        },
      ],
    } as never);
    render(<PropertiesPanel />);
    const workers = screen.getByLabelText(/^workers$/i);
    expect(workers).not.toBeDisabled();
    await userEvent.clear(workers);
    await userEvent.type(workers, "2");
    (workers as HTMLInputElement).blur();
    expect(useCanvasStore.getState().clusters[0].workers).toBe(2);
    expect(screen.getByText(/workers built after sno installation/i)).toBeInTheDocument();
  });

  it("mirrors summary fields onto the cluster node data", async () => {
    render(<PropertiesPanel />);
    const workers = screen.getByLabelText(/^workers$/i);
    await userEvent.clear(workers);
    await userEvent.type(workers, "5");
    const node = useCanvasStore.getState().nodes.find((n) => n.id === "cluster-prod")!;
    expect((node.data as Record<string, unknown>).workers).toBe(5);
  });

  it("materializes worker member VMs when the count grows", async () => {
    render(<PropertiesPanel />);
    const workers = screen.getByLabelText(/^workers$/i);
    await userEvent.clear(workers);
    await userEvent.type(workers, "3");
    const members = useCanvasStore
      .getState()
      .nodes.filter(
        (n) =>
          n.type === "vmNode" &&
          (n.data as Record<string, unknown>).clusterId === "prod" &&
          (n.data as Record<string, unknown>).clusterRole === "worker",
      );
    expect(members).toHaveLength(3);
  });

  it("propagates control-plane sizing to existing generated members", () => {
    const cpMember = (n: number): Node => ({
      id: `prod-cp-${n}`,
      type: "vmNode",
      position: { x: 0, y: 0 },
      parentId: "cluster-prod",
      data: {
        os: "rhcos",
        clusterId: "prod",
        clusterRole: "control-plane",
        generated: true,
        vcpus: 8,
        ram: 16,
        disk: 120,
      },
    });
    useCanvasStore.setState({
      nodes: [
        useCanvasStore.getState().nodes[0],
        cpMember(0),
        cpMember(1),
        cpMember(2),
      ],
    } as never);
    render(<PropertiesPanel />);
    const cpu = screen.getByLabelText(/control plane vcpus/i);
    // fireEvent.change sets the whole value in one shot (deterministic).
    fireEvent.change(cpu, { target: { value: "16" } });
    const members = useCanvasStore
      .getState()
      .nodes.filter(
        (n) =>
          (n.data as Record<string, unknown>).clusterRole === "control-plane" &&
          (n.data as Record<string, unknown>).generated === true,
      );
    expect(members).toHaveLength(3);
    expect(members.every((m) => (m.data as Record<string, unknown>).vcpus === 16)).toBe(true);
    // clusters[] also updated
    expect(useCanvasStore.getState().clusters[0].controlPlaneCpu).toBe(16);
  });

  it("locks OCP version when the cluster is in the deployed baseline", () => {
    useCanvasStore.setState({
      clusters: [
        {
          ...useCanvasStore.getState().clusters[0],
          ocpVersion: "4.22",
        },
      ],
      deployedClusterRows: [
        {
          id: "prod",
          name: "prod",
          nodeId: "cluster-prod",
          type: "standard",
          ocpVersion: "4.22",
        },
      ],
    } as never);
    render(<PropertiesPanel />);
    const version = screen.getByLabelText(/ocp version/i);
    expect(version).toBeDisabled();
  });

  it("hides Configure bastion browser for pod installs without a bastion VM", () => {
    useCanvasStore.setState({ ocpInstallVia: "pod" } as never);
    render(<PropertiesPanel />);
    expect(screen.queryByText(/configure bastion browser/i)).not.toBeInTheDocument();
  });

  it("hides Configure bastion browser when install_via is unset (defaults to pod)", () => {
    useCanvasStore.setState({ ocpInstallVia: null } as never);
    render(<PropertiesPanel />);
    expect(screen.queryByText(/configure bastion browser/i)).not.toBeInTheDocument();
  });

  it("shows pull-through registry checkbox when user has PTR configured", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              pull_through_registry: true,
              pull_through_registry_url: "registry.example.com",
            }),
        }),
      ) as unknown as typeof fetch,
    );
    render(<PropertiesPanel />);
    expect(await screen.findByText(/use pull-through registry/i)).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("hides pull-through registry checkbox when user has no PTR configured", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ pull_through_registry: false }),
        }),
      ) as unknown as typeof fetch,
    );
    render(<PropertiesPanel />);
    await screen.findByText(/install openshift on deploy/i);
    expect(screen.queryByText(/use pull-through registry/i)).not.toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("shows Configure bastion browser for bastion installs with a bastion VM", () => {
    useCanvasStore.setState({
      ocpInstallVia: "bastion",
      nodes: [
        ...useCanvasStore.getState().nodes,
        {
          id: "bastion",
          type: "vmNode",
          position: { x: 0, y: 0 },
          data: { label: "bastion", name: "bastion" },
        },
      ],
    } as never);
    render(<PropertiesPanel />);
    expect(screen.getByText(/configure bastion browser/i)).toBeInTheDocument();
  });

  it("flags a VIP that collides with another cluster", async () => {
    useCanvasStore.setState({
      clusters: [
        ...useCanvasStore.getState().clusters,
        {
          id: "dev",
          nodeId: "cluster-dev",
          name: "dev",
          type: "standard",
          controlPlane: 3,
          workers: 0,
          apiVip: "10.0.0.5",
        },
      ],
    } as never);
    render(<PropertiesPanel />);
    const apiVip = screen.getByLabelText(/api vip/i);
    await userEvent.type(apiVip, "10.0.0.5");
    // typing is never blocked
    expect(useCanvasStore.getState().clusters[0].apiVip).toBe("10.0.0.5");
    // and an inline collision error is shown
    expect(screen.getByText(/already used by cluster/i)).toBeInTheDocument();
  });
});
