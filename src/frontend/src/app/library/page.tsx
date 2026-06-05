"use client";

import { EmptyState, EmptyStateBody, EmptyStateVariant, PageSection } from "@patternfly/react-core";
import { EmptyStateHeader } from "@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateHeader";

export default function LibraryPage() {
  return (
    <PageSection>
      <EmptyState variant={EmptyStateVariant.full}>
        <EmptyStateHeader titleText="Library" headingLevel="h1" />
        <EmptyStateBody>Templates, snapshots, and ISOs — coming in Phase 7</EmptyStateBody>
      </EmptyState>
    </PageSection>
  );
}
