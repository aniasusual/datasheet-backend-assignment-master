import { useState, useCallback, useEffect, useRef } from 'react';
import { Check, X, Pencil, ChevronDown, ChevronRight } from 'lucide-react';
import { api } from '../api/client';
import type { ExtractedField, FieldStatus } from '../types';

interface Props {
  sessionId: string;
  fields: ExtractedField[];
  selectedFieldId: string | null;
  onSelectField: (field: ExtractedField) => void;
  onFieldUpdated: () => void;
  filterSection: string;
  onFilterSection: (s: string) => void;
  filterStatus: string;
  onFilterStatus: (s: string) => void;
}

function ConfidenceBadge({ value }: { value: number }) {
  const color = value >= 0.9 ? 'bg-emerald-500/20 text-emerald-400'
    : value >= 0.7 ? 'bg-yellow-500/20 text-yellow-400'
    : 'bg-red-500/20 text-red-400';
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-mono ${color}`}>
      {(value * 100).toFixed(0)}%
    </span>
  );
}

function StatusBadge({ status }: { status: FieldStatus }) {
  const colors: Record<string, string> = {
    extracted: 'bg-gray-600/30 text-gray-400',
    verified: 'bg-emerald-500/20 text-emerald-400',
    corrected: 'bg-blue-500/20 text-blue-400',
    rejected: 'bg-red-500/20 text-red-400',
  };
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${colors[status] || ''}`}>
      {status}
    </span>
  );
}

function InlineEditor({ field, sessionId, onDone }: { field: ExtractedField; sessionId: string; onDone: () => void }) {
  const [value, setValue] = useState(field.raw_value);
  const [unit, setUnit] = useState(field.unit || '');
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    try {
      await api.updateField(sessionId, field.id, {
        raw_value: value,
        unit: unit || undefined,
        reason: reason || undefined,
        status: 'corrected',
      });
      onDone();
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mt-2 p-3 bg-gray-800/50 rounded-lg border border-gray-700 space-y-2">
      <div className="flex gap-2">
        <div className="flex-1">
          <label className="text-[10px] text-gray-500 uppercase">Value</label>
          <input
            value={value}
            onChange={e => setValue(e.target.value)}
            className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 focus:border-primary-500 focus:outline-none"
          />
        </div>
        <div className="w-24">
          <label className="text-[10px] text-gray-500 uppercase">Unit</label>
          <input
            value={unit}
            onChange={e => setUnit(e.target.value)}
            className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 focus:border-primary-500 focus:outline-none"
            placeholder="e.g. GPM"
          />
        </div>
      </div>
      <div>
        <label className="text-[10px] text-gray-500 uppercase">Reason (optional)</label>
        <input
          value={reason}
          onChange={e => setReason(e.target.value)}
          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 focus:border-primary-500 focus:outline-none"
          placeholder="Why is this correction needed?"
        />
      </div>
      <div className="flex gap-2 justify-end">
        <button onClick={onDone} className="px-3 py-1 text-xs text-gray-400 hover:text-gray-200 transition-colors">Cancel</button>
        <button
          onClick={save}
          disabled={saving}
          className="px-3 py-1 text-xs bg-primary-600 hover:bg-primary-700 text-white rounded transition-colors disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save'}
        </button>
      </div>
    </div>
  );
}

export default function FieldPanel({
  sessionId, fields, selectedFieldId, onSelectField, onFieldUpdated,
  filterSection, onFilterSection, filterStatus, onFilterStatus,
}: Props) {
  const [expandedSections, setExpandedSections] = useState<Set<string>>(new Set());
  const initializedRef = useRef(false);
  const [editingFieldId, setEditingFieldId] = useState<string | null>(null);

  // Expand all sections when fields first arrive
  useEffect(() => {
    if (fields.length > 0 && !initializedRef.current) {
      setExpandedSections(new Set(fields.map(f => f.section)));
      initializedRef.current = true;
    }
    if (fields.length === 0) {
      initializedRef.current = false;
    }
  }, [fields]);

  const toggleSection = (section: string) => {
    setExpandedSections(prev => {
      const next = new Set(prev);
      next.has(section) ? next.delete(section) : next.add(section);
      return next;
    });
  };

  const handleVerify = useCallback(async (field: ExtractedField) => {
    try {
      await api.updateField(sessionId, field.id, { status: 'verified' });
      onFieldUpdated();
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Failed');
    }
  }, [sessionId, onFieldUpdated]);

  const handleReject = useCallback(async (field: ExtractedField) => {
    try {
      await api.updateField(sessionId, field.id, { status: 'rejected' });
      onFieldUpdated();
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Failed');
    }
  }, [sessionId, onFieldUpdated]);

  // Group by section
  const grouped: Record<string, ExtractedField[]> = {};
  for (const f of fields) {
    (grouped[f.section] ??= []).push(f);
  }

  // Sections derived entirely from extracted data — no hardcoded list
  const sections = [...new Set(fields.map(f => f.section))];

  return (
    <div className="w-96 bg-[#151821] border-l border-gray-800 flex flex-col h-full">
      {/* Header */}
      <div className="p-3 border-b border-gray-800">
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">Extracted Fields</h2>
          <span className="text-xs text-gray-500">{fields.length} fields</span>
        </div>

        {/* Filters */}
        <div className="flex gap-2">
          <select
            value={filterSection}
            onChange={e => onFilterSection(e.target.value)}
            className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300 focus:border-primary-500 focus:outline-none"
          >
            <option value="">All sections</option>
            {sections.map(s => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <select
            value={filterStatus}
            onChange={e => onFilterStatus(e.target.value)}
            className="w-28 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300 focus:border-primary-500 focus:outline-none"
          >
            <option value="">All status</option>
            <option value="extracted">Extracted</option>
            <option value="verified">Verified</option>
            <option value="corrected">Corrected</option>
            <option value="rejected">Rejected</option>
          </select>
        </div>
      </div>

      {/* Field list */}
      <div className="flex-1 overflow-y-auto">
        {fields.length === 0 ? (
          <div className="p-6 text-center text-gray-500 text-sm">
            No fields extracted yet.
            <br />
            <span className="text-xs">Upload a PDF and click Extract.</span>
          </div>
        ) : (
          sections.filter(s => grouped[s]?.length).map(section => (
            <div key={section}>
              {/* Section header */}
              <button
                onClick={() => toggleSection(section)}
                className="w-full flex items-center gap-2 px-3 py-2 bg-gray-800/30 hover:bg-gray-800/50 transition-colors sticky top-0 z-10"
              >
                {expandedSections.has(section) ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
                  {section}
                </span>
                <span className="text-[10px] text-gray-600 ml-auto">{grouped[section]?.length}</span>
              </button>

              {/* Fields */}
              {expandedSections.has(section) && grouped[section]?.map(field => (
                <div
                  key={field.id}
                  className={`px-3 py-2 border-b border-gray-800/30 cursor-pointer transition-colors ${
                    selectedFieldId === field.id
                      ? 'bg-primary-600/10 border-l-2 border-l-primary-500'
                      : 'hover:bg-gray-800/30 border-l-2 border-l-transparent'
                  } ${field.status === 'rejected' ? 'opacity-40' : ''}`}
                  onClick={() => onSelectField(field)}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="text-xs text-gray-400 truncate">{field.display_name}</div>
                      <div className="text-sm text-gray-100 font-medium mt-0.5">
                        {field.raw_value}
                        {field.unit && <span className="text-gray-400 ml-1">{field.unit}</span>}
                      </div>
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                      <ConfidenceBadge value={field.confidence} />
                      <StatusBadge status={field.status} />
                    </div>
                  </div>

                  {/* Citation */}
                  {selectedFieldId === field.id && field.citation_text && (
                    <div className="mt-1.5 text-[11px] text-gray-500 italic truncate">
                      p.{field.citation_page}: "{field.citation_text}"
                    </div>
                  )}

                  {/* Actions */}
                  {selectedFieldId === field.id && field.status !== 'rejected' && (
                    <div className="flex items-center gap-1 mt-2">
                      {field.status !== 'verified' && (
                        <button
                          onClick={(e) => { e.stopPropagation(); handleVerify(field); }}
                          className="flex items-center gap-1 px-2 py-1 text-[11px] bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 rounded transition-colors"
                          title="Verify"
                        >
                          <Check size={12} /> Verify
                        </button>
                      )}
                      <button
                        onClick={(e) => { e.stopPropagation(); setEditingFieldId(field.id); }}
                        className="flex items-center gap-1 px-2 py-1 text-[11px] bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 rounded transition-colors"
                        title="Edit"
                      >
                        <Pencil size={12} /> Edit
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleReject(field); }}
                        className="flex items-center gap-1 px-2 py-1 text-[11px] bg-red-500/10 text-red-400 hover:bg-red-500/20 rounded transition-colors"
                        title="Reject"
                      >
                        <X size={12} /> Reject
                      </button>
                    </div>
                  )}

                  {/* Inline editor */}
                  {editingFieldId === field.id && (
                    <InlineEditor
                      field={field}
                      sessionId={sessionId}
                      onDone={() => { setEditingFieldId(null); onFieldUpdated(); }}
                    />
                  )}
                </div>
              ))}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
