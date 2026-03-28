export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/+$/, "") || "";

function buildUrl(baseUrl: string, path: string): string {
  return `${baseUrl}${path.startsWith("/") ? "" : "/"}${path}`;
}

async function parseResponse(res: Response) {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    if (res.status >= 500 && !API_BASE_URL) {
      throw new Error(
        `Backend proxy error (${res.status}). Make sure FastAPI is running on http://127.0.0.1:8000, then refresh.`
      );
    }
    throw new Error(`API ${res.status}: ${text || res.statusText}`);
  }

  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res.text();
}

async function apiFetch(path: string, init?: RequestInit) {
  const primaryUrl = buildUrl(API_BASE_URL, path);
  try {
    const res = await fetch(primaryUrl, init);
    return await parseResponse(res);
  } catch (error: any) {
    // If an explicit API base URL fails, retry via Next.js rewrite proxy.
    if (API_BASE_URL) {
      const proxyUrl = buildUrl("", path);
      try {
        const retryRes = await fetch(proxyUrl, init);
        return await parseResponse(retryRes);
      } catch (retryError: any) {
        const firstReason = String(error?.message || error);
        const retryReason = String(retryError?.message || retryError);
        throw new Error(
          `Unable to reach backend API. Tried ${primaryUrl} and ${proxyUrl}. Start backend server on port 8000 and retry. Errors: ${firstReason}; ${retryReason}`
        );
      }
    } else {
      const reason = String(error?.message || error);
      throw new Error(
        `Unable to reach backend API at ${primaryUrl}. Start backend server on port 8000 and retry. Original error: ${reason}`
      );
    }
  }
}

export type DocumentOut = {
  id: number;
  filename: string;
  source: string;
  company: string;
  risk_score: number;
  mnpi_score: number;
  restricted: boolean;
  created_at: string;
};

export type AlertOut = {
  id: number;
  alert_type: string;
  severity: number;
  title: string;
  employee_id: string | null;
  document_id: number | null;
  trade_id: number | null;
  created_at: string;
  resolved: boolean;
  details: Record<string, unknown>;
};

export type CorrelationEdge = {
  employee_id: string;
  symbol: string;
  document_id: number;
  trade_id: number;
  score: number;
  access_time: string;
  trade_time: string;
};

export type InvestigationTradeOut = {
  employee_id: string;
  symbol: string;
  quantity: number;
  access_time: string;
  trade_time: string;
  time_difference_days: number;
  risk_tag: "HIGH" | "LOW";
};

export type EmployeeInvestigationOut = {
  employee_id: string;
  document_id: number;
  document_company: string;
  document_created_at: string;
  access_time: string;
  access_source: string;
  note?: string | null;
  trades_after_access: InvestigationTradeOut[];
  total_trades_after_access: number;
  matching_trades_count: number;
  employee_total_trades_in_db: number;
  employee_earliest_trade_at?: string | null;
  employee_latest_trade_at?: string | null;
  hint?: string | null;
};

export async function seedDemo() {
  return apiFetch("/api/seed-demo", { method: "POST" });
}

export async function resetAll() {
  return apiFetch("/api/reset", { method: "POST" });
}

export async function listDocuments(): Promise<DocumentOut[]> {
  return apiFetch("/api/documents");
}

export async function logDocumentAccess(id: number, employee_id: string, access_type: "view" | "download") {
  return apiFetch(`/api/documents/${id}/access`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ employee_id, access_type })
  });
}

export function documentViewUrl(id: number): string {
  return `${API_BASE_URL}/api/documents/${id}/view`;
}

export function documentDownloadUrl(id: number): string {
  return `${API_BASE_URL}/api/documents/${id}/download`;
}

export async function uploadDocument(file: File): Promise<DocumentOut> {
  const fd = new FormData();
  fd.append("file", file);
  return apiFetch("/api/documents/upload", { method: "POST", body: fd });
}

export async function listTrades(): Promise<Array<Record<string, unknown>>> {
  return apiFetch("/api/trades");
}

export async function listAlerts(): Promise<AlertOut[]> {
  return apiFetch("/api/alerts");
}

export async function getCorrelation(): Promise<{ edges: CorrelationEdge[] }> {
  return apiFetch("/api/correlation");
}

export async function getAutoDetectedTradesDebug(documentId?: number): Promise<{
  ok: boolean;
  document_id?: number;
  extracted_company?: string;
  normalized_company?: string;
  available_trade_symbols?: string[];
  matching_buy_trades?: number;
  message?: string;
}> {
  const qs = typeof documentId === "number" ? `?document_id=${documentId}` : "";
  return apiFetch(`/api/auto-detected-trades/debug${qs}`);
}

export async function importTradesCsv(
  file: File,
  options?: {
    alignToAccess?: boolean;
    documentId?: number | null;
    replaceEmployeesInCsv?: boolean;
  }
): Promise<{
  ok: boolean;
  imported_trades: number;
  align_to_access?: boolean;
  document_id?: number | null;
  replace_employees_in_csv?: boolean;
}> {
  const alignToAccess = options?.alignToAccess === true;
  const documentId = options?.documentId;
  const replaceEmployeesInCsv = options?.replaceEmployeesInCsv !== false;
  const fd = new FormData();
  fd.append("file", file);
  const params = new URLSearchParams();
  params.set("align_to_access", alignToAccess ? "true" : "false");
  params.set("replace_employees_in_csv", replaceEmployeesInCsv ? "true" : "false");
  if (typeof documentId === "number") {
    params.set("document_id", String(documentId));
  }
  return apiFetch(`/api/trades/import-csv?${params.toString()}`, { method: "POST", body: fd });
}

export async function getEmployeeInvestigation(employeeId: string, documentId?: number): Promise<EmployeeInvestigationOut> {
  const qs =
    typeof documentId === "number" ? `?document_id=${documentId}` : "";
  return apiFetch(`/api/employee-investigation/${encodeURIComponent(employeeId)}${qs}`);
}
