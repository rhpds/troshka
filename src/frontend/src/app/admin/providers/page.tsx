"use client";

import { EmptyState, EmptyStateBody, EmptyStateVariant, PageSection } from "@patternfly/react-core";
import { EmptyStateHeader } from "@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateHeader";

export default function AdminProvidersPage() {
  return (
    <PageSection>
      <EmptyState variant={EmptyStateVariant.full}>
        <EmptyStateHeader titleText="Provider Management" headingLevel="h1" />
        <EmptyStateBody>Admin provider management — coming in Phase 2</EmptyStateBody>
      </EmptyState>
    </PageSection>
  );
}
