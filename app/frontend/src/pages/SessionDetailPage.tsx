import { useEffect, useState, useCallback } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft, Eye, Bot, RefreshCw } from 'lucide-react';
import { api } from '../api/client';
import type { Document, DocumentDetail, ExtractedField } from '../types';
import DocumentSidebar from '../components/DocumentSidebar';
import PageViewer from '../components/PageViewer';
import FieldPanel from '../components/FieldPanel';
import AgentChat from '../components/AgentChat';

type Mode = 'review' | 'query';

export default function SessionDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const [documents, setDocuments] = useState<Document[]>([]);
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null);
  const [selectedDoc, setSelectedDoc] = useState<DocumentDetail | null>(null);
  const [fields, setFields] = useState<ExtractedField[]>([]);
  const [selectedFieldId, setSelectedFieldId] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [mode, setMode] = useState<Mode>('review');
  const [highlightText, setHighlightText] = useState<string | null>(null);
  const [filterSection, setFilterSection] = useState('');
  const [filterStatus, setFilterStatus] = useState('');
  const [reExtracting, setReExtracting] = useState(false);
  const [injectedPrompt, setInjectedPrompt] = useState<string | null>(null);

  // Load documents
  const loadDocs = useCallback(async () => {
    if (!sessionId) return;
    const docs = await api.listDocuments(sessionId);
    setDocuments(docs);
    // Auto-select first doc if none selected
    if (!selectedDocId && docs.length > 0) {
      setSelectedDocId(docs[0].id);
    }
  }, [sessionId, selectedDocId]);

  useEffect(() => { loadDocs(); }, [loadDocs]);

  // Load selected document detail
  useEffect(() => {
    if (!sessionId || !selectedDocId) {
      setSelectedDoc(null);
      return;
    }
    api.getDocument(sessionId, selectedDocId).then(doc => {
      setSelectedDoc(doc);
      setCurrentPage(1);
    });
  }, [sessionId, selectedDocId]);

  // Load fields for selected document
  const loadFields = useCallback(async () => {
    if (!sessionId || !selectedDocId) {
      setFields([]);
      return;
    }
    const params: Record<string, string | number> = { document_id: selectedDocId, limit: 500 };
    if (filterSection) params.section = filterSection;
    if (filterStatus) params.status = filterStatus;
    const res = await api.listFields(sessionId, params);
    setFields(res.fields);
  }, [sessionId, selectedDocId, filterSection, filterStatus]);

  useEffect(() => { loadFields(); }, [loadFields]);

  // Select field → jump to its page
  const handleSelectField = useCallback((field: ExtractedField) => {
    setSelectedFieldId(field.id);
    setCurrentPage(field.citation_page);
    setHighlightText(field.citation_text);
  }, []);

  // Handle extraction complete — switch to agent and ask it to analyze
  const handleExtractionComplete = useCallback(async () => {
    if (!sessionId) return;
    setMode('query');
    setInjectedPrompt(
      'Extraction just completed for all documents. Please analyze the extracted data: ' +
      'summarize what was extracted from each document, identify any missing or low-confidence fields, ' +
      'and suggest what I should review or correct.'
    );
    loadFields();
  }, [sessionId, loadFields]);

  // Re-extract with corrections
  const handleReExtract = useCallback(async () => {
    if (!sessionId || !selectedDocId) return;
    if (!confirm('Re-extract this document? Past corrections will be used to improve results.')) return;
    setReExtracting(true);
    try {
      await api.reExtractDocument(sessionId, selectedDocId);
      loadFields();
      loadDocs();
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Re-extraction failed');
    } finally {
      setReExtracting(false);
    }
  }, [sessionId, selectedDocId, loadFields, loadDocs]);

  if (!sessionId) return null;

  const extractedDoc = selectedDoc && documents.find(d => d.id === selectedDocId);
  const isExtracted = extractedDoc?.status === 'extracted';

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Top bar */}
      <div className="h-10 bg-[#151821] border-b border-gray-800 flex items-center px-4 shrink-0 gap-3">
        <Link to="/" className="text-gray-500 hover:text-gray-300 transition-colors">
          <ArrowLeft size={16} />
        </Link>

        <div className="h-4 w-px bg-gray-700" />

        {/* Mode toggle */}
        <div className="flex items-center bg-gray-800 rounded-md p-0.5">
          <button
            onClick={() => setMode('review')}
            className={`flex items-center gap-1.5 px-3 py-1 text-xs rounded transition-colors ${
              mode === 'review' ? 'bg-primary-600 text-white' : 'text-gray-400 hover:text-gray-200'
            }`}
          >
            <Eye size={12} /> Review
          </button>
          <button
            onClick={() => setMode('query')}
            className={`flex items-center gap-1.5 px-3 py-1 text-xs rounded transition-colors ${
              mode === 'query' ? 'bg-primary-600 text-white' : 'text-gray-400 hover:text-gray-200'
            }`}
          >
            <Bot size={12} /> Agent
          </button>
        </div>

        {/* Re-extract button */}
        {mode === 'review' && isExtracted && (
          <>
            <div className="h-4 w-px bg-gray-700" />
            <button
              onClick={handleReExtract}
              disabled={reExtracting}
              className="flex items-center gap-1.5 px-3 py-1 text-xs text-gray-400 hover:text-gray-200 hover:bg-gray-800 rounded transition-colors disabled:opacity-50"
            >
              <RefreshCw size={12} className={reExtracting ? 'animate-spin' : ''} />
              {reExtracting ? 'Re-extracting...' : 'Re-extract'}
            </button>
          </>
        )}

        {/* Doc info */}
        <div className="ml-auto flex items-center gap-3 text-xs text-gray-500">
          {extractedDoc && (
            <span>{extractedDoc.pump_tag || extractedDoc.filename}</span>
          )}
          {fields.length > 0 && (
            <span>{fields.length} fields</span>
          )}
        </div>
      </div>

      {/* Main content */}
      <div className="flex-1 flex min-h-0">
        {/* Left: Document sidebar */}
        <DocumentSidebar
          sessionId={sessionId}
          documents={documents}
          selectedDocId={selectedDocId}
          onSelectDoc={(id) => { setSelectedDocId(id); setSelectedFieldId(null); setHighlightText(null); }}
          onRefresh={loadDocs}
          onExtractionComplete={handleExtractionComplete}
        />

        {/* Center: Page viewer or Query panel */}
        <div className="flex-1 min-w-0">
          {mode === 'query' ? (
            <AgentChat
              sessionId={sessionId}
              onFieldsChanged={loadFields}
              injectedPrompt={injectedPrompt}
              onPromptConsumed={() => setInjectedPrompt(null)}
            />
          ) : selectedDoc ? (
            <PageViewer
              sessionId={sessionId}
              documentId={selectedDoc.id}
              totalPages={selectedDoc.num_pages}
              currentPage={currentPage}
              onPageChange={setCurrentPage}
              highlightText={highlightText}
            />
          ) : (
            <div className="flex items-center justify-center h-full text-gray-500 text-sm">
              Select a document to view
            </div>
          )}
        </div>

        {/* Right: Field panel (review mode only) */}
        {mode === 'review' && (
          <FieldPanel
            sessionId={sessionId}
            fields={fields}
            selectedFieldId={selectedFieldId}
            onSelectField={handleSelectField}
            onFieldUpdated={loadFields}
            filterSection={filterSection}
            onFilterSection={setFilterSection}
            filterStatus={filterStatus}
            onFilterStatus={setFilterStatus}
          />
        )}
      </div>
    </div>
  );
}
