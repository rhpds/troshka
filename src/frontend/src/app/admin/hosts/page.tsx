"use client";

import { EmptyState, EmptyStateBody, EmptyStateVariant, PageSection } from "@patternfly/react-core";
import { EmptyStateHeader } from "@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateHeader";

export default function AdminHostsPage() {
  return (
    <PageSection>
      <EmptyState variant={EmptyStateVariant.full}>
        <EmptyStateHeader titleText="Host Management" headingLevel="h1" />
        <EmptyStateBody>Admin host management — coming in Phase 2</EmptyStateBody>
      </EmptyState>
    </PageSection>
  );
}
