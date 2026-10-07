type EventMessage = {
  type: string;
  [key: string]: unknown;
};

const status = document.querySelector<HTMLDivElement>("#status")!;
const transcript = document.querySelector<HTMLDivElement>("#transcript")!;
const approvals = document.querySelector<HTMLElement>("#approvals")!;
const startButton = document.querySelector<HTMLButtonElement>("#start")!;
const muteButton = document.querySelector<HTMLButtonElement>("#mute")!;
const interruptButton = document.querySelector<HTMLButtonElement>("#interrupt")!;
const stopButton = document.querySelector<HTMLButtonElement>("#stop")!;
const textInput = document.querySelector<HTMLInputElement>("#text-input")!;
const textForm = document.querySelector<HTMLFormElement>("#text-form")!;

let sessionId = sessionStorage.getItem("voice.session.id");
let sessionSecret = sessionStorage.getItem("voice.session.secret");
let socket: WebSocket | null = null;
let peer: RTCPeerConnection | null = null;
let stream: MediaStream | null = null;
let audio: HTMLAudioElement | null = null;
let muted = false;
let pendingOfferSdp: string | null = null;
const approvalNodes = new Map<string, HTMLElement>();

function apiHeaders(): HeadersInit {
  // The ticket endpoint has no request body. Declaring JSON for an empty POST
  // makes Fastify correctly reject it as malformed JSON.
  return { "X-Voice-Session-Token": sessionSecret ?? "" };
}

async function ensureSession(): Promise<void> {
  if (sessionId && sessionSecret) return;
  const response = await fetch("/api/sessions", { method: "POST" });
  if (!response.ok) throw new Error("无法创建语音会话。");
  const data = await response.json() as { session: { id: string }; secret: string };
  sessionId = data.session.id;
  sessionSecret = data.secret;
  sessionStorage.setItem("voice.session.id", sessionId);
  sessionStorage.setItem("voice.session.secret", sessionSecret);
}

function setStatus(value: string): void {
  status.textContent = value;
}

function setConnected(connected: boolean): void {
  startButton.disabled = connected;
  muteButton.disabled = !connected;
  interruptButton.disabled = !connected;
  stopButton.disabled = !connected;
  textInput.disabled = !connected;
  document.querySelector<HTMLButtonElement>("#send")!.disabled = !connected;
}

function addMessage(text: string, kind: "user" | "assistant" | "meta"): HTMLElement {
  const empty = transcript.querySelector(".empty-state");
  empty?.remove();
  const node = document.createElement("p");
  node.className = `message ${kind}`;
  node.textContent = text;
  transcript.append(node);
  node.scrollIntoView({ block: "end" });
  return node;
}

function send(message: object): void {
  if (socket?.readyState !== WebSocket.OPEN) throw new Error("语音连接尚未建立。");
  socket.send(JSON.stringify(message));
}

async function waitForIceGathering(candidate: RTCPeerConnection): Promise<void> {
  if (candidate.iceGatheringState === "complete") return;
  await new Promise<void>(resolve => {
    const timer = window.setTimeout(done, 8_000);
    function done() {
      window.clearTimeout(timer);
      candidate.removeEventListener("icegatheringstatechange", check);
      resolve();
    }
    function check() { if (candidate.iceGatheringState === "complete") done(); }
    candidate.addEventListener("icegatheringstatechange", check);
  });
}

function wsUrl(): string {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}/api/sessions/${encodeURIComponent(sessionId!)}/live`;
}

async function start(): Promise<void> {
  await ensureSession();
  setStatus("正在连接");
  stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  peer = new RTCPeerConnection();
  for (const track of stream.getAudioTracks()) peer.addTrack(track, stream);
  peer.createDataChannel("oai-events");
  audio = new Audio();
  audio.autoplay = true;
  peer.addEventListener("track", event => {
    if (!audio || !event.streams[0]) return;
    audio.srcObject = event.streams[0];
    void audio.play().catch(() => addMessage("浏览器阻止了音频播放，请再次点击开始。", "meta"));
  });
  peer.addEventListener("connectionstatechange", () => {
    if (peer?.connectionState === "connected") setStatus("正在聆听");
    if (["failed", "closed"].includes(peer?.connectionState ?? "")) setStatus("连接已断开");
  });
  const offer = await peer.createOffer({ offerToReceiveAudio: true });
  await peer.setLocalDescription(offer);
  await waitForIceGathering(peer);
  if (!peer.localDescription?.sdp) throw new Error("浏览器未生成 WebRTC offer。");

  const ticketResponse = await fetch(`/api/sessions/${encodeURIComponent(sessionId!)}/ticket`, { method: "POST", headers: apiHeaders() });
  if (!ticketResponse.ok) throw new Error("无法获取语音连接凭证。");
  const { ticket } = await ticketResponse.json() as { ticket: string };
  socket = new WebSocket(wsUrl());
  socket.addEventListener("open", () => send({ type: "authenticate", ticket }));
  socket.addEventListener("message", event => handleEvent(JSON.parse(String(event.data)) as EventMessage));
  socket.addEventListener("close", () => { if (socket) cleanup("连接已结束"); });
  socket.addEventListener("error", () => addMessage("语音连接出现错误。", "meta"));
  pendingOfferSdp = peer.localDescription.sdp;
}

function handleEvent(event: EventMessage): void {
  switch (event.type) {
    case "ready":
      if (pendingOfferSdp) {
        send({ type: "start", sdp: pendingOfferSdp });
        pendingOfferSdp = null;
      }
      return;
    case "session.sdp":
      if (peer && typeof event.sdp === "string") void peer.setRemoteDescription({ type: "answer", sdp: event.sdp });
      return;
    case "session.started":
      setConnected(true);
      setStatus("正在聆听");
      return;
    case "transcript.done":
      if (typeof event.text === "string" && event.text.trim()) addMessage(event.text, event.role === "user" ? "user" : "assistant");
      return;
    case "message.final":
      if (typeof event.text === "string" && event.text.trim()) addMessage(event.text, "assistant");
      return;
    case "manager.status":
      setStatus(event.status === "thinking" ? "正在思考" : event.status === "working" ? "正在执行" : "正在聆听");
      return;
    case "approval.requested":
      return renderApproval(event.approval as { id: string; kind: string; summary: string; expiresAt: number });
    case "approval.resolved":
      return removeApproval(String(event.approvalId));
    case "session.error":
      if (typeof event.message === "string") addMessage(event.message, "meta");
      return;
    case "session.closed":
      cleanup("会话已结束");
  }
}

function renderApproval(approval: { id: string; kind: string; summary: string; expiresAt: number }): void {
  approvals.hidden = false;
  const node = document.createElement("article");
  node.className = "approval";
  const title = document.createElement("h2");
  title.textContent = "需要确认";
  const description = document.createElement("p");
  description.textContent = approval.summary;
  const actions = document.createElement("div");
  actions.className = "approval-actions";
  for (const [label, decision] of [["确认", "accept"], ["拒绝", "decline"]] as const) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", () => send({ type: "approval.decision", approvalId: approval.id, decision }));
    actions.append(button);
  }
  node.append(title, description, actions);
  approvals.append(node);
  approvalNodes.set(approval.id, node);
}

function removeApproval(id: string): void {
  approvalNodes.get(id)?.remove();
  approvalNodes.delete(id);
  approvals.hidden = approvalNodes.size === 0;
}

function cleanup(message: string): void {
  socket = null;
  peer?.close();
  peer = null;
  stream?.getTracks().forEach(track => track.stop());
  stream = null;
  audio?.pause();
  audio = null;
  pendingOfferSdp = null;
  setConnected(false);
  setStatus(message);
}

startButton.addEventListener("click", () => void start().catch(error => { cleanup("连接失败"); addMessage(error instanceof Error ? error.message : String(error), "meta"); }));
muteButton.addEventListener("click", () => {
  muted = !muted;
  stream?.getAudioTracks().forEach(track => { track.enabled = !muted; });
  muteButton.textContent = muted ? "取消静音" : "静音";
});
interruptButton.addEventListener("click", () => send({ type: "interrupt" }));
stopButton.addEventListener("click", () => send({ type: "stop" }));
textForm.addEventListener("submit", event => {
  event.preventDefault();
  const text = textInput.value.trim();
  if (!text) return;
  send({ type: "text", text });
  addMessage(text, "user");
  textInput.value = "";
});
