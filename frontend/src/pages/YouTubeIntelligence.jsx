import React, { useEffect, useMemo, useState } from "react";
import { Clock3, Copy, Download, FileText, LoaderCircle, Play, Sparkles, Youtube, X, Check, ExternalLink } from "lucide-react";
import { request } from "../lib/api";
import { Card, PageHeader } from "../components/UI";

function saveText(filename, text) {
  const blob = new Blob([`${String(text || "").trim()}\n`], { type: "text/plain;charset=utf-8" });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return "Duration unavailable";
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}` : `${minutes}:${String(secs).padStart(2, "0")}`;
}

function formatTranscriptSource(source) {
  const labels = {
    youtube_browser: "YouTube browser transcript panel",
    youtube_transcript_api: "YouTube captions library",
    yt_dlp: "yt-dlp captions",
    browser_paste: "Pasted transcript",
  };
  return labels[source] || source || "Automatic extraction";
}

function renderInline(text, keyPrefix = "inline") {
  const parts = String(text || "").split(/(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g);
  return parts.map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={`${keyPrefix}-${index}`} className="font-bold text-slate-900">{part.slice(2, -2)}</strong>;
    if (part.startsWith("*") && part.endsWith("*")) return <em key={`${keyPrefix}-${index}`}>{part.slice(1, -1)}</em>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={`${keyPrefix}-${index}`} className="rounded bg-slate-100 px-1.5 py-0.5 text-[0.92em] text-indigo-700">{part.slice(1, -1)}</code>;
    return part;
  });
}

function FormattedSummary({ text }) {
  const lines = String(text || "").split(/\r?\n/);
  return (
    <div className="space-y-3 text-[14px] leading-7 text-slate-700 sm:text-[15px]">
      {lines.map((rawLine, index) => {
        const line = rawLine.trim();
        if (!line) return <div key={`space-${index}`} className="h-1" />;
        const heading = line.match(/^(#{1,4})\s+(.+)$/);
        if (heading) {
          const level = heading[1].length;
          const HeadingTag = level === 1 ? "h2" : level === 2 ? "h3" : "h4";
          return (
            <HeadingTag key={`heading-${index}`} className={`mt-5 border-l-4 border-indigo-500 bg-indigo-50/90 px-4 py-2.5 font-bold tracking-tight text-indigo-950 shadow-sm first:mt-0 ${level === 1 ? "rounded-r-xl text-lg" : level === 2 ? "rounded-r-lg text-base" : "rounded-r-lg text-sm"}`}>
              {renderInline(heading[2], `heading-${index}`)}
            </HeadingTag>
          );
        }
        const bullet = line.match(/^(?:[-*•])\s+(.+)$/);
        if (bullet) return <div key={`bullet-${index}`} className="flex gap-3 pl-1"><span className="mt-[11px] h-1.5 w-1.5 shrink-0 rounded-full bg-indigo-500"/><p className="min-w-0 flex-1">{renderInline(bullet[1], `bullet-${index}`)}</p></div>;
        const numbered = line.match(/^\d+[.)]\s+(.+)$/);
        if (numbered) return <div key={`number-${index}`} className="flex gap-3 pl-1"><span className="mt-1 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-indigo-100 text-[11px] font-bold text-indigo-800">{line.match(/^\d+/)[0]}</span><p className="min-w-0 flex-1">{renderInline(numbered[1], `number-${index}`)}</p></div>;
        return <p key={`paragraph-${index}`}>{renderInline(line, `paragraph-${index}`)}</p>;
      })}
    </div>
  );
}

export default function YouTubeIntelligence() {
  const [url, setUrl] = useState("");
  const [manualMode, setManualMode] = useState(false);
  const [transcriptText, setTranscriptText] = useState("");
  const [result, setResult] = useState(null);
  const [tab, setTab] = useState("summary");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);

  useEffect(() => {
    const clear = () => { setResult(null); setModalOpen(false); };
    window.addEventListener("auth-expired", clear);
    return () => window.removeEventListener("auth-expired", clear);
  }, []);

  useEffect(() => {
    if (!modalOpen) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event) => { if (event.key === "Escape") setModalOpen(false); };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [modalOpen]);

  const analyze = async (event) => {
    event.preventDefault();
    if (!url.trim()) return;
    setLoading(true);
    setError("");
    setResult(null);
    setModalOpen(false);
    try {
      const data = await request("/youtube-intelligence/analyze", {
        method: "POST",
        retry: false,
        timeoutMs: 0,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url.trim(), transcript_text: manualMode ? transcriptText : null }),
      });
      setResult(data);
      setTab(data?.summary ? "summary" : "transcript");
      setModalOpen(true);
    } catch (err) {
      setError(err?.message || "Unable to analyze this YouTube video.");
    } finally {
      setLoading(false);
    }
  };

  const currentText = useMemo(() => tab === "summary" ? (result?.summary || "") : (result?.transcript || ""), [tab, result]);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(currentText);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setError("Clipboard access was blocked by your browser. Use Download instead.");
    }
  };

  const videoTitle = result?.video_title || "YouTube video analysis";
  const videoUrl = result?.video_url || (result?.video_id ? `https://www.youtube.com/watch?v=${result.video_id}` : url);

  return (
    <div className="space-y-6">
      <div className="mx-auto max-w-4xl pt-3 sm:pt-8">
        <PageHeader title="YouTube Intelligence" description="Turn any YouTube video into a structured AI summary with its complete transcript." />
        <Card className="mx-auto max-w-3xl overflow-hidden border-slate-200 shadow-[0_18px_55px_rgba(15,23,42,.09)]">
          <form onSubmit={analyze} className="space-y-5 p-5 sm:p-8">
            <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-red-50 text-red-600"><Youtube size={30}/></div>
            <div className="text-center">
              <h2 className="text-xl font-bold tracking-tight text-slate-900 sm:text-2xl">Summarize a YouTube video</h2>
              <p className="mt-1 text-sm text-slate-500">Paste the video link below to extract its transcript and generate an AI-powered summary.</p>
            </div>
            <div className="space-y-2">
              <label htmlFor="youtube-url" className="block text-xs font-bold uppercase tracking-wide text-slate-600">YouTube video URL</label>
              <input id="youtube-url" type="url" required value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://www.youtube.com/watch?v=..." className="w-full rounded-xl border border-slate-300 bg-slate-50 px-4 py-4 text-sm text-slate-800 outline-none transition placeholder:text-slate-400 focus:border-indigo-500 focus:bg-white focus:ring-4 focus:ring-indigo-100" />
            </div>
            <label className="flex cursor-pointer items-center justify-center gap-2 text-xs font-medium text-slate-600">
              <input type="checkbox" checked={manualMode} onChange={(event) => setManualMode(event.target.checked)} className="rounded border-slate-300 text-indigo-600 focus:ring-indigo-500" />
              I already have the transcript — paste it instead
            </label>
            {manualMode && (
              <div className="space-y-2">
                <label htmlFor="youtube-transcript" className="block text-xs font-semibold text-slate-600">Transcript copied from YouTube's Transcript panel</label>
                <textarea id="youtube-transcript" value={transcriptText} onChange={(event) => setTranscriptText(event.target.value)} rows={7} placeholder="Paste the transcript here…" className="w-full rounded-xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-800 outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
              </div>
            )}
            <button type="submit" disabled={loading || !url.trim() || (manualMode && !transcriptText.trim())} className="flex w-full items-center justify-center gap-2 rounded-xl bg-navy-900 px-5 py-3.5 text-sm font-bold text-white shadow-md shadow-slate-900/10 transition hover:-translate-y-0.5 hover:bg-navy-800 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:translate-y-0">
              {loading ? <LoaderCircle className="animate-spin" size={18}/> : <Play size={17}/>}
              {loading ? "Analyzing video…" : manualMode ? "Summarize pasted transcript" : "Generate summary & transcript"}
            </button>
            <p className="text-center text-[11px] leading-5 text-slate-500">Uses YouTube captions with automatic fallbacks and local Qwen summarization. Results are not saved to your account.</p>
            {loading && <div className="rounded-xl border border-indigo-100 bg-indigo-50 px-4 py-3 text-center text-xs leading-5 text-indigo-800"><LoaderCircle className="mr-2 inline animate-spin" size={14}/>Extracting captions and generating your summary. Longer videos may take a few minutes.</div>}
            {error && <div role="alert" className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>}
          </form>
        </Card>
      </div>

      {result && !modalOpen && (
        <div className="flex justify-center">
          <button type="button" onClick={() => setModalOpen(true)} className="inline-flex items-center gap-2 rounded-xl border border-indigo-200 bg-white px-4 py-2.5 text-sm font-semibold text-indigo-800 shadow-sm hover:bg-indigo-50"><FileText size={16}/> Reopen video results</button>
        </div>
      )}

      {modalOpen && result && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-950/65 p-3 backdrop-blur-sm sm:p-6" onMouseDown={(event) => { if (event.target === event.currentTarget) setModalOpen(false); }}>
          <section role="dialog" aria-modal="true" aria-labelledby="youtube-result-title" className="flex h-[88vh] w-[94vw] max-w-6xl flex-col overflow-hidden rounded-2xl border border-white/20 bg-white shadow-2xl sm:h-[82vh] sm:w-[82vw]">
            <header className="flex shrink-0 items-start gap-3 border-b border-slate-200 bg-gradient-to-r from-slate-50 to-indigo-50/70 px-4 py-4 sm:px-6">
              <div className="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-red-100 text-red-600"><Youtube size={22}/></div>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2 text-[10px] font-bold uppercase tracking-[.14em] text-indigo-700"><Sparkles size={12}/> Analysis complete</div>
                <h2 id="youtube-result-title" className="mt-1 line-clamp-2 text-base font-bold text-slate-900 sm:text-lg">{videoTitle}</h2>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
                  <span>{result.channel_name || "YouTube video"}</span>
                  <span className="inline-flex items-center gap-1"><Clock3 size={12}/>{formatDuration(result.duration_seconds)}</span>
                  <span>Language: {result.transcript_language || "unknown"}</span>
                  <span>{formatTranscriptSource(result.transcript_source)}</span>
                </div>
              </div>
              <button type="button" onClick={() => setModalOpen(false)} aria-label="Close results" className="rounded-lg p-2 text-slate-500 transition hover:bg-white hover:text-slate-900"><X size={20}/></button>
            </header>

            <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-white px-4 py-3 sm:px-6">
              <div className="flex gap-1 rounded-xl bg-slate-100 p-1">
                {[['summary', 'AI summary', Sparkles], ['transcript', 'Full transcript', FileText]].map(([key, label, Icon]) => (
                  <button key={key} type="button" onClick={() => { setTab(key); setCopied(false); }} className={`inline-flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-bold transition sm:px-4 ${tab === key ? "bg-white text-indigo-900 shadow-sm" : "text-slate-500 hover:text-slate-800"}`}><Icon size={14}/>{label}</button>
                ))}
              </div>
              <div className="flex gap-2">
                <button type="button" onClick={copy} disabled={!currentText} className="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-bold text-slate-700 transition hover:border-indigo-200 hover:bg-indigo-50 disabled:opacity-40"><Copy size={14}/>{copied ? <><Check size={13}/>Copied</> : "Copy"}</button>
                <button type="button" onClick={() => saveText(`youtube-${result.video_id || "transcript"}-${tab}.txt`, currentText)} disabled={!currentText} className="inline-flex items-center gap-2 rounded-lg bg-indigo-700 px-3 py-2 text-xs font-bold text-white transition hover:bg-indigo-800 disabled:opacity-40"><Download size={14}/>Download</button>
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50/70 px-4 py-5 sm:px-8 sm:py-7">
              {tab === "summary" ? (
                result.summary ? (
                  <div className="mx-auto max-w-4xl rounded-xl border border-slate-200 bg-white p-5 shadow-sm sm:p-8"><div className="mb-5 flex items-center gap-2 border-b border-slate-100 pb-4 text-sm font-bold text-slate-800"><Sparkles size={17} className="text-indigo-600"/> Qwen video summary</div><FormattedSummary text={result.summary}/></div>
                ) : (
                  <div role="status" className="mx-auto max-w-3xl rounded-xl border border-amber-200 bg-amber-50 p-5 text-sm leading-6 text-amber-900"><strong>Transcript extracted successfully, but the summary could not be generated.</strong>{result.summary_error ? <div className="mt-2 text-xs">{result.summary_error}</div> : null}<div className="mt-2 text-xs">Your full transcript is available in the Full transcript tab and can still be copied or downloaded.</div><button type="button" onClick={() => setTab("transcript")} className="mt-4 rounded-lg bg-amber-900 px-3 py-2 text-xs font-bold text-white">View transcript</button></div>
                )
              ) : (
                <div className="mx-auto max-w-4xl rounded-xl border border-slate-200 bg-white p-5 shadow-sm sm:p-8"><div className="mb-5 flex items-center justify-between gap-2 border-b border-slate-100 pb-4 text-sm font-bold text-slate-800"><span className="flex items-center gap-2"><FileText size={17} className="text-indigo-600"/> Full transcript</span><span className="text-[11px] font-medium text-slate-400">{(result.transcript || "").length.toLocaleString()} characters</span></div><pre className="whitespace-pre-wrap break-words font-sans text-sm leading-7 text-slate-700">{result.transcript}</pre></div>
              )}
            </div>
            <footer className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-slate-200 bg-white px-4 py-3 text-[11px] text-slate-500 sm:px-6"><span>Results are temporary and are not saved to your account.</span><a href={videoUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-semibold text-indigo-700 hover:text-indigo-900">Open on YouTube <ExternalLink size={12}/></a></footer>
          </section>
        </div>
      )}
    </div>
  );
}
