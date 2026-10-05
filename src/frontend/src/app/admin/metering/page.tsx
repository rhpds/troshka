"use client";

import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  CardBody,
  PageSection,
  Title,
} from "@patternfly/react-core";

const FIELDS = [
  { key: "vcpu_hour", label: "CPU per core per hour ($)" },
  { key: "ram_gib_hour", label: "Memory per GB per hour ($)" },
  { key: "disk_gib_hour", label: "Storage per GB per hour ($)" },
  { key: "ceph_gib_hour", label: "Ceph per GB per hour ($)" },
  { key: "eip_hour", label: "Elastic IP per hour ($)" },
] as const;

type UnitRates = Record<string, number>;

function pickFallback(data: Record<string, UnitRates>): UnitRates {
  return { ...(data.kubevirt || data.ocpvirt || data.libvirt || {}) };
}

export default function AdminMeteringPage() {
  const [rates, setRates] = useState<UnitRates>({});
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetch("/api/v1/metering/rates")
      .then((r) => {
        if (!r.ok) throw new Error("Failed to load rates");
        return r.json();
      })
      .then((data) => setRates(pickFallback(data)))
      .catch((e: Error) => setError(e.message));
  }, []);

  const save = async () => {
    setError("");
    setSaved(false);
    const resp = await fetch("/api/v1/metering/rates", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rates: { kubevirt: rates } }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      setError(data.detail || "Failed to save rates");
      return;
    }
    setRates(pickFallback(await resp.json()));
    setSaved(true);
  };

  const inputStyle = {
    width: 160,
    padding: "6px 10px",
    borderRadius: 6,
    border: "1px solid var(--pf-t--global--border--color--default)",
    background: "var(--pf-t--global--background--color--primary--default)",
    color: "var(--pf-t--global--text--color--regular)",
    fontSize: 13,
  };

  return (
    <PageSection>
      <Title headingLevel="h1">Metering rates</Title>
      <p style={{ opacity: 0.75, margin: "8px 0 16px", maxWidth: 560 }}>
        Default unit rates (KubeVirt). AWS, GCP, and Azure hosts bill from the
        instance catalog instead. Per-host overrides are on Hosts.
      </p>
      {error && <Alert variant="danger" title={error} />}
      {saved && <Alert variant="success" title="Rates saved" />}
      <Card style={{ maxWidth: 480 }}>
        <CardBody>
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {FIELDS.map((field) => (
              <label
                key={field.key}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: 16,
                  fontSize: 13,
                }}
              >
                <span>{field.label}</span>
                <input
                  type="number"
                  min={0}
                  step="0.0000001"
                  value={rates[field.key] ?? 0}
                  onChange={(e) =>
                    setRates((prev) => ({
                      ...prev,
                      [field.key]: Number(e.target.value),
                    }))
                  }
                  style={inputStyle}
                />
              </label>
            ))}
          </div>
          <Button variant="primary" onClick={save} style={{ marginTop: 20 }}>
            Save
          </Button>
        </CardBody>
      </Card>
    </PageSection>
  );
}
