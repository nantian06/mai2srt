import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { FileAudio, FileJson, FileVideo, FolderOpen, SlidersHorizontal } from 'lucide-react';
import { open } from '@tauri-apps/plugin-dialog';
import { BatchQueue } from '../components/BatchQueue';
import { Button, Card, StatusDot, Toggle, useToast } from '../components/ui';
import { useI18n, type Key } from '../lib/i18n';
import {
  cancelJob, getSubtitleParams, getSystem, openProject, preflight, revealPath,
  warmParams, warmSystem, withBackendRetry,
  type PreflightKey, type SubtitleParams, type SystemInfo,
} from '../lib/api';
import {
  TRANSCRIBE_KINDS, getJob, startJob, useJob, useJobs, type TrackedJob,
} from '../lib/jobs';
import { STAGES } from '../lib/stages';
import { loadUseLlm, storeUseLlm } from '../lib/subtitle';

/** how a missing prerequisite is named in the refusal toast */
const PREFLIGHT_LABEL: Record<PreflightKey, Key> = {
  ffmpeg: 't.pre.ffmpeg',
  browser: 't.pre.browser',
  session: 't.pre.session',
  llm: 't.pre.llm',
};

/** accepted drop/pick inputs (user-approved curated lists; the backend
 *  pipeline itself is container-agnostic -- probe reads the audio stream
 *  and -vn encode strips the video track) */
const AUDIO_EXTS = ['mp3', 'wav', 'm4a', 'flac', 'ogg', 'opus', 'aac', 'wma'];
const VIDEO_EXTS = ['mp4', 'mkv', 'webm', 'mov', 'm4v', 'avi', 'wmv', 'ts', 'flv'];

function classify(p: string): 'json' | 'video' | 'audio' | null {
  const low = p.toLowerCase();
  if (low.endsWith('.mai.json') || low.endsWith('.json')) return 'json';
  if (VIDEO_EXTS.some((e) => low.endsWith('.' + e))) return 'video';
  if (AUDIO_EXTS.some((e) => low.endsWith('.' + e))) return 'audio';
  return null;
}

function isVideoPath(p: string): boolean {
  return VIDEO_EXTS.some((e) => p.toLowerCase().endsWith('.' + e));
}

/**
 * What the page was showing, kept OUTSIDE the component: switching to another
 * page unmounts it, and coming back must find the same selection and the job
 * it started (the job itself lives in the app-wide store, lib/jobs).
 */
const draft: { path: string; isJson: boolean; batchPaths: string[]; jobId: string | null } = {
  path: '', isJson: false, batchPaths: [], jobId: null,
};

// stable empties: a fresh [] per render would re-fire the log autoscroll
const NO_FILES: TrackedJob['files'] = [];
const NO_LOGS: string[] = [];

/** the selection a job was started with (adopting a job this page lost) */
function selectionOf(j: TrackedJob): { path: string; isJson: boolean; batchPaths: string[] } {
  if (j.kind === 'batch') return { path: '', isJson: false, batchPaths: j.meta.audio_paths ?? [] };
  if (j.kind === 'process') return { path: j.meta.mai_json_path ?? '', isJson: true, batchPaths: [] };
  return { path: j.meta.audio_path ?? '', isJson: false, batchPaths: [] };
}

export function TranscribePage() {
  const { t, tf } = useI18n();
  // seeded from the startup warm cache: on a hit the status line and the
  // params card are complete on the first frame (their reserved/invisible
  // slots below cover only the cold-cache case)
  const [sys, setSys] = useState<SystemInfo | null>(warmSystem);
  const [path, setPath] = useState(draft.path);
  const [isJson, setIsJson] = useState(draft.isJson);
  // two or more media files = a batch (a single file keeps the classic flow)
  const [batchPaths, setBatchPaths] = useState<string[]>(draft.batchPaths);
  // shared with the refine workspace (persisted) so both pages agree
  const [useLlm, setUseLlmState] = useState(loadUseLlm);
  const [params, setParams] = useState<SubtitleParams | null>(warmParams);
  // the job this page shows; its state (stage, log, batch rows, result) is
  // read from the app-wide store, which keeps streaming while the page is
  // unmounted
  const [jobId, setJobId] = useState<string | null>(draft.jobId);
  const job = useJob(jobId);
  const allJobs = useJobs();
  // true from the click until the job exists: instant feedback even before
  // the createJob roundtrip resolves (the click must never feel dead)
  const [starting, setStarting] = useState(false);
  // a start that failed before any job existed (409 busy, bad path)
  const [startError, setStartError] = useState('');
  const [dragOver, setDragOver] = useState(false);
  const toast = useToast();
  const logRef = useRef<HTMLDivElement>(null);

  const running = job?.status === 'running';
  const busy = starting || running;
  const isBatch = batchPaths.length > 1;
  // the batch job's per-file state (plan event, then one event per change)
  const files = job?.files ?? NO_FILES;
  const stage = job?.stage ?? '';
  const logs = job?.logs ?? NO_LOGS;
  const result = job?.status === 'done' ? job.result : null;
  const error = startError
    || (job?.status === 'error' ? (job.lost ? t('k.lost') : job.error)
      : job?.status === 'cancelled' ? t('t.toast.cancelled') : '');

  // refine loads the backend's last-opened project, which another page
  // (Tasks, storage) may have moved on since this result was produced:
  // point it back at this result first. A batch's hand-off is the last file
  // it finished, as the backend's own persist() leaves it.
  const toRefine = () => {
    const last = result?.items?.filter((f) => f.status === 'done').at(-1);
    const target = result?.json_path || last?.json;
    const nav = () => window.dispatchEvent(new CustomEvent('mai2srt:nav', { detail: 'refine' }));
    if (!target) { nav(); return; }
    openProject(target).then(nav).catch((e) => toast('error', String(e).slice(0, 160)));
  };

  // write-through: the next mount starts from here
  useEffect(() => {
    Object.assign(draft, { path, isJson, batchPaths, jobId });
  }, [path, isJson, batchPaths, jobId]);

  // a transcription running that this page is not showing (the webview was
  // reloaded, or the job was started before the draft existed): adopt it,
  // with the selection it was started with
  useEffect(() => {
    if (running || starting) return;
    const live = allJobs.find((j) => j.status === 'running'
      && TRANSCRIBE_KINDS.includes(j.kind) && j.id !== jobId);
    if (!live) return;
    const sel = selectionOf(live);
    setPath(sel.path);
    setIsJson(sel.isJson);
    setBatchPaths(sel.batchPaths);
    setStartError('');
    setJobId(live.id);
  }, [allJobs, running, starting, jobId]);

  // the run may have refreshed the session/cookie state the status line shows
  useEffect(() => {
    if (job?.status === 'done') getSystem().then(setSys).catch(() => {});
  }, [job?.status]);

  const setUseLlm = (v: boolean) => {
    setUseLlmState(v);
    storeUseLlm(v);
  };

  useEffect(() => {
    // retry-wrapped: this page mounts BEFORE the startup prefetch settles, so
    // its own first fetch lands while the packaged sidecar may still be
    // booting. A one-shot fetch then failed once and left the status line and
    // the params card invisible for good (they only reappear after the user
    // switches pages and the warm cache happens to be filled by then).
    let alive = true;
    withBackendRetry(() => getSystem())
      .then((s) => { if (alive) setSys(s); })
      .catch(() => { if (alive) toast('warn', t('t.toast.backendDown')); });
    withBackendRetry(() => getSubtitleParams())
      .then(({ params: p }) => { if (alive) setParams(p); })
      .catch(() => { /* the status-line toast above already said it */ });
    // the job's stream is NOT closed here: it belongs to the store and
    // keeps running while another page is showing
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [logs]);

  /** One entry point for drop + browse: decides single vs batch. */
  function selectPaths(paths: string[]) {
    // one job at a time (the backend's mutex): a new selection would detach
    // the page from the run it is showing. Read from the draft + store, not
    // render state: the native drag-drop listener keeps its first closure
    if (getJob(draft.jobId)?.status === 'running') {
      toast('warn', t('t.busy.select'));
      return;
    }
    const ok = paths.filter((p) => classify(p) !== null);
    if (ok.length === 0) {
      toast('warn', t('t.drop.rejected'));
      return;
    }
    if (paths.length > ok.length) {
      toast('warn', tf('t.batch.rejected', { n: paths.length - ok.length }));
    }
    const media = ok.filter((p) => classify(p) !== 'json');
    if (ok.length > 1 && media.length > 0 && media.length < ok.length) {
      toast('warn', t('t.batch.jsonIgnored'));
    }
    if (media.length > 1) {
      setIsJson(false);
      setPath('');
      setBatchPaths(media);
    } else {
      const p = media[0] ?? ok[0];
      // classify(), not a suffix test: the picker also accepts plain .json
      setIsJson(classify(p) === 'json');
      setPath(p);
      setBatchPaths([]);
    }
    // a finished job stays in the task monitor; the page moves on
    setJobId(null);
    setStartError('');
  }

  function removeFromQueue(p: string) {
    const left = batchPaths.filter((x) => x !== p);
    if (left.length > 1) {
      setBatchPaths(left);
    } else {
      // down to one file (or none): back to the single-file flow
      setBatchPaths([]);
      setPath(left[0] ?? '');
    }
  }

  // native drag-drop with absolute paths (tauri)
  useEffect(() => {
    let un: (() => void) | undefined;
    (async () => {
      try {
        const { getCurrentWebview } = await import('@tauri-apps/api/webview');
        un = await getCurrentWebview().listen<{ paths: string[] }>(
          'tauri://drag-drop',
          (e) => {
            const dropped = e.payload.paths ?? [];
            if (dropped.length === 0) return;
            selectPaths(dropped);
          },
        );
      } catch { /* plain browser */ }
    })();
    return () => un?.();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function pick() {
    // the dialog's overloads key off a literal `multiple`; ours is a runtime flag
    const p = (await open({
      multiple: !isJson,
      filters: isJson
        ? [{ name: 'mai2srt json', extensions: ['json'] }]
        : [
            { name: 'audio', extensions: AUDIO_EXTS },
            { name: 'video', extensions: VIDEO_EXTS },
          ],
    })) as string | string[] | null;
    if (typeof p === 'string') selectPaths([p]);
    else if (Array.isArray(p) && p.length > 0) selectPaths(p);
  }

  async function start() {
    if (busy) return;
    // busy FIRST (a double click must not fire two preflights), but the page
    // state is only cleared once the job is really going to start: a refused
    // click must not wipe the previous run's result and log
    setStarting(true);
    try {
      // refuse up front rather than watch the job die a minute later: the
      // backend names exactly which prerequisite is missing (static check,
      // instant -- an expired session is still caught inside the job)
      const kind = isJson ? 'process' : 'run';
      const pf = await preflight(kind, useLlm).catch(() => null);
      if (pf && !pf.ok) {
        toast('warn', tf('t.toast.missing', {
          items: pf.missing.map((k) => t(PREFLIGHT_LABEL[k])).join('、'),
        }));
        return;
      }
      setStartError('');
      // completion toasts come from the app shell (lib/jobs settle hook),
      // so they still fire when the job ends on another page
      const started = await startJob(
        isBatch
          ? { kind: 'batch', audio_paths: batchPaths, use_llm: useLlm }
          : isJson
            ? { kind: 'process', mai_json_path: path, use_llm: useLlm }
            : { kind: 'run', audio_path: path, use_llm: useLlm },
      );
      setJobId(started.id);
    } catch (e) {
      setJobId(null);
      setStartError(String(e));
    } finally {
      setStarting(false);
    }
  }

  const stageIdx = STAGES.findIndex((s) => s.id === stage);
  // center the invitation until real work content appears, then anchor top
  const engaged = busy || logs.length > 0 || !!result || !!error || isBatch || files.length > 0;

  return (
    <div
      className={`mx-auto flex min-h-full w-full max-w-[min(1200px,92%)] flex-col gap-5 px-8 py-8 ${
        engaged ? 'justify-start' : 'justify-center'
      }`}
    >
      {/* system status line: ALWAYS occupies its row (invisible until sys
          lands). The column is justify-center while nothing is engaged,
          so a block that appears late would shove the drop zone up by
          half its height -- reserving the slot keeps the page still */}
      <div
        className={`flex flex-wrap items-center gap-4 text-[12px] text-ink-2 ${
          sys ? '' : 'invisible'
        }`}
      >
        <span className="flex items-center gap-1.5">
          <StatusDot ok={!!sys?.ffmpeg.found} /> ffmpeg
        </span>
        <span className="flex items-center gap-1.5">
          <StatusDot ok={!!sys?.chrome.found} /> {sys?.chrome.channel ?? t('t.sys.noBrowser')}
        </span>
        <span className="flex items-center gap-1.5">
          <StatusDot ok={!!sys?.cookie_jar} /> {t('t.sys.session')}
        </span>
        <span className="ml-auto font-mono text-[11px] text-ink-3">
          {sys ? `v${sys.version}` : ''}
        </span>
      </div>

      {/* drop zone */}
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          const f = e.dataTransfer.files[0];
          if (f) selectPaths([f.name]);
        }}
        className="relative"
      >
        <Card
          className={`cursor-pointer p-10 text-center transition-all duration-(--dur-in) ${
            dragOver ? 'scale-[1.01] border-primary/60' : ''
          }`}
        >
          <div onClick={pick} className="flex min-h-[240px] flex-col items-center justify-center gap-3">
            <motion.div
              animate={dragOver ? { y: -4, scale: 1.06 } : { y: 0, scale: 1 }}
              transition={{ duration: 0.2, ease: [0.34, 1.56, 0.64, 1] }}
              className="grid h-14 w-14 place-items-center rounded-(--radius-l) bg-primary-dim text-primary"
            >
              {isJson ? <FileJson size={26} strokeWidth={1.8} />
                : isVideoPath(path) ? <FileVideo size={26} strokeWidth={1.8} />
                : <FileAudio size={26} strokeWidth={1.8} />}
            </motion.div>
            {isBatch ? (
              <p className="text-[15px] font-semibold text-ink-1">
                {tf('t.batch.files', { n: batchPaths.length })}
              </p>
            ) : path ? (
              <>
                <p className="text-[15px] font-semibold text-ink-1">
                  {path.split(/[\\/]/).pop()}
                </p>
                <p className="font-mono text-[11px] text-ink-3">{path}</p>
              </>
            ) : (
              <p className="text-[14px] text-ink-2">
                {t('t.drop.hint')}
              </p>
            )}
          </div>
        </Card>
      </div>

      {/* batch queue: removable rows before the start, live statuses after */}
      {(isBatch || files.length > 0) && (
        <BatchQueue
          files={files}
          selected={batchPaths}
          onRemove={busy || files.length > 0 ? undefined : removeFromQueue}
        />
      )}

      {/* persisted subtitle params (all 8, edited on the refine page): sits
          ABOVE the action row so it reads as "what will this run use"
          before the eye reaches the start button. Like the status line it
          is ALWAYS in the flow (invisible until loaded) -- a card that
          appeared late would shift the centered column */}
      <Card className={`flex items-center gap-3 px-4 py-2.5 ${params ? '' : 'invisible'}`}>
        <span className="grid h-7 w-7 shrink-0 place-items-center rounded-(--radius-s) bg-primary-dim text-primary">
          <SlidersHorizontal size={13} strokeWidth={2} />
        </span>
        <span className="min-w-0 flex-1 truncate text-[12px] text-ink-2">
          {params && (
            <>
              {tf('t.params.summary', {
                dur: params.max_duration,
                chars: params.max_chars,
                minDur: params.min_duration,
                minCh: params.min_chars,
                pause: params.split_pause,
                merge: params.merge_gap,
                tol: params.tolerance,
                exp: params.expand,
              })}
              <span className="mx-2 text-ink-3">|</span>
              <span className={useLlm ? 'font-medium text-primary' : 'text-ink-3'}>
                {useLlm ? t('t.params.llmOn') : t('t.params.llmOff')}
              </span>
            </>
          )}
        </span>
        {/* custom (not Button ghost): border+bg+text all shift to the theme
            color on hover and the press scales down, so the click is felt */}
        <button
          onClick={() => window.dispatchEvent(new CustomEvent('mai2srt:nav', { detail: 'refine' }))}
          className="inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-(--radius-s) border border-line-1 px-4 text-[13px] font-semibold text-ink-1 transition-all duration-(--dur-in) hover:border-primary/60 hover:bg-primary-dim hover:text-primary active:scale-[0.97]"
        >
          <SlidersHorizontal size={13} strokeWidth={2} /> {t('t.params.adjust')}
        </button>
      </Card>

      {/* controls: actions left, options right so the row uses its width */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <Button onClick={start} busy={busy} disabled={!(path || isBatch) || busy}>
            {busy ? t('t.action.running')
              : isJson ? t('t.action.process')
              : isBatch ? tf('t.batch.start', { n: batchPaths.length })
              : t('t.action.run')}
          </Button>
          {!busy && (result?.failed ?? 0) > 0 && (
            <Button variant="ghost" onClick={start}>
              {t('t.batch.retry')}
            </Button>
          )}
          {running && (
            <Button variant="danger" onClick={() => jobId && cancelJob(jobId).catch(() => {})}>
              {t('t.action.cancel')}
            </Button>
          )}
        </div>
        <Toggle checked={useLlm} onChange={setUseLlm} label={t('t.toggle.llm')} />
      </div>

      {/* stage timeline: appears the moment the click lands (busy), even
          though the first stage event may still be in flight */}
      <AnimatePresence>
        {busy && (
          /* height-only reveal: a glass Card lives inside, and opacity
             animation breaks backdrop-filter sampling mid-fade (the
             glass flash bug) -- the card would look flat gray until the
             fade lands and the tint pops in late */
          <motion.div
            initial={{ height: 0 }}
            animate={{ height: 'auto' }}
            exit={{ height: 0 }}
            className="overflow-hidden"
          >
            <Card className="flex items-center justify-between px-6 py-4">
              {isBatch && files.length > 0 && (
                <span className="mr-4 shrink-0 text-[12px] font-medium text-ink-2">
                  {tf('t.batch.progress', {
                    i: Math.max(1, files.findIndex((f) => f.status === 'running') + 1),
                    n: files.length,
                  })}
                </span>
              )}
              {STAGES.map((s, i) => {
                const on = i <= stageIdx;
                const current = i === stageIdx;
                return (
                  <div key={s.id} className="flex flex-col items-center gap-2">
                    <motion.div
                      animate={current ? { scale: [1, 1.15, 1] } : { scale: 1 }}
                      transition={current ? { repeat: Infinity, duration: 1.4 } : {}}
                      className={`grid h-9 w-9 place-items-center rounded-full border transition-colors duration-(--dur-in) ${
                        on
                          ? 'border-primary bg-primary-dim text-primary'
                          : 'border-line-1 text-ink-3'
                      }`}
                    >
                      <s.icon size={15} strokeWidth={2} />
                    </motion.div>
                    <span className={`text-[10.5px] font-medium ${on ? 'text-primary' : 'text-ink-3'}`}>
                      {t(s.label)}
                    </span>
                  </div>
                );
              })}
            </Card>
          </motion.div>
        )}
      </AnimatePresence>

      {/* logs */}
      {logs.length > 0 && (
        <div
          ref={logRef}
          className="glass rounded-(--radius-m) h-56 overflow-y-auto px-4 py-3 font-mono text-[11.5px] leading-[1.7] text-ink-2"
        >
          {logs.map((l, i) => (
            <div key={i} className={l.startsWith('dialogue') ? 'text-primary' : ''}>
              {l}
            </div>
          ))}
        </div>
      )}

      {/* result */}
      {result?.items && (
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}>
          <Card className="flex items-center justify-between p-5">
            <span className="text-[14px] font-semibold text-ok">{t('t.result.done')}</span>
            <span className="text-[12px] text-ink-2">
              {tf('t.batch.summary', {
                done: result.done ?? 0, skipped: result.skipped ?? 0, failed: result.failed ?? 0,
              })}
              {(result.llm_fallback ?? 0) > 0 &&
                ` · ${tf('t.batch.fallback', { n: result.llm_fallback ?? 0 })}`}
            </span>
          </Card>
        </motion.div>
      )}
      {result && !result.items && (
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}>
          <Card className="flex flex-col gap-3 p-5">
            <div className="flex items-center justify-between">
              <span className="text-[14px] font-semibold text-ok">{t('t.result.done')}</span>
              <span className="text-[12px] text-ink-2">
                {result.words != null && `${result.words} ${t('t.result.words')} / `}
                {result.entries} {t('t.result.entries')}
                {result.dialogue != null && ` / ${result.dialogue} ${t('t.result.dialogue')}`}
              </span>
            </div>
            {(['srt_path', 'json_path'] as const).map(
              (k) =>
                result[k] && (
                  <div key={k} className="flex items-center gap-3">
                    <span className="w-10 text-[11px] uppercase text-ink-3">
                      {k === 'srt_path' ? 'srt' : 'json'}
                    </span>
                    <code className="flex-1 truncate font-mono text-[11.5px] text-primary">
                      {result[k]}
                    </code>
                    <button
                      className="text-ink-2 transition-colors hover:text-primary"
                      onClick={() =>
                        result[k] &&
                        revealPath(result[k]!).catch((e) =>
                          toast('error', String(e).slice(0, 160)),
                        )
                      }
                      title={t('t.hint.reveal')}
                    >
                      <FolderOpen size={15} strokeWidth={2} />
                    </button>
                  </div>
                ),
            )}
          </Card>
        </motion.div>
      )}

      {/* one-click hand-off: refine auto-loads this transcription and
          restores its LLM segmentation when the run produced one.
          Styled as a primary action (same colors/hover/shadow as 开始转录)
          but keeps its own size; flush left, not centered */}
      {result && (!result.items || (result.done ?? 0) > 0) && (
        <div>
          <button
            onClick={toRefine}
            className="inline-flex h-9 items-center justify-center gap-2 rounded-(--radius-s) bg-primary px-5 text-[13px] font-semibold text-on-primary shadow-[var(--shadow-1)] transition-all duration-(--dur-in) hover:bg-primary-hover active:brightness-95"
          >
            <SlidersHorizontal size={13} strokeWidth={2} /> {t('t.result.toRefine')}
          </button>
        </div>
      )}

      {error && (
        <Card className="border-err/40 p-4">
          <p className="break-words font-mono text-[12px] text-err">{error}</p>
        </Card>
      )}
    </div>
  );
}
