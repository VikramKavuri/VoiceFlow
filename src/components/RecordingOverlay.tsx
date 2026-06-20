import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import type { AppState, DeliveryStatus, TranscriptionWarningPayload } from "../hooks/useTauriEvents";

interface OverlaySettings {
  hotkey?: string;
  microphoneDevice?: string;
}

interface MicrophoneInfo {
  id: number;
  name: string;
  hostApi: string;
  recommended: boolean;
}

interface ActiveMicrophonePayload {
  id?: number;
  name?: string;
  host_api?: string;
  from_default?: boolean;
}

interface RecordingOverlayProps {
  appState: AppState;
  partialTranscript: string;
  finalTranscript: string;
  recordingStartTime: number | null;
  deliveryStatus: DeliveryStatus;
  failureReason?: string | null;
  copiedToClipboard: boolean;
  manualPasteRequired: boolean;
  transcriptionWarning?: TranscriptionWarningPayload | null;
  onDismissWarning?: () => void;
}

function formatElapsed(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return `${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

export default function RecordingOverlay({
  appState,
  partialTranscript,
  finalTranscript,
  recordingStartTime,
  deliveryStatus,
  failureReason = null,
  copiedToClipboard,
  manualPasteRequired,
  transcriptionWarning = null,
  onDismissWarning,
}: RecordingOverlayProps) {
  const [elapsed, setElapsed] = useState(0);
  const [audioLevel, setAudioLevel] = useState(0);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [hotkey, setHotkey] = useState("Ctrl+Shift+Space");
  const [microphones, setMicrophones] = useState<MicrophoneInfo[]>([]);
  const [selectedMicrophone, setSelectedMicrophone] = useState("default");
  const [recommendedMicrophoneId, setRecommendedMicrophoneId] = useState<number | null>(null);
  const [activeMicrophoneName, setActiveMicrophoneName] = useState("Resolving microphone...");
  const [micSaveState, setMicSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [micChangeDeferred, setMicChangeDeferred] = useState(false);
  const [completionDismissed, setCompletionDismissed] = useState(false);
  const levelBars = 20;
  const audioLevelRef = useRef(0);

  const recommendedMicrophone = microphones.find(
    (mic) => mic.id === recommendedMicrophoneId || mic.recommended,
  );

  const isRecording = appState === "recording";
  const isProcessing = appState === "processing";
  const isDelivering = isProcessing;
  const isCompleting = appState === "idle" && deliveryStatus !== null && !completionDismissed;
  const isVisible = isRecording || isProcessing || isCompleting;
  const progressStep = isRecording ? 1 : isDelivering ? 3 : isCompleting ? 3 : 0;
  const committedTranscript = finalTranscript.trim();
  const livePreview = partialTranscript.trim();
  const showCommittedTranscript = committedTranscript.length > 0;
  const showLivePreview = livePreview.length > 0;

  let statusTitle = formatElapsed(elapsed);
  let statusSubtitle = isSpeaking ? "Previewing your speech" : "Waiting for speech...";
  let statusToneClass = "text-voiceflow-recording";
  let microphoneContext = "Current recording";

  if (isDelivering) {
    statusTitle = "Delivering...";
    statusSubtitle = "Transcribing your speech and delivering to the target";
    statusToneClass = "text-voiceflow-processing";
    microphoneContext = "Current recording";
  } else if (isCompleting) {
    if (deliveryStatus === "paste_succeeded") {
      statusTitle = "Done — delivered";
      statusSubtitle = "Transcript is in the clipboard and pasted into the target app";
      statusToneClass = "text-green-400";
    } else if (deliveryStatus === "paste_failed_but_copied") {
      statusTitle = "Copied to Clipboard";
      statusSubtitle = "Transcript is ready in the clipboard. Paste it manually into the target app";
      statusToneClass = "text-yellow-400";
    } else {
      statusTitle = failureReason
        ? `Delivery failed: ${failureReason}`
        : "Delivery failed";
      statusSubtitle = "Transcription finished, but the delivery step did not complete";
      statusToneClass = "text-voiceflow-error";
    }
    microphoneContext = "Delivery complete";
  }

  const processSummary = isRecording
    ? "Recording is active. The bar below advances through capture, transcription, and delivery."
    : isDelivering
      ? "The recording has stopped. VoiceFlow is transcribing your speech and delivering the result."
      : deliveryStatus === "paste_succeeded"
        ? "Complete. The transcript was copied to the clipboard and pasted into the target location."
        : deliveryStatus === "paste_failed_but_copied"
          ? "Complete. The transcript is safely in the clipboard, but the target app needs a manual paste."
          : failureReason
            ? `Delivery failed: ${failureReason}. Start a new recording to try again.`
            : "Delivery failed. Start a new recording to try again.";

  const describeConfiguredMic = (value: string) => {
    if (value === "default") {
      return recommendedMicrophone
        ? `Most Active (${recommendedMicrophone.name})`
        : "Most Active Microphone";
    }
    const match = microphones.find((mic) => String(mic.id) === value);
    return match?.name ?? "Selected microphone";
  };

  /* Fetch the hotkey and mic selection used by the overlay */
  useEffect(() => {
    invoke<OverlaySettings>("get_settings")
      .then((s) => {
        if (s?.hotkey) {
          setHotkey(s.hotkey);
        }
        if (s?.microphoneDevice) {
          setSelectedMicrophone(s.microphoneDevice);
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!isVisible) {
      return;
    }

    invoke<OverlaySettings>("get_settings")
      .then((s) => {
        if (s?.microphoneDevice) {
          setSelectedMicrophone(s.microphoneDevice);
        }
      })
      .catch(() => {});

    invoke("list_microphones").catch(() => {});
  }, [isVisible]);

  useEffect(() => {
    if (isRecording || isProcessing) {
      setCompletionDismissed(false);
    }
  }, [isProcessing, isRecording]);

  useEffect(() => {
    if (appState !== "idle" || deliveryStatus === null) {
      return;
    }

    setCompletionDismissed(false);

    // Success: auto-dismiss after 1.2 s; failures stay visible until next recording or manual close.
    const isSuccess =
      deliveryStatus === "paste_succeeded" || deliveryStatus === "paste_failed_but_copied";
    if (!isSuccess) {
      return;
    }

    const timer = window.setTimeout(() => {
      setCompletionDismissed(true);
    }, 1200);

    return () => window.clearTimeout(timer);
  }, [appState, deliveryStatus]);

  useEffect(() => {
    if (isVisible) {
      return;
    }

    try {
      const currentWindow = getCurrentWindow();
      if (typeof currentWindow.hide === "function") {
        void currentWindow.hide();
      }
    } catch {
      // Ignore window API failures in non-overlay contexts/tests.
    }
  }, [isVisible]);

  /* Tick the elapsed timer while recording */
  useEffect(() => {
    if (!isRecording || recordingStartTime === null) {
      setElapsed(0);
      return;
    }

    const tick = () => setElapsed(Date.now() - recordingStartTime);
    tick();
    const id = setInterval(tick, 250);
    return () => clearInterval(id);
  }, [isRecording, recordingStartTime]);

  /* Listen for VAD speech detection events for the audio level indicator */
  useEffect(() => {
    if (!isRecording) {
      setAudioLevel(0);
      setIsSpeaking(false);
      return;
    }

    let mounted = true;

    const unlisteners: Array<() => void> = [];

    (async () => {
      const u1 = await listen<{ probability?: number }>(
        "vad-speech-detected",
        (event) => {
          if (!mounted) return;
          const prob = event.payload?.probability ?? 0.7;
          audioLevelRef.current = prob;
          setAudioLevel(prob);
          setIsSpeaking(true);
        },
      );
      unlisteners.push(u1);
    })();

    /* Decay audio level when no speech */
    const decayId = setInterval(() => {
      if (!mounted) return;
      audioLevelRef.current *= 0.85;
      if (audioLevelRef.current < 0.05) {
        audioLevelRef.current = 0;
        setIsSpeaking(false);
      }
      setAudioLevel(audioLevelRef.current);
    }, 100);

    return () => {
      mounted = false;
      unlisteners.forEach((u) => u());
      clearInterval(decayId);
    };
  }, [isRecording]);

  useEffect(() => {
    let mounted = true;
    const unlisteners: Array<() => void> = [];

    (async () => {
      const micListUnlisten = await listen<{
        devices: {
          id: number;
          name: string;
          host_api: string;
          recommended: boolean;
        }[];
        recommended_device_id?: number;
      }>("microphones-list", (event) => {
        if (!mounted) return;
        const devices = event.payload.devices || [];
        setRecommendedMicrophoneId(
          typeof event.payload.recommended_device_id === "number"
            ? event.payload.recommended_device_id
            : null,
        );
        setMicrophones(
          devices.map((device) => ({
            id: device.id,
            name: device.name,
            hostApi: device.host_api,
            recommended: device.recommended,
          })),
        );
      });
      unlisteners.push(micListUnlisten);

      const activeMicUnlisten = await listen<ActiveMicrophonePayload>(
        "active-microphone",
        (event) => {
          if (!mounted) return;
          if (event.payload.name) {
            setActiveMicrophoneName(event.payload.name);
          }
          setMicChangeDeferred(false);
        },
      );
      unlisteners.push(activeMicUnlisten);
    })();

    return () => {
      mounted = false;
      unlisteners.forEach((unlisten) => unlisten());
    };
  }, []);

  useEffect(() => {
    if (!isRecording) {
      setMicChangeDeferred(false);
    }
  }, [isRecording]);

  useEffect(() => {
    if (
      activeMicrophoneName === "Resolving microphone..."
      && (isRecording || isProcessing || isCompleting)
    ) {
      setActiveMicrophoneName(describeConfiguredMic(selectedMicrophone));
    }
  }, [
    activeMicrophoneName,
    isCompleting,
    isProcessing,
    isRecording,
    selectedMicrophone,
    recommendedMicrophone,
  ]);

  if (!isVisible) return null;

  const handleStop = async () => {
    try {
      await invoke("toggle_recording");
    } catch (err) {
      console.error("Failed to stop recording:", err);
    }
  };

  const handleMicrophoneChange = async (value: string) => {
      setSelectedMicrophone(value);
      setMicSaveState("saving");
    try {
      await invoke("set_microphone", { microphone: value });
      setMicSaveState("saved");
      setMicChangeDeferred(isRecording);
      invoke("list_microphones").catch(() => {});
    } catch (err) {
      console.error("Failed to update microphone:", err);
      setMicSaveState("error");
    }
  };

  /* Render audio level bars */
  const renderAudioBars = () => {
    const activeBars = Math.round(audioLevel * levelBars);
    return (
      <div className="flex items-end gap-[2px] h-6">
        {Array.from({ length: levelBars }, (_, i) => {
          const isActive = i < activeBars;
          const barHeight = 4 + (i / levelBars) * 20;
          const color =
            i < levelBars * 0.6
              ? "bg-green-400"
              : i < levelBars * 0.85
                ? "bg-yellow-400"
                : "bg-red-400";
          return (
            <div
              key={i}
              className={`w-[3px] rounded-sm transition-all duration-75 ${
                isActive ? color : "bg-white/10"
              }`}
              style={{ height: `${isActive ? barHeight : 4}px` }}
            />
          );
        })}
      </div>
    );
  };

  const renderStageProgress = () => {
    const stages = [
      { label: "Capture", active: progressStep >= 1 },
      { label: "Transcribe", active: progressStep >= 2 },
      { label: "Deliver", active: progressStep >= 3 },
    ];

    return (
      <div className="rounded-xl border border-white/8 bg-voiceflow-bg/45 px-3 py-3 space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <span className="block text-[10px] uppercase tracking-[0.18em] text-voiceflow-text/35">
              Progress
            </span>
            <span className="block text-sm text-voiceflow-text/85">
              {statusTitle}
            </span>
          </div>
          <span className={`text-[10px] font-medium ${statusToneClass}`}>
            Step {Math.max(progressStep, 1)} of 3
          </span>
        </div>

        <div className="grid grid-cols-3 gap-2">
          {stages.map((stage, index) => (
            <div key={stage.label} className="space-y-1">
              <div className="h-2 rounded-full bg-white/8 overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-300 ${
                    stage.active
                      ? isCompleting && index === 2
                        ? deliveryStatus === "paste_succeeded"
                          ? "bg-green-400"
                          : deliveryStatus === "paste_failed_but_copied"
                            ? "bg-yellow-400"
                            : "bg-voiceflow-error"
                        : isProcessing && index === 1
                          ? "bg-voiceflow-processing"
                          : "bg-voiceflow-recording"
                      : "bg-transparent"
                  }`}
                  style={{ width: stage.active ? "100%" : "0%" }}
                />
              </div>
              <span className="block text-[10px] text-voiceflow-text/45">
                {stage.label}
              </span>
            </div>
          ))}
        </div>

        <p className="text-[11px] text-voiceflow-text/45 leading-relaxed">
          {processSummary}
        </p>
      </div>
    );
  };

  const renderCompletionSummary = () => {
    if (!isCompleting || deliveryStatus === null) {
      return (
        <div className="rounded-xl border border-white/8 bg-voiceflow-bg/45 px-3 py-3">
          <div className="flex items-center justify-between gap-3 mb-2">
            <span className="block text-[10px] uppercase tracking-[0.18em] text-voiceflow-text/35">
              Live Preview
            </span>
            <span className="text-[10px] text-voiceflow-text/30">
              {isRecording ? "Changes as you speak" : "Finalizing"}
            </span>
          </div>
          <div className="max-h-40 overflow-y-auto whitespace-pre-wrap break-words text-sm leading-relaxed text-voiceflow-text/75 pr-1">
            {showLivePreview ? (
              livePreview
            ) : (
              <span className="text-voiceflow-text/35">
                {isRecording
                  ? "Listening for your next words..."
                  : "No live preview pending."}
              </span>
            )}
          </div>
        </div>
      );
    }

    const isSuccess = deliveryStatus === "paste_succeeded";
    const isClipboardOnly = deliveryStatus === "paste_failed_but_copied";
    const resultIconClass = isSuccess
      ? "bg-green-400/18 text-green-400"
      : isClipboardOnly
        ? "bg-yellow-400/18 text-yellow-400"
        : "bg-voiceflow-error/18 text-voiceflow-error";
    const resultTitle = isSuccess
      ? "Available in clipboard + target location"
      : isClipboardOnly
        ? "Available in clipboard only"
        : "Clipboard copy failed";
    const resultBody = isSuccess
      ? "The final transcript was copied and pasted successfully."
      : isClipboardOnly
        ? "The transcript is safe in the clipboard. Press Ctrl+V in the target app if needed."
        : "The transcription completed, but the clipboard step failed, so manual paste is not available.";

    return (
      <div className="rounded-xl border border-white/8 bg-voiceflow-bg/45 px-3 py-3">
        <span className="block text-[10px] uppercase tracking-[0.18em] text-voiceflow-text/35 mb-3">
          Delivery Result
        </span>
        <div className="flex items-start gap-3">
          <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-full ${resultIconClass}`}>
            {isSuccess ? (
              <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M20 6 9 17l-5-5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            ) : isClipboardOnly ? (
              <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="9" y="3" width="6" height="4" rx="1" />
                <path d="M9 5H7a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
              </svg>
            ) : (
              <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="9" />
                <path d="M12 8v5" strokeLinecap="round" />
                <circle cx="12" cy="16.5" r="0.75" fill="currentColor" stroke="none" />
              </svg>
            )}
          </div>
          <div className="min-w-0">
            <span className="block text-sm font-medium text-voiceflow-text/90">
              {resultTitle}
            </span>
            <p className="mt-1 text-[11px] leading-relaxed text-voiceflow-text/50">
              {resultBody}
            </p>
            {(copiedToClipboard || manualPasteRequired) && (
              <p className="mt-2 text-[10px] text-voiceflow-text/38">
                {manualPasteRequired
                  ? "Clipboard fallback is ready."
                  : copiedToClipboard
                    ? "Clipboard updated successfully."
                    : "Clipboard was not updated."}
              </p>
            )}
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="fixed inset-0 flex items-center justify-center p-4 no-select animate-fade-in">
      <div className="w-full max-w-2xl max-h-[calc(100vh-2rem)] overflow-hidden rounded-2xl bg-voiceflow-surface/95 backdrop-blur-md border border-white/10 shadow-2xl p-5 flex flex-col gap-4">
        {/* Transcription warning banner */}
        {transcriptionWarning && (
          <div className="flex items-start gap-2 rounded-xl bg-red-600 px-3 py-2.5 text-white transition-all duration-200">
            <p className="flex-1 text-[12px] leading-snug font-medium">
              {transcriptionWarning.message}
            </p>
            <button
              type="button"
              aria-label="Dismiss warning"
              onClick={onDismissWarning}
              className="shrink-0 ml-1 text-white/80 hover:text-white text-base leading-none transition-colors"
            >
              ×
            </button>
          </div>
        )}

        {/* Header: Mic icon + timer + state */}
        <div className="flex items-center gap-3 w-full">
          {/* Mic icon */}
          <div
            className={`flex items-center justify-center rounded-full h-12 w-12 shrink-0 ${
              isRecording
                ? isSpeaking
                  ? "bg-voiceflow-recording/30 animate-pulse-recording"
                  : "bg-voiceflow-recording/15"
                : isCompleting
                  ? deliveryStatus === "paste_succeeded"
                    ? "bg-green-400/18"
                    : deliveryStatus === "paste_failed_but_copied"
                      ? "bg-yellow-400/18"
                      : "bg-voiceflow-error/18"
                  : "bg-voiceflow-processing/20"
            }`}
          >
            {isRecording ? (
              <svg
                className={`h-6 w-6 ${isSpeaking ? "text-voiceflow-recording" : "text-voiceflow-recording/60"}`}
                viewBox="0 0 24 24"
                fill="currentColor"
              >
                <path d="M12 14a3 3 0 003-3V5a3 3 0 10-6 0v6a3 3 0 003 3z" />
                <path d="M19 11a1 1 0 10-2 0 5 5 0 01-10 0 1 1 0 10-2 0 7 7 0 006 6.93V20H8a1 1 0 100 2h8a1 1 0 100-2h-3v-2.07A7 7 0 0019 11z" />
              </svg>
            ) : isCompleting ? (
              deliveryStatus === "paste_succeeded" ? (
                <svg className="h-6 w-6 text-green-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                  <path d="M20 6 9 17l-5-5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              ) : deliveryStatus === "paste_failed_but_copied" ? (
                <svg className="h-6 w-6 text-yellow-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <rect x="9" y="3" width="6" height="4" rx="1" />
                  <path d="M9 5H7a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
                </svg>
              ) : (
                <svg className="h-6 w-6 text-voiceflow-error" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <circle cx="12" cy="12" r="9" />
                  <path d="M12 8v5" strokeLinecap="round" />
                  <circle cx="12" cy="16.5" r="0.75" fill="currentColor" stroke="none" />
                </svg>
              )
            ) : (
              <svg
                className="h-6 w-6 text-voiceflow-processing animate-spin-slow"
                viewBox="0 0 24 24"
                fill="none"
              >
                <circle
                  className="opacity-25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="3"
                />
                <path
                  className="opacity-75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8v3a5 5 0 00-5 5H4z"
                />
              </svg>
            )}
          </div>

          {/* Timer / state label */}
          <div className="flex flex-col flex-1 min-w-0">
            <span className={`text-lg font-semibold tabular-nums ${statusToneClass}`}>
              {statusTitle}
            </span>
            <span className="text-[11px] text-voiceflow-text/50">
              {statusSubtitle}
            </span>
            <span className="text-[10px] text-voiceflow-text/35 break-words">
              Using {activeMicrophoneName}
            </span>
          </div>
        </div>

        {/* Audio level visualizer */}
        {isRecording && (
          <div className="w-full flex items-center justify-center py-1">
            {renderAudioBars()}
          </div>
        )}

        {renderStageProgress()}

        <div className="w-full rounded-xl border border-white/8 bg-voiceflow-bg/45 px-3 py-2.5 space-y-2">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <span className="block text-[10px] uppercase tracking-[0.18em] text-voiceflow-text/35">
                Microphone
              </span>
              <span className="block text-sm text-voiceflow-text/85 break-words">
                {activeMicrophoneName}
              </span>
            </div>
            <span className="text-[10px] text-voiceflow-text/35 text-right">
              {microphoneContext}
            </span>
          </div>

          <select
            value={selectedMicrophone}
            onChange={(event) => void handleMicrophoneChange(event.target.value)}
            className="w-full rounded-lg border border-white/10 bg-voiceflow-surface/80 px-3 py-2 text-sm text-voiceflow-text/80 outline-none focus:border-voiceflow-primary transition-smooth appearance-none cursor-pointer"
          >
            <option value="default">
              {recommendedMicrophone
                ? `Most Active (${recommendedMicrophone.name})`
                : "Most Active Microphone"}
            </option>
            {microphones.map((mic) => (
              <option key={mic.id} value={String(mic.id)}>
                {mic.recommended ? "Recommended: " : ""}
                {mic.name}
              </option>
            ))}
          </select>

          <p className="text-[10px] text-voiceflow-text/42 leading-relaxed">
            {micSaveState === "saving"
              ? "Updating microphone..."
              : micSaveState === "error"
                ? "Could not update the microphone selection."
                : micChangeDeferred
                  ? `Saved. Next recording will use ${describeConfiguredMic(selectedMicrophone)}.`
                  : isRecording
                    ? "This recording stays on the current microphone. Changes apply next time."
                    : isProcessing
                      ? "Recording has stopped. Any microphone changes apply on the next recording."
                      : `Ready to use ${describeConfiguredMic(selectedMicrophone)}.`}
          </p>
        </div>

        <div className="w-full min-h-0 flex flex-col gap-3">
          <div className="rounded-xl border border-white/8 bg-voiceflow-bg/60 px-3 py-3">
            <span className="block text-[10px] uppercase tracking-[0.18em] text-voiceflow-text/35 mb-2">
              {isCompleting ? "Final Transcript" : "Committed Transcript"}
            </span>
            <div className="max-h-56 overflow-y-auto whitespace-pre-wrap break-words text-sm leading-relaxed text-voiceflow-text/85 pr-1">
              {showCommittedTranscript ? (
                committedTranscript
              ) : (
                <span className="text-voiceflow-text/35">
                  {isCompleting
                    ? "Final transcript will appear here once delivery completes."
                    : "Stable transcript will build here as recording continues."}
                </span>
              )}
            </div>
          </div>

          {renderCompletionSummary()}
        </div>

        {/* Action buttons */}
        {isRecording && (
          <div className="w-full flex flex-col items-center gap-2">
            {/* Stop button */}
            <button
              type="button"
              onClick={handleStop}
              className="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-voiceflow-recording/20 hover:bg-voiceflow-recording/30 border border-voiceflow-recording/30 text-voiceflow-recording font-medium text-sm transition-all duration-150 active:scale-[0.98]"
            >
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="currentColor">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
              Stop Recording
            </button>

            {/* Hotkey hint */}
            <p className="text-[10px] text-voiceflow-text/35 text-center">
              or press{" "}
              <kbd className="px-1 py-0.5 rounded bg-white/10 text-voiceflow-text/50 font-mono text-[10px]">
                {hotkey}
              </kbd>
            </p>
          </div>
        )}

        {/* Delivering state */}
        {isDelivering && (
          <p className="text-[11px] text-voiceflow-text/40 text-center">
            Transcribing and delivering — almost done...
          </p>
        )}

        {isCompleting && deliveryStatus !== "copy_failed" && (
          <p className="text-[11px] text-voiceflow-text/40 text-center">
            This overlay will close automatically in a moment.
          </p>
        )}

        {isCompleting && deliveryStatus === "copy_failed" && (
          <p className="text-[11px] text-voiceflow-error/60 text-center">
            Start a new recording or close this overlay to dismiss.
          </p>
        )}
      </div>
    </div>
  );
}
