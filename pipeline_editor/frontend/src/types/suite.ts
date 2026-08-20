import type { Recognition } from "./task";

export interface SuiteDoc {
  name: string;
  cases: string[];
  resume_after: string;
  case_entry: string;
  landing: Recognition | null; // 必须显式声明（null = 明确禁用落地检查）
  on_case_failure?: "restart_retry" | "restart_continue" | "abort";
  max_retries?: number;
  full_boot_cases?: string[];
  [key: string]: unknown;
}
