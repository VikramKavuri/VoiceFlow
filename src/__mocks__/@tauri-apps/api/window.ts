import { vi } from "vitest";

export const getCurrentWindow = vi.fn(() => ({ label: "settings" }));
