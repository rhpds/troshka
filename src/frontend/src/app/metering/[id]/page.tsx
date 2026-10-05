"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import {
  Alert,
  Button,
  Card,
  CardBody,
  PageSection,
  Title,
} from "@patternfly/react-core";

interface InvoiceDetail {
  id: string;
  project_id: string;
  project_name: string;
  total_usd: number;
  currency: string;
  period_start: string | null;
  period_end: string | null;
  finalized_at: string | null;
  line_items: { by_kind?: Array<{ kind: string; qty_hours: number; subtotal: number }> };
}

const KIND_LABELS: Record<string, string> = {
  vcpu: "vCPU",
  ram: "RAM",
  disk: "Disk",
  eip: "Elastic IP",
  ceph: "Ceph",
};

function money(n: number): string {
  return `$${n.toFixed(2)}`;
}

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] || kind;
}

export default function InvoiceDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = String(params?.id || "");
  const [invoice, setInvoice] = useState<InvoiceDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!id) return;
    fetch(`/api/v1/metering/invoices/${id}`)
      .then((r) => {
        if (r.status === 404) throw new Error("Invoice not found");
        if (!r.ok) throw new Error("Failed to load invoice");
        return r.json();
      })
      .then(setInvoice)
      .catch((e: Error) => setError(e.message));
  }, [id]);

  return (
    <PageSection>
      <Button variant="link" onClick={() => router.push("/metering")}>
        ← Past
      </Button>
      {error && <Alert variant="danger" title={error} />}
      {invoice && (
        <>
          <Title headingLevel="h1">{invoice.project_name}</Title>
          <p style={{ opacity: 0.75, marginTop: 8 }}>
            {money(invoice.total_usd)} {invoice.currency}
            {invoice.finalized_at
              ? ` · finalized ${new Date(invoice.finalized_at).toLocaleString()}`
              : ""}
          </p>
          <Card style={{ marginTop: 16 }}>
            <CardBody>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
                <thead>
                  <tr style={{ textAlign: "left" }}>
                    <th style={{ padding: "8px 4px" }}>Kind</th>
                    <th style={{ padding: "8px 4px" }}>Qty-hours</th>
                    <th style={{ padding: "8px 4px" }}>Subtotal</th>
                  </tr>
                </thead>
                <tbody>
                  {(invoice.line_items?.by_kind || []).map((row) => (
                    <tr key={row.kind} style={{ borderTop: "1px solid var(--pf-t--global--border--color--default)" }}>
                      <td style={{ padding: "8px 4px" }}>{kindLabel(row.kind)}</td>
                      <td style={{ padding: "8px 4px" }}>{row.qty_hours.toFixed(4)}</td>
                      <td style={{ padding: "8px 4px" }}>{money(row.subtotal)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardBody>
          </Card>
        </>
      )}
    </PageSection>
  );
}
