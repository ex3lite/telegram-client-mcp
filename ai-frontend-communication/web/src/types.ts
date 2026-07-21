export interface Project {
  id: string;
  slug: string;
  name: string;
  enabled: boolean;
}

export interface AuditEvent {
  id: string;
  event_type: string;
  occurred_at: string;
  project_id: string | null;
  actor: { type: string; id: string };
  correlation_id: string;
  subject: { type: string | null; id: string | null };
  outcome: string;
  payload: Record<string, unknown>;
}

export interface Overview {
  attention: {
    open_requests: number;
    pending_clarifications: number;
    repository_errors: number;
    delivery_uncertain: number;
  };
  recent_events: AuditEvent[];
}

export interface Clarification {
  id: string;
  project_id: string;
  recipient_user_id: string;
  agent_run_id: string;
  correlation_id: string;
  context: string;
  question: string;
  status: "pending" | "answered" | "expired" | "cancelled";
  expires_at: string;
  answer: string | null;
  answered_at: string | null;
  created_at: string;
}

export interface ChangeRequest {
  id: string;
  project_id: string;
  correlation_id: string;
  source: string;
  kind: "bug" | "task" | "feature";
  title: string;
  description: string;
  priority: "low" | "normal" | "high" | "urgent";
  status: "open" | "in_progress" | "done" | "rejected";
  version: number;
  created_at: string;
  updated_at: string;
}

export interface Repository {
  id: string;
  project_id: string;
  name: string;
  ssh_url: string;
  default_branch: string;
  allowed_paths: string[];
  current_commit: string | null;
  status: "never_synced" | "syncing" | "ready" | "stale" | "failed" | "disabled";
  last_synced_at: string | null;
  last_error: string | null;
}

export interface ReadyStatus {
  status: "ok" | "not_ready";
  checks: {
    database: boolean;
    redis: boolean;
    telegram?: {
      ok: boolean;
      has_topics_enabled?: boolean;
      supports_guest_queries?: boolean;
    };
  };
}

