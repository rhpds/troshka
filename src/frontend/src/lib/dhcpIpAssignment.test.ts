import { describe, it, expect } from "vitest";
import {
  pickTemplateStyleNicIp,
  pickClusterMemberNicIp,
  pickHighEndNicIp,
} from "./dhcpIpAssignment";

describe("pickTemplateStyleNicIp", () => {
  it("prefers .10 on a typical /24 (gateway .1 reserved)", () => {
    expect(pickTemplateStyleNicIp("10.0.0.0/24", new Set())).toBe("10.0.0.10");
  });

  it("skips used addresses and keeps climbing from .10", () => {
    expect(
      pickTemplateStyleNicIp("10.0.0.0/24", new Set(["10.0.0.10", "10.0.0.11"])),
    ).toBe("10.0.0.12");
  });

  it("falls back below .10 when .10+ is exhausted on a tiny subnet", () => {
    // /29 → hosts .1–.6; preferFrom = min(9,5)=5 → try .6 first then .2–.5
    const used = new Set(["10.0.0.6"]);
    expect(pickTemplateStyleNicIp("10.0.0.0/29", used)).toBe("10.0.0.2");
  });
});

describe("pickClusterMemberNicIp", () => {
  it("uses template-style IPs on machine networks (not high-end)", () => {
    expect(pickClusterMemberNicIp({ cidr: "192.168.101.0/24" }, new Set())).toBe(
      "192.168.101.10",
    );
    expect(pickHighEndNicIp("192.168.101.0/24", new Set())).toBe("192.168.101.254");
  });

  it("still uses BMC .11+ allocation for BMC networks", () => {
    expect(
      pickClusterMemberNicIp(
        { cidr: "192.168.100.0/24", networkType: "bmc" },
        new Set(),
      ),
    ).toBe("192.168.100.11");
  });
});
