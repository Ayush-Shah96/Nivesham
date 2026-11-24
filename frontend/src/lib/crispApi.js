
import { api, downloadBlob } from "./api";

function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);

  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;

  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();

  URL.revokeObjectURL(url);
}

export async function downloadCrispTranscript(noteId) {
  const blob = await downloadBlob(
    `/crisp-notes/${noteId}/transcript.txt`
  );

  triggerBlobDownload(
    blob,
    `crisp-note-${noteId}-transcript.txt`
  );
}

export { api };
