import { spawn } from "node:child_process";
import readline from "node:readline";
import wrtc from "@roamhq/wrtc";
import { loadConfig, codexEnvironment } from "../dist/config.js";

const config = loadConfig();
const peer = new wrtc.RTCPeerConnection();
const source = new wrtc.nonstandard.RTCAudioSource();
const localTrack = source.createTrack();
peer.addTrack(localTrack);
peer.createDataChannel("oai-events");

let remoteAudioTracks = 0;
peer.ontrack = event => {
  if (event.track.kind !== "audio") return;
  remoteAudioTracks += 1;
  const sink = new wrtc.nonstandard.RTCAudioSink(event.track);
  sink.ondata = frame => {
    console.log(JSON.stringify({ event: "remote-audio", sampleRate: frame.sampleRate, channelCount: frame.channelCount, frames: frame.numberOfFrames }));
    sink.stop();
  };
};
peer.onconnectionstatechange = () => console.log(JSON.stringify({ event: "connection-state", state: peer.connectionState }));

async function waitForIceComplete() {
  if (peer.iceGatheringState === "complete") return;
  await new Promise(resolve => {
    const timer = setTimeout(done, 8_000);
    function done() {
      clearTimeout(timer);
      peer.removeEventListener("icegatheringstatechange", check);
      resolve();
    }
    function check() { if (peer.iceGatheringState === "complete") done(); }
    peer.addEventListener("icegatheringstatechange", check);
  });
}

await peer.setLocalDescription(await peer.createOffer({ offerToReceiveAudio: true }));
await waitForIceComplete();
if (!peer.localDescription?.sdp) throw new Error("Failed to create a local WebRTC SDP offer.");
console.log(JSON.stringify({ event: "local-candidates", candidates: candidateSummary(peer.localDescription.sdp) }));

const child = spawn(config.codexBin, ["app-server", "--enable", "realtime_conversation"], {
  env: codexEnvironment(config),
  stdio: ["pipe", "pipe", "pipe"],
});
let nextId = 1;
const pending = new Map();
let resolveSdp;
const answerSdp = new Promise((resolve, reject) => {
  resolveSdp = resolve;
  setTimeout(() => reject(new Error("Timed out waiting for WebRTC SDP answer.")), 60_000);
});

function write(message) { child.stdin.write(`${JSON.stringify(message)}\n`); }
function request(method, params, timeoutMs = 30_000) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out waiting for ${method}`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    write({ id, method, params });
  });
}
function idFrom(result) { return typeof result.threadId === "string" ? result.threadId : result.thread?.id; }
function candidateSummary(sdp) {
  return sdp.split(/\r?\n/)
    .filter(line => line.startsWith("a=candidate:"))
    .map(line => {
      const fields = line.slice(12).split(/\s+/);
      return { protocol: fields[2]?.toLowerCase(), port: Number(fields[5]), type: fields[7] };
    });
}
function tcpOnlyCandidates(sdp) {
  return sdp.split(/\r?\n/)
    .filter(line => !line.startsWith("a=candidate:") || /\sTCP\s/i.test(line))
    .join("\r\n");
}
async function selectedPairSummary() {
  const stats = await peer.getStats();
  const transport = [...stats.values()].find(report => report.type === "transport" && report.selectedCandidatePairId);
  const pair = transport ? stats.get(transport.selectedCandidatePairId) : [...stats.values()].find(report => report.type === "candidate-pair" && report.nominated && report.state === "succeeded");
  const local = pair ? stats.get(pair.localCandidateId) : null;
  const remote = pair ? stats.get(pair.remoteCandidateId) : null;
  return pair ? {
    state: pair.state,
    local: local ? { protocol: local.protocol, port: local.port, type: local.candidateType } : null,
    remote: remote ? { protocol: remote.protocol, port: remote.port, type: remote.candidateType } : null,
  } : null;
}
readline.createInterface({ input: child.stdout }).on("line", line => {
  let message;
  try { message = JSON.parse(line); } catch { return; }
  if (typeof message.id === "number" && pending.has(message.id)) {
    const entry = pending.get(message.id);
    clearTimeout(entry.timer);
    pending.delete(message.id);
    message.error ? entry.reject(new Error(`${entry.method}: ${JSON.stringify(message.error)}`)) : entry.resolve(message.result ?? {});
  } else if (message.method === "thread/realtime/sdp" && typeof message.params?.sdp === "string") {
    resolveSdp(message.params.sdp);
  } else if (message.method === "thread/realtime/error") {
    console.error(JSON.stringify({ event: message.method, message: message.params?.message }));
  }
});
readline.createInterface({ input: child.stderr }).on("line", line => {
  if (/error|failed/i.test(line)) console.error(`[app-server] ${line.slice(0, 500)}`);
});

try {
  await request("initialize", { clientInfo: { name: "voice_gateway_webrtc_spike", title: "Voice Gateway WebRTC Spike", version: "0.1.0" }, capabilities: { experimentalApi: true } });
  write({ method: "initialized", params: {} });
  const started = await request("thread/start", { cwd: config.projectDir, approvalPolicy: "on-request", sandbox: "read-only", serviceName: "codex_voice_gateway_webrtc_spike" });
  const threadId = idFrom(started);
  if (typeof threadId !== "string") throw new Error("App Server did not return a thread id.");
  const offerSdp = process.env.FORCE_TCP === "1" ? tcpOnlyCandidates(peer.localDescription.sdp) : peer.localDescription.sdp;
  await request("thread/realtime/start", {
    threadId,
    version: "v3",
    outputModality: "audio",
    transport: { type: "webrtc", sdp: offerSdp },
    includeStartupContext: true,
    clientManagedHandoffs: false,
    codexResponseHandoffMode: "bemTags",
  }, 65_000);
  const remoteSdp = await answerSdp;
  console.log(JSON.stringify({ event: "remote-candidates", candidates: candidateSummary(remoteSdp) }));
  await peer.setRemoteDescription({ type: "answer", sdp: process.env.FORCE_TCP === "1" ? tcpOnlyCandidates(remoteSdp) : remoteSdp });
  console.log(JSON.stringify({ started: true, threadId }));
  const silence = new Int16Array(480); // 10 ms at 48 kHz, required by libwebrtc's audio source.
  const timer = setInterval(() => source.onData({ samples: silence, sampleRate: 48_000, bitsPerSample: 16, channelCount: 1, numberOfFrames: 480 }), 10);
  await new Promise(resolve => setTimeout(resolve, Number(process.env.HOLD_MS ?? 10_000)));
  clearInterval(timer);
  console.log(JSON.stringify({ completed: true, connectionState: peer.connectionState, remoteAudioTracks, selectedPair: await selectedPairSummary() }));
} finally {
  localTrack.stop();
  peer.close();
  child.kill();
}
