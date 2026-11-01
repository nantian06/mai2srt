/**
 * App-wide job store: the single owner of every job's live state.
 *
 * The transcribe page used to hold its job (stage, log, batch rows, result)
 * in component state and close the SSE stream on unmount, so switching to
 * another page and back showed an idle page while the job kept running.
 * Jobs now live here, outside React: one stream per job, kept open until
 * the job settles, whatever page is showing. Pages read it through
 * useJobs()/useJob(); the task monitor lists all of it.
 *
 * The server keeps every job's full event history (see server/jobs.py), so
 * the store can always be rebuilt from it: discoverJobs() lists the jobs the
 * backend knows and replays the unknown ones -- a reloaded webview finds its
 * running batch again. A dropped stream resumes from the last event seen
 * (from_idx), so a reconnect never duplicates log lines or file rows.
 */
import { useSyncExternalStore } from 'react';
import {
  API_BASE, createJob, type BatchFile, type DoneData, type JobInfo,
} from './api';

export type JobKind = 'run' | 'process' | 'batch' | 'login' | 'consent';
export type JobStatus = 'running' | 'done' | 'error' | 'cancelled';

export interface JobMeta {
  audio_path?: string;
  audio_paths?: string[];
  mai_json_path?: string;
  use_llm?: boolean;
}

export interface TrackedJob {
  id: string;
  kind: JobKind;
  title: string;
  status: JobStatus;
  /** epoch ms */
  createdAt: number;
  finishedAt: number | null;
  meta: JobMeta;
  stage: string;
  stageDetail: string;
  /** batch: the file the last stage event was about */
  stageFile: number | null;
  files: BatchFile[];
  logs: string[];
  result: DoneData | null;
  error: string;
  /** the stream could not be resumed: the backend restarted (forgot the job)
   *  or stayed unreachable -- the job's real outcome is unknown */
  lost?: boolean;
  /** the client watched this job run (vs. found it already settled) --
   *  only those get a completion notification */
  live: boolean;
}

/** what the listing returns (server Job.summary) */
interface JobSummary extends JobInfo {
  created_at?: number;
  finished_at?: number | null;
  meta?: JobMeta;
  /** length of the server-side event history */
  events?: number;
}

const LOG_CAP = 1000;
/** transcription-class kinds: the ones the transcribe page shows */
export const TRANSCRIBE_KINDS: readonly JobKind[] = ['run', 'process', 'batch'];

const jobs = new Map<string, TrackedJob>();
const streams = new Map<string, { es: EventSource | null; seen: number; retries: number }>();
const listeners = new Set<() => void>();
const settleListeners = new Set<(job: TrackedJob) => void>();
let snapshot: TrackedJob[] = [];

function emit() {
  snapshot = [...jobs.values()].sort((a, b) => b.createdAt - a.createdAt);
  for (const l of listeners) l();
}

function patch(id: string, fn: (j: TrackedJob) => Partial<TrackedJob>) {
  const j = jobs.get(id);
  if (!j) return;
  const next = { ...j, ...fn(j) };
  jobs.set(id, next);
  if (j.status === 'running' && next.status !== 'running') {
    for (const l of settleListeners) l(next);
  }
  emit();
}

function fromSummary(s: JobSummary, live: boolean): TrackedJob {
  const status = (s.status === 'queued' ? 'running' : s.status) as JobStatus;
  return {
    id: s.id,
    kind: s.kind as JobKind,
    title: s.title,
    status,
    createdAt: s.created_at ? s.created_at * 1000 : Date.now(),
    finishedAt: s.finished_at ? s.finished_at * 1000 : null,
    meta: s.meta ?? {},
    stage: '',
    stageDetail: '',
    stageFile: null,
    files: [],
    logs: [],
    result: null,
    error: '',
    live,
  };
}

/* -------------------------------------------------------------- stream */

function open(id: string) {
  const st = streams.get(id);
  if (!st) return;
  const es = new EventSource(`${API_BASE}/api/jobs/${id}/events?from_idx=${st.seen}`);
  st.es = es;
  const on = (type: string, fn: (data: Record<string, unknown>) => void) =>
    es.addEventListener(type, (e) => {
      if (!(e instanceof MessageEvent)) return;
      st.seen += 1;
      st.retries = 0;
      fn(JSON.parse(e.data));
    });
  const end = () => { es.close(); streams.delete(id); };

  on('stage', (d) => patch(id, () => ({
    stage: String(d.stage ?? ''),
    stageDetail: String(d.detail ?? ''),
    stageFile: typeof d.file_index === 'number' ? d.file_index : null,
  })));
  on('plan', (d) => patch(id, () => ({ files: d.items as BatchFile[] })));
  on('file', (d) => {
    const f = d as unknown as BatchFile;
    patch(id, (j) => ({ files: j.files.map((x) => (x.index === f.index ? f : x)) }));
  });
  on('log', (d) => patch(id, (j) => ({
    logs: j.logs.length >= LOG_CAP
      ? [...j.logs.slice(-(LOG_CAP - 1)), String(d.line)]
      : [...j.logs, String(d.line)],
  })));
  on('done', (d) => {
    end();
    const r = d as DoneData;
    patch(id, (j) => ({
      status: 'done',
      result: r,
      files: r.items ?? j.files,
      finishedAt: j.finishedAt ?? Date.now(),
    }));
  });
  on('error', (d) => {
    end();
    patch(id, (j) => ({
      status: 'error',
      error: String(d.message ?? 'unknown error'),
      finishedAt: j.finishedAt ?? Date.now(),
    }));
  });
  on('cancelled', () => {
    end();
    // the terminal event closes the stream before a batch can report the
    // files it stopped, so settle those rows here
    patch(id, (j) => ({
      status: 'cancelled',
      finishedAt: j.finishedAt ?? Date.now(),
      files: j.files.map((f) => (f.status === 'running' || f.status === 'pending'
        ? { ...f, status: 'cancelled' as const } : f)),
    }));
  });
  // transport failure (backend restarting, sleep/wake): EventSource would
  // reconnect on its own -- from event 0, replaying everything as
  // duplicates. Take over: close, then resume from the last event seen.
  es.addEventListener('error', (e) => {
    if (e instanceof MessageEvent) return;      // the job's own error event
    es.close();
    st.es = null;
    st.retries += 1;
    setTimeout(() => void recover(id), Math.min(8000, 500 * 2 ** st.retries));
  });
}

async function recover(id: string) {
  const st = streams.get(id);
  if (!st) return;
  try {
    const list = await fetchJobs();
    if (!streams.has(id)) return;
    const s = list.find((x) => x.id === id);
    if (s && s.status !== 'running' && s.status !== 'queued'
        && (s.events ?? Infinity) <= st.seen) {
      // settled and every event already seen (a job that wound down without
      // a terminal event of its own): reopening would only end again
      streams.delete(id);
      patch(id, (j) => ({
        status: s.status as JobStatus,
        finishedAt: s.finished_at ? s.finished_at * 1000 : (j.finishedAt ?? Date.now()),
      }));
      return;
    }
    if (!s) {
      // the backend no longer knows it (restarted): nothing to resume
      streams.delete(id);
      patch(id, (j) => ({
        status: 'error', error: '', lost: true,
        finishedAt: j.finishedAt ?? Date.now(),
      }));
      return;
    }
    open(id);
  } catch {
    if (st.retries >= 8) {
      streams.delete(id);
      patch(id, (j) => ({
        status: 'error', error: '', lost: true,
        finishedAt: j.finishedAt ?? Date.now(),
      }));
      return;
    }
    st.retries += 1;
    setTimeout(() => void recover(id), Math.min(8000, 500 * 2 ** st.retries));
  }
}

/* -------------------------------------------------------------- public */

/** Start tracking a job (idempotent): its history replays, then it streams. */
export function trackJob(info: JobSummary, live = true): TrackedJob {
  const known = jobs.get(info.id);
  // ids restart at 1 with a new backend process: same id, other job
  const stale = known && info.created_at
    && Math.abs(known.createdAt - info.created_at * 1000) > 1;
  if (known && !stale) return known;
  if (stale) { streams.get(info.id)?.es?.close(); streams.delete(info.id); }
  const job = fromSummary(info, live);
  jobs.set(job.id, job);
  streams.set(job.id, { es: null, seen: 0, retries: 0 });
  open(job.id);
  emit();
  return job;
}

async function fetchJobs(): Promise<JobSummary[]> {
  const r = await fetch(`${API_BASE}/api/jobs`);
  if (!r.ok) throw new Error(`jobs: HTTP ${r.status}`);
  return (await r.json()).jobs;
}

/** Sync with the backend's job list: pick up jobs this client did not start
 *  (another page's login, a run started before a webview reload) and drop
 *  the ones the backend forgot. */
export async function discoverJobs(): Promise<void> {
  const list = await fetchJobs();
  const ids = new Set(list.map((s) => s.id));
  let changed = false;
  for (const [id, j] of jobs) {
    if (!ids.has(id) && j.status !== 'running') { jobs.delete(id); changed = true; }
  }
  for (const s of list) {
    // a job found already settled replays quietly (no completion toast)
    trackJob(s, s.status === 'running' || s.status === 'queued');
  }
  if (changed) emit();
}

/** createJob + track: the way pages start transcription-class jobs. */
export async function startJob(body: Parameters<typeof createJob>[0]): Promise<TrackedJob> {
  const info = (await createJob(body)) as JobSummary;
  return trackJob({
    ...info,
    created_at: info.created_at ?? Date.now() / 1000,
    // older backends do not echo the inputs: keep what we sent
    meta: info.meta ?? {
      audio_path: body.audio_path,
      audio_paths: body.audio_paths,
      mai_json_path: body.mai_json_path,
      use_llm: body.use_llm,
    },
  });
}

/** Remove settled jobs from the history (server and client). */
export async function forgetJobs(ids: string[]): Promise<void> {
  await Promise.all(ids.map(async (id) => {
    const j = jobs.get(id);
    if (!j || j.status === 'running') return;
    const r = await fetch(`${API_BASE}/api/jobs/${id}`, { method: 'DELETE' });
    // 404: already gone server-side -- drop it here too
    if (r.ok || r.status === 404) jobs.delete(id);
  }));
  emit();
}

export function getJob(id: string | null): TrackedJob | undefined {
  return id ? jobs.get(id) : undefined;
}

/** Called once per job when it leaves `running`. */
export function onJobSettled(fn: (job: TrackedJob) => void): () => void {
  settleListeners.add(fn);
  return () => { settleListeners.delete(fn); };
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

/** every tracked job, newest first */
export function useJobs(): TrackedJob[] {
  return useSyncExternalStore(subscribe, () => snapshot);
}

export function useJob(id: string | null): TrackedJob | undefined {
  return useSyncExternalStore(subscribe, () => (id ? jobs.get(id) : undefined));
}

/** Overall progress in [0, 1] (null: indeterminate). */
export function jobProgress(j: TrackedJob, stageOrder: readonly string[]): number | null {
  if (j.status === 'done') return 1;
  if (j.kind === 'batch') {
    if (j.files.length === 0) return 0;
    const settled = j.files.filter((f) => f.status !== 'pending' && f.status !== 'running').length;
    // the running file counts by its stage, so a long file still moves the bar
    const runningIdx = stageOrder.indexOf(j.stage);
    const partial = j.files.some((f) => f.status === 'running') && runningIdx >= 0
      ? runningIdx / stageOrder.length : 0;
    return Math.min(1, (settled + partial) / j.files.length);
  }
  if (j.kind === 'run' || j.kind === 'process') {
    const i = stageOrder.indexOf(j.stage);
    return i < 0 ? 0 : i / stageOrder.length;
  }
  return null;
}
