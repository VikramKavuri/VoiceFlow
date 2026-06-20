import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

type Progress = {
  event: string;
  label?: string;
  name?: string;
  index?: number;
  total?: number;
  pct?: number;
  downloaded_mb?: number;
  total_mb?: number;
  message?: string;
};

export function SetupScreen({ onDone }: { onDone: () => void }) {
  const [status, setStatus] = useState("Preparing one-time setup…");
  const [pct, setPct] = useState(0);
  const [step, setStep] = useState<{ i: number; n: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let unlistens: Array<() => void> = [];
    (async () => {
      unlistens.push(
        await listen<Progress>("setup-progress", (e) => {
          const p = e.payload;
          if (p.event === "setup_step")
            setStep({ i: p.index ?? 0, n: p.total ?? 0 });
          if (p.event === "model_start")
            setStatus(`Downloading ${p.label ?? "model"}…`);
          if (p.event === "model_progress") setPct(p.pct ?? 0);
          if (p.event === "setup_error") setError(p.message ?? "Setup failed");
        })
      );
      unlistens.push(await listen("setup-complete", () => onDone()));
      try {
        await invoke("start_model_setup");
      } catch (err) {
        setError(String(err));
      }
    })();
    return () => unlistens.forEach((u) => u());
  }, [onDone]);

  return (
    <div className="flex flex-col items-center justify-center h-screen gap-4 p-8 text-center">
      <h1 className="text-xl font-semibold">Setting up VoiceFlow</h1>
      <p className="text-sm text-gray-500">
        Downloading the speech and correction models (~2.6 GB). This happens once;
        after that VoiceFlow works fully offline.
      </p>
      {error ? (
        <div className="text-red-600 text-sm">
          {error}
          <br />
          Check your internet connection and reopen VoiceFlow.
        </div>
      ) : (
        <>
          <p className="text-sm">
            {status}
            {step ? ` (${step.i}/${step.n})` : ""}
          </p>
          <div className="w-72 h-2 bg-gray-200 rounded">
            <div
              className="h-2 bg-blue-500 rounded transition-all"
              style={{ width: `${pct}%` }}
            />
          </div>
          <p className="text-xs text-gray-400">{pct.toFixed(0)}%</p>
        </>
      )}
    </div>
  );
}
