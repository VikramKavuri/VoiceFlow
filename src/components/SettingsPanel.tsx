import { useEffect, useState, useCallback, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

/* ---------- Types ---------- */

interface PostProcessing {
  enabled: boolean;
  provider: string;
  apiKey: string;
  model: string;
  llmEnabled: boolean;
  llmModelPath: string;
  llmContextEnabled: boolean;
  itnEnabled: boolean;
  customVocabularyPath: string;
  promptTemplate: string;
  autoPunctuation: boolean;
  autoCapitalization: boolean;
  removeFillerWords: boolean;
}

interface Settings {
  hotkey: string;
  microphoneDevice: string;
  language: string;
  modelSize: string;
  modelPath: string;
  autoCopyToClipboard: boolean;
  soundFeedback: boolean;
  showOverlay: boolean;
  overlayPosition: string;
  startMinimized: boolean;
  launchAtStartup: boolean;
  postProcessing: PostProcessing;
}

interface MicrophoneInfo {
  name: string;
  id: number;
  hostApi: string;
  activityScore: number;
  recommended: boolean;
}

const DEFAULT_SETTINGS: Settings = {
  hotkey: "Ctrl+Shift+Space",
  microphoneDevice: "default",
  language: "en",
  modelSize: "base",
  modelPath: "",
  autoCopyToClipboard: true,
  soundFeedback: true,
  showOverlay: true,
  overlayPosition: "cursor",
  startMinimized: true,
  launchAtStartup: false,
  postProcessing: {
    enabled: false,
    provider: "openai",
    apiKey: "",
    model: "gpt-4o-mini",
    llmEnabled: true,
    llmModelPath: "models/qwen3-1.7b-q4_k_m/Qwen3-1.7B.Q4_K_M.gguf",
    llmContextEnabled: true,
    itnEnabled: true,
    customVocabularyPath: "",
    promptTemplate: "",
    autoPunctuation: true,
    autoCapitalization: true,
    removeFillerWords: true,
  },
};

/* ---------- Sub-components ---------- */

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <label className="flex items-center justify-between gap-3 cursor-pointer group">
      <span className="text-sm text-voiceflow-text/80 group-hover:text-voiceflow-text transition-colors-smooth">
        {label}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-smooth ${
          checked ? "bg-voiceflow-primary" : "bg-voiceflow-idle/40"
        }`}
      >
        <span
          className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-smooth ${
            checked ? "translate-x-[18px]" : "translate-x-[3px]"
          }`}
        />
      </button>
    </label>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-xs font-semibold uppercase tracking-wider text-voiceflow-primary mb-2">
      {children}
    </h3>
  );
}

/* ---------- Main component ---------- */

export default function SettingsPanel() {
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
  const [microphones, setMicrophones] = useState<MicrophoneInfo[]>([]);
  const [recommendedMicrophoneId, setRecommendedMicrophoneId] = useState<number | null>(null);
  const [isCapturingHotkey, setIsCapturingHotkey] = useState(false);
  const [micLevel, setMicLevel] = useState<number>(0);
  const [isTesting, setIsTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const testIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  /* ---- Load settings + microphones ---- */
  useEffect(() => {
    async function load() {
      try {
        const s = await invoke<Settings>("get_settings");
        setSettings(s);
      } catch (err) {
        console.warn("Failed to load settings, using defaults:", err);
        setLoadError(String(err));
      }

      // Request microphone list - sidecar responds asynchronously via event
      try {
        await invoke("list_microphones");
      } catch {
        /* sidecar may not be ready yet */
      }
    }
    load();

    // Listen for the microphone list from the sidecar
    const unlisten = listen<{
      devices: {
        id: number;
        name: string;
        channels: number;
        sample_rate: number;
        host_api: string;
        activity_score: number;
        recommended: boolean;
      }[];
      recommended_device_id?: number;
    }>(
      "microphones-list",
      (event) => {
        const devices = event.payload.devices || [];
        setRecommendedMicrophoneId(
          typeof event.payload.recommended_device_id === "number"
            ? event.payload.recommended_device_id
            : null,
        );
        setMicrophones(
          devices.map((d) => ({
            id: d.id,
            name: d.name,
            hostApi: d.host_api,
            activityScore: d.activity_score,
            recommended: d.recommended,
          }))
        );
      }
    );

    return () => {
      unlisten.then((fn) => fn());
    };
  }, []);

  /* ---- Hotkey capture ---- */
  useEffect(() => {
    if (!isCapturingHotkey) return;

    function handleKey(e: KeyboardEvent) {
      e.preventDefault();
      e.stopPropagation();

      const parts: string[] = [];
      if (e.ctrlKey || e.metaKey) parts.push("CmdOrCtrl");
      if (e.shiftKey) parts.push("Shift");
      if (e.altKey) parts.push("Alt");

      const key = e.key;
      if (!["Control", "Shift", "Alt", "Meta"].includes(key)) {
        parts.push(key.length === 1 ? key.toUpperCase() : key);
        setSettings((prev) => ({ ...prev, hotkey: parts.join("+") }));
        setIsCapturingHotkey(false);
      }
    }

    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [isCapturingHotkey]);

  /* ---- Helpers ---- */
  const updatePost = useCallback(
    (key: keyof PostProcessing, value: boolean | string) => {
      setSettings((prev) => ({
        ...prev,
        postProcessing: { ...prev.postProcessing, [key]: value },
      }));
    },
    [],
  );

  async function handleSave() {
    setSaving(true);
    try {
      await invoke("update_settings", { settings });
    } catch (err) {
      console.error("Failed to save settings:", err);
    } finally {
      setSaving(false);
    }
  }

  async function handleCancel() {
    try {
      const s = await invoke<Settings>("get_settings");
      setSettings(s);
    } catch {
      setSettings(DEFAULT_SETTINGS);
    }
  }

  function handleTestMic() {
    if (isTesting) {
      setIsTesting(false);
      setMicLevel(0);
      if (testIntervalRef.current) {
        clearInterval(testIntervalRef.current);
        testIntervalRef.current = null;
      }
      return;
    }

    setIsTesting(true);
    /* Simulate mic level for visual feedback until the backend exposes a real stream */
    testIntervalRef.current = setInterval(() => {
      setMicLevel(Math.random() * 100);
    }, 120);
  }

  /* ---- Cleanup test interval on unmount ---- */
  useEffect(() => {
    return () => {
      if (testIntervalRef.current) clearInterval(testIntervalRef.current);
    };
  }, []);

  /* ---- Render ---- */
  const recommendedMic = microphones.find(
    (mic) => mic.id === recommendedMicrophoneId || mic.recommended,
  );

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      {/* Scrollable body */}
      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">
        {/* Header */}
        <h1 className="text-lg font-bold text-voiceflow-text">
          VoiceFlow Settings
        </h1>
        <p className="text-xs text-voiceflow-text/50">
          VoiceFlow previews your transcript here while recording, then copies
          the final text to the clipboard and pastes it once into your target
          app when processing finishes.
        </p>

        {loadError && (
          <p className="text-xs text-voiceflow-error">{loadError}</p>
        )}

        {/* ---- Hotkey ---- */}
        <section className="space-y-2">
          <SectionTitle>Hotkey</SectionTitle>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setIsCapturingHotkey((v) => !v)}
              className={`flex-1 rounded-lg border px-3 py-2 text-sm text-left transition-smooth ${
                isCapturingHotkey
                  ? "border-voiceflow-primary bg-voiceflow-primary/10 text-voiceflow-primary"
                  : "border-white/10 bg-voiceflow-bg text-voiceflow-text/70 hover:border-voiceflow-primary/40"
              }`}
            >
              {isCapturingHotkey
                ? "Press a key combination..."
                : settings.hotkey}
            </button>
          </div>
        </section>

        {/* ---- Microphone ---- */}
        <section className="space-y-2">
          <SectionTitle>Microphone</SectionTitle>
          <div className="flex items-center gap-2">
            <select
              value={settings.microphoneDevice}
              onChange={(e) =>
                setSettings((prev) => ({
                  ...prev,
                  microphoneDevice: e.target.value,
                }))
              }
              className="flex-1 rounded-lg border border-white/10 bg-voiceflow-bg px-3 py-2 text-sm text-voiceflow-text/80 outline-none focus:border-voiceflow-primary transition-smooth appearance-none cursor-pointer"
            >
              <option value="default">
                {recommendedMic
                  ? `Most Active Microphone (${recommendedMic.name})`
                  : "Most Active Microphone"}
              </option>
              {microphones.map((mic) => (
                <option key={mic.id} value={String(mic.id)}>
                  {mic.recommended ? "Recommended: " : ""}
                  {mic.name}
                </option>
              ))}
            </select>

            <button
              type="button"
              onClick={handleTestMic}
              className={`shrink-0 rounded-lg border px-3 py-2 text-sm transition-smooth ${
                isTesting
                  ? "border-voiceflow-recording bg-voiceflow-recording/15 text-voiceflow-recording"
                  : "border-white/10 bg-voiceflow-bg text-voiceflow-text/60 hover:border-voiceflow-primary/40 hover:text-voiceflow-primary"
              }`}
            >
              {isTesting ? "Stop" : "Test"}
            </button>
          </div>

          {recommendedMic && (
            <p className="text-xs text-voiceflow-text/45">
              Default picks the most active mic right now:
              {" "}
              <span className="text-voiceflow-text/70">{recommendedMic.name}</span>
            </p>
          )}

          {/* Level meter */}
          {isTesting && (
            <div className="h-2 w-full rounded-full bg-voiceflow-bg overflow-hidden">
              <div
                className="h-full rounded-full bg-voiceflow-primary transition-all duration-100"
                style={{ width: `${Math.min(micLevel, 100)}%` }}
              />
            </div>
          )}
        </section>

        {/* ---- Post-processing ---- */}
        <section className="space-y-3">
          <SectionTitle>Post-Processing</SectionTitle>
          <div className="space-y-2.5 rounded-xl border border-white/5 bg-voiceflow-bg/50 p-3">
            <Toggle
              label="Auto punctuation"
              checked={settings.postProcessing.autoPunctuation}
              onChange={(v) => updatePost("autoPunctuation", v)}
            />
            <Toggle
              label="Auto capitalization"
              checked={settings.postProcessing.autoCapitalization}
              onChange={(v) => updatePost("autoCapitalization", v)}
            />
            <Toggle
              label="Remove filler words"
              checked={settings.postProcessing.removeFillerWords}
              onChange={(v) => updatePost("removeFillerWords", v)}
            />
            <Toggle
              label="Inverse text normalization"
              checked={settings.postProcessing.itnEnabled}
              onChange={(v) => updatePost("itnEnabled", v)}
            />
            <Toggle
              label="Local LLM formatter"
              checked={settings.postProcessing.llmEnabled}
              onChange={(v) => updatePost("llmEnabled", v)}
            />
            <Toggle
              label="Use active app context"
              checked={settings.postProcessing.llmContextEnabled}
              onChange={(v) => updatePost("llmContextEnabled", v)}
            />
            <input
              type="text"
              value={settings.postProcessing.llmModelPath}
              onChange={(e) => updatePost("llmModelPath", e.target.value)}
              placeholder="Local GGUF model path"
              className="w-full rounded-lg border border-white/10 bg-voiceflow-bg px-3 py-2 text-sm text-voiceflow-text outline-none transition-smooth placeholder:text-voiceflow-text/30 focus:border-voiceflow-primary/50"
            />
            <input
              type="text"
              value={settings.postProcessing.customVocabularyPath}
              onChange={(e) => updatePost("customVocabularyPath", e.target.value)}
              placeholder="Custom vocabulary file path"
              className="w-full rounded-lg border border-white/10 bg-voiceflow-bg px-3 py-2 text-sm text-voiceflow-text outline-none transition-smooth placeholder:text-voiceflow-text/30 focus:border-voiceflow-primary/50"
            />
          </div>
        </section>

        {/* ---- Display ---- */}
        <section className="space-y-3">
          <SectionTitle>Display</SectionTitle>
          <div className="space-y-2.5">
            <Toggle
              label="Show overlay while recording"
              checked={settings.showOverlay}
              onChange={(v) =>
                setSettings((prev) => ({ ...prev, showOverlay: v }))
              }
            />
            <Toggle
              label="Sound feedback"
              checked={settings.soundFeedback}
              onChange={(v) =>
                setSettings((prev) => ({ ...prev, soundFeedback: v }))
              }
            />
          </div>
        </section>

        {/* ---- Startup ---- */}
        <section>
          <Toggle
            label="Launch at startup"
            checked={settings.launchAtStartup}
            onChange={(v) =>
              setSettings((prev) => ({ ...prev, launchAtStartup: v }))
            }
          />
        </section>
      </div>

      {/* ---- Footer with Save / Cancel ---- */}
      <div className="shrink-0 flex items-center justify-end gap-2 border-t border-white/5 bg-voiceflow-surface/60 px-5 py-3">
        <button
          type="button"
          onClick={handleCancel}
          className="rounded-lg border border-white/10 px-4 py-1.5 text-sm text-voiceflow-text/60 hover:text-voiceflow-text hover:border-white/20 transition-smooth"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={handleSave}
          disabled={saving}
          className="rounded-lg bg-voiceflow-primary px-4 py-1.5 text-sm font-medium text-white hover:bg-voiceflow-primary/90 disabled:opacity-50 transition-smooth"
        >
          {saving ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
  );
}
