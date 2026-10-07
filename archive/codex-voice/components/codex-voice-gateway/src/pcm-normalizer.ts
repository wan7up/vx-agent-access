export const NORMALIZED_SAMPLE_RATE = 48_000;
export const NORMALIZED_CHANNELS = 1;
export const NORMALIZED_SAMPLES_PER_FRAME = 480;

const SUPPORTED_SAMPLE_RATES = new Set([16_000, 24_000, 48_000]);

export interface Pcm16AudioFrame {
  samples: Int16Array;
  sampleRate: number;
  channelCount?: number;
  numberOfFrames?: number;
}

function clampPcm16(sample: number): number {
  return Math.max(-32_768, Math.min(32_767, Math.round(sample)));
}

/** Converts supported mono PCM16 input into continuous 48 kHz, 10 ms frames. */
export class Pcm16Mono48kNormalizer {
  private previousSample: number | null = null;
  private pending: number[] = [];

  reset(): void {
    this.previousSample = null;
    this.pending = [];
  }

  normalize(frame: Pcm16AudioFrame): Int16Array[] {
    const channelCount = frame.channelCount ?? 1;
    const numberOfFrames = frame.numberOfFrames ?? frame.samples.length;
    if (!SUPPORTED_SAMPLE_RATES.has(frame.sampleRate)) {
      throw new Error(`Unsupported remote audio sample rate: ${frame.sampleRate} Hz.`);
    }
    if (channelCount !== NORMALIZED_CHANNELS) {
      throw new Error(`Unsupported remote audio channel count: ${channelCount}.`);
    }
    if (!Number.isInteger(numberOfFrames) || numberOfFrames <= 0 || frame.samples.length !== numberOfFrames) {
      throw new Error("Remote audio frame has an invalid PCM16 sample count.");
    }

    if (frame.sampleRate === NORMALIZED_SAMPLE_RATE) {
      for (const sample of frame.samples) this.pending.push(sample);
    } else {
      const ratio = NORMALIZED_SAMPLE_RATE / frame.sampleRate;
      for (const sample of frame.samples) {
        if (this.previousSample === null) {
          // Prime the causal resampler without adding a full frame of latency.
          for (let phase = 0; phase < ratio; phase += 1) this.pending.push(sample);
        } else {
          for (let phase = 0; phase < ratio; phase += 1) {
            const interpolated = this.previousSample + (sample - this.previousSample) * phase / ratio;
            this.pending.push(clampPcm16(interpolated));
          }
        }
        this.previousSample = sample;
      }
    }

    if (frame.samples.length > 0) this.previousSample = frame.samples[frame.samples.length - 1];
    const output: Int16Array[] = [];
    while (this.pending.length >= NORMALIZED_SAMPLES_PER_FRAME) {
      output.push(Int16Array.from(this.pending.splice(0, NORMALIZED_SAMPLES_PER_FRAME)));
    }
    return output;
  }
}
