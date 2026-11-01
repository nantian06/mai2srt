import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import {
  AudioLines, ChevronDown, FileJson, FolderOpen, Layers, ListChecks, LogIn, ShieldCheck,
  SlidersHorizontal, Trash2,
} from 'lucide-react';
import { BatchQueue } from '../components/BatchQueue';
import { Button, Card, useToast } from '../components/ui';
import { useI18n, type Key } from '../lib/i18n';
import { cancelJob, openProject, revealPath, type BatchFile } from '../lib/api';
import {
  TRANSCRIBE_KINDS, discoverJobs, forgetJobs, jobProgress, useJobs,
  type JobKind, type JobStatus, type TrackedJob,
} from '../lib/jobs';
import { STAGES } from '../lib/stages';

const STAGE_IDS = STAGES.map((s) => s.id);

const KIND_ICON: Record<JobKind, typeof AudioLines> = {
  run: AudioLines,
  process: FileJson,
  batch: Layers,
  login: LogIn,
  consent: ShieldCheck,
};

const STATUS_STYLE: Record<JobStatus, { label: Key; cls: string }> = {
  running: { label: 'k.st.running', cls: 'bg-primary-dim text-primary' },
  done: { label: 'k.st.done', cls: 'bg-ok/12 text-ok' },
  error: { label: 'k.st.error', cls: 'bg-err/12 text-err' },
  cancelled: { label: 'k.st.cancelled', cls: 'bg-hover text-ink-2' },
};

const baseName = (p: string) => p.split(/[\\/]/).pop() ?? p;

function fmtDur(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

function fmtClock(ms: number): string {
  const d = new Date(ms);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, '0')).join(':');
}

/** re-render every second while something runs (elapsed counters) */
function useTicker(on: boolean) {
  const [, setN] = useState(0);
  useEffect(() => {
    if (!on) return;
    const h = setInterval(() => setN((n) => n + 1), 1000);
    return () => clearInterval(h);
  }, [on]);
}

const nav = (detail: string) => window.dispatchEvent(new CustomEvent('mai2srt:nav', { detail }));

export function TasksPage() {
  const { t, tf } = useI18n();
  const jobs = useJobs();
  const toast = useToast();
  const running = jobs.filter((j) => j.status === 'running').length;
  useTicker(running > 0);

  // the store already holds every job this window started; a sync picks up
  // the rest (a sign-in started elsewhere, a run from before a reload)
  useEffect(() => { discoverJobs().catch(() => {}); }, []);

  const fail = (e: unknown) => toast('error', String(e).slice(0, 160));
  const ended = jobs.filter((j) => j.status !== 'running');

  return (
    <div className="mx-auto flex min-h-full w-full max-w-[min(1200px,92%)] flex-col gap-5 px-8 py-8">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-[18px] font-semibold text-ink-1">{t('k.title')}</h1>
          <p className="mt-1 text-[12px] text-ink-3">{t('k.sub')}</p>
        </div>
        <div className="flex items-center gap-3">
          {jobs.length > 0 && (
            <span className="text-[12px] text-ink-2">
              {tf('k.count', { running, ended: ended.length })}
            </span>
          )}
          {ended.length > 0 && (
            <Button variant="ghost" onClick={() => forgetJobs(ended.map((j) => j.id)).catch(fail)}>
              <Trash2 size={13} strokeWidth={2} /> {t('k.clear')}
            </Button>
          )}
        </div>
      </div>

      {jobs.length === 0 ? (
        <Card className="flex min-h-[220px] flex-col items-center justify-center gap-3 p-10 text-center">
          <span className="grid h-12 w-12 place-items-center rounded-(--radius-l) bg-primary-dim text-primary">
            <ListChecks size={22} strokeWidth={1.8} />
          </span>
          <p className="max-w-md text-[13px] text-ink-2">{t('k.empty')}</p>
        </Card>
      ) : (
        <div className="flex flex-col gap-3">
          {jobs.map((j) => <JobCard key={`${j.id}@${j.createdAt}`} job={j} onFail={fail} />)}
        </div>
      )}
    </div>
  );
}

function JobCard({ job: j, onFail }: { job: TrackedJob; onFail: (e: unknown) => void }) {
  const { t, tf } = useI18n();
  const isRunning = j.status === 'running';
  // running jobs open, history folded; the user's toggle wins afterwards
  const [open, setOpen] = useState<boolean | null>(null);
  const expanded = open ?? isRunning;
  const [tab, setTab] = useState<'files' | 'log'>(j.kind === 'batch' ? 'files' : 'log');
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (expanded && tab === 'log') logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [expanded, tab, j.logs]);

  // media always runs as a queue: one file reads like a single transcription
  const kind: JobKind = j.kind === 'batch' && (j.files.length || j.meta.audio_paths?.length || 0) <= 1
    ? 'run' : j.kind;
  const Icon = KIND_ICON[kind] ?? AudioLines;
  const st = STATUS_STYLE[j.status];
  const progress = jobProgress(j, STAGE_IDS);
  const stage = STAGES.find((s) => s.id === j.stage);
  const end = j.finishedAt ?? Date.now();

  const batchPaths = j.meta.audio_paths ?? [];
  const batchCount = j.files.length || batchPaths.length;
  const title = j.kind === 'batch'
    ? batchCount === 1
      ? baseName(j.files[0]?.audio ?? batchPaths[0] ?? j.title)
      : tf('k.batchTitle', { n: batchCount })
    : j.kind === 'run' && j.meta.audio_path ? baseName(j.meta.audio_path)
    : j.kind === 'process' && j.meta.mai_json_path ? baseName(j.meta.mai_json_path)
    : j.kind === 'login' || j.kind === 'consent' ? t(`k.kind.${j.kind}` as Key)
    : j.title;
  const subtitle = j.kind === 'run' ? j.meta.audio_path
    : j.kind === 'process' ? j.meta.mai_json_path
    : j.kind === 'batch' && batchCount === 1 ? (j.files[0]?.audio ?? batchPaths[0])
    : undefined;

  const settled = j.files.filter((f) => f.status !== 'pending' && f.status !== 'running').length;
  // a batch of several files is a pipeline: several run at once, each row
  // names its own phase, so the header shows no single "current" stage
  const showStage = j.kind !== 'batch' || batchCount <= 1;
  const errorText = j.status === 'error' ? (j.lost ? t('k.lost') : j.error) : '';

  const refine = (jsonPath: string) =>
    openProject(jsonPath).then(() => nav('refine')).catch(onFail);
  const sole = j.result?.items?.length === 1 && j.result.items[0].status === 'done'
    ? j.result.items[0] : null;
  const single = j.result && !j.result.items ? j.result
    : sole ? {
        srt_path: sole.srt, json_path: sole.json, words: sole.words,
        entries: sole.entries, llm_note: sole.llm_note,
      }
    : null;

  return (
    <Card className="flex flex-col gap-3 p-4">
      {/* header */}
      <div className="flex items-center gap-3">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-(--radius-s) bg-primary-dim text-primary">
          <Icon size={16} strokeWidth={2} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="shrink-0 text-[11px] font-medium text-ink-3">
              {t(`k.kind.${kind}` as Key)}
            </span>
            <span className="truncate text-[14px] font-semibold text-ink-1" title={subtitle}>
              {title}
            </span>
            {j.meta.use_llm && TRANSCRIBE_KINDS.includes(j.kind) && (
              <span className="shrink-0 rounded-full border border-primary/40 px-1.5 text-[10px] font-medium text-primary">
                LLM
              </span>
            )}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-3 text-[11.5px] text-ink-3">
            <span>{tf('k.started', { time: fmtClock(j.createdAt) })}</span>
            <span className="font-mono">{tf('k.elapsed', { dur: fmtDur(end - j.createdAt) })}</span>
            {j.kind === 'batch' && j.files.length > 1 && (
              <span>{tf('k.settled', { n: settled, total: j.files.length })}</span>
            )}
            {isRunning && showStage && stage && (
              <span className="text-primary">
                {t(stage.label)}{j.stageDetail ? ` · ${j.stageDetail}` : ''}
              </span>
            )}
          </div>
        </div>
        <span className={`shrink-0 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${st.cls}`}>
          {t(st.label)}
        </span>
        <div className="flex shrink-0 items-center gap-2">
          {isRunning && TRANSCRIBE_KINDS.includes(j.kind) && (
            <Button variant="ghost" className="h-8 px-3" onClick={() => nav('transcribe')}>
              {t('k.toTranscribe')}
            </Button>
          )}
          {isRunning && (
            <Button variant="danger" className="h-8 px-3" onClick={() => cancelJob(j.id).catch(onFail)}>
              {t('k.cancel')}
            </Button>
          )}
          {!isRunning && (
            <button
              className="text-ink-3 transition-colors hover:text-err"
              onClick={() => forgetJobs([j.id]).catch(onFail)}
              title={t('k.remove')}
            >
              <Trash2 size={14} strokeWidth={2} />
            </button>
          )}
          <button
            className="text-ink-3 transition-colors hover:text-ink-1"
            onClick={() => setOpen(!expanded)}
          >
            <ChevronDown
              size={16} strokeWidth={2}
              className={`transition-transform duration-(--dur-in) ${expanded ? 'rotate-180' : ''}`}
            />
          </button>
        </div>
      </div>

      {/* progress: determinate for transcription kinds, a sweep otherwise */}
      {isRunning && (
        <div className="relative h-1.5 overflow-hidden rounded-full bg-line-1">
          {progress === null ? (
            <motion.div
              className="absolute inset-y-0 w-1/3 rounded-full bg-primary"
              animate={{ left: ['-33%', '100%'] }}
              transition={{ repeat: Infinity, duration: 1.4, ease: 'easeInOut' }}
            />
          ) : (
            <motion.div
              className="absolute inset-y-0 left-0 rounded-full bg-primary"
              animate={{ width: `${Math.max(3, progress * 100)}%` }}
              transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
            />
          )}
        </div>
      )}

      {/* outcome line */}
      {j.status === 'done' && j.result?.items && !sole && (
        <p className="text-[12px] text-ink-2">
          {tf('t.batch.summary', {
            done: j.result.done ?? 0, skipped: j.result.skipped ?? 0, failed: j.result.failed ?? 0,
          })}
          {(j.result.llm_fallback ?? 0) > 0 && ` · ${tf('t.batch.fallback', { n: j.result.llm_fallback ?? 0 })}`}
        </p>
      )}
      {single && (single.srt_path || single.json_path) && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-ink-2">
          {single.entries != null && (
            <span>
              {single.words != null && `${single.words} ${t('t.result.words')} / `}
              {single.entries} {t('t.result.entries')}
            </span>
          )}
          {single.srt_path && (
            <button
              className="inline-flex items-center gap-1.5 text-ink-2 transition-colors hover:text-primary"
              onClick={() => revealPath(single.srt_path!).catch(onFail)}
              title={single.srt_path}
            >
              <FolderOpen size={13} strokeWidth={2} /> srt
            </button>
          )}
          {single.json_path && (
            <button
              className="inline-flex items-center gap-1.5 text-ink-2 transition-colors hover:text-primary"
              onClick={() => refine(single.json_path!)}
            >
              <SlidersHorizontal size={13} strokeWidth={2} /> {t('t.result.toRefine')}
            </button>
          )}
        </div>
      )}
      {single?.llm_note && (
        <p className="text-[11.5px] text-warn">
          {tf('t.toast.llmDegraded', { err: String(single.llm_note).slice(0, 160) })}
        </p>
      )}
      {errorText && (
        <p className="break-words font-mono text-[11.5px] text-err">{errorText}</p>
      )}

      {/* details: the batch rows and/or the log */}
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ height: 0 }}
            animate={{ height: 'auto' }}
            exit={{ height: 0 }}
            transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
            className="overflow-hidden"
          >
            {j.kind === 'batch' && (
              <div className="mb-2 flex gap-1">
                {(['files', 'log'] as const).map((k) => (
                  <button
                    key={k}
                    onClick={() => setTab(k)}
                    className={`rounded-(--radius-s) px-2.5 py-1 text-[12px] font-medium transition-colors ${
                      tab === k ? 'bg-primary-dim text-primary' : 'text-ink-3 hover:bg-hover hover:text-ink-1'
                    }`}
                  >
                    {t(k === 'files' ? 'k.files' : 'k.log')}
                  </button>
                ))}
              </div>
            )}
            {j.kind === 'batch' && tab === 'files' ? (
              <div className="rounded-(--radius-s) border border-line-1 p-1">
                <BatchQueue
                  bare
                  files={j.files}
                  selected={batchPaths}
                  fileDetail={j.fileDetail}
                  onRefine={(f: BatchFile) => refine(f.json)}
                  onReveal={(f: BatchFile) => revealPath(f.srt).catch(onFail)}
                />
              </div>
            ) : (
              <div
                ref={logRef}
                className="h-48 overflow-y-auto rounded-(--radius-s) border border-line-1 px-3 py-2 font-mono text-[11.5px] leading-[1.7] text-ink-2"
              >
                {j.logs.length === 0
                  ? <span className="text-ink-3">—</span>
                  : j.logs.map((l, i) => (
                    <div key={i} className={l.startsWith('dialogue') ? 'text-primary' : ''}>{l}</div>
                  ))}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </Card>
  );
}
