"use client";

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

interface UserInfo {
  id: string;
  email: string;
  display_name: string | null;
  role: string;
  auth_source: string;
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

export default function AdminUsersPage() {
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [currentUserId, setCurrentUserId] = useState("");
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [creating, setCreating] = useState(false);

  const [newEmail, setNewEmail] = useState("");
  const [newDisplayName, setNewDisplayName] = useState("");
  const [newRole, setNewRole] = useState("user");

  const [editId, setEditId] = useState<string | null>(null);
  const [editRole, setEditRole] = useState("");

  const loadUsers = () => {
    fetch("/api/v1/users/")
      .then((r) => (r.ok ? r.json() : []))
      .then((data) => setUsers(Array.isArray(data) ? data : []))
      .catch(() => setError("Failed to load users"));
  };

  useEffect(() => {
    fetch("/api/v1/auth/me")
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data?.id) setCurrentUserId(data.id);
      })
      .catch(() => {});

    loadUsers();
    const interval = setInterval(loadUsers, 10000);
    return () => clearInterval(interval);
  }, []);

  const handleCreate = async () => {
    if (!newEmail.trim()) {
      setError("Email is required");
      return;
    }
    setCreating(true);
    setError("");
    try {
      const resp = await fetch("/api/v1/users/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: newEmail,
          display_name: newDisplayName || undefined,
          role: newRole,
        }),
      });
      if (resp.ok) {
        setShowCreate(false);
        setNewEmail("");
        setNewDisplayName("");
        setNewRole("user");
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

  const handleUpdateRole = async (userId: string) => {
    setError("");
    try {
      const resp = await fetch(`/api/v1/users/${userId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: editRole }),
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

  const handleDelete = async (id: string, email: string) => {
    if (!confirm(`Delete user "${email}"? This cannot be undone.`)) return;
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

  return (
    <>
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
                  <label
                    style={{ fontSize: 12, display: "block", marginBottom: 4 }}
                  >
                    Email
                  </label>
                  <input
                    style={inputStyle}
                    value={newEmail}
                    onChange={(e) => setNewEmail(e.target.value)}
                    placeholder="user@redhat.com"
                  />
                </div>
                <div>
                  <label
                    style={{ fontSize: 12, display: "block", marginBottom: 4 }}
                  >
                    Display Name (optional)
                  </label>
                  <input
                    style={inputStyle}
                    value={newDisplayName}
                    onChange={(e) => setNewDisplayName(e.target.value)}
                    placeholder="Jane Doe"
                  />
                </div>
                <div>
                  <label
                    style={{ fontSize: 12, display: "block", marginBottom: 4 }}
                  >
                    Role
                  </label>
                  <select
                    style={inputStyle}
                    value={newRole}
                    onChange={(e) => setNewRole(e.target.value)}
                  >
                    <option value="user">User</option>
                    <option value="operator">Operator</option>
                    <option value="admin">Admin</option>
                  </select>
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
          {users.map((u) => (
            <Card key={u.id}>
              <CardBody
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <div style={{ flex: 1 }}>
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
                    {u.display_name && `${u.display_name} · `}
                    {u.auth_source}
                    {" · joined "}
                    {new Date(u.created_at).toLocaleDateString()}
                  </div>
                </div>

                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                  }}
                >
                  {editId === u.id ? (
                    <>
                      <select
                        style={{ ...inputStyle, width: "auto" }}
                        value={editRole}
                        onChange={(e) => setEditRole(e.target.value)}
                      >
                        <option value="user">User</option>
                        <option value="operator">Operator</option>
                        <option value="admin">Admin</option>
                      </select>
                      <Button
                        variant="primary"
                        size="sm"
                        onClick={() => handleUpdateRole(u.id)}
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
                      {u.id !== currentUserId && (
                        <>
                          <Button
                            variant="secondary"
                            size="sm"
                            onClick={() => {
                              setEditId(u.id);
                              setEditRole(u.role);
                            }}
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
          ))}
        </div>
      </PageSection>
    </>
  );
}
