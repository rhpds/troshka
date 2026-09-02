import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// page.tsx imports PatternFly at module scope, which pulls in CSS that vitest
// cannot load. These components are only used by ProjectsPage, never by the
// NewProjectModal under test, so stub them out to keep the import graph clean.
vi.mock("@patternfly/react-core", () => {
  const stub = () => null;
  return {
    Button: stub,
    Card: stub,
    CardBody: stub,
    CardTitle: stub,
    EmptyState: stub,
    EmptyStateBody: stub,
    EmptyStateVariant: { full: "full" },
    PageSection: stub,
    Title: stub,
    Toolbar: stub,
    ToolbarContent: stub,
    ToolbarItem: stub,
  };
});
vi.mock(
  "@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateHeader",
  () => ({ EmptyStateHeader: () => null }),
);
vi.mock(
  "@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateIcon",
  () => ({ EmptyStateIcon: () => null }),
);
vi.mock("@patternfly/react-icons/dist/esm/icons/plus-circle-icon", () => ({
  default: () => null,
}));
vi.mock("@patternfly/react-icons/dist/esm/icons/cubes-icon", () => ({
  default: () => null,
}));

import { NewProjectModal } from "@/app/projects/page";

// The create flow dynamically imports the canvas store after a successful
// from-template POST. Stub it so the test never touches real store logic.
vi.mock("@/stores/canvasStore", () => ({
  useCanvasStore: {
    getState: () => ({ loadProject: vi.fn().mockResolvedValue(undefined) }),
  },
}));

const OCP_TEMPLATE = {
  id: "ocp-sno",
  name: "OpenShift SNO",
  description: "Single-node OpenShift",
  category: "openshift",
};

const LIBRARY = [
  {
    id: "img1",
    name: "RHEL 9.4 KVM Guest Image",
    format: "qcow2",
    state: "ready",
    size_bytes: 1024 ** 3,
  },
  {
    id: "iso1",
    name: "RHEL 9.4 Binary DVD",
    format: "iso",
    state: "ready",
    size_bytes: 1024 ** 3,
  },
];

let lastFromTemplateBody: Record<string, unknown> | null = null;

function installFetchMock() {
  lastFromTemplateBody = null;
  const okJson = (data: unknown) =>
    Promise.resolve({ ok: true, json: () => Promise.resolve(data) } as Response);

  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/projects/templates")) return okJson([OCP_TEMPLATE]);
      if (url.includes("/ocp/versions"))
        return okJson([{ minor: "4.20", latest: "4.20.1" }]);
      if (url.includes("/library/")) return okJson(LIBRARY);
      if (url.includes("/auth/ssh-keys")) return okJson([]);
      if (url.includes("/auth/ocp-pull-secret")) return okJson({ has_secret: true });
      if (url.includes("/patterns/")) return okJson([]);
      if (url.includes("/projects/from-template")) {
        lastFromTemplateBody = JSON.parse((init?.body as string) || "{}");
        return okJson({ id: "proj-1", name: "OpenShift SNO" });
      }
      // deploy + any other calls
      return okJson({ status: "deploying" });
    }),
  );
}

function renderModal() {
  return render(
    <NewProjectModal
      onClose={() => {}}
      onCreated={() => {}}
      userRole="admin"
      availableHosts={[]}
      setAlertMsg={() => {}}
    />,
  );
}

async function navigateToOcpTemplateForm() {
  renderModal();
  fireEvent.click(await screen.findByText("Quick Starts"));
  fireEvent.click(await screen.findByText("OpenShift SNO"));
}

beforeEach(() => {
  installFetchMock();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("create-project OCP form", () => {
  it("renders the openshift template form without the cluster name / base domain inputs", async () => {
    await navigateToOcpTemplateForm();

    // Project-level OCP field stays in the dialog.
    expect(await screen.findByText("OCP Version")).toBeInTheDocument();

    // Cluster-boundary config now lives on the canvas, not the dialog.
    expect(screen.queryByText("Cluster Name")).not.toBeInTheDocument();
    expect(screen.queryByText("Base Domain")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("ocp.local")).not.toBeInTheDocument();
    // The DNS preview line depended on cluster name/base domain.
    expect(screen.queryByText(/→ 10\.0\.0\.2 \(LB\)/)).not.toBeInTheDocument();
  });

  it("still submits the project-level OCP fields on create", async () => {
    await navigateToOcpTemplateForm();

    // Wait for auto-selected image/iso (library fetch) so Create is enabled.
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: /create/i }) as HTMLButtonElement)
          .disabled,
      ).toBe(false),
    );

    fireEvent.click(screen.getByRole("button", { name: /create/i }));

    await waitFor(() => expect(lastFromTemplateBody).not.toBeNull());
    expect(lastFromTemplateBody).toMatchObject({
      template_id: "ocp-sno",
      auto_install_ocp: true,
      ocp_version: "4.20",
      // Back-compat: from-template still seeds the legacy single cluster with
      // its default name/domain so existing single-cluster deploys keep working.
      cluster_name: "ocp",
      base_domain: "ocp.local",
    });
  });
});
