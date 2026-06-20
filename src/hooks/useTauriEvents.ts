import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { useCallback, useEffect, useRef, useState } from "react";

/* ---------- Event payload types ---------- */

export interface RecordingStartedPayload {
  timestamp?: number;
}

export interface PartialTranscriptPayload {
  text: string;
}

export interface FinalTranscriptPayload {
  text: string;
}

export interface FinalDeliveryPayload {
  text: string;
  delivery_status:
    | "paste_succeeded"
    | "paste_failed_but_copied"
    | "copy_failed";
  copied_to_clipboard: boolean;
  pasted_to_target: boolean;
  manual_paste_required: boolean;
  failure_reason?: string | null;
  // The Python sidecar attaches the low-confidence warning to this payload
  // because the Rust shell currently does not forward the standalone
  // `transcription_warning` event. Reading it here ensures the banner shows
  // up even without a Rust rebuild.
  transcription_warning?: TranscriptionWarningPayload | null;
}

export interface ErrorPayload {
  message: string;
}

export interface TranscriptionWarningPayload {
  code: "low_confidence";
  audio_seconds: number;
  transcript_chars: number;
  ratio: number;
  message: string;
}

/* ---------- App state ---------- */

export type AppState = "idle" | "recording" | "processing" | "error";
export type DeliveryStatus =
  | "paste_succeeded"
  | "paste_failed_but_copied"
  | "copy_failed"
  | null;

export interface TauriEventState {
  appState: AppState;
  partialTranscript: string;
  finalTranscript: string;
  error: string | null;
  recordingStartTime: number | null;
  deliveryStatus: DeliveryStatus;
  failureReason: string | null;
  copiedToClipboard: boolean;
  pastedToTarget: boolean;
  manualPasteRequired: boolean;
  transcriptionWarning: TranscriptionWarningPayload | null;
  clearTranscriptionWarning: () => void;
  clearError: () => void;
}

/* ---------- Hook ---------- */

export function useTauriEvents(): TauriEventState {
  const [appState, setAppState] = useState<AppState>("idle");
  const [partialTranscript, setPartialTranscript] = useState("");
  const [finalTranscript, setFinalTranscript] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [recordingStartTime, setRecordingStartTime] = useState<number | null>(
    null,
  );
  const [deliveryStatus, setDeliveryStatus] = useState<DeliveryStatus>(null);
  const [failureReason, setFailureReason] = useState<string | null>(null);
  const [copiedToClipboard, setCopiedToClipboard] = useState(false);
  const [pastedToTarget, setPastedToTarget] = useState(false);
  const [manualPasteRequired, setManualPasteRequired] = useState(false);
  const [transcriptionWarning, setTranscriptionWarning] =
    useState<TranscriptionWarningPayload | null>(null);

  const unlisteners = useRef<UnlistenFn[]>([]);

  const clearTranscriptionWarning = useCallback(() => {
    setTranscriptionWarning(null);
  }, []);

  const clearError = useCallback(() => {
    setError(null);
    if (appState === "error") {
      setAppState("idle");
    }
  }, [appState]);

  useEffect(() => {
    let mounted = true;

    async function subscribe() {
      const u1 = await listen<RecordingStartedPayload>(
        "recording-started",
        (event) => {
          if (!mounted) return;
          setAppState("recording");
          setPartialTranscript("");
          setFinalTranscript("");
          setError(null);
          setDeliveryStatus(null);
          setFailureReason(null);
          setCopiedToClipboard(false);
          setPastedToTarget(false);
          setManualPasteRequired(false);
          setTranscriptionWarning(null);
          setRecordingStartTime(event.payload?.timestamp ?? Date.now());
        },
      );

      const u2 = await listen(
        "recording-stopped",
        () => {
          if (!mounted) return;
          setAppState("processing");
          setRecordingStartTime(null);
          // Safety timeout: if the final delivery event never arrives, go back to idle.
          setTimeout(() => {
            if (!mounted) return;
            setAppState((prev) => (prev === "processing" ? "idle" : prev));
          }, 15000);
        },
      );

      const u3 = await listen<PartialTranscriptPayload>(
        "partial-transcript",
        (event) => {
          if (!mounted) return;
          setPartialTranscript(event.payload.text);
        },
      );

      const u4 = await listen<FinalTranscriptPayload>(
        "final-transcript",
        (event) => {
          if (!mounted) return;
          setFinalTranscript(event.payload.text);
          setPartialTranscript("");
        },
      );

      const u5 = await listen<ErrorPayload>("error", (event) => {
        if (!mounted) return;
        setError(event.payload.message);
        setAppState("error");
        setRecordingStartTime(null);
        // Auto-clear error after 5 seconds so the app doesn't stay stuck
        setTimeout(() => {
          if (!mounted) return;
          setAppState((prev) => (prev === "error" ? "idle" : prev));
        }, 5000);
      });

      const u6 = await listen<FinalDeliveryPayload>(
        "final-delivery",
        (event) => {
          if (!mounted) return;
          setFinalTranscript(event.payload.text ?? "");
          setPartialTranscript("");
          setDeliveryStatus(event.payload.delivery_status);
          setFailureReason(event.payload.failure_reason ?? null);
          setCopiedToClipboard(Boolean(event.payload.copied_to_clipboard));
          setPastedToTarget(Boolean(event.payload.pasted_to_target));
          setManualPasteRequired(Boolean(event.payload.manual_paste_required));
          if (event.payload.transcription_warning) {
            setTranscriptionWarning(event.payload.transcription_warning);
          }
          setAppState("idle");
          if (event.payload.delivery_status === "paste_succeeded") {
            setTimeout(() => {
              if (!mounted) return;
              setDeliveryStatus((prev) =>
                prev === "paste_succeeded" ? null : prev,
              );
            }, 8000);
          }
        },
      );

      const u7 = await listen<TranscriptionWarningPayload>(
        "transcription-warning",
        (event) => {
          if (!mounted) return;
          setTranscriptionWarning(event.payload);
        },
      );

      unlisteners.current = [u1, u2, u3, u4, u5, u6, u7];

      // Sync current state from the Rust backend on mount.
      // This handles the case where events fired before listeners were ready
      // (e.g., overlay window shown after recording-started was emitted).
      try {
        const currentState = await invoke<string>("get_app_state");
        if (!mounted) return;
        if (currentState === "recording" || currentState === "processing" || currentState === "error") {
          setAppState(currentState as AppState);
          if (currentState === "recording") {
            setRecordingStartTime(Date.now());
          }
        }
      } catch {
        // Ignore — command may not exist in older builds
      }
    }

    subscribe();

    return () => {
      mounted = false;
      unlisteners.current.forEach((u) => u());
      unlisteners.current = [];
    };
  }, []);

  return {
    appState,
    partialTranscript,
    finalTranscript,
    error,
    recordingStartTime,
    deliveryStatus,
    failureReason,
    copiedToClipboard,
    pastedToTarget,
    manualPasteRequired,
    transcriptionWarning,
    clearTranscriptionWarning,
    clearError,
  };
}
