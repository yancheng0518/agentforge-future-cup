export interface Agent {
  id: string; name: string; role: string; capabilities: string[]; enabled: boolean;
  version: number; prompt: string; model: string; tools: string[];
}
export interface WorkflowNode { id: string; label: string; capability?: string; depends_on?: string[]; kind?: string; children?: WorkflowNode[]; conditional?: boolean }
export interface Workflow { id: string; name: string; description: string; nodes: WorkflowNode[] }
export interface ProjectFile { path: string; content: string }
export interface TaskDefinition {
  id: string; name: string; requirement: string; expected_tests: number;
  version?: number; provenance?: string;
  paths?: { baseline: string; requirement: string; acceptance: string };
  baseline_sha256?: string; acceptance_sha256?: string; requirement_sha256?: string; snapshot_sha256?: string;
}
export interface Knowledge { path: string; excerpt: string; score: number }
export interface Event { id: number | string; timestamp: string; type: string; node_id?: string; message: string; data?: unknown }
export interface Step { id: string; node_id?: string; label: string; status: string; agent_id?: string; agent_name?: string; attempt?: number; output?: unknown }
export interface DiffFile { path: string; before: string; after: string; diff: string }
export interface Run {
  id: string; requirement: string; workflow_id: string; mode: 'mock' | 'live'; status: string;
  created_at: string; updated_at?: string; attempt: number; summary?: string;
  events?: Event[]; steps?: Step[]; snapshots?: unknown[];
  workflow?: Workflow;
  task_id?: string; task?: TaskDefinition | null;
  model_config?: { model?: string; base_url?: string; [key: string]: unknown } | null;
  candidate_origin?: string; fault_injection?: boolean;
  diff?: { files: DiffFile[]; unified_diff: string }; applied?: boolean;
  [key: string]: unknown;
}
export interface Health {
  status?: string; mode?: string;
  model: { configured: boolean; verified?: boolean; model?: string; base_url?: string; [key: string]: unknown };
  sandbox: { available: boolean; reason?: string; [key: string]: unknown };
}
