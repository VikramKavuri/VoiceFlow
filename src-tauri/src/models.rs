//! First-run model setup: detect whether the model files exist in the
//! writable appdata dir, and run the sidecar in `--setup` mode while
//! streaming progress events to the frontend.

use serde_json::Value;
use std::path::PathBuf;
use tauri::{AppHandle, Emitter};
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

/// `%LOCALAPPDATA%\VoiceFlow\models` (matches sidecar `model_paths.models_root`).
fn models_root() -> Option<PathBuf> {
    let base = std::env::var("LOCALAPPDATA").ok()?;
    Some(PathBuf::from(base).join("VoiceFlow").join("models"))
}

/// The sentinel files that must all exist for the app to run offline.
fn required_files(root: &PathBuf) -> Vec<PathBuf> {
    vec![
        root.join("parakeet-tdt-0.6b-v3-onnx").join("encoder-model.int8.onnx"),
        root.join("parakeet-tdt-0.6b-v3-onnx").join("decoder_joint-model.int8.onnx"),
        root.join("llama-3.2-3b-finetuned-q4_k_m").join("llama-3.2-3b-finetuned.Q4_K_M.gguf"),
        root.join("lm").join("3gram-pruned.arpa"),
    ]
}

/// True when any required model file is missing → first-run setup needed.
pub fn needs_setup_internal() -> bool {
    match models_root() {
        Some(root) => required_files(&root).iter().any(|p| !p.exists()),
        None => true,
    }
}

#[tauri::command]
pub fn needs_setup() -> bool {
    needs_setup_internal()
}

/// Spawn the sidecar in `--setup` mode and forward each JSON progress line to
/// the frontend as a `setup-progress` event. Emits `setup-complete` /
/// `setup-failed` at the end.
#[tauri::command]
pub async fn start_model_setup(app: AppHandle) -> Result<(), String> {
    let shell = app.shell();

    #[cfg(debug_assertions)]
    let (mut rx, _child) = {
        let sidecar_main = concat!(env!("CARGO_MANIFEST_DIR"), "/../sidecar/main.py");
        shell
            .command("python")
            .args(["-u", sidecar_main, "--setup"])
            .spawn()
            .map_err(|e| format!("spawn python --setup: {}", e))?
    };

    #[cfg(not(debug_assertions))]
    let (mut rx, _child) = shell
        .sidecar("voiceflow-sidecar")
        .map_err(|e| format!("sidecar cmd: {}", e))?
        .args(["--setup"])
        .spawn()
        .map_err(|e| format!("spawn sidecar --setup: {}", e))?;

    while let Some(event) = rx.recv().await {
        if let CommandEvent::Stdout(line) = event {
            let s = String::from_utf8_lossy(&line);
            let trimmed = s.trim();
            if trimmed.is_empty() {
                continue;
            }
            if let Ok(val) = serde_json::from_str::<Value>(trimmed) {
                let _ = app.emit("setup-progress", &val);
                let ev = val.get("event").and_then(|v| v.as_str()).unwrap_or("");
                if ev == "setup_error" {
                    let _ = app.emit("setup-failed", &val);
                    return Err(val
                        .get("message")
                        .and_then(|v| v.as_str())
                        .unwrap_or("setup failed")
                        .to_string());
                }
            }
        }
    }

    let _ = app.emit("setup-complete", ());
    Ok(())
}
