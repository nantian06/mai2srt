import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { FileAudio, FileJson, FileVideo, FolderOpen, SlidersHorizontal } from 'lucide-react';
import { open } from '@tauri-apps/plugin-dialog';
import { BatchQueue } from '../components/BatchQueue';
import { Button, Card, StatusDot, Toggle, useToast } from '../components/ui';
import { useI18n, type Key } from '../lib/i18n';
import {
  BatchClosedError, appendBatchFiles, cancelJob, getSubtitleParams, getSystem, openProject,
  preflight, removeBatchFile, revealPath, warmParams, warmSystem, withBackendRetry,
  type BatchFile, type PreflightKey, type SubtitleParams, type SystemInfo,
} from '../lib/api';
import {
  TRANSCRIBE_KINDS, jobProgress, startJob, useJob, useJobs, type TrackedJob,
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
 *
 * Media files, one or many, always run as a batch queue (a pipeline the user
 * can keep dropping files onto); a .mai.json runs on its own.
 */
const draft: { media: string[]; json: string; jobId: string | null } = {
  media: [], json: '', jobId: null,
};

// stable empties: a fresh [] per render would re-fire the log autoscroll
const NO_FILES: TrackedJob['files'] = [];
const NO_LOGS: string[] = [];
const NO_DETAIL: TrackedJob['fileDetail'] = {};
const STAGE_IDS = STAGES.map((s) => s.id);

/** the selection a job was started with (adopting a job this page lost) */
function selectionOf(j: TrackedJob): { media: string[]; json: string } {
  if (j.kind === 'batch') return { media: j.meta.audio_paths ?? [], json: '' };
  if (j.kind === 'process') return { media: [], json: j.meta.mai_json_path ?? '' };
  return { media: j.meta.audio_path ? [j.meta.audio_path] : [], json: '' };
}

/** a row the single-file view still has to show (its reason, its actions) */
const NEEDS_ROW: BatchFile['status'][] = ['skipped', 'failed', 'cancelled'];

export function TranscribePage() {
  const { t, tf } = useI18n();
  // seeded from the startup warm cache: on a hit the status line and the
  // params card are complete on the first frame (their reserved/invisible
  // slots below cover only the cold-cache case)
  const [sys, setSys] = useState<SystemInfo | null>(warmSystem);
  // the selected media (one or many: always a queue) or one .mai.json
  const [media, setMedia] = useState<string[]>(draft.media);
  const [json, setJson] = useState(draft.json);
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
  const isJson = !!json;
  const isBatch = media.length > 1;
  // what the drop zone names
  const path = json || media[0] || '';
  // the batch job's per-file state (plan event, then one event per change)
  const files = job?.files ?? NO_FILES;
  const queueRunning = running && job?.kind === 'batch';
  // one file reads like the classic single run (stage timeline, result
  // card); several files are a pipeline, each row with its own phase
  const singleView = files.length <= 1 && !isBatch;
  const stage = job?.stage ?? '';
  const logs = job?.logs ?? NO_LOGS;
  const result = job?.status === 'done' ? job.result : null;
  // a one-file batch's outcome, shown like a single run's
  const soleItem = result?.items?.length === 1 ? result.items[0] : null;
  const single = result && !result.items
    ? {
        srt: result.srt_path, json: result.json_path, words: result.words,
        entries: result.entries, dialogue: result.dialogue,
      }
    : soleItem?.status === 'done'
      ? {
          srt: soleItem.srt, json: soleItem.json, words: soleItem.words,
          entries: soleItem.entries, dialogue: soleItem.dialogue,
        }
      : null;
  const settled = files.filter((f) => f.status !== 'pending' && f.status !== 'running').length;
  const showQueue = files.length > 1
    || (files.length === 1 && NEEDS_ROW.includes(files[0].status))
    || (files.length === 0 && isBatch);
  const error = startError
    || (job?.status === 'error' ? (job.lost ? t('k.lost') : job.error)
      : job?.status === 'cancelled' ? t('t.toast.cancelled') : '');

  const nav = () => window.dispatchEvent(new CustomEvent('mai2srt:nav', { detail: 'refine' }));
  const refineFile = (f: BatchFile) =>
    openProject(f.json).then(nav).catch((e) => toast('error', String(e).slice(0, 160)));

  // refine loads the backend's last-opened project, which another page
  // (Tasks, storage) may have moved on since this result was produced:
  // point it back at this result first. A batch's hand-off is the last file
  // it finished, as the backend's own persist() leaves it (a lone skipped
  // file: that file).
  const refineTarget = result?.json_path
    || (result && job?.kind === 'process' ? job.meta.mai_json_path : '')
    || result?.items?.filter((f) => f.status === 'done').at(-1)?.json
    || (soleItem?.status === 'skipped' ? soleItem.json : '');
  const toRefine = () => {
    if (!refineTarget) { nav(); return; }
    openProject(refineTarget).then(nav).catch((e) => toast('error', String(e).slice(0, 160)));
  };

  // write-through: the next mount starts from here
  useEffect(() => {
    Object.assign(draft, { media, json, jobId });
  }, [media, json, jobId]);

  // a transcription running that this page is not showing (the webview was
  // reloaded, or the job was started before the draft existed): adopt it,
  // with the selection it was started with
  useEffect(() => {
    if (running || starting) return;
    const live = allJobs.find((j) => j.status === 'running'
      && TRANSCRIBE_KINDS.includes(j.kind) && j.id !== jobId);
    if (!live) return;
    const sel = selectionOf(live);
    setMedia(sel.media);
    setJson(sel.json);
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

  /** One entry point for drop + browse.
   *  Idle: the files become the selection. A queue running: media files
   *  join it (the pipeline picks them up when their turn comes). */
  function selectPaths(paths: string[]) {
    const ok = paths.filter((p) => classify(p) !== null);
    if (ok.length === 0) {
      toast('warn', t('t.drop.rejected'));
      return;
    }
    if (paths.length > ok.length) {
      toast('warn', tf('t.batch.rejected', { n: paths.length - ok.length }));
    }
    const mediaIn = ok.filter((p) => classify(p) !== 'json');
    const mixed = ok.length > 1 && mediaIn.length > 0 && mediaIn.length < ok.length;
    if (starting) {
      toast('warn', t('t.busy.select'));
      return;
    }
    if (running && job) {
      if (job.kind !== 'batch') {
        // a .mai.json run cannot take media files
        toast('warn', t('t.busy.select'));
        return;
      }
      if (mediaIn.length === 0) {
        toast('warn', t('t.busy.json'));
        return;
      }
      if (mixed) toast('warn', t('t.batch.jsonIgnored'));
      void appendToQueue(job.id, mediaIn);
      return;
    }
    if (mixed) toast('warn', t('t.batch.jsonIgnored'));
    if (mediaIn.length > 0) {
      setJson('');
      setMedia(mediaIn);
    } else {
      // classify(), not a suffix test: the picker also accepts plain .json
      setMedia([]);
      setJson(ok[0]);
    }
    // a finished job stays in the task monitor; the page moves on
    setJobId(null);
    setStartError('');
  }

  /** Files dropped on a running queue (or a skipped row re-transcribed). */
  async function appendToQueue(id: string, paths: string[], overwrite = false) {
    try {
      const r = await appendBatchFiles(id, paths, overwrite);
      if (r.items.length > 0) toast('ok', tf('t.queue.added', { n: r.items.length }));
      if (r.ignored > 0) toast('warn', tf('t.queue.dup', { n: r.ignored }));
      setMedia((cur) => [...cur, ...r.items.map((f) => f.audio).filter((a) => !cur.includes(a))]);
    } catch (e) {
      if (e instanceof BatchClosedError) {
        // the queue finished between the drop and the request: the files
        // were meant to run, so they start a queue of their own
        await start({ media: paths, overwrite: overwrite ? paths : undefined });
        return;
      }
      toast('error', tf('t.queue.failed', { err: String(e).slice(0, 140) }));
    }
  }

  async function removeQueued(f: BatchFile) {
    if (!jobId) return;
    try {
      await removeBatchFile(jobId, f.index);
      setMedia((cur) => cur.filter((p) => p !== f.audio));
    } catch (e) {
      toast('error', tf('t.queue.removeFailed', { err: String(e).slice(0, 140) }));
    }
  }

  /** A skipped file's "re-transcribe": into the running queue, else a run
   *  of its own. */
  async function retranscribe(f: BatchFile) {
    if (queueRunning && jobId) {
      await appendToQueue(jobId, [f.audio], true);
      return;
    }
    await start({ media: [f.audio], overwrite: [f.audio] });
  }

  function removeFromQueue(p: string) {
    setMedia(media.filter((x) => x !== p));
  }

  // the native listener below is registered once; it calls whatever
  // selectPaths is current, so a drop always sees the live page state
  const selectRef = useRef(selectPaths);
  useEffect(() => { selectRef.current = selectPaths; });

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
            selectRef.current(dropped);
          },
        );
      } catch { /* plain browser */ }
    })();
    return () => un?.();
  }, []);

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

  /** Start the selection (or `opts.media`, e.g. one file to redo). */
  async function start(opts: { media?: string[]; overwrite?: string[] } = {}) {
    if (busy) return;
    const mediaList = opts.media ?? media;
    const jsonPath = opts.media ? '' : json;
    // busy FIRST (a double click must not fire two preflights), but the page
    // state is only cleared once the job is really going to start: a refused
    // click must not wipe the previous run's result and log
    setStarting(true);
    try {
      // refuse up front rather than watch the job die a minute later: the
      // backend names exactly which prerequisite is missing (static check,
      // instant -- an expired session is still caught inside the job)
      const kind = jsonPath ? 'process' : 'run';
      const pf = await preflight(kind, useLlm).catch(() => null);
      if (pf && !pf.ok) {
        toast('warn', tf('t.toast.missing', {
          items: pf.missing.map((k) => t(PREFLIGHT_LABEL[k])).join('、'),
        }));
        return;
      }
      setStartError('');
      if (opts.media) {
        setJson('');
        setMedia(opts.media);
      }
      // completion toasts come from the app shell (lib/jobs settle hook),
      // so they still fire when the job ends on another page
      const started = await startJob(
        jsonPath
          ? { kind: 'process', mai_json_path: jsonPath, use_llm: useLlm }
          : {
              kind: 'batch', audio_paths: mediaList, use_llm: useLlm,
              ...(opts.overwrite ? { overwrite_paths: opts.overwrite } : {}),
            },
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
  const progress = job && files.length > 0 ? jobProgress(job, STAGE_IDS) : null;

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
                {tf('t.batch.files', { n: media.length })}
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
            {queueRunning && (
              <p className="text-[12px] text-primary">{t('t.queue.hint')}</p>
            )}
          </div>
        </Card>
      </div>

      {/* the queue: removable rows before the start, live statuses after
          (each running file with its own pipeline phase; waiting files can
          still be taken out, skipped ones redone) */}
      {showQueue && (
        <BatchQueue
          files={files}
          selected={media}
          fileDetail={job?.fileDetail ?? NO_DETAIL}
          onRemove={busy || files.length > 0 ? undefined : removeFromQueue}
          onRemoveFile={queueRunning ? removeQueued : undefined}
          onRetranscribe={starting ? undefined : retranscribe}
          onRefine={refineFile}
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
          <Button onClick={() => start()} busy={busy} disabled={!path || busy}>
            {busy ? t('t.action.running')
              : isJson ? t('t.action.process')
              : isBatch ? tf('t.batch.start', { n: media.length })
              : t('t.action.run')}
          </Button>
          {!busy && (result?.failed ?? 0) > 0 && (
            <Button variant="ghost" onClick={() => start()}>
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

      {/* one file: the stage timeline, from the moment the click lands
          (busy), even though the first stage event may still be in flight.
          Several: files sit in different stages at once (the pipeline), so
          the rows carry the phases and this shows the overall progress */}
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
            {singleView ? (
              <Card className="flex items-center justify-between px-6 py-4">
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
            ) : (
              <Card className="flex items-center gap-4 px-6 py-4">
                <span className="shrink-0 text-[12px] font-medium text-ink-2">
                  {tf('k.settled', { n: settled, total: files.length || media.length })}
                </span>
                <div className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-line-1">
                  <motion.div
                    className="absolute inset-y-0 left-0 rounded-full bg-primary"
                    animate={{ width: `${Math.max(3, (progress ?? 0) * 100)}%` }}
                    transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
                  />
                </div>
              </Card>
            )}
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
      {result?.items && !single && (
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
      {single && (
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}>
          <Card className="flex flex-col gap-3 p-5">
            <div className="flex items-center justify-between">
              <span className="text-[14px] font-semibold text-ok">{t('t.result.done')}</span>
              <span className="text-[12px] text-ink-2">
                {single.words != null && `${single.words} ${t('t.result.words')} / `}
                {single.entries} {t('t.result.entries')}
                {single.dialogue != null && ` / ${single.dialogue} ${t('t.result.dialogue')}`}
              </span>
            </div>
            {(['srt', 'json'] as const).map(
              (k) =>
                single[k] && (
                  <div key={k} className="flex items-center gap-3">
                    <span className="w-10 text-[11px] uppercase text-ink-3">{k}</span>
                    <code className="flex-1 truncate font-mono text-[11.5px] text-primary">
                      {single[k]}
                    </code>
                    <button
                      className="text-ink-2 transition-colors hover:text-primary"
                      onClick={() =>
                        single[k] &&
                        revealPath(single[k]!).catch((e) =>
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
      {result && refineTarget && (
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
