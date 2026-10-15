// Open Transfer — direct browser-to-browser transfers (WebRTC data channels).
//
// Browsers can't accept connections, but two of them can open a WebRTC data
// channel once they've swapped an offer and an answer. The apps carry those two
// messages (/api/signal → the right app → the other page); the file itself then
// goes straight from one browser to the other over the local network. No STUN
// or TURN servers: on a LAN the devices reach each other directly, and if they
// can't, the caller falls back to sending through the app.

import { api } from "./lib.js";

/** Receivers keep direct files in memory until saved, so cap the size. */
export const DIRECT_MAX_BYTES = 1_000_000_000;
const CHUNK = 64 * 1024;
const HIGH_WATER = 4 * 1024 * 1024;
const LOW_WATER = 1024 * 1024;
const CONNECT_TIMEOUT = 12_000;
const ACK_TIMEOUT = 60_000;

export const directSupported = () => typeof window.RTCPeerConnection === "function";

const answers = new Map(); // "job:target" -> resolve(answer)
const receivers = new Map(); // session id -> { pc }

function gathered(pc, limit = 2500) {
  // Wait until every local address is in the description (no trickle ICE).
  return new Promise((resolve) => {
    if (pc.iceGatheringState === "complete") return resolve();
    const done = () => {
      if (pc.iceGatheringState === "complete") resolve();
    };
    pc.addEventListener("icegatheringstatechange", done);
    setTimeout(resolve, limit);
  });
}

function withTimeout(promise, ms, what) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${what} timed out`)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

/** Route connection messages that arrived for this page. */
export function handleSignals(signals, ctx) {
  for (const s of signals) {
    if (s.job && s.kind === "answer") {
      answers.get(`${s.job}:${s.target}`)?.(s);
    } else if (s.session && s.kind === "offer") {
      receive(s, ctx).catch(() => {
        /* the sender will time out and fall back */
      });
    } else if (s.session && s.kind === "bye") {
      receivers.get(s.session)?.pc.close();
    }
  }
}

// ------------------------------------------------------------------ sending

/**
 * Send ``files`` straight to ``target``'s browser. Resolves true when every
 * file was received, false if a direct connection wasn't possible or broke
 * (the caller then sends through the app instead).
 */
export async function sendDirect({ job, target, files, onProgress, signal }) {
  const pc = new RTCPeerConnection({ iceServers: [] });
  const key = `${job}:${target}`;
  signal?.addEventListener("abort", () => pc.close(), { once: true });
  try {
    const channel = pc.createDataChannel("open-transfer", { ordered: true });
    channel.binaryType = "arraybuffer";
    channel.bufferedAmountLowThreshold = LOW_WATER;
    const acks = new Map();
    let closed = false;
    channel.addEventListener("message", (event) => {
      if (typeof event.data !== "string") return;
      const msg = JSON.parse(event.data);
      if (msg.t === "done") acks.get(msg.i)?.();
    });
    channel.addEventListener("close", () => {
      closed = true;
      for (const ack of acks.values()) ack(new Error("closed"));
    });

    await pc.setLocalDescription(await pc.createOffer());
    await gathered(pc);
    const answered = new Promise((resolve) => answers.set(key, resolve));
    await api("/api/signal", { method: "POST", json: { job, target, kind: "offer", sdp: JSON.stringify(pc.localDescription) } });
    const answer = await withTimeout(answered, CONNECT_TIMEOUT, "answer");
    await pc.setRemoteDescription(JSON.parse(answer.sdp));
    await withTimeout(
      new Promise((resolve, reject) => {
        if (channel.readyState === "open") return resolve();
        channel.addEventListener("open", resolve, { once: true });
        channel.addEventListener("error", () => reject(new Error("channel error")), { once: true });
      }),
      CONNECT_TIMEOUT,
      "connection",
    );

    let sent = 0;
    for (const [index, file] of files.entries()) {
      const acked = new Promise((resolve, reject) => acks.set(index, (err) => (err ? reject(err) : resolve())));
      channel.send(JSON.stringify({ t: "file", i: index, name: file.name, size: file.size, mime: file.type || "application/octet-stream" }));
      for (let offset = 0; offset < file.size; offset += CHUNK) {
        if (closed) throw new Error("closed");
        if (channel.bufferedAmount > HIGH_WATER) {
          await new Promise((resolve) => channel.addEventListener("bufferedamountlow", resolve, { once: true }));
        }
        const chunk = await file.slice(offset, offset + CHUNK).arrayBuffer();
        channel.send(chunk);
        sent += chunk.byteLength;
        onProgress?.(sent);
      }
      channel.send(JSON.stringify({ t: "end", i: index }));
      await withTimeout(acked, ACK_TIMEOUT, "receipt");
    }
    channel.send(JSON.stringify({ t: "bye" }));
    setTimeout(() => pc.close(), 1000);
    return true;
  } catch {
    pc.close();
    return false;
  } finally {
    answers.delete(key);
  }
}

// ---------------------------------------------------------------- receiving

async function receive(offer, ctx) {
  const session = ctx.session(offer.session);
  if (!session || !["accepted", "receiving"].includes(session.state)) return;
  receivers.get(offer.session)?.pc.close();
  const pc = new RTCPeerConnection({ iceServers: [] });
  receivers.set(offer.session, { pc });
  pc.addEventListener("datachannel", (event) => attach(event.channel, offer.session, pc, ctx));
  await pc.setRemoteDescription(JSON.parse(offer.sdp));
  await pc.setLocalDescription(await pc.createAnswer());
  await gathered(pc);
  await api("/api/signal", { method: "POST", json: { session: offer.session, kind: "answer", sdp: JSON.stringify(pc.localDescription) } });
}

function attach(channel, sessionId, pc, ctx) {
  channel.binaryType = "arraybuffer";
  let current = null;
  let lastReport = 0;
  const report = (index, received, done = false) =>
    api(`/api/incoming/${encodeURIComponent(sessionId)}/direct`, { method: "POST", json: { index, received, done } }).catch(() => {
      /* progress is best effort; completion is retried below */
    });

  channel.addEventListener("message", async (event) => {
    if (typeof event.data !== "string") {
      if (!current) return;
      current.chunks.push(event.data);
      current.received += event.data.byteLength;
      ctx.progress?.(sessionId, current.index, current.received);
      const now = Date.now();
      if (now - lastReport > 1000) {
        lastReport = now;
        report(current.index, current.received);
      }
      return;
    }
    const msg = JSON.parse(event.data);
    if (msg.t === "file") {
      current = { index: msg.i, name: msg.name, size: msg.size, mime: msg.mime, chunks: [], received: 0 };
    } else if (msg.t === "end" && current && current.index === msg.i) {
      const file = current;
      current = null;
      if (file.received !== file.size) {
        channel.close();
        return;
      }
      const blob = new Blob(file.chunks, { type: file.mime });
      ctx.file({ session: sessionId, index: file.index, name: file.name, size: file.size, mime: file.mime, blob });
      await report(file.index, file.size, true);
      channel.send(JSON.stringify({ t: "done", i: file.index }));
    } else if (msg.t === "bye") {
      setTimeout(() => pc.close(), 500);
      receivers.delete(sessionId);
    }
  });
  channel.addEventListener("close", () => receivers.delete(sessionId));
}
