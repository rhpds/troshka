"use client";

import { useEffect, useState } from "react";
import {
  PageSection,
  Title,
  Button,
  Card,
  CardBody,
  Alert,
} from "@patternfly/react-core";

interface DnsProvider {
  id: string;
  name: string;
  type: string;
  config: Record<string, any>;
  created_at: string;
}

const inputStyle = {
  width: "100%",
  padding: "6px 10px",
  borderRadius: 6,
  border: "1px solid var(--pf-t--global--border--color--default)",
  background: "var(--pf-t--global--background--color--primary--default)",
  color: "var(--pf-t--global--text--color--regular)",
  fontSize: 13,
};

const typeLabels: Record<string, string> = {
  nsupdate: "BIND (nsupdate)",
  route53: "AWS Route53",
};

export default function DnsProvidersPage() {
  const [providers, setProviders] = useState<DnsProvider[]>([]);
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [creating, setCreating] = useState(false);

  const [newName, setNewName] = useState("");
  const [newType, setNewType] = useState("nsupdate");
  // nsupdate fields
  const [nsServer, setNsServer] = useState("");
  const [nsPort, setNsPort] = useState(53);
  const [nsKeyName, setNsKeyName] = useState("");
  const [nsKeySecret, setNsKeySecret] = useState("");
  const [nsAlgorithm, setNsAlgorithm] = useState("hmac-sha256");
  const [nsZone, setNsZone] = useState("");
  // route53 fields
  const [r53AccessKey, setR53AccessKey] = useState("");
  const [r53SecretKey, setR53SecretKey] = useState("");
  const [r53ZoneId, setR53ZoneId] = useState("");

  const loadProviders = () => {
    fetch("/api/v1/dns-providers")
      .then((r) => r.ok ? r.json() : [])
      .then((data) => setProviders(Array.isArray(data) ? data : []))
      .catch(() => setError("Failed to load DNS providers"));
  };

  useEffect(() => { loadProviders(); }, []);

  const handleCreate = async () => {
    if (!newName.trim()) { setError("Name is required"); return; }
    setCreating(true);
    setError("");

    const config: Record<string, any> = {};
    if (newType === "nsupdate") {
      if (!nsServer) { setError("Server is required"); setCreating(false); return; }
      if (!nsKeyName || !nsKeySecret) { setError("TSIG key name and secret are required"); setCreating(false); return; }
      config.server = nsServer;
      config.port = nsPort;
      config.key_name = nsKeyName;
      config.key_secret = nsKeySecret;
      config.key_algorithm = nsAlgorithm;
      config.default_zone = nsZone;
    } else if (newType === "route53") {
      if (!r53AccessKey || !r53SecretKey || !r53ZoneId) { setError("All Route53 fields are required"); setCreating(false); return; }
      config.access_key_id = r53AccessKey;
      config.secret_access_key = r53SecretKey;
      config.hosted_zone_id = r53ZoneId;
    }

    try {
      const resp = await fetch("/api/v1/dns-providers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName, type: newType, config }),
      });
      if (resp.ok) {
        setShowCreate(false);
        setNewName(""); setNsServer(""); setNsKeyName(""); setNsKeySecret(""); setNsZone("");
        setR53AccessKey(""); setR53SecretKey(""); setR53ZoneId("");
        loadProviders();
      } else {
        const err = await resp.json().catch(() => ({ detail: "Create failed" }));
        setError(err.detail || "Create failed");
      }
    } catch { setError("Failed to connect"); }
    setCreating(false);
  };

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete DNS provider "${name}"?`)) return;
    try {
      await fetch(`/api/v1/dns-providers/${id}`, { method: "DELETE" });
      loadProviders();
    } catch { setError("Delete failed"); }
  };

  return (
    <>
      <PageSection>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <Title headingLevel="h1">DNS Providers</Title>
          <Button variant="primary" onClick={() => setShowCreate(!showCreate)}>
            {showCreate ? "Cancel" : "Add DNS Provider"}
          </Button>
        </div>

        {error && <Alert variant="danger" title={error} isInline style={{ marginBottom: 16 }} />}

        {showCreate && (
          <Card style={{ marginBottom: 16 }}>
            <CardBody>
              <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 500 }}>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Name</label>
                  <input style={inputStyle} value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Production BIND" />
                </div>
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Type</label>
                  <select style={inputStyle} value={newType} onChange={(e) => setNewType(e.target.value)}>
                    <option value="nsupdate">BIND (nsupdate)</option>
                    <option value="route53">AWS Route53</option>
                  </select>
                </div>

                {newType === "nsupdate" && (
                  <>
                    <div style={{ display: "flex", gap: 8 }}>
                      <div style={{ flex: 3 }}>
                        <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Server</label>
                        <input style={inputStyle} value={nsServer} onChange={(e) => setNsServer(e.target.value)} placeholder="10.0.0.53" />
                      </div>
                      <div style={{ flex: 1 }}>
                        <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Port</label>
                        <input style={inputStyle} type="number" value={nsPort} onChange={(e) => setNsPort(parseInt(e.target.value) || 53)} />
                      </div>
                    </div>
                    <div>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>TSIG Key Name</label>
                      <input style={inputStyle} value={nsKeyName} onChange={(e) => setNsKeyName(e.target.value)} placeholder="update-key" />
                    </div>
                    <div>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>TSIG Key Secret</label>
                      <input style={inputStyle} type="password" value={nsKeySecret} onChange={(e) => setNsKeySecret(e.target.value)} />
                    </div>
                    <div>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Algorithm</label>
                      <select style={inputStyle} value={nsAlgorithm} onChange={(e) => setNsAlgorithm(e.target.value)}>
                        <option value="hmac-sha256">HMAC-SHA256</option>
                        <option value="hmac-md5">HMAC-MD5</option>
                      </select>
                    </div>
                    <div>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Default Zone</label>
                      <input style={inputStyle} value={nsZone} onChange={(e) => setNsZone(e.target.value)} placeholder="dynamic.example.com" />
                    </div>
                  </>
                )}

                {newType === "route53" && (
                  <>
                    <div>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Access Key ID</label>
                      <input style={inputStyle} value={r53AccessKey} onChange={(e) => setR53AccessKey(e.target.value)} />
                    </div>
                    <div>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Secret Access Key</label>
                      <input style={inputStyle} type="password" value={r53SecretKey} onChange={(e) => setR53SecretKey(e.target.value)} />
                    </div>
                    <div>
                      <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Hosted Zone ID</label>
                      <input style={inputStyle} value={r53ZoneId} onChange={(e) => setR53ZoneId(e.target.value)} placeholder="Z1234567890" />
                    </div>
                  </>
                )}

                <Button variant="primary" isDisabled={creating} onClick={handleCreate}>
                  {creating ? "Creating..." : "Create"}
                </Button>
              </div>
            </CardBody>
          </Card>
        )}

        {providers.length === 0 && !showCreate && (
          <Card><CardBody style={{ textAlign: "center", padding: 40, opacity: 0.6 }}>
            No DNS providers configured
          </CardBody></Card>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {providers.map((p) => (
            <Card key={p.id}>
              <CardBody style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div>
                  <div style={{ fontWeight: 600 }}>{p.name}</div>
                  <div style={{ fontSize: 12, opacity: 0.7, marginTop: 2 }}>
                    {typeLabels[p.type] || p.type}
                    {p.config.server && ` — ${p.config.server}`}
                    {p.config.default_zone && ` (${p.config.default_zone})`}
                    {p.config.hosted_zone_id && ` — Zone: ${p.config.hosted_zone_id}`}
                  </div>
                </div>
                <Button variant="danger" onClick={() => handleDelete(p.id, p.name)}>Delete</Button>
              </CardBody>
            </Card>
          ))}
        </div>
      </PageSection>
    </>
  );
}
