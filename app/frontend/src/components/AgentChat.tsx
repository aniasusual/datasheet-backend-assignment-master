import { useState, useRef, useEffect, useCallback } from 'react';
import { Send, Bot, User, Wrench, Loader2, Sparkles, Trash2 } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import { api } from '../api/client';
import type { AgentMessage, ToolAction } from '../types';

interface Props {
  sessionId: string;
  onFieldsChanged: () => void;
  injectedPrompt?: string | null;
  onPromptConsumed?: () => void;
}

const EXAMPLE_QUERIES = [
  'What is the material for impeller in pump P300228?',
  'For P300228, what fluid is pumped and what are the flow rates?',
  'Will pump P300228 corrode or erode over time?',
  'Compare suction pressures across all pumps.',
  'Verify all product_handled fields for P-300228 with confidence above 0.9',
  'Reject all page/date/revision metadata fields',
];

function ToolActionBadge({ action }: { action: ToolAction }) {
  const labels: Record<string, string> = {
    update_field: 'Updated field',
    verify_fields: 'Verified fields',
    reject_fields: 'Rejected fields',
  };

  const colors: Record<string, string> = {
    update_field: 'bg-blue-500/10 text-blue-400',
    verify_fields: 'bg-emerald-500/10 text-emerald-400',
    reject_fields: 'bg-red-500/10 text-red-400',
  };

  const result = action.result as Record<string, unknown>;
  let detail = '';
  if (action.tool === 'verify_fields') detail = `${result.verified || 0} verified`;
  else if (action.tool === 'reject_fields') detail = `${result.rejected || 0} rejected`;
  else if (action.tool === 'update_field') detail = `${(result.field_name as string) || ''}`;

  return (
    <span className={`inline-flex items-center gap-1.5 text-[11px] px-2 py-1 rounded-full ${colors[action.tool] || 'bg-gray-700/50 text-gray-300'}`}>
      <Wrench size={10} />
      {labels[action.tool] || action.tool}
      {detail && <span className="opacity-70">&middot; {detail}</span>}
    </span>
  );
}

const proseClasses = "prose prose-invert prose-sm max-w-none prose-p:my-1.5 prose-ul:my-1.5 prose-ol:my-1.5 prose-li:my-0.5 prose-headings:my-2 prose-table:my-2 prose-th:px-2 prose-th:py-1 prose-td:px-2 prose-td:py-1 prose-pre:bg-gray-900 prose-pre:border prose-pre:border-gray-700 prose-code:text-primary-300";

export default function AgentChat({ sessionId, onFieldsChanged, injectedPrompt, onPromptConsumed }: Props) {
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Load chat history from DB on mount
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { messages: history } = await api.getChatHistory(sessionId);
        if (!cancelled) setMessages(history);
      } catch {
        // ignore — fresh chat
      } finally {
        if (!cancelled) setInitialLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [sessionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  const sendMessage = useCallback(async (text?: string) => {
    const msg = text || input.trim();
    if (!msg || loading) return;
    setInput('');
    setLoading(true);

    // Optimistic: add user message to UI
    setMessages(prev => [...prev, { role: 'user', content: msg }]);

    try {
      const result = await api.agentChat(sessionId, msg);

      // Add the assistant response (with tool_actions attached)
      const assistantMsg: AgentMessage = {
        role: 'assistant',
        content: result.response,
        tool_actions: result.tool_actions.length > 0 ? result.tool_actions : null,
      };
      setMessages(prev => [...prev, assistantMsg]);

      // If any write actions were taken, refresh fields
      const hasWriteAction = result.tool_actions.some(
        a => ['update_field', 'verify_fields', 'reject_fields'].includes(a.tool)
      );
      if (hasWriteAction) {
        onFieldsChanged();
      }
    } catch (e) {
      setMessages(prev => [
        ...prev,
        { role: 'assistant', content: `Error: ${e instanceof Error ? e.message : 'Request failed'}` },
      ]);
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  }, [sessionId, input, loading, onFieldsChanged]);

  // When extraction completes, send the report to the agent for LLM summarization
  const injectedRef = useRef(false);
  useEffect(() => {
    if (injectedPrompt && !injectedRef.current && !initialLoading) {
      injectedRef.current = true;
      const prompt = `Here is the extraction report. Summarize the key findings, highlight any issues, and suggest next steps:\n\n${injectedPrompt}`;
      sendMessage(prompt);
      onPromptConsumed?.();
    }
    if (!injectedPrompt) {
      injectedRef.current = false;
    }
  }, [injectedPrompt, onPromptConsumed, sendMessage, initialLoading]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const handleClearChat = useCallback(async () => {
    if (!confirm('Clear all chat messages?')) return;
    try {
      await api.clearChat(sessionId);
      setMessages([]);
    } catch {
      // ignore
    }
  }, [sessionId]);

  if (initialLoading) {
    return (
      <div className="flex items-center justify-center h-full bg-[#0c0d12]">
        <Loader2 size={24} className="text-primary-400 animate-spin" />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-[#0c0d12]">
      {/* Messages */}
      <div className="flex-1 overflow-y-auto">
        {messages.length === 0 ? (
          /* Empty state */
          <div className="max-w-xl mx-auto mt-12 px-4">
            <div className="text-center mb-8">
              <div className="w-12 h-12 bg-primary-600/10 rounded-xl flex items-center justify-center mx-auto mb-3">
                <Sparkles size={24} className="text-primary-400" />
              </div>
              <h3 className="text-lg text-gray-200 font-medium">Data Agent</h3>
              <p className="text-sm text-gray-500 mt-1">
                Ask questions, verify fields, correct errors, or bulk manage extractions.
              </p>
            </div>
            <div className="grid grid-cols-1 gap-2">
              {EXAMPLE_QUERIES.map((q, i) => (
                <button
                  key={i}
                  onClick={() => sendMessage(q)}
                  className="text-left px-4 py-3 bg-[#151821] border border-gray-800 hover:border-gray-700 rounded-lg text-sm text-gray-400 hover:text-gray-200 transition-colors"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="max-w-3xl mx-auto px-4 py-4 space-y-4">
            {messages.map((msg, i) => {
              const actions = msg.tool_actions || [];

              return (
                <div key={msg.id || i} className={`flex gap-3 ${msg.role === 'user' ? 'justify-end' : ''}`}>
                  {msg.role === 'assistant' && (
                    <div className="w-7 h-7 rounded-lg bg-primary-600/10 flex items-center justify-center shrink-0 mt-0.5">
                      <Bot size={14} className="text-primary-400" />
                    </div>
                  )}
                  <div className={`max-w-[85%] ${msg.role === 'user' ? 'order-first' : ''}`}>
                    {/* Tool actions */}
                    {actions.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 mb-2">
                        {actions.map((a, j) => (
                          <ToolActionBadge key={j} action={a} />
                        ))}
                      </div>
                    )}
                    <div className={`rounded-xl px-4 py-3 text-sm leading-relaxed ${
                      msg.role === 'user'
                        ? 'bg-primary-600 text-white'
                        : 'bg-[#1a1d27] text-gray-200 border border-gray-800'
                    }`}>
                      <div className={proseClasses}>
                        <ReactMarkdown>{msg.content}</ReactMarkdown>
                      </div>
                    </div>
                  </div>
                  {msg.role === 'user' && (
                    <div className="w-7 h-7 rounded-lg bg-gray-700 flex items-center justify-center shrink-0 mt-0.5">
                      <User size={14} className="text-gray-300" />
                    </div>
                  )}
                </div>
              );
            })}

            {/* Loading indicator */}
            {loading && (
              <div className="flex gap-3">
                <div className="w-7 h-7 rounded-lg bg-primary-600/10 flex items-center justify-center shrink-0">
                  <Bot size={14} className="text-primary-400" />
                </div>
                <div className="bg-[#1a1d27] border border-gray-800 rounded-xl px-4 py-3">
                  <Loader2 size={16} className="text-primary-400 animate-spin" />
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      {/* Input */}
      <div className="p-4 bg-[#151821] border-t border-gray-800">
        <div className="max-w-3xl mx-auto relative">
          <textarea
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about extracted data, or tell me to correct/verify/reject fields..."
            rows={1}
            className="w-full bg-gray-900 border border-gray-700 rounded-xl pl-4 pr-20 py-3 text-sm text-gray-200 placeholder-gray-500 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500/30 resize-none"
          />
          <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center gap-1">
            {messages.length > 0 && (
              <button
                onClick={handleClearChat}
                disabled={loading}
                className="p-2 text-gray-600 hover:text-red-400 disabled:opacity-30 transition-colors"
                title="Clear chat"
              >
                <Trash2 size={14} />
              </button>
            )}
            <button
              onClick={() => sendMessage()}
              disabled={loading || !input.trim()}
              className="p-2 text-gray-500 hover:text-primary-400 disabled:opacity-30 disabled:hover:text-gray-500 transition-colors"
            >
              <Send size={16} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
