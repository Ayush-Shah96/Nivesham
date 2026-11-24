// Microphone recorder for Crisp Notes.
//
// The recorder lives in this module rather than in a React component on purpose:
//  - switching browser tabs or windows does not stop it (MediaRecorder keeps running
//    in background tabs; we never pause on visibility changes),
//  - moving between pages of the app does not unmount and lose it,
//  - there is no duration limit. Audio is flushed to memory every second so the
//    recording is assembled from many small chunks rather than one large buffer.
//
// Timing uses wall-clock timestamps so the displayed duration is correct after
// the tab has been in the background for a long time.

const listeners = new Set();
let session = null;
let state = {
  status: "idle", // idle | recording | stopped
  startedAt: null,
  pendingFile: null,
  error: "",
};

function emit() {
  for (const listener of listeners) listener();
}

function update(patch) {
  state = { ...state, ...patch };
  emit();
}

export function subscribeRecorder(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getRecorderState() {
  return state;
}

const MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/ogg;codecs=opus",
];

function pickMimeType() {
  if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) return "";
  return MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

function extensionFor(mimeType) {
  if (mimeType.includes("mp4")) return "m4a";
  if (mimeType.includes("ogg")) return "ogg";
  return "webm";
}

function timestampForFilename(date = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `_${pad(date.getHours())}-${pad(date.getMinutes())}-${pad(date.getSeconds())}`
  );
}

// ---------------------------------------------------------------------------
// Screen wake lock keeps phones from dimming while recording. It is released by
// the browser whenever the page is hidden, so it is requested again on return.
// Recording itself does not depend on it.
// ---------------------------------------------------------------------------

async function requestWakeLock() {
  try {
    if (session && !session.wakeLock && typeof navigator !== "undefined" && navigator.wakeLock) {
      session.wakeLock = await navigator.wakeLock.request("screen");
    }
  } catch {
    // Unsupported or denied; recording continues without it.
  }
}

function releaseWakeLock() {
  try {
    session?.wakeLock?.release?.();
  } catch {
    // ignore
  }
  if (session) session.wakeLock = null;
}

function handleVisibilityChange() {
  if (session && document.visibilityState === "visible") {
    requestWakeLock();
  }
}

function handleBeforeUnload(event) {
  if (session) {
    event.preventDefault();
    event.returnValue = "";
  }
}

function attachPageListeners() {
  document.addEventListener("visibilitychange", handleVisibilityChange);
  window.addEventListener("beforeunload", handleBeforeUnload);
}

function detachPageListeners() {
  document.removeEventListener("visibilitychange", handleVisibilityChange);
  window.removeEventListener("beforeunload", handleBeforeUnload);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function startRecording() {
  if (session) return;

  if (
    typeof navigator === "undefined" ||
    !navigator.mediaDevices?.getUserMedia ||
    typeof MediaRecorder === "undefined"
  ) {
    update({
      error: "This browser cannot record audio. Use the latest Chrome, Edge, Firefox or Safari.",
    });
    return;
  }

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  } catch (err) {
    const blocked = err?.name === "NotAllowedError" || err?.name === "SecurityError";
    update({
      error: blocked
        ? "Microphone access was blocked. Allow microphone access for this site and try again."
        : `Could not open the microphone${err?.message ? `: ${err.message}` : "."}`,
    });
    return;
  }

  const mimeType = pickMimeType();
  let recorder;
  try {
    recorder = new MediaRecorder(stream, {
      ...(mimeType ? { mimeType } : {}),
      audioBitsPerSecond: 48000,
    });
  } catch {
    stream.getTracks().forEach((track) => track.stop());
    update({ error: "This browser could not start recording from the microphone." });
    return;
  }

  const chunks = [];
  recorder.addEventListener("dataavailable", (event) => {
    if (event.data && event.data.size > 0) chunks.push(event.data);
  });

  session = {
    recorder,
    stream,
    chunks,
    mimeType: (recorder.mimeType || mimeType || "audio/webm").split(";")[0],
    wakeLock: null,
    endedByDevice: false,
  };

  // If the microphone is unplugged or revoked mid-recording, keep what was captured.
  stream.getAudioTracks().forEach((track) => {
    track.addEventListener("ended", () => {
      if (session && session.recorder === recorder) {
        session.endedByDevice = true;
        stopRecording();
      }
    });
  });

  // Timeslice of 1s: audio is delivered continuously, so nothing is lost if the
  // browser is slow to produce a final chunk, and the recording has no length cap.
  recorder.start(1000);
  attachPageListeners();
  requestWakeLock();

  update({ status: "recording", startedAt: Date.now(), pendingFile: null, error: "" });
}

export function stopRecording() {
  const current = session;
  if (!current) return Promise.resolve(null);

  return new Promise((resolve) => {
    const { recorder, stream, chunks, mimeType, endedByDevice } = current;

    const finish = () => {
      releaseWakeLock();
      session = null;
      detachPageListeners();
      stream.getTracks().forEach((track) => track.stop());

      const blob = new Blob(chunks, { type: mimeType });
      if (blob.size === 0) {
        update({
          status: "idle",
          startedAt: null,
          pendingFile: null,
          error: "No audio was captured. Check the microphone and try again.",
        });
        resolve(null);
        return;
      }

      const file = new File(
        [blob],
        `call-recording_${timestampForFilename()}.${extensionFor(mimeType)}`,
        { type: mimeType }
      );

      update({
        status: "stopped",
        startedAt: null,
        pendingFile: file,
        error: endedByDevice
          ? "The microphone was disconnected. Everything recorded up to that point has been kept."
          : "",
      });
      resolve(file);
    };

    if (recorder.state === "inactive") {
      finish();
    } else {
      recorder.addEventListener("stop", finish, { once: true });
      recorder.stop();
    }
  });
}

// Drop the recorded audio without saving it. Also used after a successful transcription.
export function clearRecording() {
  if (session) {
    // Recording in progress: discard it entirely.
    const current = session;
    session = null;
    releaseWakeLock();
    detachPageListeners();
    try {
      current.recorder.stop();
    } catch {
      // ignore
    }
    current.stream.getTracks().forEach((track) => track.stop());
  }
  update({ status: "idle", startedAt: null, pendingFile: null, error: "" });
}

export function clearRecorderError() {
  if (state.error) update({ error: "" });
}

export function isRecorderSupported() {
  return (
    typeof navigator !== "undefined" &&
    Boolean(navigator.mediaDevices?.getUserMedia) &&
    typeof MediaRecorder !== "undefined"
  );
}