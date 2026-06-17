const BASE = '/api/v1';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.headers || {}),
      ...(!init?.body || init.body instanceof FormData
        ? {}
        : { 'Content-Type': 'application/json' }),
    },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.detail || err.error || `Request failed: ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

import type {
  Session,
  SessionListItem,
  SessionDetail,
  Document,
  DocumentDetail,
  FieldsResponse,
  FieldStats,
  ExtractedField,
  EquipmentEntity,
  ExtractionResult,
  QueryResult,
  FieldStatus,
} from '../types';

export const api = {
  // Sessions
  createSession: (title?: string) =>
    request<Session>('/sessions', {
      method: 'POST',
      body: JSON.stringify({ title }),
    }),

  listSessions: () =>
    request<SessionListItem[]>('/sessions'),

  getSession: (id: string) =>
    request<SessionDetail>(`/sessions/${id}`),

  deleteSession: (id: string) =>
    request<void>(`/sessions/${id}`, { method: 'DELETE' }),

  // Documents
  uploadDocuments: (sessionId: string, files: File[]) => {
    const form = new FormData();
    files.forEach((f) => form.append('files', f));
    return request<{ documents: Document[]; message: string }>(
      `/sessions/${sessionId}/documents/upload`,
      { method: 'POST', body: form }
    );
  },

  listDocuments: (sessionId: string) =>
    request<Document[]>(`/sessions/${sessionId}/documents`),

  getDocument: (sessionId: string, docId: string) =>
    request<DocumentDetail>(`/sessions/${sessionId}/documents/${docId}`),

  getPageImageUrl: (sessionId: string, docId: string, pageNum: number) =>
    `${BASE}/sessions/${sessionId}/documents/${docId}/pages/${pageNum}/image`,

  // Extraction
  extractDocument: (sessionId: string, docId: string) =>
    request<ExtractionResult>(
      `/sessions/${sessionId}/documents/${docId}/extract`,
      { method: 'POST' }
    ),

  extractAll: (sessionId: string) =>
    request<{ status: string; documents_processed: number; results: ExtractionResult[] }>(
      `/sessions/${sessionId}/documents/extract-all`,
      { method: 'POST' }
    ),

  reExtractDocument: (sessionId: string, docId: string) =>
    request<ExtractionResult>(
      `/sessions/${sessionId}/documents/${docId}/re-extract`,
      { method: 'POST' }
    ),

  // Fields
  listFields: (
    sessionId: string,
    params?: {
      document_id?: string;
      section?: string;
      status?: FieldStatus;
      min_confidence?: number;
      field_name?: string;
      page?: number;
      limit?: number;
      offset?: number;
    }
  ) => {
    const qs = new URLSearchParams();
    if (params) {
      Object.entries(params).forEach(([k, v]) => {
        if (v !== undefined && v !== '') qs.set(k, String(v));
      });
    }
    const q = qs.toString();
    return request<FieldsResponse>(
      `/sessions/${sessionId}/fields${q ? `?${q}` : ''}`
    );
  },

  getField: (sessionId: string, fieldId: string) =>
    request<ExtractedField>(`/sessions/${sessionId}/fields/${fieldId}`),

  updateField: (
    sessionId: string,
    fieldId: string,
    data: {
      raw_value?: string;
      unit?: string;
      section?: string;
      status?: FieldStatus;
      reason?: string;
    }
  ) =>
    request<ExtractedField>(`/sessions/${sessionId}/fields/${fieldId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),

  bulkVerify: (sessionId: string, fieldIds: string[]) =>
    request<{ verified: number; total_requested: number }>(
      `/sessions/${sessionId}/fields/bulk-verify`,
      { method: 'POST', body: JSON.stringify({ field_ids: fieldIds }) }
    ),

  getFieldStats: (sessionId: string) =>
    request<FieldStats>(`/sessions/${sessionId}/fields/stats`),

  // Entities
  listEntities: (sessionId: string) =>
    request<{ entities: EquipmentEntity[] }>(`/sessions/${sessionId}/entities`),

  // Query
  queryFields: (sessionId: string, question: string) =>
    request<QueryResult>(`/sessions/${sessionId}/query`, {
      method: 'POST',
      body: JSON.stringify({ question }),
    }),
};
