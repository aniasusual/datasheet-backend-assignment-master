import { useCallback, useRef, useState, useEffect } from 'react';
import { Upload, Play, RefreshCw, FileText, ChevronRight } from 'lucide-react';
import { api } from '../api/client';
import type { Document, ExtractionStatus, DocumentExtractionProgress } from '../types';

interface Props {
  sessionId: string;
  documents: Document[];
  selectedDocId: string | null;
  onSelectDoc: (docId: string) => void;
  onRefresh: () => void;
  onExtractionComplete?: () => void;
}

const STATUS_COLORS: Record<string, string> = {
  uploading: 'bg-yellow-500/20 text-yellow-400',
  uploaded: 'bg-blue-500/20 text-blue-400',
  extracting: 'bg-purple-500/20 text-purple-400',
  extracted: 'bg-emerald-500/20 text-emerald-400',
  failed: 'bg-red-500/20 text-red-400',
};

const PHASE_LABELS: Record<string, string> = {
  queued: 'Queued',
  discovery: 'Discovering fields',
  extracting_values: 'Extracting values',
  verifying: 'Verifying',
  post_processing: 'Post-processing',
  done: 'Done',
  failed: 'Failed',
};

function ProgressBar({ doc }: { doc: DocumentExtractionProgress }) {
  if (doc.phase === 'queued') {
    return (
      <div className="mt-1.5">
        <div className="text-[10px] text-gray-500">Waiting...</div>
      </div>
    );
  }

  if (doc.phase === 'done') {
    return (
      <div className="mt-1.5">
        <div className="text-[10px] text-emerald-400">{doc.fields_extracted} fields extracted</div>
      </div>
    );
  }

  if (doc.phase === 'failed') {
    return (
      <div className="mt-1.5">
        <div className="text-[10px] text-red-400">Failed</div>
      </div>
    );
  }

  // Active extraction — show page + phase
  const pageProgress = doc.total_pages > 0
    ? Math.round((doc.current_page / doc.total_pages) * 100)
    : 0;

  return (
    <div className="mt-1.5 space-y-1">
      <div className="flex items-center justify-between text-[10px]">
        <span className="text-purple-300">{PHASE_LABELS[doc.phase] || doc.phase}</span>
        <span className="text-gray-500">p.{doc.current_page}/{doc.total_pages}</span>
      </div>
      <div className="h-1 bg-gray-700 rounded-full overflow-hidden">
        <div
          className="h-full bg-purple-500 rounded-full transition-all duration-500"
          style={{ width: `${pageProgress}%` }}
        />
      </div>
    </div>
  );
}

export default function DocumentSidebar({ sessionId, documents, selectedDocId, onSelectDoc, onRefresh, onExtractionComplete }: Props) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [extractionStatus, setExtractionStatus] = useState<ExtractionStatus | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const startPolling = useCallback(() => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const status = await api.getExtractionStatus(sessionId);
        setExtractionStatus(status);

        if (status.status === 'completed' || status.status === 'failed') {
          stopPolling();
          setExtracting(false);
          onRefresh();
          if (status.status === 'completed') {
            onExtractionComplete?.();
          }
          // Clear status after a brief delay so user sees the final state
          setTimeout(() => setExtractionStatus(null), 3000);
        } else {
          // Refresh doc list to update status badges
          onRefresh();
        }
      } catch {
        // Ignore polling errors
      }
    }, 1500);
  }, [sessionId, stopPolling, onRefresh, onExtractionComplete]);

  const handleUpload = useCallback(async (files: FileList | null) => {
    if (!files?.length) return;
    setUploading(true);
    try {
      await api.uploadDocuments(sessionId, Array.from(files));
      onRefresh();
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Upload failed');
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  }, [sessionId, onRefresh]);

  const handleExtractAll = useCallback(async () => {
    setExtracting(true);
    try {
      await api.extractAll(sessionId);
      startPolling();
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Extraction failed');
      setExtracting(false);
    }
  }, [sessionId, startPolling]);

  const hasUploadedDocs = documents.some(d => d.status === 'uploaded' || d.status === 'failed');
  const isRunning = extracting || extractionStatus?.status === 'running';

  // Get progress map by document ID for easy lookup
  const docProgress = extractionStatus?.documents || {};

  return (
    <div className="w-56 bg-[#151821] border-r border-gray-800 flex flex-col h-full">
      {/* Header */}
      <div className="p-3 border-b border-gray-800">
        <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">Documents</h2>
        {isRunning && extractionStatus && (
          <div className="mt-2 text-[10px] text-purple-300">
            Extracting {extractionStatus.documents_completed}/{extractionStatus.total_documents} docs
            <span className="text-gray-500 ml-1">({extractionStatus.elapsed_seconds}s)</span>
          </div>
        )}
      </div>

      {/* Document list */}
      <div className="flex-1 overflow-y-auto">
        {documents.length === 0 ? (
          <div className="p-4 text-center text-gray-500 text-sm">
            No documents yet
          </div>
        ) : (
          documents.map((doc) => {
            const progress = docProgress[doc.id];
            const isActive = progress && !['queued', 'done', 'failed'].includes(progress.phase);

            return (
              <button
                key={doc.id}
                onClick={() => onSelectDoc(doc.id)}
                className={`w-full text-left px-3 py-2.5 border-b border-gray-800/50 transition-colors flex items-start gap-2 ${
                  selectedDocId === doc.id
                    ? 'bg-primary-600/10 border-l-2 border-l-primary-500'
                    : 'hover:bg-gray-800/50 border-l-2 border-l-transparent'
                }`}
              >
                <FileText size={14} className={`mt-0.5 shrink-0 ${isActive ? 'text-purple-400 animate-pulse' : 'text-gray-500'}`} />
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-gray-200 truncate">{doc.pump_tag || doc.filename}</div>
                  <div className="text-xs text-gray-500 truncate">{doc.filename}</div>

                  {/* Show progress during extraction, normal status otherwise */}
                  {progress && progress.phase !== 'done' && progress.phase !== 'queued' ? (
                    <ProgressBar doc={progress} />
                  ) : (
                    <div className="flex items-center gap-2 mt-1">
                      <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${STATUS_COLORS[doc.status] || ''}`}>
                        {doc.status}
                      </span>
                      <span className="text-[10px] text-gray-600">{doc.num_pages}p</span>
                      {progress?.phase === 'done' && (
                        <span className="text-[10px] text-emerald-400">{progress.fields_extracted} fields</span>
                      )}
                    </div>
                  )}
                </div>
                {selectedDocId === doc.id && <ChevronRight size={12} className="text-primary-400 mt-1 shrink-0" />}
              </button>
            );
          })
        )}
      </div>

      {/* Actions */}
      <div className="p-3 border-t border-gray-800 space-y-2">
        <input
          ref={fileRef}
          type="file"
          accept=".pdf"
          multiple
          className="hidden"
          onChange={(e) => handleUpload(e.target.files)}
        />
        <button
          onClick={() => fileRef.current?.click()}
          disabled={uploading || isRunning}
          className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 text-gray-200 text-sm rounded-lg transition-colors disabled:opacity-50"
        >
          <Upload size={14} />
          {uploading ? 'Uploading...' : 'Upload PDF'}
        </button>

        {hasUploadedDocs && (
          <button
            onClick={handleExtractAll}
            disabled={isRunning}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-primary-600 hover:bg-primary-700 text-white text-sm rounded-lg transition-colors disabled:opacity-50"
          >
            {isRunning ? <RefreshCw size={14} className="animate-spin" /> : <Play size={14} />}
            {isRunning ? 'Extracting...' : 'Extract All'}
          </button>
        )}
      </div>
    </div>
  );
}
