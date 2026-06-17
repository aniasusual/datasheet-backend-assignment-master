import { Routes, Route, Link, Navigate } from 'react-router-dom';
import SessionsPage from './pages/SessionsPage';
import SessionDetailPage from './pages/SessionDetailPage';
import { Database } from 'lucide-react';

export default function App() {
  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#0f1117]">
      <nav className="h-11 bg-[#151821] border-b border-gray-800 flex items-center px-4 shrink-0">
        <Link to="/" className="flex items-center gap-2 text-gray-200 font-semibold text-sm hover:text-white transition-colors">
          <Database size={16} className="text-primary-400" />
          Datasheet Extractor
        </Link>
      </nav>
      <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
        <Routes>
          <Route path="/" element={<SessionsPage />} />
          <Route path="/sessions/:sessionId" element={<SessionDetailPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </div>
  );
}
