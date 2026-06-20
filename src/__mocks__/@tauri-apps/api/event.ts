import { vi } from "vitest";

export const listen = vi.fn().mockResolvedValue(vi.fn());

export type UnlistenFn = () => void;
