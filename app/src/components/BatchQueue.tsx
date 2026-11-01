import {
  CheckCircle2, CircleDashed, FolderOpen, Loader2, MinusCircle, RotateCcw, SlidersHorizontal,
  X, XCircle,
} from 'lucide-react';
import { Card } from './ui';
import { useI18n, type Key } from '../lib/i18n';
import type { BatchFile } from '../lib/api';

type RowStatus = Exclude<BatchFile['status'], 'removed'>;

const STATUS_LABEL: Record<RowStatus, Key> = {
  pending: 't.batch.st.pending',
  running: 't.batch.st.running',
  done: 't.batch.st.done',
  skipped: 't.batch.st.skipped',
  failed: 't.batch.st.failed',
  cancelled: 't.batch.st.cancelled',
};

/** a running file's pipeline phase, shown instead of a bare "running" */
const PHASE_LABEL: Record<string, Key> = {
  prepare: 't.batch.ph.prepare',
  wait_upload: 't.batch.ph.wait_upload',
  upload: 't.batch.ph.upload',
  wait_segment: 't.batch.ph.wait_segment',
  segment: 't.batch.ph.segment',
};

function StatusIcon({ status, waiting }: { status: RowStatus; waiting: boolean }) {
  const common = { size: 15, strokeWidth: 2 } as const;
  switch (status) {
    case 'done': return <CheckCircle2 {...common} className="text-ok" />;
    case 'failed': return <XCircle {...common} className="text-err" />;
    case 'running':
      // between two stages the file is queued, not worked on: no spinner
      return waiting
        ? <CircleDashed {...common} className="text-primary" />
        : <Loader2 {...common} className="animate-spin text-primary" />;
    case 'skipped': return <MinusCircle {...common} className="text-ink-3" />;
    default: return <CircleDashed {...common} className="text-ink-3" />;
  }
}

const baseName = (p: string) => p.split(/[\\/]/).pop() ?? p;

const iconBtn = 'text-ink-3 transition-colors hover:text-primary';

/**
 * The batch file list. Two modes share one look:
 * - `files` (the server's plan, then its per-file events): live statuses.
 *   Several files are in flight at once (the batch is a pipeline), so each
 *   running row names its own phase; `fileDetail` adds the stage detail
 *   (e.g. the upload's chunk counter). Rows still waiting can be taken out
 *   (`onRemoveFile`), skipped ones re-transcribed (`onRetranscribe`).
 * - `selected` (plain paths, before anything started): removable rows.
 * Order is the queue order; files dropped on a running batch join the end.
 *
 * The task monitor adds per-file actions on finished rows (`onRefine`,
 * `onReveal`) and drops the card chrome (`bare`) because it already sits
 * inside one.
 */
export function BatchQueue({
  files, selected, onRemove, onRemoveFile, onRetranscribe, onRefine, onReveal, fileDetail,
  bare = false,
}: {
  files: BatchFile[];
  selected: string[];
  onRemove?: (path: string) => void;
  onRemoveFile?: (file: BatchFile) => void;
  onRetranscribe?: (file: BatchFile) => void;
  onRefine?: (file: BatchFile) => void;
  onReveal?: (file: BatchFile) => void;
  fileDetail?: Record<number, string>;
  bare?: boolean;
}) {
  const { t, tf } = useI18n();
  const live = files.length > 0;
  const rows = live
    ? files.filter((f) => f.status !== 'removed').map((f) => ({
        key: String(f.index), path: f.audio, status: f.status as RowStatus,
        phase: f.status === 'running' ? f.phase ?? '' : '',
        detail: f.detail,
        note: f.action === 'resegment' && f.status !== 'skipped' ? t('t.batch.act.resegment') : '',
        lines: f.status === 'done' ? tf('t.batch.lines', { n: f.entries }) : '',
        fallback: !!f.llm_note,
        file: f as BatchFile | null,
      }))
    : selected.map((p) => ({
        key: p, path: p, status: 'pending' as RowStatus, phase: '', detail: '', note: '',
        lines: '', fallback: false, file: null as BatchFile | null,
      }));

  const list = (
      <ul className="flex flex-col">
        {rows.map((r) => {
          const waiting = r.phase === 'wait_upload' || r.phase === 'wait_segment';
          // the upload's chunk counter etc.; only while that stage is live
          // (preparation opens with the file's own name: nothing to add)
          const rawNote = r.status === 'running' && !waiting && r.file
            ? fileDetail?.[r.file.index] ?? '' : '';
          const stageNote = rawNote === baseName(r.path) ? '' : rawNote;
          const statusText = r.status === 'running' && PHASE_LABEL[r.phase]
            ? t(PHASE_LABEL[r.phase]) : t(STATUS_LABEL[r.status]);
          return (
          <li
            key={r.key}
            className="flex items-center gap-3 rounded-(--radius-s) px-3 py-1.5 hover:bg-primary-dim/40"
            title={r.path}
          >
            <StatusIcon status={r.status} waiting={waiting} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] text-ink-1">{baseName(r.path)}</span>
              {r.status === 'failed' && r.detail && (
                <span className="block truncate font-mono text-[11px] text-err" title={r.detail}>
                  {r.detail.split('\n')[0]}
                </span>
              )}
            </span>
            {r.note && <span className="shrink-0 text-[11px] text-ink-3">{r.note}</span>}
            {stageNote && (
              <span className="max-w-[40%] shrink truncate text-[11px] text-ink-3" title={stageNote}>
                {stageNote}
              </span>
            )}
            {r.fallback && (
              <span className="shrink-0 text-[11px] text-warn">{tf('t.batch.fallback', { n: 1 })}</span>
            )}
            {r.lines && <span className="shrink-0 text-[11px] text-ink-3">{r.lines}</span>}
            {r.file && r.status === 'skipped' && onRetranscribe && (
              <button
                className={`inline-flex shrink-0 items-center gap-1 text-[11.5px] font-medium ${iconBtn}`}
                onClick={() => onRetranscribe(r.file!)}
              >
                <RotateCcw size={12} strokeWidth={2} /> {t('t.batch.retranscribe')}
              </button>
            )}
            {r.file && (r.status === 'done' || r.status === 'skipped') && (onRefine || onReveal) && (
              <span className="flex shrink-0 items-center gap-2">
                {onRefine && (
                  <button className={iconBtn} onClick={() => onRefine(r.file!)} title={t('t.result.toRefine')}>
                    <SlidersHorizontal size={13} strokeWidth={2} />
                  </button>
                )}
                {onReveal && (
                  <button className={iconBtn} onClick={() => onReveal(r.file!)} title={t('t.hint.reveal')}>
                    <FolderOpen size={13} strokeWidth={2} />
                  </button>
                )}
              </span>
            )}
            {live ? (
              <span className="flex w-24 shrink-0 items-center justify-end gap-2 text-right text-[11px] text-ink-2">
                <span className={r.status === 'running' ? 'text-primary' : ''}>{statusText}</span>
                {r.file && r.status === 'pending' && onRemoveFile && (
                  <button
                    className="text-ink-3 transition-colors hover:text-err"
                    onClick={() => onRemoveFile(r.file!)}
                    title={t('t.batch.remove')}
                  >
                    <X size={14} strokeWidth={2} />
                  </button>
                )}
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
          );
        })}
      </ul>
  );

  return bare
    ? <div className="max-h-72 overflow-y-auto">{list}</div>
    : <Card className="max-h-72 overflow-y-auto p-2">{list}</Card>;
}
