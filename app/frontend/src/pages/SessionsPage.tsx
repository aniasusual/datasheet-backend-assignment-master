import { useEffect, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, FileText, Trash2, Clock } from 'lucide-react';
import { api } from '../api/client';
import type { SessionListItem } from '../types';

export default function SessionsPage() {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const data = await api.listSessions();
      setSessions(data);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const createSession = async () => {
    const session = await api.createSession('New Session');
    navigate(`/sessions/${session.id}`);
  };

  const deleteSession = async (id: string) => {
    if (!confirm('Delete this session and all its data?')) return;
    await api.deleteSession(id);
    load();
  };

  return (
    <div className="flex-1 overflow-auto p-8">
      <div className="max-w-3xl mx-auto">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-2xl font-bold text-gray-100">Sessions</h1>
            <p className="text-sm text-gray-500 mt-1">Upload datasheets, extract fields, and query results.</p>
          </div>
          <button
            onClick={createSession}
            className="flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-700 text-white text-sm font-medium rounded-lg transition-colors"
          >
            <Plus size={16} /> New Session
          </button>
        </div>

        {loading ? (
          <div className="text-center text-gray-500 py-12">Loading...</div>
        ) : sessions.length === 0 ? (
          <div className="text-center py-16">
            <FileText size={48} className="text-gray-700 mx-auto mb-4" />
            <p className="text-gray-400">No sessions yet.</p>
            <p className="text-sm text-gray-500 mt-1">Create one to get started.</p>
          </div>
        ) : (
          <div className="space-y-3">
            {sessions.map((s) => (
              <div
                key={s.id}
                onClick={() => navigate(`/sessions/${s.id}`)}
                className="flex items-center justify-between px-5 py-4 bg-[#151821] border border-gray-800 rounded-lg hover:border-gray-700 cursor-pointer transition-colors group"
              >
                <div>
                  <h3 className="text-sm font-medium text-gray-200 group-hover:text-white transition-colors">
                    {s.title || 'Untitled Session'}
                  </h3>
                  <div className="flex items-center gap-4 mt-1.5 text-xs text-gray-500">
                    <span className="flex items-center gap-1">
                      <Clock size={11} />
                      {new Date(s.created_at).toLocaleDateString()}
                    </span>
                    <span className="flex items-center gap-1">
                      <FileText size={11} />
                      {s.document_count} doc{s.document_count !== 1 ? 's' : ''}
                    </span>
                  </div>
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); deleteSession(s.id); }}
                  className="p-2 text-gray-600 hover:text-red-400 rounded-lg hover:bg-red-500/10 transition-colors opacity-0 group-hover:opacity-100"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
