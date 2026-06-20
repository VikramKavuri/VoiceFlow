use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;

/// Post-processing configuration for AI-powered text enhancement.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(default)]
pub struct PostProcessingConfig {
    pub enabled: bool,
    pub provider: String,
    pub api_key: String,
    pub model: String,
    pub llm_enabled: bool,
    pub llm_model_path: String,
    pub llm_context_enabled: bool,
    pub itn_enabled: bool,
    pub custom_vocabulary_path: String,
    pub prompt_template: String,
    pub auto_punctuation: bool,
    pub auto_capitalization: bool,
    pub remove_filler_words: bool,
}

impl Default for PostProcessingConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            provider: "openai".to_string(),
            api_key: String::new(),
            model: "gpt-4o-mini".to_string(),
            llm_enabled: true,
            llm_model_path: "models/llama-3.2-3b-q4_k_m/Llama-3.2-3B-Instruct-Q4_K_M.gguf".to_string(),
            llm_context_enabled: true,
            // ITN is rule-based and destroys spoken currency / dates / emails
            // ("point six zero dollars" → "point 6 $0"). The local LLM handles
            // these correctly with full sentence context.
            itn_enabled: false,
            custom_vocabulary_path: String::new(),
            prompt_template: "Clean up this transcription. Fix grammar, punctuation, and remove filler words while preserving the original meaning:\n\n{text}".to_string(),
            auto_punctuation: true,
            auto_capitalization: true,
            // Adverbs like "actually" / "just" / "then" carry intent in
            // dictation. The LLM judges meaning; rule-based stripping
            // damages it.
            remove_filler_words: false,
        }
    }
}

/// Main settings struct for VoiceFlow Transcriptor.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(default)]
pub struct VoiceFlowSettings {
    pub hotkey: String,
    /// "toggle" (press once to start, again to stop) or "push_to_talk"
    /// (hold to record, release to stop). Defaults to "toggle" to preserve
    /// the long-standing behaviour; the hotkey handler honours this field.
    pub recording_mode: String,
    pub microphone_device: String,
    pub language: String,
    pub model_size: String,
    pub model_path: String,
    pub auto_copy_to_clipboard: bool,
    pub sound_feedback: bool,
    pub show_overlay: bool,
    pub overlay_position: String,
    pub start_minimized: bool,
    pub launch_at_startup: bool,
    pub post_processing: PostProcessingConfig,
}

impl Default for VoiceFlowSettings {
    fn default() -> Self {
        Self {
            hotkey: "Ctrl+Shift+Space".to_string(),
            recording_mode: "toggle".to_string(),
            microphone_device: "default".to_string(),
            language: "en".to_string(),
            model_size: "base".to_string(),
            model_path: String::new(),
            auto_copy_to_clipboard: true,
            sound_feedback: true,
            show_overlay: true,
            overlay_position: "cursor".to_string(),
            start_minimized: true,
            launch_at_startup: false,
            post_processing: PostProcessingConfig::default(),
        }
    }
}

/// Returns the path to the config directory: %APPDATA%\VoiceFlow
fn config_dir() -> Result<PathBuf, String> {
    let appdata = std::env::var("APPDATA")
        .map_err(|_| "APPDATA environment variable not found".to_string())?;
    Ok(PathBuf::from(appdata).join("VoiceFlow"))
}

/// Returns the path to the config file: %APPDATA%\VoiceFlow\config.json
fn config_file_path() -> Result<PathBuf, String> {
    Ok(config_dir()?.join("config.json"))
}

/// Load settings from disk. Returns defaults if file doesn't exist.
pub fn load() -> Result<VoiceFlowSettings, String> {
    let path = config_file_path()?;

    if !path.exists() {
        let defaults = VoiceFlowSettings::default();
        save(&defaults)?;
        return Ok(defaults);
    }

    let contents = fs::read_to_string(&path)
        .map_err(|e| format!("Failed to read config file: {}", e))?;

    let settings: VoiceFlowSettings = serde_json::from_str(&contents)
        .map_err(|e| format!("Failed to parse config file: {}", e))?;

    Ok(settings)
}

/// Save settings to disk. Creates the config directory if it doesn't exist.
pub fn save(settings: &VoiceFlowSettings) -> Result<(), String> {
    let dir = config_dir()?;
    if !dir.exists() {
        fs::create_dir_all(&dir)
            .map_err(|e| format!("Failed to create config directory: {}", e))?;
    }

    let path = config_file_path()?;
    let json = serde_json::to_string_pretty(settings)
        .map_err(|e| format!("Failed to serialize settings: {}", e))?;

    fs::write(&path, json)
        .map_err(|e| format!("Failed to write config file: {}", e))?;

    Ok(())
}

/// Return default settings.
pub fn defaults() -> VoiceFlowSettings {
    VoiceFlowSettings::default()
}

// ── Unit Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::env;
    use std::fs;

    // ── PostProcessingConfig defaults ────────────────────────────────────────

    #[test]
    fn post_processing_default_enabled_false() {
        let cfg = PostProcessingConfig::default();
        assert!(!cfg.enabled);
    }

    #[test]
    fn post_processing_default_provider_openai() {
        let cfg = PostProcessingConfig::default();
        assert_eq!(cfg.provider, "openai");
    }

    #[test]
    fn post_processing_default_api_key_empty() {
        let cfg = PostProcessingConfig::default();
        assert!(cfg.api_key.is_empty());
    }

    #[test]
    fn post_processing_default_model_gpt4o_mini() {
        let cfg = PostProcessingConfig::default();
        assert_eq!(cfg.model, "gpt-4o-mini");
    }

    #[test]
    fn post_processing_default_auto_punctuation_true() {
        let cfg = PostProcessingConfig::default();
        assert!(cfg.auto_punctuation);
    }

    #[test]
    fn post_processing_default_auto_capitalization_true() {
        let cfg = PostProcessingConfig::default();
        assert!(cfg.auto_capitalization);
    }

    #[test]
    fn post_processing_default_remove_filler_words_false() {
        // Deliberately false: adverbs like "actually"/"just"/"then" carry
        // intent in dictation, so rule-based stripping is off and the LLM
        // judges meaning instead. (See PostProcessingConfig::default.)
        let cfg = PostProcessingConfig::default();
        assert!(!cfg.remove_filler_words);
    }

    // ── VoiceFlowSettings defaults ───────────────────────────────────────────

    #[test]
    fn settings_default_hotkey() {
        let s = defaults();
        assert_eq!(s.hotkey, "Ctrl+Shift+Space");
    }

    #[test]
    fn settings_default_microphone_device() {
        let s = defaults();
        assert_eq!(s.microphone_device, "default");
    }

    #[test]
    fn settings_default_recording_mode_toggle() {
        // Defaults to toggle so existing users' behaviour is unchanged.
        let s = defaults();
        assert_eq!(s.recording_mode, "toggle");
    }

    #[test]
    fn settings_recording_mode_round_trips_push_to_talk() {
        let mut s = defaults();
        s.recording_mode = "push_to_talk".to_string();
        let json = serde_json::to_string(&s).expect("serialize");
        assert!(json.contains("\"recordingMode\""));
        let restored: VoiceFlowSettings = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(restored.recording_mode, "push_to_talk");
    }

    #[test]
    fn settings_missing_recording_mode_defaults_to_toggle() {
        // Older config.json files have no recordingMode key; serde(default)
        // must fill it in rather than failing to parse.
        let json = r#"{"hotkey":"Ctrl+Shift+Space"}"#;
        let restored: VoiceFlowSettings = serde_json::from_str(json).expect("deserialize");
        assert_eq!(restored.recording_mode, "toggle");
    }

    #[test]
    fn settings_default_language_en() {
        let s = defaults();
        assert_eq!(s.language, "en");
    }

    #[test]
    fn settings_default_model_size_base() {
        let s = defaults();
        assert_eq!(s.model_size, "base");
    }

    #[test]
    fn settings_default_model_path_empty() {
        let s = defaults();
        assert!(s.model_path.is_empty());
    }

    #[test]
    fn settings_default_auto_copy_to_clipboard_true() {
        let s = defaults();
        assert!(s.auto_copy_to_clipboard);
    }

    #[test]
    fn settings_default_sound_feedback_true() {
        let s = defaults();
        assert!(s.sound_feedback);
    }

    #[test]
    fn settings_default_show_overlay_true() {
        let s = defaults();
        assert!(s.show_overlay);
    }

    #[test]
    fn settings_default_overlay_position_cursor() {
        let s = defaults();
        assert_eq!(s.overlay_position, "cursor");
    }

    #[test]
    fn settings_default_start_minimized_true() {
        let s = defaults();
        assert!(s.start_minimized);
    }

    #[test]
    fn settings_default_launch_at_startup_false() {
        let s = defaults();
        assert!(!s.launch_at_startup);
    }

    // ── JSON round-trip serialization ────────────────────────────────────────

    #[test]
    fn settings_serialize_deserialize_round_trip() {
        let original = defaults();
        let json = serde_json::to_string(&original).expect("serialize");
        let restored: VoiceFlowSettings = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(original.hotkey, restored.hotkey);
        assert_eq!(original.microphone_device, restored.microphone_device);
        assert_eq!(original.language, restored.language);
        assert_eq!(original.model_size, restored.model_size);
        assert_eq!(original.auto_copy_to_clipboard, restored.auto_copy_to_clipboard);
        assert_eq!(original.sound_feedback, restored.sound_feedback);
        assert_eq!(original.show_overlay, restored.show_overlay);
        assert_eq!(original.overlay_position, restored.overlay_position);
        assert_eq!(original.start_minimized, restored.start_minimized);
        assert_eq!(original.launch_at_startup, restored.launch_at_startup);
    }

    #[test]
    fn post_processing_serialize_uses_camel_case() {
        let cfg = PostProcessingConfig::default();
        let json = serde_json::to_string(&cfg).expect("serialize");
        // camelCase keys expected (serde rename_all = "camelCase")
        assert!(json.contains("\"autoPunctuation\""));
        assert!(json.contains("\"removeFillerWords\""));
        assert!(json.contains("\"autoCapitalization\""));
    }

    #[test]
    fn settings_serialize_uses_camel_case() {
        let s = defaults();
        let json = serde_json::to_string(&s).expect("serialize");
        assert!(json.contains("\"hotkey\""));
        assert!(json.contains("\"microphoneDevice\""));
        assert!(json.contains("\"modelSize\""));
        assert!(json.contains("\"autoCopyToClipboard\""));
        assert!(json.contains("\"soundFeedback\""));
        assert!(json.contains("\"showOverlay\""));
        assert!(json.contains("\"overlayPosition\""));
        assert!(json.contains("\"startMinimized\""));
        assert!(json.contains("\"launchAtStartup\""));
    }

    #[test]
    fn settings_modified_round_trip() {
        let mut s = defaults();
        s.hotkey = "Ctrl+Alt+V".to_string();
        s.launch_at_startup = true;
        s.model_size = "large".to_string();

        let json = serde_json::to_string(&s).expect("serialize");
        let restored: VoiceFlowSettings = serde_json::from_str(&json).expect("deserialize");

        assert_eq!(restored.hotkey, "Ctrl+Alt+V");
        assert!(restored.launch_at_startup);
        assert_eq!(restored.model_size, "large");
    }

    // ── save() / load() round-trip ───────────────────────────────────────────

    #[test]
    fn save_and_load_round_trip() {
        let tmp = env::temp_dir().join(format!("voiceflow_test_{}", std::process::id()));
        fs::create_dir_all(&tmp).unwrap();

        // Override APPDATA to use tmp dir so config_dir() points there
        env::set_var("APPDATA", tmp.to_str().unwrap());

        let mut s = defaults();
        s.hotkey = "Ctrl+Alt+T".to_string();
        s.launch_at_startup = true;

        save(&s).expect("save failed");
        let loaded = load().expect("load failed");

        assert_eq!(loaded.hotkey, "Ctrl+Alt+T");
        assert!(loaded.launch_at_startup);

        // Cleanup
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn load_creates_default_config_when_file_missing() {
        let tmp = env::temp_dir().join(format!("voiceflow_test_missing_{}", std::process::id()));
        // Ensure the dir does NOT exist yet
        let _ = fs::remove_dir_all(&tmp);

        env::set_var("APPDATA", tmp.to_str().unwrap());

        let loaded = load().expect("load failed");
        assert_eq!(loaded.hotkey, "Ctrl+Shift+Space");

        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn config_file_path_contains_voiceflow_and_config_json() {
        env::set_var("APPDATA", env::temp_dir().to_str().unwrap());
        let path = config_file_path().expect("path");
        let path_str = path.to_string_lossy();
        assert!(path_str.contains("VoiceFlow"));
        assert!(path_str.ends_with("config.json"));
    }
}
