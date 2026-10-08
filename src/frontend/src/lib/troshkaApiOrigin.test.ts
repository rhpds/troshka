import { describe, expect, it } from "vitest";

import {
  troshkaApiOriginForCli,
  troshkaTunnelOriginForCli,
} from "./troshkaApiOrigin";

describe("troshkaApiOriginForCli", () => {
  it("maps prod UI host to troshka-api Route", () => {
    expect(
      troshkaApiOriginForCli(
        "https://troshka.apps.ocpv-infra01.dal12.infra.demo.redhat.com",
      ),
    ).toBe("https://troshka-api.apps.ocpv-infra01.dal12.infra.demo.redhat.com");
  });

  it("leaves troshka-api host unchanged", () => {
    expect(
      troshkaApiOriginForCli(
        "https://troshka-api.apps.ocpv-infra01.dal12.infra.demo.redhat.com",
      ),
    ).toBe("https://troshka-api.apps.ocpv-infra01.dal12.infra.demo.redhat.com");
  });

  it("maps local Next.js UI to backend :8200", () => {
    expect(troshkaApiOriginForCli("http://localhost:3100")).toBe(
      "http://localhost:8200",
    );
    expect(troshkaApiOriginForCli("http://127.0.0.1:3100")).toBe(
      "http://127.0.0.1:8200",
    );
  });

  it("keeps explicit local backend port", () => {
    expect(troshkaApiOriginForCli("http://localhost:8200")).toBe(
      "http://localhost:8200",
    );
  });
});

describe("troshkaTunnelOriginForCli", () => {
  it("maps prod API host to troshka-tunnel Route", () => {
    expect(
      troshkaTunnelOriginForCli(
        "https://troshka-api.apps.ocpv-infra01.dal12.infra.demo.redhat.com",
      ),
    ).toBe(
      "https://troshka-tunnel.apps.ocpv-infra01.dal12.infra.demo.redhat.com",
    );
  });

  it("maps prod UI host through api then tunnel", () => {
    expect(
      troshkaTunnelOriginForCli(
        "https://troshka.apps.ocpv-infra01.dal12.infra.demo.redhat.com",
      ),
    ).toBe(
      "https://troshka-tunnel.apps.ocpv-infra01.dal12.infra.demo.redhat.com",
    );
  });

  it("maps local UI/API to tunnel :8201", () => {
    expect(troshkaTunnelOriginForCli("http://localhost:3100")).toBe(
      "http://localhost:8201",
    );
    expect(troshkaTunnelOriginForCli("http://localhost:8200")).toBe(
      "http://localhost:8201",
    );
  });
});
