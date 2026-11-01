import { CheckCircle2, CircleDashed, Loader2, MinusCircle, X, XCircle } from 'lucide-react';
import { Card } from './ui';
import { useI18n, type Key } from '../lib/i18n';
import type { BatchFile } from '../lib/api';

const STATUS_LABEL: Record<BatchFile['status'], Key> = {
  pending: 't.batch.st.pending',
  running: 't.batch.st.running',
  done: 't.batch.st.done',
  skipped: 't.batch.st.skipped',
  failed: 't.batch.st.failed',
  cancelled: 't.batch.st.cancelled',
};

function StatusIcon({ status }: { status: BatchFile['status'] }) {
  const common = { size: 15, strokeWidth: 2 } as const;
  switch (status) {
    case 'done': return <CheckCircle2 {...common} className="text-ok" />;
    case 'failed': return <XCircle {...common} className="text-err" />;
    case 'running': return <Loader2 {...common} className="animate-spin text-primary" />;
    case 'skipped': return <MinusCircle {...common} className="text-ink-3" />;
    default: return <CircleDashed {...common} className="text-ink-3" />;
  }
}

const baseName = (p: string) => p.split(/[\\/]/).pop() ?? p;

/**
 * The batch file list. Two modes share one look:
 * - `files` (the server's plan, then its per-file events): live statuses;
 * - `selected` (plain paths, before anything started): removable rows.
 * Order is the run order; the queue is serial, so there is no reordering.
 */
export function BatchQueue({
  files, selected, onRemove,
}: {
  files: BatchFile[];
  selected: string[];
  onRemove?: (path: string) => void;
}) {
  const { t, tf } = useI18n();
  const live = files.length > 0;
  const rows = live
    ? files.map((f) => ({
        key: String(f.index), path: f.audio, status: f.status, detail: f.detail,
        note: f.action === 'resegment' && f.status !== 'skipped' ? t('t.batch.act.resegment') : '',
        lines: f.status === 'done' ? tf('t.batch.lines', { n: f.entries }) : '',
        fallback: !!f.llm_note,
      }))
    : selected.map((p) => ({
        key: p, path: p, status: 'pending' as const, detail: '', note: '', lines: '', fallback: false,
      }));

  return (
    <Card className="max-h-72 overflow-y-auto p-2">
      <ul className="flex flex-col">
        {rows.map((r) => (
          <li
            key={r.key}
            className="flex items-center gap-3 rounded-(--radius-s) px-3 py-1.5 hover:bg-primary-dim/40"
            title={r.path}
          >
            <StatusIcon status={r.status} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] text-ink-1">{baseName(r.path)}</span>
              {r.status === 'failed' && r.detail && (
                <span className="block truncate font-mono text-[11px] text-err" title={r.detail}>
                  {r.detail.split('\n')[0]}
                </span>
              )}
            </span>
            {r.note && <span className="shrink-0 text-[11px] text-ink-3">{r.note}</span>}
            {r.fallback && (
              <span className="shrink-0 text-[11px] text-warn">{tf('t.batch.fallback', { n: 1 })}</span>
            )}
            {r.lines && <span className="shrink-0 text-[11px] text-ink-3">{r.lines}</span>}
            {live ? (
              <span className="w-24 shrink-0 text-right text-[11px] text-ink-2">
                {t(STATUS_LABEL[r.status])}
              </span>
            ) : (
              onRemove && (
                <button
                  className="shrink-0 text-ink-3 transition-colors hover:text-err"
                  onClick={() => onRemove(r.path)}
                  title={t('t.batch.remove')}
                >
                  <X size={14} strokeWidth={2} />
                </button>
              )
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}
