import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import StatusIndicator from "../components/StatusIndicator";
import type { AppState, DeliveryStatus } from "../hooks/useTauriEvents";

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderIndicator(
  state: AppState,
  error?: string | null,
  deliveryStatus?: DeliveryStatus,
) {
  return render(
    <StatusIndicator state={state} error={error} deliveryStatus={deliveryStatus} />,
  );
}

// ---------------------------------------------------------------------------
// Status text
// ---------------------------------------------------------------------------

describe("StatusIndicator — status text", () => {
  it('shows "Idle" when state is idle and no deliveryStatus', () => {
    renderIndicator("idle");
    expect(screen.getByText("Idle")).toBeInTheDocument();
  });

  it('shows "Recording" when state is recording', () => {
    renderIndicator("recording");
    expect(screen.getByText("Recording")).toBeInTheDocument();
  });

  it('shows "Processing..." when state is processing', () => {
    renderIndicator("processing");
    expect(screen.getByText("Processing...")).toBeInTheDocument();
  });

  it('shows "Error" when state is error and no error message', () => {
    renderIndicator("error");
    expect(screen.getByText("Error")).toBeInTheDocument();
  });

  it("shows error message when state is error and error prop is provided", () => {
    renderIndicator("error", "Microphone not found");
    expect(screen.getByText("Error: Microphone not found")).toBeInTheDocument();
  });

  it('shows "Pasted to target app" when idle + paste_succeeded', () => {
    renderIndicator("idle", null, "paste_succeeded");
    expect(screen.getByText("Pasted to target app")).toBeInTheDocument();
  });

  it('shows "Copied to clipboard" when idle + paste_failed_but_copied', () => {
    renderIndicator("idle", null, "paste_failed_but_copied");
    expect(screen.getByText("Copied to clipboard")).toBeInTheDocument();
  });

  it('shows "Could not copy transcript" when idle + copy_failed', () => {
    renderIndicator("idle", null, "copy_failed");
    expect(screen.getByText("Could not copy transcript")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Dot indicator presence
// ---------------------------------------------------------------------------

describe("StatusIndicator — dot indicator", () => {
  it("renders an idle dot when state is idle", () => {
    const { container } = renderIndicator("idle");
    const dot = container.querySelector(".bg-voiceflow-idle");
    expect(dot).toBeInTheDocument();
  });

  it("renders a recording dot when state is recording", () => {
    const { container } = renderIndicator("recording");
    const dot = container.querySelector(".bg-voiceflow-recording");
    expect(dot).toBeInTheDocument();
  });

  it("renders an error dot when state is error", () => {
    const { container } = renderIndicator("error");
    const dot = container.querySelector(".bg-voiceflow-error");
    expect(dot).toBeInTheDocument();
  });

  it("renders an SVG spinner when state is processing", () => {
    const { container } = renderIndicator("processing");
    expect(container.querySelector("svg")).toBeInTheDocument();
  });

  it("does not render an SVG spinner when state is idle", () => {
    const { container } = renderIndicator("idle");
    expect(container.querySelector("svg")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// CSS class on text
// ---------------------------------------------------------------------------

describe("StatusIndicator — CSS colour classes", () => {
  it("text has idle colour class when state is idle with no delivery", () => {
    const { container } = renderIndicator("idle");
    const span = container.querySelector(".text-voiceflow-idle");
    expect(span).toBeInTheDocument();
  });

  it("text has recording colour class when state is recording", () => {
    const { container } = renderIndicator("recording");
    const span = container.querySelector(".text-voiceflow-recording");
    expect(span).toBeInTheDocument();
  });

  it("text has processing colour class when state is processing", () => {
    const { container } = renderIndicator("processing");
    const span = container.querySelector(".text-voiceflow-processing");
    expect(span).toBeInTheDocument();
  });

  it("text has error colour class when state is error", () => {
    const { container } = renderIndicator("error");
    const span = container.querySelector(".text-voiceflow-error");
    expect(span).toBeInTheDocument();
  });

  it("text has green-400 class on paste_succeeded delivery", () => {
    const { container } = renderIndicator("idle", null, "paste_succeeded");
    const span = container.querySelector(".text-green-400");
    expect(span).toBeInTheDocument();
  });

  it("text has yellow-400 class on paste_failed_but_copied delivery", () => {
    const { container } = renderIndicator("idle", null, "paste_failed_but_copied");
    const span = container.querySelector(".text-yellow-400");
    expect(span).toBeInTheDocument();
  });

  it("text has error colour class on copy_failed delivery", () => {
    const { container } = renderIndicator("idle", null, "copy_failed");
    const span = container.querySelector(".text-voiceflow-error");
    expect(span).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Default props
// ---------------------------------------------------------------------------

describe("StatusIndicator — default props", () => {
  it("renders without optional props", () => {
    expect(() => renderIndicator("idle")).not.toThrow();
  });

  it("null deliveryStatus defaults to Idle text", () => {
    renderIndicator("idle", null, null);
    expect(screen.getByText("Idle")).toBeInTheDocument();
  });

  it("undefined error shows generic Error label", () => {
    renderIndicator("error", undefined);
    expect(screen.getByText("Error")).toBeInTheDocument();
  });
});
