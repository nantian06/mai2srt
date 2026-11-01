import { AudioLines, FileAudio, Scissors, Upload, Wand2 } from 'lucide-react';
import type { Key } from './i18n';

/** A transcription's stages, in run order (the server's `stage` events);
 *  shared by the transcribe page's timeline and the task monitor. */
export const STAGES: { id: string; label: Key; icon: typeof FileAudio }[] = [
  { id: 'prepare', label: 't.stage.prepare', icon: FileAudio },
  { id: 'upload', label: 't.stage.upload', icon: Upload },
  { id: 'transcribe', label: 't.stage.transcribe', icon: AudioLines },
  { id: 'segment', label: 't.stage.segment', icon: Scissors },
  { id: 'postprocess', label: 't.stage.postprocess', icon: Wand2 },
];
