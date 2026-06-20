import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import type { AppState, DeliveryStatus, TranscriptionWarningPayload } from "../hooks/useTauriEvents";
import RecordingOverlay from "../components/RecordingOverlay";

const hideMock = vi.hoisted(() => vi.fn());
const invokeMock = vi.hoisted(() => vi.fn());
const listenMock = vi.hoisted(() => vi.fn());

vi.mock("@tauri-apps/api/core", () => ({
  invoke: invokeMock,
}));

vi.mock("@tauri-apps/api/event", () => ({
  listen: listenMock,
}));

vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: vi.fn(() => ({
    label: "overlay",
    hide: hideMock,
  })),
}));

function renderOverlay({
  appState = "recording",
  partialTranscript = "",
  finalTranscript = "",
  recordingStartTime = Date.now(),
  deliveryStatus = null,
  failureReason = null,
  copiedToClipboard = false,
  manualPasteRequired = false,
  transcriptionWarning = null,
  onDismissWarning = vi.fn(),
}: {
  appState?: AppState;
  partialTranscript?: string;
  finalTranscript?: string;
  recordingStartTime?: number | null;
  deliveryStatus?: DeliveryStatus;
  failureReason?: string | null;
  copiedToClipboard?: boolean;
  manualPasteRequired?: boolean;
  transcriptionWarning?: TranscriptionWarningPayload | null;
  onDismissWarning?: () => void;
} = {}) {
  return render(
    <RecordingOverlay
      appState={appState}
      partialTranscript={partialTranscript}
      finalTranscript={finalTranscript}
      recordingStartTime={recordingStartTime}
      deliveryStatus={deliveryStatus}
      failureReason={failureReason}
      copiedToClipboard={copiedToClipboard}
      manualPasteRequired={manualPasteRequired}
      transcriptionWarning={transcriptionWarning}
      onDismissWarning={onDismissWarning}
    />,
  );
}

describe("RecordingOverlay", () => {
  beforeEach(() => {
    hideMock.mockReset();
    invokeMock.mockReset();
    listenMock.mockReset();

    invokeMock.mockImplementation((command: string) => {
      if (command === "get_settings") {
        return Promise.resolve({
          hotkey: "Ctrl+Shift+Space",
          microphoneDevice: "default",
        });
      }
      return Promise.resolve(undefined);
    });

    listenMock.mockResolvedValue(() => {});
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows stage progress during processing (delivering state)", async () => {
    renderOverlay({
      appState: "processing",
      finalTranscript: "Hello world",
      recordingStartTime: null,
    });

    // After recording-stopped, overlay shows "Delivering..." (step 3) — text appears in header and progress bar
    expect(screen.getAllByText("Delivering...").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Progress")).toBeInTheDocument();
    expect(screen.getByText("Capture")).toBeInTheDocument();
    expect(screen.getByText("Transcribe")).toBeInTheDocument();
    expect(screen.getByText("Deliver")).toBeInTheDocument();
    expect(screen.getByText("Transcribing and delivering — almost done...")).toBeInTheDocument();

    await waitFor(() => {
      expect(invokeMock).toHaveBeenCalledWith("list_microphones");
    });
  });

  it("shows a completed success state and hides after 1.2 s", async () => {
    vi.useFakeTimers();

    renderOverlay({
      appState: "idle",
      finalTranscript: "Final text",
      recordingStartTime: null,
      deliveryStatus: "paste_succeeded",
      copiedToClipboard: true,
    });

    // Updated success title — appears in header and progress block
    expect(screen.getAllByText("Done — delivered").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Available in clipboard + target location")).toBeInTheDocument();
    expect(screen.getByText("This overlay will close automatically in a moment.")).toBeInTheDocument();

    // Overlay dismisses after 1.2 s (not 3 s) — advance timers then restore real timers for waitFor
    await act(async () => {
      vi.advanceTimersByTime(1200);
    });

    vi.useRealTimers();

    await waitFor(() => {
      expect(hideMock).toHaveBeenCalled();
    });
  });

  it("shows clipboard-only completion messaging when paste fallback is needed", async () => {
    renderOverlay({
      appState: "idle",
      finalTranscript: "Clipboard fallback text",
      recordingStartTime: null,
      deliveryStatus: "paste_failed_but_copied",
      copiedToClipboard: true,
      manualPasteRequired: true,
    });

    expect(screen.getByText("Available in clipboard only")).toBeInTheDocument();
    expect(screen.getByText("Clipboard fallback is ready.")).toBeInTheDocument();
    // "Copied to Clipboard" appears in both header and progress block
    expect(screen.getAllByText("Copied to Clipboard").length).toBeGreaterThanOrEqual(1);
  });

  // -------------------------------------------------------------------------
  // Fix 2: transcription warning banner
  // -------------------------------------------------------------------------

  it("renders warning banner with the message when transcriptionWarning prop is set", async () => {
    const warning: TranscriptionWarningPayload = {
      code: "low_confidence",
      audio_seconds: 3.2,
      transcript_chars: 5,
      ratio: 1.56,
      message: "Couldn't hear you clearly — transcript may be inaccurate.",
    };

    renderOverlay({ transcriptionWarning: warning });

    expect(
      screen.getByText("Couldn't hear you clearly — transcript may be inaccurate."),
    ).toBeInTheDocument();
  });

  it("calls onDismissWarning when the close button on the banner is clicked", async () => {
    const warning: TranscriptionWarningPayload = {
      code: "low_confidence",
      audio_seconds: 2.0,
      transcript_chars: 3,
      ratio: 1.5,
      message: "Low confidence transcript.",
    };
    const onDismissWarning = vi.fn();

    renderOverlay({ transcriptionWarning: warning, onDismissWarning });

    const closeBtn = screen.getByRole("button", { name: /dismiss warning/i });
    fireEvent.click(closeBtn);

    expect(onDismissWarning).toHaveBeenCalledTimes(1);
  });

  // -------------------------------------------------------------------------
  // Fix 1: overlay stays visible during processing until final-delivery
  // -------------------------------------------------------------------------

  it("overlay remains visible in processing state before final-delivery arrives", async () => {
    // When appState=processing (recording-stopped fired, final-delivery not yet),
    // the overlay should be visible with the delivering UI.
    renderOverlay({
      appState: "processing",
      recordingStartTime: null,
    });

    // "Delivering..." appears in header title (and progress bar label area)
    expect(screen.getAllByText("Delivering...").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("Done — delivered")).not.toBeInTheDocument();
  });

  it("shows done state after final-delivery with success arrives", async () => {
    vi.useFakeTimers();

    renderOverlay({
      appState: "idle",
      deliveryStatus: "paste_succeeded",
      finalTranscript: "Delivered text",
      recordingStartTime: null,
      copiedToClipboard: true,
    });

    // Overlay is visible with success status — title appears in header and progress block
    expect(screen.getAllByText("Done — delivered").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("This overlay will close automatically in a moment.")).toBeInTheDocument();

    // After 1.2 s the overlay hides — advance fake timers then restore real ones for waitFor
    await act(async () => {
      vi.advanceTimersByTime(1200);
    });

    vi.useRealTimers();

    await waitFor(() => {
      expect(hideMock).toHaveBeenCalled();
    });
  });
});
