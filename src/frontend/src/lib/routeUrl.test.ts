import { describe, expect, it } from "vitest";
import { findRouteForForward, type RouteEndpoint } from "./routeUrl";

const routes: RouteEndpoint[] = [
  { type: "route", port: 6443, vmIp: "10.0.0.10", hostname: "rt-cp-0-6443" },
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

  it("falls back to port-only when endpoints lack vmIp (legacy)", () => {
    const legacy: RouteEndpoint[] = [{ type: "route", port: 443, hostname: "rt-legacy-443" }];
    expect(findRouteForForward(legacy, { extPort: "443", intIp: "10.0.0.10" })?.hostname).toBe(
      "rt-legacy-443",
    );
  });
});
