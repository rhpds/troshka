import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { Node } from "@xyflow/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ReadOnlyPropertiesPanel from "../ReadOnlyPropertiesPanel";

afterEach(cleanup);
const node = (type: string, data: Node["data"]): Node => ({ id: "n", type, data: { name: "Resource", ...data }, position: { x: 0, y: 0 } });

describe("read-only properties", () => {
  it("shows VM configuration without exposing editing controls and closes on request", () => {
    const close = vi.fn();
    render(<ReadOnlyPropertiesPanel node={node("vmNode", {
      vcpus: 4, ram: 16, os: "rhel9", cloudInit: true, ciHostname: "host",
      bootMethod: "pxe", powerOnAtDeploy: false, consoleType: "serial", icon: "X",
      nics: [{ id: "n1", name: "eth0", mac: "aa", model: "virtio" }, { name: "eth1", mac: "bb", model: "e1000" }],
      diskControllers: [{ id: "d1", name: "ssd", bus: "sata", rotationRate: 1 }, { name: "hdd", bus: "scsi", rotationRate: 7200 }, { id: "d3", name: "virtio", bus: "virtio", rotationRate: 1 }],
      tags: { purpose: "test", empty: "" },
    })} onClose={close} />);
    for (const value of ["Virtual Machine", "4", "16 GB", "rhel9", "host", "eth0", "eth1", "ssd (sata) — SSD", "hdd (scsi) — 7200 RPM", "virtio (virtio)", "test"]) expect(screen.getByText(value)).toBeInTheDocument();
    expect(screen.queryByText("empty")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button"));
    expect(close).toHaveBeenCalledOnce();
  });

  it("uses defaults and omits empty VM sections", () => {
    render(<ReadOnlyPropertiesPanel node={node("vmNode", { vcpus: 1, ram: 2 })} onClose={vi.fn()} />);
    expect(screen.getByText("template")).toBeInTheDocument();
    expect(screen.getByText("vnc")).toBeInTheDocument();
    expect(screen.queryByText("Cloud-Init")).not.toBeInTheDocument();
    expect(screen.queryByText("Tags")).not.toBeInTheDocument();
  });

  it.each(["router", "gateway", undefined])("shows %s network services and DHCP allocation", (subtype) => {
    render(<ReadOnlyPropertiesPanel node={node("networkNode", { subtype, cidr: "10.0.0.0/24", gateway: "10.0.0.1", dhcp: true, dns: false, pxe: true, dhcpStart: "10.0.0.20", dhcpEnd: "10.0.0.30", dnsDomain: "lab.test" })} onClose={vi.fn()} />);
    expect(screen.getByText("10.0.0.0/24")).toBeInTheDocument();
    expect(screen.getByText("10.0.0.20")).toBeInTheDocument();
    expect(screen.getByText("lab.test")).toBeInTheDocument();
    expect(screen.getByText("Disabled")).toBeInTheDocument();
  });

  it.each(["iso", "raw", undefined])("shows %s disk metadata", (format) => {
    render(<ReadOnlyPropertiesPanel node={node("storageNode", { size: 20, format, source: "library", libraryItemName: "Base image" })} onClose={vi.fn()} />);
    expect(screen.getByText("20 GB")).toBeInTheDocument();
    expect(screen.getByText(format || "qcow2")).toBeInTheDocument();
    expect(screen.getByText("Base image")).toBeInTheDocument();
  });
});
