// Session
export interface Session {
  id: string;
  status: 'active' | 'archived';
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface SessionListItem {
  id: string;
  status: 'active' | 'archived';
  title: string | null;
  created_at: string;
  document_count: number;
}

export interface SessionDetail {
  id: string;
  status: 'active' | 'archived';
  title: string | null;
  created_at: string;
  updated_at: string;
  document_count: number;
  field_count: number;
}

// Document
export type DocumentStatus = 'uploading' | 'uploaded' | 'extracting' | 'extracted' | 'failed';

export interface Document {
  id: string;
  session_id: string;
  filename: string;
  file_path: string;
  pump_tag: string | null;
  format_type: string | null;
  status: DocumentStatus;
  num_pages: number;
  created_at: string;
}

export interface DocumentPage {
  id: string;
  page_number: number;
  raw_text: string;
  layout_text: string | null;
  tables_json: unknown[] | null;
  width: number;
  height: number;
  extraction_quality: string;
}

export interface DocumentDetail extends Document {
  pages: DocumentPage[];
}

// Extracted Field
export type FieldStatus = 'extracted' | 'verified' | 'corrected' | 'rejected';
export type FieldDataType = 'numeric' | 'text' | 'boolean' | 'reference';

export interface ExtractedField {
  id: string;
  document_id: string;
  entity_id: string | null;
  field_name: string;
  display_name: string;
  raw_value: string;
  unit: string | null;
  data_type: FieldDataType;
  section: string;
  confidence: number;
  status: FieldStatus;
  citation_page: number;
  citation_bbox: { x0: number; y0: number; x1: number; y1: number } | null;
  citation_text: string;
  created_at: string | null;
  updated_at: string | null;
  corrections?: FieldCorrection[];
}

export interface FieldCorrection {
  id: string;
  original_value: string;
  corrected_value: string;
  reason: string | null;
  corrected_by: string;
  created_at: string | null;
}

export interface FieldsResponse {
  total: number;
  offset: number;
  limit: number;
  fields: ExtractedField[];
}

export interface FieldStats {
  total_fields: number;
  by_section: Record<string, number>;
  by_status: Record<string, number>;
  by_confidence_tier: Record<string, number>;
  per_document: {
    document_id: string;
    filename: string;
    pump_tag: string | null;
    field_count: number;
  }[];
}

// Entity
export interface EquipmentEntity {
  id: string;
  tag: string;
  entity_type: string;
  name: string;
  metadata_json: Record<string, unknown> | null;
  document_count: number;
  field_count: number;
  created_at: string | null;
}

// Query
export interface QueryResult {
  answer: string;
  cited_fields: {
    id: string;
    field_name: string;
    display_name: string;
    raw_value: string;
    unit: string | null;
    section: string;
    confidence: number;
    citation_page: number;
    citation_text: string;
    filename: string;
    pump_tag: string | null;
  }[];
  confidence: 'high' | 'medium' | 'low';
}

// Extraction result
export interface ExtractionResult {
  status: string;
  document_id: string;
  fields_extracted: number;
  corrections_applied?: number;
  entity: { id: string; tag: string; name: string } | null;
}

// Agent
export interface AgentMessage {
  id?: string;
  role: 'user' | 'assistant';
  content: string;
  tool_actions?: ToolAction[] | null;
  created_at?: string | null;
}

export interface ToolAction {
  tool: string;
  args: Record<string, unknown>;
  result: Record<string, unknown>;
}

export interface AgentResponse {
  response: string;
  tool_actions: ToolAction[];
  message_id: string;
}

export interface ChatHistoryResponse {
  messages: AgentMessage[];
}

