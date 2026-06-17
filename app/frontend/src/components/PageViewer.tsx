import { useState, useEffect } from 'react';
import { ChevronLeft, ChevronRight, ZoomIn, ZoomOut, RotateCcw } from 'lucide-react';
import { api } from '../api/client';

interface Props {
  sessionId: string;
  documentId: string;
  totalPages: number;
  currentPage: number;
  onPageChange: (page: number) => void;
  highlightText?: string | null;
}

export default function PageViewer({ sessionId, documentId, totalPages, currentPage, onPageChange, highlightText }: Props) {
  const [zoom, setZoom] = useState(100);
  const pdfUrl = api.getDocumentPdfUrl(sessionId, documentId);

  // The #page=N fragment tells the browser's PDF viewer which page to show
  const pdfUrlWithPage = `${pdfUrl}#page=${currentPage}`;

  // Reset zoom on document change
  useEffect(() => {
    setZoom(100);
  }, [documentId]);

  return (
    <div className="flex flex-col h-full bg-[#0c0d12]">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-4 py-2 bg-[#151821] border-b border-gray-800">
        {/* Page nav */}
        <div className="flex items-center gap-1">
          <button
            onClick={() => onPageChange(currentPage - 1)}
            disabled={currentPage <= 1}
            className="p-1.5 rounded hover:bg-gray-700 text-gray-400 disabled:opacity-30 disabled:hover:bg-transparent transition-colors"
          >
            <ChevronLeft size={16} />
          </button>
          <span className="text-sm text-gray-300 min-w-[80px] text-center">
            Page {currentPage} / {totalPages}
          </span>
          <button
            onClick={() => onPageChange(currentPage + 1)}
            disabled={currentPage >= totalPages}
            className="p-1.5 rounded hover:bg-gray-700 text-gray-400 disabled:opacity-30 disabled:hover:bg-transparent transition-colors"
          >
            <ChevronRight size={16} />
          </button>
        </div>

        {/* Zoom */}
        <div className="flex items-center gap-1">
          <button
            onClick={() => setZoom(z => Math.max(50, z - 25))}
            className="p-1.5 rounded hover:bg-gray-700 text-gray-400 transition-colors"
          >
            <ZoomOut size={16} />
          </button>
          <span className="text-xs text-gray-400 min-w-[40px] text-center">{zoom}%</span>
          <button
            onClick={() => setZoom(z => Math.min(200, z + 25))}
            className="p-1.5 rounded hover:bg-gray-700 text-gray-400 transition-colors"
          >
            <ZoomIn size={16} />
          </button>
          <button
            onClick={() => setZoom(100)}
            className="p-1.5 rounded hover:bg-gray-700 text-gray-400 transition-colors ml-1"
          >
            <RotateCcw size={14} />
          </button>
        </div>
      </div>

      {/* PDF rendered natively by the browser */}
      <div className="flex-1 overflow-hidden">
        <iframe
          key={`${documentId}-${currentPage}`}
          src={pdfUrlWithPage}
          className="w-full h-full border-0"
          style={{ transform: `scale(${zoom / 100})`, transformOrigin: 'top left', width: `${10000 / zoom}%`, height: `${10000 / zoom}%` }}
          title={`PDF page ${currentPage}`}
        />
      </div>

      {/* Citation highlight hint */}
      {highlightText && (
        <div className="px-4 py-2 bg-primary-600/10 border-t border-primary-500/20">
          <p className="text-xs text-primary-300 truncate">
            Citation: &ldquo;{highlightText}&rdquo;
          </p>
        </div>
      )}
    </div>
  );
}
