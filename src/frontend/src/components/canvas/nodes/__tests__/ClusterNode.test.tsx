import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import ClusterNode from "../ClusterNode";

function renderNode(data: any) {
  return render(
    <ReactFlowProvider>
      {/* @ts-expect-error minimal NodeProps for test */}
      <ClusterNode id="cluster-prod" selected={false} data={data} />
    </ReactFlowProvider>,
  );
}

describe("ClusterNode", () => {
  it("shows the cluster name and a type/count badge", () => {
    renderNode({ name: "prod", type: "standard", controlPlane: 3, workers: 2 });
    expect(screen.getByText(/prod/)).toBeInTheDocument();
    expect(screen.getByText(/standard/)).toBeInTheDocument();
    expect(screen.getByText(/3cp/)).toBeInTheDocument();
    expect(screen.getByText(/2\s*wrk/)).toBeInTheDocument();
  });
});
