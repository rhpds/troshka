import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import RunWorkloadModal from "@/components/canvas/RunWorkloadModal";

function okJson(data: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(data),
  } as Response);
}

describe("RunWorkloadModal", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("launches an ad-hoc cluster run and calls onLaunched with the run id", async () => {
    let postBody: any = null;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.endsWith("/workloads") && init?.method === "POST") {
          postBody = JSON.parse(init!.body as string);
          return okJson({ id: "run-123", status: "pending" }, 202);
        }
        return okJson({});
      }),
    );
    const onLaunched = vi.fn();
    render(<RunWorkloadModal projectId="p1" onClose={() => {}} onLaunched={onLaunched} />);

    fireEvent.change(screen.getByPlaceholderText(/role/i), {
      target: { value: "agnosticd.core_workloads.ocp4_workload_example" },
    });
    fireEvent.click(screen.getByRole("button", { name: /launch/i }));

    await waitFor(() => expect(onLaunched).toHaveBeenCalledWith("run-123"));
    expect(postBody.kind).toBe("ad_hoc");
    expect(postBody.role_fqcn).toBe("agnosticd.core_workloads.ocp4_workload_example");
    expect(postBody.target_map.mode).toBe("cluster");
  });

  it("disables Launch when VM-mode preview reports contract errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.endsWith("/inventory-preview")) {
          return okJson({ groups: {}, errors: ["VM vm1 has no AnsibleGroup tag"] });
        }
        return okJson({});
      }),
    );
    render(<RunWorkloadModal projectId="p1" onClose={() => {}} onLaunched={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText(/role/i), { target: { value: "some.role" } });
    fireEvent.click(screen.getByLabelText(/vms/i));
    await waitFor(() => expect(screen.getByText(/no AnsibleGroup tag/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /launch/i })).toBeDisabled();
  });
});
