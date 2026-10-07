import wrtc from "@roamhq/wrtc";
import type { CodexAppServer } from "./codex-app-server.js";
import {
  NORMALIZED_CHANNELS,
  NORMALIZED_SAMPLE_RATE,
  NORMALIZED_SAMPLES_PER_FRAME,
  Pcm16Mono48kNormalizer,
  type Pcm16AudioFrame,
} from "./pcm-normalizer.js";
import type { AudioChunk, GatewayEvent } from "./types.js";

const INPUT_SAMPLE_RATE = 48_000;
const INPUT_CHANNELS = 1;
const INPUT_SAMPLES_PER_FRAME = 480;
const INPUT_BYTES_PER_FRAME = INPUT_SAMPLES_PER_FRAME * Int16Array.BYTES_PER_ELEMENT;
const CONNECTION_TIMEOUT_MS = 30_000;
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;

export function tcpOnlyIceCandidates(sdp: string): string {
  return sdp
    .split(/\r?\n/)
    .filter(line => !line.startsWith("a=candidate:") || /\sTCP\s/i.test(line))
    .join("\r\n");
}

export class HeadlessWebRtcPeer {
  private peer: wrtc.RTCPeerConnection | null = null;
  private source: wrtc.nonstandard.RTCAudioSource | null = null;
  private localTrack: wrtc.MediaStreamTrack | null = null;
  private sink: wrtc.nonstandard.RTCAudioSink | null = null;
  private remoteDescription: Promise<void> | null = null;
  private resolveRemoteDescription: (() => void) | null = null;
  private rejectRemoteDescription: ((error: Error) => void) | null = null;
  private startedEvent: GatewayEvent | null = null;
  private ready = false;
  private readonly outputNormalizer = new Pcm16Mono48kNormalizer();
  private lastRemoteAudioWarning: string | null = null;

  constructor(private readonly onEvent: (event: GatewayEvent) => void) {}

  async start(adapter: CodexAppServer): Promise<void> {
    if (this.peer) throw new Error("WebRTC peer is already running.");
    this.outputNormalizer.reset();
    this.lastRemoteAudioWarning = null;
    this.peer = new wrtc.RTCPeerConnection();
    this.source = new wrtc.nonstandard.RTCAudioSource();
    this.localTrack = this.source.createTrack();
    this.peer.addTrack(this.localTrack);
    this.peer.createDataChannel("oai-events");
    this.peer.ontrack = event => this.attachRemoteAudio(event.track);
    this.peer.onconnectionstatechange = () => {
      if (this.peer?.connectionState === "failed") this.onEvent({ type: "session.error", message: "Codex Voice WebRTC connection failed.", recoverable: true });
    };
    this.remoteDescription = new Promise<void>((resolve, reject) => {
      this.resolveRemoteDescription = resolve;
      this.rejectRemoteDescription = reject;
    });
    void this.remoteDescription.catch(() => {});

    await this.peer.setLocalDescription(await this.peer.createOffer({ offerToReceiveAudio: true }));
    await this.waitForIceGathering();
    const offer = this.peer.localDescription?.sdp;
    if (!offer) throw new Error("Unable to create a WebRTC offer.");

    // Keep the complete offer and let ICE select the viable path. Through
    // OpenClash, negotiation is more reliable with all candidates present even
    // when the selected pair ultimately uses TCP 443.
    await adapter.start(offer);
    await this.remoteDescription;
    await this.waitForConnection();
    this.ready = true;
    this.onEvent(this.startedEvent ?? { type: "session.started", threadId: adapter.currentThreadId ?? "" });
  }

  handleCodexEvent(event: GatewayEvent): boolean {
    if (event.type === "session.sdp") {
      void this.setRemoteDescription(event.sdp);
      return true;
    }
    if (event.type === "session.started") {
      this.startedEvent = event;
      return true;
    }
    return false;
  }

  appendAudio(audio: AudioChunk): void {
    if (!this.source) throw new Error("WebRTC audio input is not running.");
    if (audio.sampleRate !== INPUT_SAMPLE_RATE || audio.numChannels !== INPUT_CHANNELS || audio.samplesPerChannel !== INPUT_SAMPLES_PER_FRAME) {
      throw new Error("Bridge audio must be 48 kHz mono signed PCM16 in 10 ms frames.");
    }
    if (!BASE64.test(audio.data)) throw new Error("Bridge audio data must be base64.");
    const bytes = Buffer.from(audio.data, "base64");
    if (bytes.length !== INPUT_BYTES_PER_FRAME) throw new Error("Bridge audio frame has an invalid length.");
    // node-webrtc inspects the backing buffer length, so do not pass a pooled
    // Node Buffer view whose ArrayBuffer is larger than the 10 ms frame.
    const samples = new Int16Array(INPUT_SAMPLES_PER_FRAME);
    new Uint8Array(samples.buffer).set(bytes);
    this.source.onData({ samples, sampleRate: INPUT_SAMPLE_RATE, bitsPerSample: 16, channelCount: INPUT_CHANNELS, numberOfFrames: INPUT_SAMPLES_PER_FRAME });
  }

  stop(): void {
    this.sink?.stop();
    this.sink = null;
    this.localTrack?.stop();
    this.localTrack = null;
    this.peer?.close();
    this.peer = null;
    this.source = null;
    this.ready = false;
    this.outputNormalizer.reset();
    this.lastRemoteAudioWarning = null;
    this.rejectRemoteDescription?.(new Error("WebRTC peer stopped."));
    this.remoteDescription = null;
    this.resolveRemoteDescription = null;
    this.rejectRemoteDescription = null;
  }

  private attachRemoteAudio(track: wrtc.MediaStreamTrack): void {
    if (track.kind !== "audio") return;
    this.sink?.stop();
    this.sink = new wrtc.nonstandard.RTCAudioSink(track);
    this.sink.ondata = (frame: Pcm16AudioFrame) => {
      if (!this.ready) return;
      try {
        for (const samples of this.outputNormalizer.normalize(frame)) {
          const data = Buffer.from(samples.buffer, samples.byteOffset, samples.byteLength).toString("base64");
          this.onEvent({
            type: "audio.delta",
            audio: {
              data,
              sampleRate: NORMALIZED_SAMPLE_RATE,
              numChannels: NORMALIZED_CHANNELS,
              samplesPerChannel: NORMALIZED_SAMPLES_PER_FRAME,
            },
          });
        }
        this.lastRemoteAudioWarning = null;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (message !== this.lastRemoteAudioWarning) {
          console.warn(`Dropped invalid Codex Voice audio frame: ${message}`);
          this.lastRemoteAudioWarning = message;
        }
      }
    };
  }

  private async setRemoteDescription(sdp: string): Promise<void> {
    try {
      if (!this.peer) throw new Error("WebRTC peer is no longer running.");
      await this.peer.setRemoteDescription({ type: "answer", sdp });
      this.resolveRemoteDescription?.();
      this.resolveRemoteDescription = null;
      this.rejectRemoteDescription = null;
    } catch (error) {
      const failure = error instanceof Error ? error : new Error(String(error));
      this.rejectRemoteDescription?.(failure);
      this.onEvent({ type: "session.error", message: "Unable to apply Codex Voice WebRTC answer.", recoverable: true });
    }
  }

  private async waitForIceGathering(): Promise<void> {
    if (this.peer?.iceGatheringState === "complete") return;
    await new Promise<void>(resolve => {
      let timer: NodeJS.Timeout;
      let check: () => void;
      const done = () => {
        clearTimeout(timer);
        this.peer?.removeEventListener("icegatheringstatechange", check);
        resolve();
      };
      check = () => { if (this.peer?.iceGatheringState === "complete") done(); };
      timer = setTimeout(done, 8_000);
      this.peer?.addEventListener("icegatheringstatechange", check);
    });
  }

  private async waitForConnection(): Promise<void> {
    if (this.peer?.connectionState === "connected") return;
    await new Promise<void>((resolve, reject) => {
      let timer: NodeJS.Timeout;
      let check: () => void;
      const done = (error?: Error) => {
        clearTimeout(timer);
        this.peer?.removeEventListener("connectionstatechange", check);
        error ? reject(error) : resolve();
      };
      check = () => {
        const state = this.peer?.connectionState;
        if (state === "connected") done();
        if (state === "failed" || state === "closed") done(new Error("Codex Voice WebRTC did not connect."));
      };
      timer = setTimeout(done, CONNECTION_TIMEOUT_MS, new Error("Timed out connecting Codex Voice WebRTC."));
      this.peer?.addEventListener("connectionstatechange", check);
      check();
    });
  }
}
