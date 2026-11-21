"use client";

import LabeledField from "@/components/LabeledField";
import { useEffect, useState } from "react";
import {
  PageSection,
  Title,
  Button,
  Card,
  CardBody,
  Alert,
  Label,
} from "@patternfly/react-core";
import { appConfirm } from "@/lib/confirm";
import { Organization, quotaFetch } from "@/lib/quotas";
import OrganizationAssignment from "@/components/quotas/OrganizationAssignment";

interface UserInfo {
  id: string;
  email: string;
  display_name: string | null;
  role: string;
  auth_source: string;
  created_at: string;
  access_enabled: boolean;
  access_starts_at?: string | null;
  access_ends_at?: string | null;
  organization_id?: string | null;
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

const roleColors: Record<string, "purple" | "orange" | "grey"> = {
  admin: "purple",
  operator: "orange",
  user: "grey",
};

const roleLabels: Record<string, string> = {
  admin: "Admin",
  operator: "Operator",
  user: "User",
};

function toLocalInput(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fromLocalInput(value: string): string | null {
  if (!value.trim()) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}

function windowStatus(user: UserInfo): "not_started" | "expired" | null {
  const now = Date.now();
  if (user.access_starts_at && new Date(user.access_starts_at).getTime() > now) {
    return "not_started";
  }
  if (user.access_ends_at && new Date(user.access_ends_at).getTime() <= now) {
    return "expired";
  }
  return null;
}

export default function AdminUsersPage() {
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [currentUserId, setCurrentUserId] = useState("");
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState("");

  const [newEmail, setNewEmail] = useState("");
  const [newRole, setNewRole] = useState("user");
  const [newStartsAt, setNewStartsAt] = useState("");
  const [newEndsAt, setNewEndsAt] = useState("");

  const [editId, setEditId] = useState<string | null>(null);
  const [editRole, setEditRole] = useState("");
  const [editStartsAt, setEditStartsAt] = useState("");
  const [editEndsAt, setEditEndsAt] = useState("");

  const loadUsers = () => {
    fetch("/api/v1/users/")
      .then((r) => { if (!r.ok) throw new Error("Failed to load users"); return r.json(); })
      .then((data) => setUsers(Array.isArray(data) ? data : []))
      .catch(() => {
        setError("Failed to load users");
        setUsers([]);
      });
  };

  useEffect(() => {
    let live = true;
    quotaFetch<Organization[]>("/quotas/organizations").then(data => { if (live && Array.isArray(data)) setOrganizations(data); }).catch(() => { if (live) setOrganizations([]); });
    fetch("/api/v1/auth/me")
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data?.id) setCurrentUserId(data.id);
      })
      .catch(() => {});

    loadUsers();
    const interval = setInterval(loadUsers, 10000);
    return () => { live = false; clearInterval(interval); };
  }, []);

  const handleCreate = async () => {
    if (!newEmail.trim()) {
      setError("Email is required");
      return;
    }
    setCreating(true);
    setError("");
    setNotice("");
    try {
      const resp = await fetch("/api/v1/users/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: newEmail,
          role: newRole,
          access_starts_at: fromLocalInput(newStartsAt),
          access_ends_at: fromLocalInput(newEndsAt),
        }),
      });
      if (resp.ok) {
        setNotice("Account created with access enabled.");
        setShowCreate(false);
        setNewEmail("");
        setNewRole("user");
        setNewStartsAt("");
        setNewEndsAt("");
        loadUsers();
      } else {
        const err = await resp.json().catch(() => ({ detail: "Create failed" }));
        setError(err.detail || "Create failed");
      }
    } catch {
      setError("Failed to connect");
    }
    setCreating(false);
  };

  const handleSaveEdit = async (userId: string) => {
    setError("");
    try {
      const resp = await fetch(`/api/v1/users/${userId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          role: editRole,
          access_starts_at: fromLocalInput(editStartsAt),
          access_ends_at: fromLocalInput(editEndsAt),
        }),
      });
      if (resp.ok) {
        setEditId(null);
        loadUsers();
      } else {
        const err = await resp.json().catch(() => ({ detail: "Update failed" }));
        setError(err.detail || "Update failed");
      }
    } catch {
      setError("Failed to connect");
    }
  };

  const handleAccess = async (user: UserInfo) => {
    if (user.access_enabled && !(await appConfirm({ title: "Disable access", message: `Disable Troshka access for ${user.email}? Their projects and billing records will be preserved.`, confirmLabel: "Disable", variant: "danger" }))) return;
    setError("");
    try {
      const response = await fetch(`/api/v1/users/${user.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ access_enabled: !user.access_enabled }) });
      if (!response.ok) {
        const body = await response.json();
        throw new Error(body.detail || "Access update failed");
      }
      loadUsers();
    } catch (e) { setError(e instanceof Error ? e.message : "Access update failed"); }
  };

  const handleDelete = async (id: string, email: string) => {
    if (!(await appConfirm({
      title: "Delete User",
      message: `Delete user "${email}"? This cannot be undone.`,
      confirmLabel: "Delete",
      variant: "danger",
    }))) return;
    setError("");
    try {
      const resp = await fetch(`/api/v1/users/${id}`, { method: "DELETE" });
      if (resp.ok) {
        loadUsers();
      } else {
        const err = await resp.json().catch(() => ({ detail: "Delete failed" }));
        setError(err.detail || "Delete failed");
      }
    } catch {
      setError("Delete failed");
    }
  };

  const beginEdit = (user: UserInfo) => {
    setEditId(user.id);
    setEditRole(user.role);
    setEditStartsAt(toLocalInput(user.access_starts_at));
    setEditEndsAt(toLocalInput(user.access_ends_at));
  };

  return (
      <PageSection>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: 16,
          }}
        >
          <Title headingLevel="h1">Users</Title>
          <Button
            variant="primary"
            onClick={() => setShowCreate(!showCreate)}
          >
            {showCreate ? "Cancel" : "Add User"}
          </Button>
        </div>

        {error && (
          <Alert
            variant="danger"
            title={error}
            isInline
            style={{ marginBottom: 16 }}
          />
        )}
        {notice && <Alert variant="info" title={notice} isInline style={{ marginBottom: 16 }} />}
        <p style={{ fontSize: 13, opacity: 0.7, marginBottom: 16 }}>Manage Troshka access and roles here. SSO verifies identity; enabled accounts can sign in. Optional access start/end dates limit login and provisioning; ending access auto-stops owned projects. New users remain unassigned until an administrator assigns an organization.</p>

        {showCreate && (
          <Card style={{ marginBottom: 16 }}>
            <CardBody>
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 12,
                  maxWidth: 500,
                }}
              >
                <div>
                  <LabeledField
                    style={{ fontSize: 12, display: "block", marginBottom: 4 }}
                   label={<>
                    Email
                  </>}>
                  <input
                    type="email"
                    style={inputStyle}
                    value={newEmail}
                    onChange={(e) => setNewEmail(e.target.value)}
                    placeholder="user@redhat.com"
                  /></LabeledField>
                </div>
                <div>
                  <LabeledField
                    style={{ fontSize: 12, display: "block", marginBottom: 4 }}
                   label={<>
                    Role
                  </>}>
                  <select
                    style={inputStyle}
                    value={newRole}
                    onChange={(e) => setNewRole(e.target.value)}
                  >
                    <option value="user">User</option>
                    <option value="operator">Operator</option>
                    <option value="admin">Admin</option>
                  </select></LabeledField>
                </div>
                <div>
                  <LabeledField
                    style={{ fontSize: 12, display: "block", marginBottom: 4 }}
                    label={<>Access starts (optional)</>}
                  >
                    <input
                      type="datetime-local"
                      style={inputStyle}
                      value={newStartsAt}
                      onChange={(e) => setNewStartsAt(e.target.value)}
                      aria-label="Access starts"
                    />
                  </LabeledField>
                </div>
                <div>
                  <LabeledField
                    style={{ fontSize: 12, display: "block", marginBottom: 4 }}
                    label={<>Access ends (optional)</>}
                  >
                    <input
                      type="datetime-local"
                      style={inputStyle}
                      value={newEndsAt}
                      onChange={(e) => setNewEndsAt(e.target.value)}
                      aria-label="Access ends"
                    />
                  </LabeledField>
                </div>
                <Button
                  variant="primary"
                  isDisabled={creating}
                  onClick={handleCreate}
                >
                  {creating ? "Creating..." : "Create"}
                </Button>
              </div>
            </CardBody>
          </Card>
        )}

        {users.length === 0 && !showCreate && (
          <Card>
            <CardBody
              style={{ textAlign: "center", padding: 40, opacity: 0.6 }}
            >
              No users found
            </CardBody>
          </Card>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {users.map((u) => {
            const status = windowStatus(u);
            return (
            <Card key={u.id}>
              <CardBody
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: 12,
                  flexWrap: "wrap",
                }}
              >
                <div style={{ flex: 1, minWidth: 240 }}>
                  <div style={{ fontWeight: 600 }}>
                    {u.email}
                    {u.id === currentUserId && (
                      <span
                        style={{
                          fontSize: 11,
                          opacity: 0.5,
                          marginLeft: 8,
                        }}
                      >
                        (you)
                      </span>
                    )}
                  </div>
                  <div
                    style={{ fontSize: 12, opacity: 0.7, marginTop: 2 }}
                  >
                    {u.auth_source}
                    {" · joined "}
                    {new Date(u.created_at).toLocaleDateString()}
                    {u.access_starts_at && ` · starts ${new Date(u.access_starts_at).toLocaleString()}`}
                    {u.access_ends_at && ` · ends ${new Date(u.access_ends_at).toLocaleString()}`}
                  </div>
                  <OrganizationAssignment key={`${u.id}:${u.organization_id || "unassigned"}`} user={u} organizations={organizations} onChanged={message => { setNotice(message); loadUsers(); }} />
                </div>

                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    flexWrap: "wrap",
                  }}
                >
                  {editId === u.id ? (
                    <>
                      <select
                        style={{ ...inputStyle, width: "auto" }}
                        value={editRole}
                        onChange={(e) => setEditRole(e.target.value)}
                        aria-label={`Role for ${u.email}`}
                      >
                        <option value="user">User</option>
                        <option value="operator">Operator</option>
                        <option value="admin">Admin</option>
                      </select>
                      <input
                        type="datetime-local"
                        style={{ ...inputStyle, width: "auto" }}
                        value={editStartsAt}
                        onChange={(e) => setEditStartsAt(e.target.value)}
                        aria-label={`Access starts for ${u.email}`}
                      />
                      <input
                        type="datetime-local"
                        style={{ ...inputStyle, width: "auto" }}
                        value={editEndsAt}
                        onChange={(e) => setEditEndsAt(e.target.value)}
                        aria-label={`Access ends for ${u.email}`}
                      />
                      <Button
                        variant="primary"
                        size="sm"
                        onClick={() => handleSaveEdit(u.id)}
                      >
                        Save
                      </Button>
                      <Button
                        variant="link"
                        size="sm"
                        onClick={() => setEditId(null)}
                      >
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <>
                      <Label color={roleColors[u.role] || "grey"}>
                        {roleLabels[u.role] || u.role}
                      </Label>
                      {!u.access_enabled && <Label color="red">Access disabled</Label>}
                      {status === "not_started" && <Label color="orange">Not started</Label>}
                      {status === "expired" && <Label color="red">Expired</Label>}
                      {u.id !== currentUserId && (
                        <>
                          <Button variant="secondary" size="sm" onClick={() => void handleAccess(u)}>{u.access_enabled ? "Disable access" : "Enable access"}</Button>
                          <Button
                            variant="secondary"
                            size="sm"
                            onClick={() => beginEdit(u)}
                          >
                            Edit
                          </Button>
                          <Button
                            variant="danger"
                            size="sm"
                            onClick={() => handleDelete(u.id, u.email)}
                          >
                            Delete
                          </Button>
                        </>
                      )}
                    </>
                  )}
                </div>
              </CardBody>
            </Card>
            );
          })}
        </div>
      </PageSection>
  );
}
