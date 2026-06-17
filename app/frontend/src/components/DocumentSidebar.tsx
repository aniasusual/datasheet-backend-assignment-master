import { useCallback, useRef, useState } from 'react';
import { Upload, Play, RefreshCw, FileText, ChevronRight } from 'lucide-react';
import { api } from '../api/client';
import type { Document } from '../types';

interface Props {
  sessionId: string;
  documents: Document[];
  selectedDocId: string | null;
  onSelectDoc: (docId: string) => void;
  onRefresh: () => void;
}

const STATUS_COLORS: Record<string, string> = {
  uploading: 'bg-yellow-500/20 text-yellow-400',
  uploaded: 'bg-blue-500/20 text-blue-400',
  extracting: 'bg-purple-500/20 text-purple-400',
  extracted: 'bg-emerald-500/20 text-emerald-400',
  failed: 'bg-red-500/20 text-red-400',
};

export default function DocumentSidebar({ sessionId, documents, selectedDocId, onSelectDoc, onRefresh }: Props) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [extracting, setExtracting] = useState(false);

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
      onRefresh();
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Extraction failed');
    } finally {
      setExtracting(false);
    }
  }, [sessionId, onRefresh]);

  const hasUploadedDocs = documents.some(d => d.status === 'uploaded' || d.status === 'failed');

  return (
    <div className="w-56 bg-[#151821] border-r border-gray-800 flex flex-col h-full">
      {/* Header */}
      <div className="p-3 border-b border-gray-800">
        <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">Documents</h2>
      </div>

      {/* Document list */}
      <div className="flex-1 overflow-y-auto">
        {documents.length === 0 ? (
          <div className="p-4 text-center text-gray-500 text-sm">
            No documents yet
          </div>
        ) : (
          documents.map((doc) => (
            <button
              key={doc.id}
              onClick={() => onSelectDoc(doc.id)}
              className={`w-full text-left px-3 py-2.5 border-b border-gray-800/50 transition-colors flex items-start gap-2 ${
                selectedDocId === doc.id
                  ? 'bg-primary-600/10 border-l-2 border-l-primary-500'
                  : 'hover:bg-gray-800/50 border-l-2 border-l-transparent'
              }`}
            >
              <FileText size={14} className="text-gray-500 mt-0.5 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="text-sm text-gray-200 truncate">{doc.pump_tag || doc.filename}</div>
                <div className="text-xs text-gray-500 truncate">{doc.filename}</div>
                <div className="flex items-center gap-2 mt-1">
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${STATUS_COLORS[doc.status] || ''}`}>
                    {doc.status}
                  </span>
                  <span className="text-[10px] text-gray-600">{doc.num_pages}p</span>
                </div>
              </div>
              {selectedDocId === doc.id && <ChevronRight size={12} className="text-primary-400 mt-1 shrink-0" />}
            </button>
          ))
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
          disabled={uploading}
          className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 text-gray-200 text-sm rounded-lg transition-colors disabled:opacity-50"
        >
          <Upload size={14} />
          {uploading ? 'Uploading...' : 'Upload PDF'}
        </button>

        {hasUploadedDocs && (
          <button
            onClick={handleExtractAll}
            disabled={extracting}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-primary-600 hover:bg-primary-700 text-white text-sm rounded-lg transition-colors disabled:opacity-50"
          >
            {extracting ? <RefreshCw size={14} className="animate-spin" /> : <Play size={14} />}
            {extracting ? 'Extracting...' : 'Extract All'}
          </button>
        )}
      </div>
    </div>
  );
}
