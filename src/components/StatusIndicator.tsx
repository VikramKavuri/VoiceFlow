import type { AppState, DeliveryStatus } from "../hooks/useTauriEvents";

interface StatusIndicatorProps {
  state: AppState;
  error?: string | null;
  deliveryStatus?: DeliveryStatus;
}

export default function StatusIndicator({
  state,
  error,
  deliveryStatus = null,
}: StatusIndicatorProps) {
  const statusText =
    state === "idle"
      ? deliveryStatus === "paste_succeeded"
        ? "Pasted to target app"
        : deliveryStatus === "paste_failed_but_copied"
          ? "Copied to clipboard"
          : deliveryStatus === "copy_failed"
            ? "Could not copy transcript"
            : "Idle"
      : state === "recording"
        ? "Recording"
        : state === "processing"
          ? "Processing..."
          : error
            ? `Error: ${error}`
            : "Error";

  const statusClass =
    state === "idle"
      ? deliveryStatus === "paste_succeeded"
        ? "text-green-400"
        : deliveryStatus === "paste_failed_but_copied"
          ? "text-yellow-400"
          : deliveryStatus === "copy_failed"
            ? "text-voiceflow-error"
            : "text-voiceflow-idle"
      : state === "recording"
        ? "text-voiceflow-recording"
        : state === "processing"
          ? "text-voiceflow-processing"
          : "text-voiceflow-error";

  return (
    <div className="flex items-center gap-2 text-xs select-none">
      {/* Dot / spinner */}
      {state === "idle" && (
        <span className="inline-block h-2 w-2 rounded-full bg-voiceflow-idle" />
      )}

      {state === "recording" && (
        <span className="inline-block h-2 w-2 rounded-full bg-voiceflow-recording animate-pulse-recording" />
      )}

      {state === "processing" && (
        <svg
          className="h-3 w-3 animate-spin-slow text-voiceflow-processing"
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

      {state === "error" && (
        <span className="inline-block h-2 w-2 rounded-full bg-voiceflow-error" />
      )}

      {/* Label */}
      <span
        className={statusClass}
      >
        {statusText}
      </span>
    </div>
  );
}
