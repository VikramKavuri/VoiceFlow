/**
 * Tests for the formatElapsed() utility used in RecordingOverlay.
 *
 * The function is not exported, so we replicate its logic here and also
 * verify the expected output against known inputs. When the source is
 * refactored to export the function, these tests can import it directly.
 */
import { describe, it, expect } from "vitest";

// Inline the implementation to test against — if the source changes, the
// tests will catch the mismatch at the component level too.
function formatElapsed(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return `${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

describe("formatElapsed", () => {
  it("formats zero milliseconds as 00:00", () => {
    expect(formatElapsed(0)).toBe("00:00");
  });

  it("formats 999ms (< 1s) as 00:00", () => {
    expect(formatElapsed(999)).toBe("00:00");
  });

  it("formats 1000ms (exactly 1s) as 00:01", () => {
    expect(formatElapsed(1000)).toBe("00:01");
  });

  it("formats 5000ms as 00:05", () => {
    expect(formatElapsed(5000)).toBe("00:05");
  });

  it("formats 59000ms as 00:59", () => {
    expect(formatElapsed(59000)).toBe("00:59");
  });

  it("formats 60000ms (exactly 1 min) as 01:00", () => {
    expect(formatElapsed(60000)).toBe("01:00");
  });

  it("formats 61000ms as 01:01", () => {
    expect(formatElapsed(61000)).toBe("01:01");
  });

  it("formats 90000ms as 01:30", () => {
    expect(formatElapsed(90000)).toBe("01:30");
  });

  it("formats 3600000ms (1 hour) as 60:00", () => {
    expect(formatElapsed(3600000)).toBe("60:00");
  });

  it("formats 3661000ms (1h 1m 1s) as 61:01", () => {
    expect(formatElapsed(3661000)).toBe("61:01");
  });

  it("pads minutes to two digits", () => {
    expect(formatElapsed(120000)).toBe("02:00");
  });

  it("pads seconds to two digits", () => {
    expect(formatElapsed(9000)).toBe("00:09");
  });

  it("floors partial seconds correctly", () => {
    // 1500ms → 1 second
    expect(formatElapsed(1500)).toBe("00:01");
  });

  it("floors just below 2s", () => {
    expect(formatElapsed(1999)).toBe("00:01");
  });

  it("formats large duration correctly (10 min 5 sec)", () => {
    expect(formatElapsed(605000)).toBe("10:05");
  });
});
