import { useEffect, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { invoke } from "@tauri-apps/api/core";
import { useTauriEvents } from "./hooks/useTauriEvents";
import SettingsPanel from "./components/SettingsPanel";
import RecordingOverlay from "./components/RecordingOverlay";
import StatusIndicator from "./components/StatusIndicator";
import { SetupScreen } from "./components/SetupScreen";

export default function App() {
  const [windowLabel, setWindowLabel] = useState<string | null>(null);
  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null);

  const {
    appState,
    partialTranscript,
    finalTranscript,
    error,
    recordingStartTime,
    deliveryStatus,
    failureReason,
    copiedToClipboard,
    manualPasteRequired,
    transcriptionWarning,
    clearTranscriptionWarning,
    clearError,
  } = useTauriEvents();

  /* Determine which window we are in */
  useEffect(() => {
    try {
      const label = getCurrentWindow().label;
      setWindowLabel(label);
    } catch {
      /* fallback: treat as settings */
      setWindowLabel("settings");
    }
  }, []);

  /* Check whether first-run model setup is needed */
  useEffect(() => {
    invoke<boolean>("needs_setup")
      .then(setNeedsSetup)
      .catch(() => setNeedsSetup(false));
  }, []);

  /* All hooks have been called — now apply early-return guards */
  if (needsSetup === null) return null; // brief flash guard
  if (needsSetup) return <SetupScreen onDone={() => setNeedsSetup(false)} />;

  if (windowLabel === null) {
    /* Still resolving window context */
    return null;
  }

  const isOverlay = windowLabel === "overlay";

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      {/* Main content area */}
      <div className="flex-1 overflow-hidden">
        {isOverlay ? (
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
            onDismissWarning={clearTranscriptionWarning}
          />
        ) : (
          <SettingsPanel />
        )}
      </div>

      {/* Status bar (settings window only) */}
      {!isOverlay && (
        <div className="shrink-0 flex items-center justify-between border-t border-white/5 bg-voiceflow-surface/40 px-4 py-1.5">
          <StatusIndicator
            state={appState}
            error={error}
            deliveryStatus={deliveryStatus}
          />

          <div className="flex items-center gap-3">
            {deliveryStatus === "paste_succeeded" && appState === "idle" && (
              <span className="text-[10px] text-green-400 font-medium">
                Pasted to target app
              </span>
            )}

            {manualPasteRequired && copiedToClipboard && appState === "idle" && (
              <span className="text-[10px] text-yellow-400 font-medium">
                Copied to clipboard, press Ctrl+V manually
              </span>
            )}

            {deliveryStatus === "copy_failed" && appState === "idle" && (
              <span className="text-[10px] text-voiceflow-error font-medium">
                Could not copy transcript
              </span>
            )}

            {finalTranscript && appState === "idle" && deliveryStatus === null && (
              <span className="text-[10px] text-voiceflow-text/40 truncate max-w-[200px]">
                Last: {finalTranscript.slice(0, 60)}
                {finalTranscript.length > 60 ? "..." : ""}
              </span>
            )}

            {appState === "error" && (
              <button
                type="button"
                onClick={clearError}
                className="text-[10px] text-voiceflow-error/70 hover:text-voiceflow-error transition-colors-smooth"
              >
                Dismiss
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
