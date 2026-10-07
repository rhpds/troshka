import { describe, it, expect } from "vitest";
import {
  appProxyPublicHost,
  appProxyRouteCode,
  deriveAppsDomain,
  nsFromShowroomHostname,
  eipFromSslipHost,
} from "@/lib/showroomAppProxy";

describe("showroomAppProxy", () => {
  it("matches backend route codes for console/oauth hosts", () => {
    expect(appProxyRouteCode("console-openshift-console.apps.ocp.local")).toBe("con-ocp");
    expect(appProxyRouteCode("oauth-openshift.apps.ocp.local")).toBe("oauth-ocp");
    expect(appProxyRouteCode("console-openshift-console.apps.ocp-2.local")).toBe("con-ocp-2");
  });

  it("builds the deterministic public host", () => {
    expect(
      appProxyPublicHost(
        "6fcf0e3e-08d8-4911",
        "console-openshift-console.apps.ocp.ocp.local",
        "apps.ocpvdev01.dal13.infra.demo.redhat.com",
        "sandbox-8zsqb-troshka",
      ),
    ).toBe(
      "tpf-6fcf0e3e-con-ocp-sandbox-8zsqb-troshka.apps.ocpvdev01.dal13.infra.demo.redhat.com",
    );
  });

  it("derives apps domain and namespace from showroom route host", () => {
    expect(
      deriveAppsDomain(
        "showroom-troshka-1e559f8a.apps.ocpv06.dal10.infra.demo.redhat.com",
      ),
    ).toBe("apps.ocpv06.dal10.infra.demo.redhat.com");
    expect(
      nsFromShowroomHostname(
        "showroom-troshka-1e559f8a.apps.ocpv06.dal10.infra.demo.redhat.com",
      ),
    ).toBe("troshka-1e559f8a");
    expect(
      nsFromShowroomHostname(
        "troshka-pf-d0cc03f4-showroom-443-sandbox-8zsqb-troshka.apps.ocpv06.dal10.infra.demo.redhat.com",
      ),
    ).toBe("sandbox-8zsqb-troshka");
  });

  it("parses EIP from sslip.io hosts", () => {
    expect(eipFromSslipHost("https://showroom.184.194.236.17.sslip.io/?token=x")).toBe(
      "184.194.236.17",
    );
  });
});
