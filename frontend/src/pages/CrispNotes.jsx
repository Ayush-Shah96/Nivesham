import React, { useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  Bot,
  Check,
  FileAudio,
  Video,
  History,
  FileText,
  Clock,
  RefreshCw,
  Download,
  LoaderCircle,
  Mic,
  Paperclip,
  Pencil,
  Save,
  Send,
  Sparkles,
  Square,
  UploadCloud,
  X,
} from "lucide-react";
import { api } from "../lib/api";
import {
  clearRecorderError,
  clearRecording,
  getRecorderState,
  isRecorderSupported,
  startRecording,
  stopRecording,
  subscribeRecorder,
} from "../lib/crispRecorder";

const ACCEPT = [
  "audio/*",
  "video/*",
  ".mp3", ".mp2", ".mpga", ".m4a", ".wav", ".wave", ".webm",
  ".ogg", ".oga", ".opus", ".flac", ".aac", ".amr", ".aif", ".aiff",
  ".caf", ".wma", ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".mpeg",
  ".mpg", ".wmv", ".3gp",
].join(",");

function formatDuration(milliseconds) {
  const total = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const pad = (value) => String(value).padStart(2, "0");
  return hours > 0
    ? `${hours}:${pad(minutes)}:${pad(seconds)}`
    : `${pad(minutes)}:${pad(seconds)}`;
}

function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function defaultTitle(file) {
  return String(file?.name || "Recording").replace(/\.[^.]+$/, "").slice(0, 255);
}

function displayTranscript(note) {
  const utterances = Array.isArray(note?.diarization) ? note.diarization : [];
  const lines = utterances
    .filter((item) => String(item?.transcript || "").trim())
    .map((item) => {
      const seconds = Math.max(0, Math.floor(Number(item.start) || 0));
      const hours = Math.floor(seconds / 3600);
      const minutes = Math.floor((seconds % 3600) / 60);
      const remainder = seconds % 60;
      const pad = (value) => String(value).padStart(2, "0");
      const timestamp = hours > 0
        ? `${pad(hours)}:${pad(minutes)}:${pad(remainder)}`
        : `${pad(minutes)}:${pad(remainder)}`;
      const speaker = item.speaker === null || item.speaker === undefined ? "" : `Speaker ${item.speaker}: `;
      return `[${timestamp}] ${speaker}${String(item.transcript).trim()}`;
    });
  return lines.length ? lines.join("\n\n") : String(note?.transcript || "No transcript is available.");
}

function MessageBubble({ message }) {
  const isUser = message.role === "user";
  return (
    <div className={`flex items-start gap-3 ${isUser ? "justify-end" : "justify-start"}`}>
      {!isUser && (
        <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-slate-900 text-white">
          <Bot size={17} />
        </div>
      )}
      <div
        className={`max-w-[88%] rounded-2xl px-4 py-3 text-sm leading-7 shadow-sm ${
          isUser
            ? "rounded-br-md bg-slate-900 text-white"
            : "rounded-tl-md border border-slate-200 bg-white text-slate-700"
        }`}
      >
        <div className="whitespace-pre-wrap break-words">{message.content}</div>
      </div>
      {isUser && (
        <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-blue-100 text-blue-800">
          <span className="text-xs font-bold">You</span>
        </div>
      )}
    </div>
  );
}

export default function CrispNotes() {
  const [mode, setMode] = useState("upload");
  const [uploadFile, setUploadFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [uploadProgress, setUploadProgress] = useState(null);
  const [now, setNow] = useState(Date.now());
  const [note, setNote] = useState(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [question, setQuestion] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [chatError, setChatError] = useState("");
  const [transcriptOpen, setTranscriptOpen] = useState(false);
  const [summaryRetryBusy, setSummaryRetryBusy] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [activeTab, setActiveTab] = useState("summary");
  const [history, setHistory] = useState([]);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [titleDraft, setTitleDraft] = useState("");
  const [editingHistoryId, setEditingHistoryId] = useState(null);
  const [historyTitleDraft, setHistoryTitleDraft] = useState("");
  const [renameBusy, setRenameBusy] = useState(false);

  const fileInput = useRef(null);
  const messageEnd = useRef(null);
  const questionInput = useRef(null);
  const recording = useSyncExternalStore(subscribeRecorder, getRecorderState, getRecorderState);
  const recordingActive = recording.status === "recording";
  const recorderSupported = isRecorderSupported();
  const recordingElapsed = recordingActive && recording.startedAt ? now - recording.startedAt : 0;
  const selectedFile = mode === "record" ? recording.pendingFile : uploadFile;

  useEffect(() => {
    if (selectedFile) setTitleDraft(defaultTitle(selectedFile));
  }, [selectedFile]);

  useEffect(() => {
    if (!busy && !recordingActive) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [busy, recordingActive]);

  useEffect(() => {
    if (recording.error) {
      setError(recording.error);
      clearRecorderError();
    }
  }, [recording.error]);

  useEffect(() => {
    messageEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, chatBusy]);

  useEffect(() => {
    if (!chatOpen) return undefined;
    const handleKeyDown = (event) => {
      if (event.key === "Escape") setChatOpen(false);
    };
    window.addEventListener("keydown", handleKeyDown);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.setTimeout(() => questionInput.current?.focus(), 80);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [chatOpen]);

  async function loadHistory() {
    setHistoryBusy(true);
    setHistoryError("");
    try {
      const rows = await api.notes();
      setHistory(Array.isArray(rows) ? rows : []);
    } catch (loadError) {
      setHistoryError(loadError?.message || "Could not load saved recordings.");
    } finally {
      setHistoryBusy(false);
    }
  }

  useEffect(() => { loadHistory(); }, []);

  async function openHistoryNote(noteId) {
    setHistoryBusy(true);
    setHistoryError("");
    try {
      const fullNote = await api.crispNote(noteId);
      setNote(fullNote);
      setActiveTab("summary");
      setTranscriptOpen(false);
      setMessages([{ role: "assistant", content: fullNote.notes || "Summary is not available for this recording yet. You can retry it or ask questions about the transcript." }]);
      setChatError("");
    } catch (loadError) {
      setHistoryError(loadError?.message || "Could not open this recording.");
    } finally {
      setHistoryBusy(false);
    }
  }

  function downloadText(text, filename) {
    const blob = new Blob([String(text || "")], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  }

  function selectMode(nextMode) {
    if (busy || recordingActive) return;
    setMode(nextMode);
    setError("");
    setStatus("");
  }

  function acceptFile(file) {
    if (!file || busy) return;
    setUploadFile(file);
    setTitleDraft(defaultTitle(file));
    setError("");
    setStatus("");
    setUploadProgress(null);
  }

  function onDrop(event) {
    event.preventDefault();
    setDragActive(false);
    const file = event.dataTransfer?.files?.[0];
    if (file) acceptFile(file);
  }

  async function handleRecordStart() {
    setError("");
    setStatus("");
    await startRecording();
  }

  async function handleSummarize() {
    if (!selectedFile || busy || recordingActive) return;
    setBusy(true);
    setError("");
    setStatus("Deepgram is transcribing your recording. Local Qwen will prepare the summary next…");
    setUploadProgress(null);

    try {
      const result = await api.uploadAudio(selectedFile, {
        title: titleDraft.trim() || defaultTitle(selectedFile),
        summarize: true,
        onProgress: (progress) => {
          if (progress.assembling) {
            setStatus("Upload complete. Preparing the recording for Deepgram…");
          } else {
            const percent = progress.totalBytes
              ? Math.round((progress.uploadedBytes / progress.totalBytes) * 100)
              : 0;
            setUploadProgress(percent);
            setStatus(`Uploading recording… ${percent}% (${progress.chunkIndex}/${progress.totalChunks})`);
          }
        },
      });

      const transcript = String(result?.transcript || "").trim();
      const summary = String(result?.notes || "").trim();
      if (!result?.id || !transcript) {
        throw new Error("The recording was uploaded, but no transcript was returned.");
      }

      setNote(result);
      setActiveTab("summary");
      setTranscriptOpen(false);
      loadHistory();
      setChatError("");
      setQuestion("");
      setMessages([
        {
          role: "assistant",
          content: summary
            ? summary
            : `Your transcript is ready, but Qwen could not create the summary. ${result.summary_error || "You can retry the summary or ask a question about the transcript."}`,
        },
      ]);
      setChatOpen(false);
      setStatus("");
      setUploadProgress(null);
      if (mode === "record") clearRecording();
      else {
        setUploadFile(null);
        if (fileInput.current) fileInput.current.value = "";
      }
    } catch (requestError) {
      setError(requestError?.message || "Unable to process this recording. Please try again.");
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  async function retrySummary(force = false) {
    if (!note?.id || summaryRetryBusy) return;
    setSummaryRetryBusy(true);
    setChatError("");
    try {
      const result = await api.summarizeCrispNote(note.id, { force });
      setNote((current) => ({ ...current, ...result }));
      setMessages((current) => [
        { role: "assistant", content: result.notes || "The summary is still unavailable. You can ask questions about the transcript." },
        ...current.slice(1),
      ]);
    } catch (requestError) {
      setChatError(requestError?.message || "Qwen could not create the summary. Check that Ollama is running.");
    } finally {
      setSummaryRetryBusy(false);
    }
  }

  async function saveHistoryRename(noteId) {
    if (!noteId || renameBusy) return;
    setRenameBusy(true);
    setHistoryError("");
    try {
      const updated = await api.renameCrispNote(noteId, historyTitleDraft);
      setHistory((current) => current.map((item) => item.id === noteId ? { ...item, ...updated } : item));
      setNote((current) => current?.id === noteId ? { ...current, title: updated.title } : current);
      setEditingHistoryId(null);
      setHistoryTitleDraft("");
    } catch (renameError) {
      setHistoryError(renameError?.message || "Could not rename this recording.");
    } finally {
      setRenameBusy(false);
    }
  }

  async function sendQuestion(event) {
    event?.preventDefault?.();
    const currentQuestion = question.trim();
    if (!currentQuestion || !note?.id || chatBusy) return;

    const previousMessages = messages.slice(-8).map(({ role, content }) => ({ role, content }));
    setMessages((current) => [...current, { role: "user", content: currentQuestion }]);
    setQuestion("");
    setChatBusy(true);
    setChatError("");
    try {
      const response = await api.crispChat(note.id, currentQuestion, previousMessages);
      setMessages((current) => [...current, {
        role: "assistant",
        content: String(response?.answer || "I couldn't find an answer in this recording."),
      }]);
    } catch (requestError) {
      setChatError(requestError?.message || "The local Qwen assistant could not answer this question.");
    } finally {
      setChatBusy(false);
      window.setTimeout(() => questionInput.current?.focus(), 0);
    }
  }

  function handleChatKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent?.isComposing) {
      event.preventDefault();
      sendQuestion(event);
    }
  }

  const summaryReady = Boolean(String(note?.notes || "").trim());
  const transcriptText = note ? displayTranscript(note) : "";

  return (
    <div className="min-h-screen bg-slate-50 px-4 py-6 sm:px-6 lg:py-8">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
        {/* Recording/upload workspace intentionally occupies roughly the upper half of the page. */}
        <section className="min-h-[430px] overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-xl shadow-slate-900/[0.04] lg:min-h-[48vh]">
          <div className="grid grid-cols-2 border-b border-slate-100 p-2">
            <button type="button" onClick={() => selectMode("record")} disabled={busy || recordingActive}
              className={`flex min-h-12 items-center justify-center gap-2 rounded-xl px-4 text-sm font-bold transition ${mode === "record" ? "bg-slate-900 text-white shadow-sm" : "text-slate-500 hover:bg-slate-50"}`}>
              <Mic size={17} /> Record audio
            </button>
            <button type="button" onClick={() => selectMode("upload")} disabled={busy || recordingActive}
              className={`flex min-h-12 items-center justify-center gap-2 rounded-xl px-4 text-sm font-bold transition ${mode === "upload" ? "bg-slate-900 text-white shadow-sm" : "text-slate-500 hover:bg-slate-50"}`}>
              <UploadCloud size={17} /> Upload audio / video
            </button>
          </div>
          <div className="flex min-h-[370px] flex-col items-center justify-center px-5 py-8 sm:px-10">
            {mode === "upload" ? (
              <div onDragEnter={(event) => { event.preventDefault(); setDragActive(true); }} onDragOver={(event) => event.preventDefault()}
                onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setDragActive(false); }} onDrop={onDrop}
                className={`flex min-h-64 w-full max-w-3xl flex-col items-center justify-center rounded-2xl border-2 border-dashed px-5 py-8 text-center transition ${dragActive ? "border-blue-500 bg-blue-50" : "border-slate-200 bg-slate-50/80 hover:border-slate-300"}`}>
                <input ref={fileInput} type="file" accept={ACCEPT} className="hidden" onChange={(event) => acceptFile(event.target.files?.[0] || null)} />
                {uploadFile ? <>
                  <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-blue-100 text-blue-700">{uploadFile.type?.startsWith("video/") ? <Video size={30} /> : <FileAudio size={30} />}</div>
                  <div className="max-w-full break-all text-base font-bold text-slate-900">{uploadFile.name}</div>
                  <div className="mt-1 text-sm text-slate-500">{formatSize(uploadFile.size)} · Ready to process</div>
                  <button type="button" onClick={() => { setUploadFile(null); if (fileInput.current) fileInput.current.value = ""; }} className="mt-4 inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-semibold text-slate-500 hover:bg-white"><X size={14} /> Remove file</button>
                </> : <>
                  <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-white text-slate-700 shadow-sm ring-1 ring-slate-200"><UploadCloud size={30} /></div>
                  <div className="text-lg font-bold text-slate-900">Drop your audio or video here</div>
                  <p className="mt-2 text-sm text-slate-500">or choose a file from your computer</p>
                  <button type="button" onClick={() => fileInput.current?.click()} className="mt-5 inline-flex min-h-11 items-center gap-2 rounded-xl border border-slate-300 bg-white px-5 py-2.5 text-sm font-bold text-slate-700 shadow-sm hover:bg-slate-50"><Paperclip size={16} /> Browse files</button>
                  <p className="mt-4 text-xs text-slate-400">Audio and video formats supported · Large files upload in chunks</p>
                </>}
              </div>
            ) : (
              <div className="flex min-h-64 w-full max-w-3xl flex-col items-center justify-center rounded-2xl border border-slate-200 bg-slate-50/80 px-5 py-8 text-center">
                {!recorderSupported ? <><Mic size={32} className="mb-3 text-amber-600" /><div className="font-bold text-slate-900">Microphone recording unavailable</div><p className="mt-2 text-sm text-slate-500">Allow microphone access or use Upload audio.</p></>
                  : recordingActive ? <><div className="mb-3 flex h-16 w-16 animate-pulse items-center justify-center rounded-full bg-rose-100 text-rose-600"><Mic size={30} /></div><div className="font-bold text-slate-900">Recording in progress</div><div className="mt-2 font-mono text-3xl font-bold tabular-nums">{formatDuration(recordingElapsed)}</div><button type="button" onClick={stopRecording} className="mt-5 inline-flex min-h-11 items-center gap-2 rounded-xl bg-rose-600 px-5 py-2.5 text-sm font-bold text-white hover:bg-rose-700"><Square size={15} fill="currentColor" /> Stop recording</button></>
                  : recording.pendingFile ? <><div className="mb-3 flex h-16 w-16 items-center justify-center rounded-2xl bg-emerald-100 text-emerald-700"><Check size={30} /></div><div className="font-bold text-slate-900">Recording ready</div><div className="mt-1 text-sm text-slate-500">{recording.pendingFile.name} · {formatSize(recording.pendingFile.size)}</div><button type="button" onClick={clearRecording} className="mt-4 text-xs font-semibold text-slate-500 hover:text-slate-900">Discard recording</button></>
                  : <><div className="mb-3 flex h-16 w-16 items-center justify-center rounded-2xl bg-white text-slate-700 shadow-sm ring-1 ring-slate-200"><Mic size={30} /></div><div className="text-lg font-bold text-slate-900">Record your audio</div><p className="mt-2 text-sm text-slate-500">Start recording and stop when finished.</p><button type="button" onClick={handleRecordStart} disabled={busy} className="mt-5 inline-flex min-h-11 items-center gap-2 rounded-xl bg-slate-900 px-5 py-2.5 text-sm font-bold text-white hover:bg-slate-800 disabled:opacity-60"><Mic size={16} /> Start recording</button></>}
              </div>
            )}
            {selectedFile && (
              <div className="mt-4 w-full max-w-3xl text-left">
                <label htmlFor="crisp-recording-title" className="mb-1.5 block text-xs font-bold text-slate-600">Name this recording for your history</label>
                <input id="crisp-recording-title" value={titleDraft} onChange={(event) => setTitleDraft(event.target.value)} maxLength={255} placeholder="e.g. Q3 earnings call – management discussion" className="min-h-11 w-full rounded-xl border border-slate-200 bg-white px-3.5 text-sm text-slate-800 outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-100" />
              </div>
            )}
            <button type="button" onClick={handleSummarize} disabled={!selectedFile || busy || recordingActive} className="mt-5 flex min-h-12 w-full max-w-3xl items-center justify-center gap-2 rounded-xl bg-blue-700 px-5 py-3 text-sm font-extrabold text-white shadow-sm hover:bg-blue-800 disabled:cursor-not-allowed disabled:bg-slate-300">
              {busy ? <LoaderCircle size={18} className="animate-spin" /> : <Sparkles size={18} />}{busy ? "Processing recording…" : "Transcribe & summarize"}
            </button>
            <div className="mt-3 flex flex-wrap justify-center gap-x-4 gap-y-1 text-[11px] text-slate-400"><span>Deepgram transcription</span><span>·</span><span>Local Qwen summary</span><span>·</span><span>Saved to your account</span></div>
            {(status || error) && <div className="mt-4 w-full max-w-3xl rounded-xl border border-slate-200 px-4 py-3 text-xs leading-5">{status && <div className="text-slate-600">{status}{uploadProgress !== null ? ` (${uploadProgress}%)` : ""}</div>}{error && <div className="mt-1 text-rose-700">{error}</div>}</div>}
          </div>
        </section>

        <section className="min-h-[360px] overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 sm:px-6">
            <div className="flex rounded-xl bg-slate-100 p-1">
              {[{id:"summary",label:"Summary",icon:Sparkles},{id:"transcript",label:"Transcript",icon:FileText},{id:"history",label:"History",icon:History}].map(({id,label,icon:Icon}) => <button key={id} type="button" onClick={() => setActiveTab(id)} className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-bold transition ${activeTab === id ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-900"}`}><Icon size={14}/>{label}</button>)}
            </div>
          </div>

          {activeTab === "history" ? <div className="p-4 sm:p-6">
            <div className="mb-4 flex items-center justify-between"><h3 className="text-sm font-bold text-slate-800">Saved recordings</h3><button type="button" onClick={loadHistory} disabled={historyBusy} className="inline-flex items-center gap-1.5 text-xs font-bold text-blue-700 disabled:opacity-50"><RefreshCw size={13} className={historyBusy ? "animate-spin" : ""}/> Refresh</button></div>
            {historyError && <p className="mb-3 rounded-lg bg-rose-50 p-3 text-xs text-rose-700">{historyError}</p>}
            {historyBusy ? <div className="py-12 text-center text-sm text-slate-500"><LoaderCircle className="mx-auto mb-2 animate-spin"/>Loading history…</div> : history.length ? <div className="divide-y divide-slate-100">{history.map((item) => <div key={item.id} className={`flex flex-wrap items-center justify-between gap-3 rounded-xl px-3 py-3 transition hover:bg-slate-50 ${note?.id === item.id ? "bg-blue-50" : ""}`}>
              <button type="button" onClick={() => openHistoryNote(item.id)} className="min-w-0 flex-1 text-left">
                <span className="block truncate text-sm font-bold text-slate-800">{item.title || `Recording #${item.id}`}</span>
                <span className="mt-1 flex items-center gap-1.5 text-xs text-slate-400"><Clock size={12}/>{item.created_at ? new Date(item.created_at).toLocaleString() : "Saved recording"}</span>
              </button>
              <div className="flex shrink-0 items-center gap-2">
                <span className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${item.summary_status === "ready" ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>{item.summary_status === "ready" ? "Summary ready" : "Transcript saved"}</span>
                <button type="button" title="Rename recording" aria-label={`Rename ${item.title || `recording ${item.id}`}`} onClick={() => { setEditingHistoryId(item.id); setHistoryTitleDraft(item.title || ""); }} className="flex h-8 w-8 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-500 hover:text-blue-700"><Pencil size={14}/></button>
              </div>
              {editingHistoryId === item.id && <form className="flex w-full gap-2" onSubmit={(event) => { event.preventDefault(); saveHistoryRename(item.id); }}>
                <input autoFocus maxLength={255} value={historyTitleDraft} onChange={(event) => setHistoryTitleDraft(event.target.value)} placeholder="Recording name" className="min-w-0 flex-1 rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-blue-500"/>
                <button type="submit" disabled={renameBusy} className="inline-flex items-center gap-1 rounded-lg bg-blue-700 px-3 py-2 text-xs font-bold text-white disabled:opacity-50"><Save size={13}/> Save</button>
                <button type="button" onClick={() => setEditingHistoryId(null)} className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-bold text-slate-600">Cancel</button>
              </form>}
            </div>)}</div> : <div className="py-12 text-center text-sm text-slate-500">No saved recordings yet. Your completed uploads will appear here.</div>}
          </div> : !note ? <div className="flex min-h-64 flex-col items-center justify-center px-6 text-center"><FileText size={30} className="mb-3 text-slate-300"/><h3 className="text-sm font-bold text-slate-700">Your results will appear here</h3><p className="mt-1 max-w-md text-xs leading-5 text-slate-400">Record or upload a file above. Once processed, your summary and full transcript will be available in these tabs.</p></div>
          : activeTab === "summary" ? <div className="p-5 sm:p-7">
            <div className="mb-5 flex flex-wrap items-center justify-between gap-3"><div><h3 className="text-base font-extrabold text-slate-900">{note.title || "Recording summary"}</h3><p className="mt-1 text-xs text-slate-500">{summaryReady ? "AI-generated investor notes" : "Transcript saved · Summary unavailable"}</p></div><div className="flex flex-wrap gap-2"><button type="button" onClick={() => retrySummary(summaryReady)} disabled={summaryRetryBusy} className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-bold text-blue-700 hover:bg-blue-50 disabled:opacity-50">{summaryRetryBusy ? "Generating…" : summaryReady ? "Regenerate summary" : "Retry summary"}</button><button type="button" onClick={() => setChatOpen(true)} className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-xs font-bold text-white hover:bg-slate-800"><Bot size={14}/> Ask AI</button><button type="button" onClick={() => downloadText(note.notes || "", `crisp-note-${note.id}-summary.txt`)} disabled={!summaryReady} className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-2 text-xs font-bold text-slate-600 disabled:opacity-40"><Download size={14}/> Download</button><button type="button" onClick={() => navigator.clipboard?.writeText(note.notes || "")} disabled={!summaryReady} className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-bold text-slate-600 disabled:opacity-40">Copy</button></div></div>
            {summaryReady ? <div className="whitespace-pre-wrap break-words text-sm leading-7 text-slate-700">{note.notes}</div> : <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">The transcript has been saved, but Qwen could not generate a summary. Use Retry summary when Ollama is available.</div>}
          </div> : <div className="p-5 sm:p-7"><div className="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h3 className="text-base font-extrabold text-slate-900">Full transcript</h3><p className="text-xs text-slate-500">{transcriptText.length.toLocaleString()} characters</p></div><div className="flex gap-2"><button type="button" onClick={() => navigator.clipboard?.writeText(transcriptText)} className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-bold text-slate-600">Copy</button><button type="button" onClick={() => downloadText(transcriptText, `crisp-note-${note.id}-transcript.txt`)} className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-2 text-xs font-bold text-slate-600"><Download size={14}/> Download</button></div></div><pre className="max-h-[65vh] overflow-auto whitespace-pre-wrap break-words rounded-xl bg-slate-50 p-4 text-xs leading-6 text-slate-700">{transcriptText}</pre></div>}
        </section>
      </div>

      {chatOpen && note && <div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-950/55 p-0 backdrop-blur-[2px] sm:p-5" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setChatOpen(false); }}>
        <section role="dialog" aria-modal="true" aria-labelledby="crisp-chat-title" className="flex h-full w-full max-w-3xl flex-col overflow-hidden bg-slate-50 shadow-2xl sm:h-[min(88vh,900px)] sm:rounded-3xl sm:border sm:border-white/70">
          <header className="flex shrink-0 items-center justify-between border-b border-slate-200 bg-white px-4 py-3.5 sm:px-6"><div className="flex min-w-0 items-center gap-3"><div className="flex h-10 w-10 items-center justify-center rounded-xl bg-slate-900 text-white"><Bot size={21}/></div><div className="min-w-0"><h2 id="crisp-chat-title" className="truncate text-sm font-extrabold text-slate-900">Ask about this recording</h2><p className="truncate text-[11px] text-slate-500">{note.title || "Your recording"} · Local Qwen assistant</p></div></div><button type="button" onClick={() => setChatOpen(false)} aria-label="Close chat" className="flex h-9 w-9 items-center justify-center rounded-xl text-slate-500 hover:bg-slate-100"><X size={19}/></button></header>
          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6"><div className="mx-auto flex max-w-2xl flex-col gap-5">{messages.map((message, index) => <MessageBubble key={`${message.role}-${index}`} message={message}/>)}{chatBusy && <div className="flex items-center gap-2 text-xs text-slate-500"><LoaderCircle size={14} className="animate-spin"/> Qwen is thinking…</div>}{chatError && <div className="rounded-xl bg-rose-50 p-3 text-xs text-rose-700">{chatError}</div>}<div ref={messageEnd}/></div></div>
          <form onSubmit={sendQuestion} className="shrink-0 border-t border-slate-200 bg-white px-3 py-3 sm:px-5"><div className="mx-auto flex max-w-2xl items-end gap-2 rounded-2xl border border-slate-200 bg-slate-50 p-2"><textarea ref={questionInput} value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={handleChatKeyDown} rows={1} maxLength={3000} placeholder="Ask anything about this recording…" className="max-h-32 min-h-10 flex-1 resize-y bg-transparent px-2 py-2 text-sm leading-6 text-slate-800 outline-none" disabled={chatBusy}/><button type="submit" disabled={!question.trim() || chatBusy} className="flex h-10 w-10 items-center justify-center rounded-xl bg-blue-700 text-white disabled:bg-slate-300">{chatBusy ? <LoaderCircle size={17} className="animate-spin"/> : <Send size={17}/>}</button></div><p className="mt-2 text-center text-[10px] text-slate-400">Answers are grounded in this recording's transcript.</p></form>
        </section>
      </div>}
    </div>
  );
}
