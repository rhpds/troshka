"use client";

import { useEffect, useState } from "react";
import {
  PageSection,
  Title,
  Button,
  Card,
  CardBody,
  Alert,
  Form,
  FormGroup,
  TextInput,
  HelperText,
  HelperTextItem,
} from "@patternfly/react-core";

const inputStyle = {
  width: "100%",
  maxWidth: 420,
  padding: "6px 10px",
  borderRadius: 6,
  border: "1px solid var(--pf-t--global--border--color--default)",
  background: "var(--pf-t--global--background--color--primary--default)",
  color: "var(--pf-t--global--text--color--regular)",
  fontSize: 13,
};

export default function AdminSettingsPage() {
  const [ingressController, setIngressController] = useState("default");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetch("/api/v1/admin/settings")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("load failed"))))
      .then((data) => {
        setIngressController(data.ingress_controller || "default");
      })
      .catch(() => setError("Failed to load settings"))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      const resp = await fetch("/api/v1/admin/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ingress_controller: ingressController.trim() || "default",
        }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "Save failed" }));
        throw new Error(err.detail || "Save failed");
      }
      const data = await resp.json();
      setIngressController(data.ingress_controller);
      setSaved(true);
    } catch (e: any) {
      setError(e?.message || "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <PageSection>
      <Title headingLevel="h1" style={{ marginBottom: 16 }}>
        System Settings
      </Title>
      {error && (
        <Alert variant="danger" title={error} style={{ marginBottom: 16 }} isInline />
      )}
      {saved && (
        <Alert
          variant="success"
          title="Settings saved. New routes use this IngressController on next deploy."
          style={{ marginBottom: 16 }}
          isInline
        />
      )}
      <Card>
        <CardBody>
          {loading ? (
            <div>Loading…</div>
          ) : (
            <Form>
              <FormGroup
                label="OpenShift IngressController"
                fieldId="ingress-controller"
              >
                <TextInput
                  id="ingress-controller"
                  value={ingressController}
                  onChange={(_e, v) => setIngressController(v)}
                  style={inputStyle}
                  placeholder="default"
                />
                <HelperText>
                  <HelperTextItem>
                    Name of the cluster IngressController used for Troshka Routes and
                    showroom. Use <code>default</code> for the cluster apps domain
                    (e.g. apps.ocpv06.dal10.infra.demo.redhat.com), or{" "}
                    <code>ingress-rhdp-net</code> for the per-cluster rhdp.net domain
                    (e.g. apps.ocpv06.rhdp.net).
                  </HelperTextItem>
                </HelperText>
              </FormGroup>
              <div style={{ marginTop: 16 }}>
                <Button
                  variant="primary"
                  onClick={handleSave}
                  isLoading={saving}
                  isDisabled={saving}
                >
                  Save
                </Button>
              </div>
            </Form>
          )}
        </CardBody>
      </Card>
    </PageSection>
  );
}
