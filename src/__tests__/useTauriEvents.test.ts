/**
 * Tests for the useTauriEvents hook.
 *
 * @tauri-apps/api/core  →  mocked via src/__mocks__/@tauri-apps/api/core.ts
 * @tauri-apps/api/event →  mocked via src/__mocks__/@tauri-apps/api/event.ts
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useTauriEvents } from "../hooks/useTauriEvents";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";

vi.mock("@tauri-apps/api/event");
vi.mock("@tauri-apps/api/core");

// ---------------------------------------------------------------------------
// Types for test helpers
// ---------------------------------------------------------------------------

type ListenerMap = Record<string, (event: { payload: unknown }) => void>;

// ---------------------------------------------------------------------------
// Setup: capture registered listeners so tests can fire them
// ---------------------------------------------------------------------------

let listeners: ListenerMap = {};

beforeEach(() => {
  vi.clearAllMocks();
  listeners = {};

  // Each call to listen(eventName, callback) stores the callback
  (listen as ReturnType<typeof vi.fn>).mockImplementation(
    (eventName: string, cb: (e: { payload: unknown }) => void) => {
      listeners[eventName] = cb;
      return Promise.resolve(() => {
        delete listeners[eventName];
      });
    },
  );

  // invoke returns "idle" by default (get_app_state)
  (invoke as ReturnType<typeof vi.fn>).mockResolvedValue("idle");
});

// ---------------------------------------------------------------------------
// Initial state
// ---------------------------------------------------------------------------

describe("useTauriEvents — initial state", () => {
  it("starts with appState=idle", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(result.current.appState).toBe("idle"));
  });

  it("starts with empty partialTranscript", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(result.current.partialTranscript).toBe(""));
  });

  it("starts with empty finalTranscript", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(result.current.finalTranscript).toBe(""));
  });

  it("starts with null error", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(result.current.error).toBeNull());
  });

  it("starts with null recordingStartTime", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(result.current.recordingStartTime).toBeNull());
  });

  it("starts with null deliveryStatus", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(result.current.deliveryStatus).toBeNull());
  });

  it("starts with copiedToClipboard=false", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(result.current.copiedToClipboard).toBe(false));
  });

  it("starts with manualPasteRequired=false", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(result.current.manualPasteRequired).toBe(false));
  });
});

// ---------------------------------------------------------------------------
// recording-started event
// ---------------------------------------------------------------------------

describe("useTauriEvents — recording-started event", () => {
  it("sets appState to recording", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["recording-started"]).toBeDefined());

    act(() => {
      listeners["recording-started"]({ payload: { timestamp: Date.now() } });
    });

    expect(result.current.appState).toBe("recording");
  });

  it("clears partialTranscript on recording-started", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["recording-started"]).toBeDefined());

    act(() => {
      listeners["partial-transcript"]({ payload: { text: "hello" } });
    });
    act(() => {
      listeners["recording-started"]({ payload: { timestamp: Date.now() } });
    });

    expect(result.current.partialTranscript).toBe("");
  });

  it("sets recordingStartTime from payload timestamp", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["recording-started"]).toBeDefined());

    const ts = 1700000000000;
    act(() => {
      listeners["recording-started"]({ payload: { timestamp: ts } });
    });

    expect(result.current.recordingStartTime).toBe(ts);
  });

  it("clears error on recording-started", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["error"]).toBeDefined());

    act(() => {
      listeners["error"]({ payload: { message: "something failed" } });
    });
    act(() => {
      listeners["recording-started"]({ payload: { timestamp: Date.now() } });
    });

    expect(result.current.error).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// recording-stopped event
// ---------------------------------------------------------------------------

describe("useTauriEvents — recording-stopped event", () => {
  it("sets appState to processing", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["recording-stopped"]).toBeDefined());

    act(() => {
      listeners["recording-stopped"]({ payload: {} });
    });

    expect(result.current.appState).toBe("processing");
  });

  it("clears recordingStartTime on recording-stopped", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["recording-started"]).toBeDefined());

    act(() => {
      listeners["recording-started"]({ payload: { timestamp: Date.now() } });
    });
    act(() => {
      listeners["recording-stopped"]({ payload: {} });
    });

    expect(result.current.recordingStartTime).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// partial-transcript event
// ---------------------------------------------------------------------------

describe("useTauriEvents — partial-transcript event", () => {
  it("sets partialTranscript from payload", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["partial-transcript"]).toBeDefined());

    act(() => {
      listeners["partial-transcript"]({ payload: { text: "Hello wor" } });
    });

    expect(result.current.partialTranscript).toBe("Hello wor");
  });

  it("updates partialTranscript on subsequent calls", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["partial-transcript"]).toBeDefined());

    act(() => {
      listeners["partial-transcript"]({ payload: { text: "Hello" } });
    });
    act(() => {
      listeners["partial-transcript"]({ payload: { text: "Hello world" } });
    });

    expect(result.current.partialTranscript).toBe("Hello world");
  });
});

// ---------------------------------------------------------------------------
// final-transcript event
// ---------------------------------------------------------------------------

describe("useTauriEvents — final-transcript event", () => {
  it("sets finalTranscript from payload", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["final-transcript"]).toBeDefined());

    act(() => {
      listeners["final-transcript"]({ payload: { text: "Complete sentence." } });
    });

    expect(result.current.finalTranscript).toBe("Complete sentence.");
  });

  it("clears partialTranscript when final-transcript arrives", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["partial-transcript"]).toBeDefined());

    act(() => {
      listeners["partial-transcript"]({ payload: { text: "partial..." } });
    });
    act(() => {
      listeners["final-transcript"]({ payload: { text: "final text" } });
    });

    expect(result.current.partialTranscript).toBe("");
    expect(result.current.finalTranscript).toBe("final text");
  });
});

// ---------------------------------------------------------------------------
// error event
// ---------------------------------------------------------------------------

describe("useTauriEvents — error event", () => {
  it("sets error message from payload", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["error"]).toBeDefined());

    act(() => {
      listeners["error"]({ payload: { message: "Microphone unavailable" } });
    });

    expect(result.current.error).toBe("Microphone unavailable");
  });

  it("sets appState to error", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["error"]).toBeDefined());

    act(() => {
      listeners["error"]({ payload: { message: "oops" } });
    });

    expect(result.current.appState).toBe("error");
  });

  it("clears recordingStartTime on error", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["recording-started"]).toBeDefined());

    act(() => {
      listeners["recording-started"]({ payload: { timestamp: Date.now() } });
    });
    act(() => {
      listeners["error"]({ payload: { message: "fail" } });
    });

    expect(result.current.recordingStartTime).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// final-delivery event
// ---------------------------------------------------------------------------

describe("useTauriEvents — final-delivery event", () => {
  it("sets appState to idle on final-delivery", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["final-delivery"]).toBeDefined());

    act(() => {
      listeners["final-delivery"]({
        payload: {
          text: "Done.",
          delivery_status: "paste_succeeded",
          copied_to_clipboard: true,
          pasted_to_target: true,
          manual_paste_required: false,
        },
      });
    });

    expect(result.current.appState).toBe("idle");
  });

  it("sets deliveryStatus from payload", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["final-delivery"]).toBeDefined());

    act(() => {
      listeners["final-delivery"]({
        payload: {
          text: "text",
          delivery_status: "paste_failed_but_copied",
          copied_to_clipboard: true,
          pasted_to_target: false,
          manual_paste_required: true,
        },
      });
    });

    expect(result.current.deliveryStatus).toBe("paste_failed_but_copied");
  });

  it("sets copiedToClipboard from payload", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["final-delivery"]).toBeDefined());

    act(() => {
      listeners["final-delivery"]({
        payload: {
          text: "text",
          delivery_status: "paste_failed_but_copied",
          copied_to_clipboard: true,
          pasted_to_target: false,
          manual_paste_required: false,
        },
      });
    });

    expect(result.current.copiedToClipboard).toBe(true);
  });

  it("sets manualPasteRequired from payload", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["final-delivery"]).toBeDefined());

    act(() => {
      listeners["final-delivery"]({
        payload: {
          text: "text",
          delivery_status: "paste_failed_but_copied",
          copied_to_clipboard: true,
          pasted_to_target: false,
          manual_paste_required: true,
        },
      });
    });

    expect(result.current.manualPasteRequired).toBe(true);
  });

  it("clears partialTranscript on final-delivery", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["partial-transcript"]).toBeDefined());

    act(() => {
      listeners["partial-transcript"]({ payload: { text: "partial..." } });
    });
    act(() => {
      listeners["final-delivery"]({
        payload: {
          text: "final",
          delivery_status: "paste_succeeded",
          copied_to_clipboard: true,
          pasted_to_target: true,
          manual_paste_required: false,
        },
      });
    });

    expect(result.current.partialTranscript).toBe("");
  });

  it("sets finalTranscript from final-delivery payload", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["final-delivery"]).toBeDefined());

    act(() => {
      listeners["final-delivery"]({
        payload: {
          text: "My final transcript.",
          delivery_status: "paste_succeeded",
          copied_to_clipboard: true,
          pasted_to_target: true,
          manual_paste_required: false,
        },
      });
    });

    expect(result.current.finalTranscript).toBe("My final transcript.");
  });
});

// ---------------------------------------------------------------------------
// clearError
// ---------------------------------------------------------------------------

describe("useTauriEvents — clearError", () => {
  it("clears the error message", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["error"]).toBeDefined());

    act(() => {
      listeners["error"]({ payload: { message: "Something failed" } });
    });

    act(() => {
      result.current.clearError();
    });

    expect(result.current.error).toBeNull();
  });

  it("resets appState to idle when clearing error state", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["error"]).toBeDefined());

    act(() => {
      listeners["error"]({ payload: { message: "fail" } });
    });

    expect(result.current.appState).toBe("error");

    act(() => {
      result.current.clearError();
    });

    expect(result.current.appState).toBe("idle");
  });

  it("does not change appState if not in error state", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(result.current.appState).toBe("idle"));

    act(() => {
      result.current.clearError();
    });

    expect(result.current.appState).toBe("idle");
  });
});

// ---------------------------------------------------------------------------
// transcription-warning event
// ---------------------------------------------------------------------------

describe("useTauriEvents — transcription-warning event", () => {
  it("sets transcriptionWarning from payload", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["transcription-warning"]).toBeDefined());

    act(() => {
      listeners["transcription-warning"]({
        payload: {
          code: "low_confidence",
          audio_seconds: 2.5,
          transcript_chars: 4,
          ratio: 1.6,
          message: "Couldn't hear you — transcript may be inaccurate.",
        },
      });
    });

    expect(result.current.transcriptionWarning).not.toBeNull();
    expect(result.current.transcriptionWarning?.message).toBe(
      "Couldn't hear you — transcript may be inaccurate.",
    );
    expect(result.current.transcriptionWarning?.code).toBe("low_confidence");
  });

  it("clears transcriptionWarning when recording-started fires", async () => {
    const { result } = renderHook(() => useTauriEvents());
    await waitFor(() => expect(listeners["transcription-warning"]).toBeDefined());

    act(() => {
      listeners["transcription-warning"]({
        payload: {
          code: "low_confidence",
          audio_seconds: 1.0,
          transcript_chars: 2,
          ratio: 2.0,
          message: "Low confidence.",
        },
      });
    });

    expect(result.current.transcriptionWarning).not.toBeNull();

    act(() => {
      listeners["recording-started"]({ payload: { timestamp: Date.now() } });
    });

    expect(result.current.transcriptionWarning).toBeNull();
  });
});
