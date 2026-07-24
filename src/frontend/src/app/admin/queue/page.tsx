"use client";

import { useEffect, useState } from "react";
import {
  PageSection,
  Card,
  CardBody,
  Button,
  Label,
  Alert,
} from "@patternfly/react-core";

interface QueueInfo {
  name: string;
  queued: number;
  started: number;
  failed: number;
  deferred: number;
  error?: string;
}

interface WorkerInfo {
  name: string;
  state: string;
  queues: string[];
  current_job: string;
  current_queue: string;
  current_func: string;
  successful_count: number;
  failed_count: number;
  total_working_time: number;
}

interface FailedJob {
  id: string;
  func: string;
  args: string[];
  error: string;
  enqueued_at: string | null;
  ended_at: string | null;
}

interface QueueStatus {
  redis: boolean;
  message?: string;
  queues?: QueueInfo[];
  workers?: WorkerInfo[];
  worker_count?: number;
  inflight_deploys?: Record<string, number>;
}

export default function QueuePage() {
  const [status, setStatus] = useState<QueueStatus | null>(null);
  const [failedJobs, setFailedJobs] = useState<FailedJob[]>([]);
  const [failedQueue, setFailedQueue] = useState("project_lifecycle");
  const [failedCount, setFailedCount] = useState(0);
  const [retrying, setRetrying] = useState<string | null>(null);

  const fetchStatus = () => {
    fetch("/api/v1/admin/queue-status")
      .then((r) => r.json())
      .then(setStatus)
      .catch(() => setStatus({ redis: false, message: "Failed to fetch queue status" }));
  };

  const fetchFailed = (queueName: string) => {
    fetch(`/api/v1/admin/failed-jobs?queue_name=${queueName}`)
      .then((r) => r.json())
      .then((data) => {
        setFailedJobs(data.jobs || []);
        setFailedCount(data.count || 0);
      })
      .catch(() => setFailedJobs([]));
  };

  const retryJob = (jobId: string) => {
    setRetrying(jobId);
    fetch(`/api/v1/admin/failed-jobs/${jobId}/retry`, { method: "POST" })
      .then(() => {
        fetchFailed(failedQueue);
        fetchStatus();
      })
      .finally(() => setRetrying(null));
  };

  const deleteJob = (jobId: string) => {
    if (!confirm("Delete this failed job?")) return;
    fetch(`/api/v1/admin/failed-jobs/${jobId}`, { method: "DELETE" })
      .then(() => {
        fetchFailed(failedQueue);
        fetchStatus();
      });
  };

  useEffect(() => {
    fetchStatus();
    fetchFailed(failedQueue);
    const interval = setInterval(() => {
      fetchStatus();
      fetchFailed(failedQueue);
    }, 5000);
    return () => clearInterval(interval);
  }, [failedQueue]);

  if (!status) return <PageSection><p>Loading...</p></PageSection>;

  if (!status.redis) {
    return (
      <PageSection>
        <Alert variant="info" isInline title="Redis Not Connected">
          {status.message || "Running in single-process mode. Jobs execute as threads in the backend."}
        </Alert>
      </PageSection>
    );
  }

  const totalQueued = (status.queues || []).reduce((s, q) => s + q.queued, 0);
  const totalStarted = (status.queues || []).reduce((s, q) => s + q.started, 0);
  const totalFailed = (status.queues || []).reduce((s, q) => s + q.failed, 0);

  return (
    <PageSection>
      {/* Summary cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12, marginBottom: 20 }}>
        <Card isCompact>
          <CardBody>
            <div style={{ fontSize: 11, opacity: 0.6, marginBottom: 4 }}>QUEUED</div>
            <div style={{ fontSize: 28, fontWeight: 600 }}>{totalQueued}</div>
          </CardBody>
        </Card>
        <Card isCompact>
          <CardBody>
            <div style={{ fontSize: 11, opacity: 0.6, marginBottom: 4 }}>RUNNING</div>
            <div style={{ fontSize: 28, fontWeight: 600, color: "var(--pf-t--global--color--status--info--default)" }}>{totalStarted}</div>
          </CardBody>
        </Card>
        <Card isCompact>
          <CardBody>
            <div style={{ fontSize: 11, opacity: 0.6, marginBottom: 4 }}>FAILED</div>
            <div style={{ fontSize: 28, fontWeight: 600, color: totalFailed > 0 ? "var(--pf-t--global--color--status--danger--default)" : undefined }}>{totalFailed}</div>
          </CardBody>
        </Card>
        <Card isCompact>
          <CardBody>
            <div style={{ fontSize: 11, opacity: 0.6, marginBottom: 4 }}>WORKERS</div>
            <div style={{ fontSize: 28, fontWeight: 600, color: "var(--pf-t--global--color--status--success--default)" }}>{status.worker_count || 0}</div>
          </CardBody>
        </Card>
      </div>

      {/* Queues */}
      <Card isCompact style={{ marginBottom: 16 }}>
        <CardBody>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Queues</div>
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>NAME</th>
                <th style={{ textAlign: "right", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>QUEUED</th>
                <th style={{ textAlign: "right", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>RUNNING</th>
                <th style={{ textAlign: "right", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>FAILED</th>
              </tr>
            </thead>
            <tbody>
              {(status.queues || []).map((q) => (
                <tr key={q.name} style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
                  <td style={{ padding: "6px 8px" }}><Label isCompact>{q.name}</Label></td>
                  <td style={{ padding: "6px 8px", textAlign: "right" }}>{q.queued}</td>
                  <td style={{ padding: "6px 8px", textAlign: "right" }}>{q.started}</td>
                  <td style={{ padding: "6px 8px", textAlign: "right", color: q.failed > 0 ? "var(--pf-t--global--color--status--danger--default)" : undefined }}>
                    {q.failed > 0 ? (
                      <Button variant="link" isInline size="sm" onClick={() => setFailedQueue(q.name)} style={{ fontSize: 13 }}>
                        {q.failed}
                      </Button>
                    ) : "0"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardBody>
      </Card>

      {/* Workers */}
      <Card isCompact style={{ marginBottom: 16 }}>
        <CardBody>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Workers</div>
          {(status.workers || []).length === 0 ? (
            <Alert variant="warning" isInline isPlain title="No workers connected. Jobs will queue until a worker starts." />
          ) : (
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
                  <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>WORKER</th>
                  <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>STATE</th>
                  <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>CURRENT JOB</th>
                  <th style={{ textAlign: "right", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>OK</th>
                  <th style={{ textAlign: "right", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>FAIL</th>
                </tr>
              </thead>
              <tbody>
                {(status.workers || []).map((w) => (
                  <tr key={w.name} style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
                    <td style={{ padding: "6px 8px", fontFamily: "monospace", fontSize: 11 }}>{w.name.slice(0, 12)}</td>
                    <td style={{ padding: "6px 8px" }}>
                      <Label isCompact color={w.state === "idle" ? "green" : w.state === "busy" ? "orange" : "grey"}>
                        {w.state}
                      </Label>
                    </td>
                    <td style={{ padding: "6px 8px", fontSize: 11 }}>
                      {w.state === "busy" && w.current_func ? (
                        <><Label isCompact color="blue">{w.current_queue}</Label> <span style={{ opacity: 0.7 }}>{w.current_func}</span></>
                      ) : (
                        <span style={{ opacity: 0.4 }}>—</span>
                      )}
                    </td>
                    <td style={{ padding: "6px 8px", textAlign: "right" }}>{w.successful_count}</td>
                    <td style={{ padding: "6px 8px", textAlign: "right", color: w.failed_count > 0 ? "var(--pf-t--global--color--status--danger--default)" : undefined }}>{w.failed_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardBody>
      </Card>

      {/* In-flight deploys */}
      {status.inflight_deploys && Object.keys(status.inflight_deploys).length > 0 && (
        <Card isCompact style={{ marginBottom: 16 }}>
          <CardBody>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>In-Flight Deploys by Host</div>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
              {Object.entries(status.inflight_deploys).map(([hostId, count]) => (
                <div key={hostId} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <span style={{ fontFamily: "monospace", fontSize: 11 }}>{hostId}</span>
                  <Label isCompact color="blue">{count}</Label>
                </div>
              ))}
            </div>
          </CardBody>
        </Card>
      )}

      {/* Failed jobs */}
      {failedJobs.length > 0 && (
        <Card isCompact>
          <CardBody>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>
                Failed Jobs — <Label isCompact>{failedQueue}</Label> ({failedCount})
              </div>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {failedJobs.map((job) => (
                <div key={job.id} style={{
                  padding: 10, borderRadius: 6,
                  border: "1px solid var(--pf-t--global--border--color--default)",
                  background: "var(--pf-t--global--background--color--secondary--default)",
                }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 4 }}>
                    <div>
                      <span style={{ fontFamily: "monospace", fontSize: 11, opacity: 0.5 }}>{job.id.slice(0, 12)}</span>
                      <span style={{ fontSize: 12, marginLeft: 8 }}>{job.func.split(".").pop()}</span>
                    </div>
                    <div style={{ display: "flex", gap: 4 }}>
                      <Button variant="secondary" size="sm" isDisabled={retrying === job.id} onClick={() => retryJob(job.id)}>
                        {retrying === job.id ? "..." : "Retry"}
                      </Button>
                      <Button variant="danger" size="sm" onClick={() => deleteJob(job.id)}>Delete</Button>
                    </div>
                  </div>
                  {job.args.length > 0 && (
                    <div style={{ fontSize: 11, opacity: 0.6, marginBottom: 4 }}>args: {job.args.join(", ")}</div>
                  )}
                  <pre style={{
                    fontSize: 11, padding: 6, borderRadius: 4, margin: 0,
                    background: "rgba(239,68,68,0.1)", color: "var(--pf-t--global--color--status--danger--default)",
                    whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 120, overflowY: "auto",
                  }}>
                    {job.error}
                  </pre>
                  {job.ended_at && (
                    <div style={{ fontSize: 10, opacity: 0.4, marginTop: 4 }}>{new Date(job.ended_at).toLocaleString()}</div>
                  )}
                </div>
              ))}
            </div>
          </CardBody>
        </Card>
      )}
    </PageSection>
  );
}
