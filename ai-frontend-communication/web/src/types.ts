export interface Project {
  id: string;
  slug: string;
  name: string;
  enabled: boolean;
}

export type MemberLanguage = "ru" | "en";
export type MemberKnowledgeScope = "integration" | "internal";

export interface ProjectMember {
  project_id: string;
  user_id: string;
  display_name: string;
  telegram_user_id: number | null;
  telegram_username: string | null;
  role: string;
  department: string | null;
  stack: string | null;
  language: MemberLanguage;
  knowledge_scope: MemberKnowledgeScope;
  can_create_requests: boolean;
  active: boolean;
  telegram_verified: boolean;
  telegram_reachable: boolean;
}

export interface AdminIdentity {
  principal_id: string;
  name: string;
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
  created_by_user_id: string | null;
  source_interaction_id: string | null;
  correlation_id: string;
  source: string;
  requester_profile: {
    display_name?: string | null;
    role?: string | null;
    department?: string | null;
    stack?: string | null;
    language?: string | null;
  };
  question: string;
  agent_summary: string;
  citations: Citation[];
  kind: "bug" | "task" | "feature" | "integration" | "change" | "question";
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
  github_repository: string | null;
  auto_sync_enabled: boolean;
  auto_sync_mode: "webhook_reconcile" | "reconcile" | "disabled";
  github_webhook_url: string;
  repository_reconcile_seconds: number;
  current_commit: string | null;
  status: "never_synced" | "syncing" | "ready" | "stale" | "failed" | "disabled";
  last_synced_at: string | null;
  last_webhook_at: string | null;
  last_webhook_commit: string | null;
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

export type ClaudeEffort = "low" | "medium" | "high" | "xhigh" | "max";
export type AnswerStyle = "brief" | "normal" | "detailed";
export type PrivacyLevel = "strict" | "balanced";
export type TelegramGroupMode = "commands_only" | "mentions" | "all_messages";
export type TelegramPrivateMode = "commands_only" | "all_messages";

export interface AgentSettings {
  project_id: string;
  enabled: boolean;
  claude_model: string | null;
  claude_effort: ClaudeEffort;
  claude_timeout_seconds: number;
  max_budget_cents: number | null;
  base_prompt: string;
  answer_style: AnswerStyle;
  privacy_level: PrivacyLevel;
  denied_globs: string[];
  memory_enabled: boolean;
  memory_recent_messages: number;
  memory_max_context_chars: number;
  telegram_group_mode: TelegramGroupMode;
  telegram_private_mode: TelegramPrivateMode;
  telegram_streaming_enabled: boolean;
  telegram_attach_markdown: boolean;
  version: number;
  updated_by_admin_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ClaudeIntegration {
  configured: boolean;
  source: "panel" | "environment" | "missing";
  proxy_configured: boolean;
}

export interface ClaudeCheck {
  ok: boolean;
  version: string | null;
  error_code: string | null;
}

export interface ClaudeOAuthStart {
  session_id: string;
  authorization_url: string;
  expires_at: string;
}

export interface McpAccount {
  id: string;
  name: string;
  active: boolean;
  tool_scopes: string[];
  project_ids: string[];
  expires_at: string | null;
  last_used_at: string | null;
  token_prefix: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface McpTokenResult {
  account: McpAccount;
  token: string;
}

export interface Citation {
  path: string;
  start_line: number;
  end_line: number;
}

export interface KnowledgeArtifact {
  name: string;
  filename?: string;
  content: string;
}

export interface ArtifactSummary {
  name?: string;
  filename?: string;
  kind?: string;
  media_type?: string;
  size_bytes?: number;
}

export interface PrivacyFinding {
  kind: string;
  location: string;
}

export interface Interaction {
  id: string;
  project_id: string;
  repository_id: string | null;
  conversation_thread_id: string | null;
  correlation_id: string;
  source: string;
  question: string;
  commit_sha: string | null;
  status: "queued" | "generating" | "answer_ready" | "published" | "failed";
  answer_markdown: string | null;
  citations: Citation[];
  rejected_citations: Array<{ citation: Citation; accepted: false; reason: string | null }>;
  uncertainty: string[];
  provider_metadata: Record<string, unknown>;
  error_code: string | null;
  artifacts: KnowledgeArtifact[];
  privacy_findings: PrivacyFinding[];
  created_at: string;
  updated_at: string;
}

export interface InteractionSummary {
  id: string;
  project_id: string;
  repository_id: string | null;
  conversation_thread_id: string | null;
  source: string;
  question: string;
  question_truncated: boolean;
  commit_sha: string | null;
  status: Interaction["status"];
  provider_metadata: Record<string, unknown>;
  error_code: string | null;
  artifacts: ArtifactSummary[];
  privacy_findings_count: number;
  created_at: string;
  updated_at: string;
}

export interface ConversationSummary {
  id: string;
  project_id: string;
  chat_id: string | null;
  user_id: string | null;
  user_display_name: string | null;
  message_count: number;
  memory_count: number;
  last_message_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationMessage {
  id: string;
  role: string;
  source: string;
  content: string;
  author_user_id: string | null;
  created_at: string;
}

export interface ConversationMemory {
  id: string;
  kind: string;
  memory_key: string;
  content: string;
  updated_at: string;
}

export interface ConversationDetail extends ConversationSummary {
  messages: ConversationMessage[];
  memories: ConversationMemory[];
}
