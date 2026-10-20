import { resolveShowroomUrl } from "./routeUrl";

/** Resolve access details at click time without reloading editable canvas state. */
export async function openShowroom(projectId: string): Promise<void> {
  // Reserve the tab during the user gesture, before awaiting the request.
  const tab = window.open("about:blank", "_blank");
  if (!tab) throw new Error("Allow popups to open Showroom.");
  tab.opener = null;
  try {
    const response = await fetch(`/api/v1/projects/${projectId}`, { cache: "no-store" });
    if (!response.ok) throw new Error("Could not refresh the Showroom URL. Please try again.");
    const project = await response.json();
    const url = resolveShowroomUrl(
      project.deployed_topology?.nodes || project.topology?.nodes || [],
      project.deployed_topology,
    );
    if (!url) throw new Error("Showroom is no longer available for this project.");
    // Preserve the previous button's no-referrer behavior.
    const policy = tab.document.createElement("meta");
    policy.name = "referrer";
    policy.content = "no-referrer";
    tab.document.head.appendChild(policy);
    tab.location.replace(url);
  } catch (error) {
    tab.close();
    throw error;
  }
}
