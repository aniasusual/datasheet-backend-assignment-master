import { useState, useCallback } from 'react';
import { Search, MessageSquare, FileText, Loader2 } from 'lucide-react';
import { api } from '../api/client';
import type { QueryResult } from '../types';

interface Props {
  sessionId: string;
}

const EXAMPLE_QUERIES = [
  'What is the material for impeller in pump P300228?',
  'For P300228, what fluid is pumped, and what are the nominal and maximum flow rates?',
  'For P300228, whether the pump will corrode / erode over time?',
  'For P600173, what estimated efficiency of the motor?',
  'Compare the suction pressures across all pumps.',
  'What are the design temperatures for all pumps?',
];

function ConfidenceIndicator({ level }: { level: string }) {
  const config: Record<string, { color: string; label: string }> = {
    high: { color: 'text-emerald-400', label: 'High confidence' },
    medium: { color: 'text-yellow-400', label: 'Medium confidence' },
    low: { color: 'text-red-400', label: 'Low confidence' },
  };
  const c = config[level] || config.low;
  return <span className={`text-xs ${c.color}`}>{c.label}</span>;
}

export default function QueryPanel({ sessionId }: Props) {
  const [question, setQuestion] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [history, setHistory] = useState<{ q: string; r: QueryResult }[]>([]);

  const handleQuery = useCallback(async (q?: string) => {
    const query = q || question;
    if (!query.trim()) return;
    setLoading(true);
    try {
      const res = await api.queryFields(sessionId, query);
      setResult(res);
      setHistory(prev => [{ q: query, r: res }, ...prev]);
      if (!q) setQuestion('');
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Query failed');
    } finally {
      setLoading(false);
    }
  }, [sessionId, question]);

  return (
    <div className="flex flex-col h-full bg-[#0c0d12]">
      {/* Search bar */}
      <div className="p-4 bg-[#151821] border-b border-gray-800">
        <div className="relative">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            value={question}
            onChange={e => setQuestion(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleQuery()}
            placeholder="Ask a question about the extracted data..."
            className="w-full bg-gray-900 border border-gray-700 rounded-lg pl-10 pr-4 py-3 text-sm text-gray-200 placeholder-gray-500 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500/30"
          />
          {loading && <Loader2 size={16} className="absolute right-3 top-1/2 -translate-y-1/2 text-primary-400 animate-spin" />}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4">
        {result ? (
          /* Answer */
          <div className="space-y-4">
            <div className="bg-[#151821] rounded-lg border border-gray-800 p-4">
              <div className="flex items-center gap-2 mb-3">
                <MessageSquare size={14} className="text-primary-400" />
                <span className="text-xs font-semibold text-gray-400 uppercase">Answer</span>
                <ConfidenceIndicator level={result.confidence} />
              </div>
              <p className="text-sm text-gray-200 leading-relaxed whitespace-pre-wrap">{result.answer}</p>
            </div>

            {/* Cited fields */}
            {result.cited_fields.length > 0 && (
              <div>
                <h3 className="text-xs font-semibold text-gray-400 uppercase mb-2 flex items-center gap-1.5">
                  <FileText size={12} />
                  Sources ({result.cited_fields.length})
                </h3>
                <div className="space-y-2">
                  {result.cited_fields.map((f) => (
                    <div key={f.id} className="bg-[#151821] rounded-lg border border-gray-800 p-3">
                      <div className="flex items-start justify-between gap-2">
                        <div>
                          <span className="text-xs text-gray-500">{f.filename} &middot; {f.pump_tag}</span>
                          <div className="text-sm text-gray-200 mt-0.5">
                            <span className="text-gray-400">{f.display_name}:</span>{' '}
                            <span className="font-medium">{f.raw_value}</span>
                            {f.unit && <span className="text-gray-400 ml-1">{f.unit}</span>}
                          </div>
                        </div>
                        <span className="text-[10px] text-gray-500 shrink-0">p.{f.citation_page}</span>
                      </div>
                      {f.citation_text && (
                        <div className="mt-1.5 text-[11px] text-gray-500 italic truncate">
                          "{f.citation_text}"
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Query history */}
            {history.length > 1 && (
              <div className="mt-6">
                <h3 className="text-xs font-semibold text-gray-400 uppercase mb-2">Previous Queries</h3>
                {history.slice(1).map((h, i) => (
                  <button
                    key={i}
                    onClick={() => { setResult(h.r); setQuestion(h.q); }}
                    className="w-full text-left px-3 py-2 text-xs text-gray-400 hover:text-gray-200 hover:bg-gray-800/30 rounded transition-colors truncate"
                  >
                    {h.q}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          /* Empty state — example queries */
          <div className="max-w-lg mx-auto mt-12">
            <div className="text-center mb-8">
              <Search size={32} className="text-gray-600 mx-auto mb-3" />
              <h3 className="text-lg text-gray-300 font-medium">Query Extracted Data</h3>
              <p className="text-sm text-gray-500 mt-1">Ask questions about the pump datasheets in natural language.</p>
            </div>
            <div className="space-y-2">
              <p className="text-xs text-gray-500 uppercase font-semibold mb-2">Try these examples</p>
              {EXAMPLE_QUERIES.map((q, i) => (
                <button
                  key={i}
                  onClick={() => { setQuestion(q); handleQuery(q); }}
                  className="w-full text-left px-4 py-3 bg-[#151821] border border-gray-800 hover:border-primary-500/30 rounded-lg text-sm text-gray-300 hover:text-gray-100 transition-colors"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
