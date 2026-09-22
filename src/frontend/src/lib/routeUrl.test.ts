import { describe, expect, it } from "vitest";
import {
  findRouteForForward,
  formatEipAccessUrl,
  formatOcpRouteUrl,
  isOcpRoutableForward,
  type RouteEndpoint,
} from "./routeUrl";

const routes: RouteEndpoint[] = [
  { type: "route", port: 6443, vmIp: "10.0.0.10", hostname: "rt-cp-0-6443" },
  { type: "route", port: 6444, vmIp: "10.1.0.10", hostname: "rt-cp-1-6444" },
  { type: "route", port: 443, vmIp: "10.0.0.10", hostname: "rt-cp-0-443" },
  { type: "route", port: 80, vmIp: "10.0.0.10", hostname: "rt-cp-0-80" },
  { type: "route", port: 443, vmIp: "172.30.67.3", hostname: "rt-showroom-443" },
];

describe("findRouteForForward", () => {
  it("disambiguates two forwards sharing external port 443 by internal IP", () => {
    // Cluster ingress 443 → cp-0 route; showroom 443 → showroom route.
    const cluster = findRouteForForward(routes, { extPort: "443", intIp: "10.0.0.10" });
    const showroom = findRouteForForward(routes, { extPort: "443", intIp: "172.30.67.3" });
    expect(cluster?.hostname).toBe("rt-cp-0-443");
    expect(showroom?.hostname).toBe("rt-showroom-443");
  });

  it("matches a unique port normally", () => {
    expect(findRouteForForward(routes, { extPort: "6443", intIp: "10.0.0.10" })?.hostname).toBe(
      "rt-cp-0-6443",
    );
  });

  it("matches secondary API listen keys by ext port + VIP", () => {
    expect(findRouteForForward(routes, { extPort: "6444", intIp: "10.1.0.10" })?.hostname).toBe(
      "rt-cp-1-6444",
    );
  });

  it("falls back to port-only when endpoints lack vmIp (legacy)", () => {
    const legacy: RouteEndpoint[] = [{ type: "route", port: 443, hostname: "rt-legacy-443" }];
    expect(findRouteForForward(legacy, { extPort: "443", intIp: "10.0.0.10" })?.hostname).toBe(
      "rt-legacy-443",
    );
  });
});

describe("isOcpRoutableForward", () => {
  it("treats web ports and any intPort 6443 listen key as routable", () => {
    expect(isOcpRoutableForward({ extPort: "443", intPort: "80" })).toBe(true);
    expect(isOcpRoutableForward({ extPort: "6443", intPort: "6443" })).toBe(true);
    expect(isOcpRoutableForward({ extPort: "6444", intPort: "6443" })).toBe(true);
    expect(isOcpRoutableForward({ extPort: "8080", intPort: "8080" })).toBe(false);
  });
});

describe("formatOcpRouteUrl", () => {
  it("uses router default ports — listen keys are not in the public URL", () => {
    expect(formatOcpRouteUrl("rt.apps.example.com", "80")).toBe("http://rt.apps.example.com");
    expect(formatOcpRouteUrl("rt.apps.example.com", "443")).toBe("https://rt.apps.example.com");
    expect(formatOcpRouteUrl("rt.apps.example.com", "6443")).toBe("https://rt.apps.example.com");
    expect(formatOcpRouteUrl("rt.apps.example.com", "6444")).toBe("https://rt.apps.example.com");
  });
});

describe("formatEipAccessUrl", () => {
  it("links web ports and leaves API ports as non-links", () => {
    expect(formatEipAccessUrl("184.194.236.17", "443")).toBe("https://184.194.236.17");
    expect(formatEipAccessUrl("184.194.236.17", "80")).toBe("http://184.194.236.17");
    expect(formatEipAccessUrl("184.194.236.17", "6443")).toBeNull();
  });

  it("uses showroom.<eip>.sslip.io for cloud showroom forwards", () => {
    expect(
      formatEipAccessUrl("184.194.236.17", "443", { showroom: true }),
    ).toBe("https://showroom.184.194.236.17.sslip.io");
    expect(
      formatEipAccessUrl("184.194.236.17", "80", { showroom: true }),
    ).toBe("http://showroom.184.194.236.17.sslip.io");
  });

  it("prefers deployed _showroom_url when present", () => {
    expect(
      formatEipAccessUrl("184.194.236.17", "443", {
        showroom: true,
        showroomUrl: "https://showroom.guid.example.com",
      }),
    ).toBe("https://showroom.guid.example.com");
  });
});
