use serde_json::Value;
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Manages the lifecycle of the Python sidecar process.
pub struct SidecarManager {
    child: Mutex<Option<CommandChild>>,
}

impl SidecarManager {
    /// Create a new SidecarManager with no child process.
    pub fn new() -> Self {
        Self {
            child: Mutex::new(None),
        }
    }

    /// Spawn the sidecar process using tauri-plugin-shell.
    /// Reads JSON lines from stdout and emits corresponding Tauri events.
    pub fn spawn(&self, app: &AppHandle) -> Result<(), String> {
        // Kill any existing sidecar first
        self.kill()?;

        let shell = app.shell();

        // In debug mode, run Python directly from source for faster iteration.
        // In release mode, use the bundled PyInstaller sidecar.
        #[cfg(debug_assertions)]
        let (mut rx, child) = {
            // Use CARGO_MANIFEST_DIR at compile time to find the sidecar source
            let sidecar_main = concat!(env!("CARGO_MANIFEST_DIR"), "/../sidecar/main.py");
            eprintln!("[sidecar] DEV MODE: running Python from {}", sidecar_main);
            shell
                .command("python")
                .args(["-u", sidecar_main])
                .spawn()
                .map_err(|e| format!("Failed to spawn Python sidecar: {}", e))?
        };

        #[cfg(not(debug_assertions))]
        let (mut rx, child) = shell
            .sidecar("voiceflow-sidecar")
            .map_err(|e| format!("Failed to create sidecar command: {}", e))?
            .spawn()
            .map_err(|e| format!("Failed to spawn sidecar: {}", e))?;

        // Store the child handle
        {
            let mut guard = self.child.lock().map_err(|e| format!("Lock error: {}", e))?;
            *guard = Some(child);
        }

        // Spawn a task to read events from the sidecar's stdout. Track
        // repeated malformed JSON lines and attempt an automatic restart
        // with exponential backoff if the sidecar dies or becomes noisy.
        let app_handle = app.clone();
        tauri::async_runtime::spawn(async move {
            let mut malformed_count: usize = 0;
            while let Some(event) = rx.recv().await {
                match event {
                    CommandEvent::Stdout(line) => {
                        let line_str = String::from_utf8_lossy(&line);
                        let trimmed = line_str.trim();
                        // Try to parse; if parsing fails increment counter and
                        // restart if we see too many malformed lines.
                        if trimmed.is_empty() {
                            // ignore
                        } else if serde_json::from_str::<serde_json::Value>(trimmed).is_err() {
                            malformed_count += 1;
                            eprintln!("[sidecar] Malformed JSON (count={}): {}", malformed_count, trimmed);
                            let _ = app_handle.emit("error", format!("Sidecar sent malformed JSON (count={}): {}", malformed_count, &trimmed[..trimmed.len().min(120)]));
                            if malformed_count >= 5 {
                                // Attempt restart in background and stop current reader.
                                let app_clone = app_handle.clone();
                                tauri::async_runtime::spawn(async move {
                                    let mut attempt: u32 = 0;
                                    loop {
                                        attempt += 1;
                                        let wait = std::time::Duration::from_secs(std::cmp::min(2u64.pow(attempt), 30));
                                        let restart_result = {
                                            let mgr = app_clone.state::<crate::sidecar::SidecarManager>();
                                            mgr.restart(&app_clone)
                                        };
                                        match restart_result {
                                            Ok(_) => {
                                                let _ = app_clone.emit("info", "Sidecar restarted after malformed output");
                                                break;
                                            }
                                            Err(e) => {
                                                eprintln!("[sidecar] restart attempt {} failed: {}", attempt, e);
                                                if attempt >= 5 {
                                                    let _ = app_clone.emit("error", format!("Sidecar restart failed after {} attempts", attempt));
                                                    break;
                                                }
                                                std::thread::sleep(wait);
                                            }
                                        }
                                    }
                                });
                                break;
                            }
                        } else {
                            // Valid JSON line — reset malformed counter and handle normally.
                            malformed_count = 0;
                            handle_sidecar_output(&app_handle, trimmed);
                        }
                    }
                    CommandEvent::Stderr(line) => {
                        let line_str = String::from_utf8_lossy(&line);
                        eprintln!("[sidecar stderr] {}", line_str.trim());
                    }
                    CommandEvent::Error(err) => {
                        eprintln!("[sidecar error] {}", err);
                        let _ = app_handle.emit("error", format!("Sidecar error: {}", err));
                    }
                    CommandEvent::Terminated(status) => {
                        eprintln!("[sidecar terminated] {:?}", status);
                        let _ = app_handle.emit(
                            "error",
                            format!("Sidecar terminated with status: {:?}", status),
                        );

                        // Try to restart in background with exponential backoff.
                        let app_clone = app_handle.clone();
                        tauri::async_runtime::spawn(async move {
                            let mut attempt: u32 = 0;
                            loop {
                                attempt += 1;
                                let restart_result = {
                                    let mgr = app_clone.state::<crate::sidecar::SidecarManager>();
                                    mgr.restart(&app_clone)
                                };
                                match restart_result {
                                    Ok(_) => {
                                        let _ = app_clone.emit("info", "Sidecar restarted after termination");
                                        break;
                                    }
                                    Err(e) => {
                                        eprintln!("[sidecar] restart attempt {} failed: {}", attempt, e);
                                        if attempt >= 6 {
                                            let _ = app_clone.emit("error", format!("Sidecar restart failed after {} attempts", attempt));
                                            break;
                                        }
                                        let wait = std::time::Duration::from_secs(std::cmp::min(2u64.pow(attempt), 30));
                                        std::thread::sleep(wait);
                                    }
                                }
                            }
                        });

                        break;
                    }
                    _ => {}
                }
            }
        });

        Ok(())
    }

    /// Send a JSON command to the sidecar via stdin.
    pub fn send_command(&self, command: &Value) -> Result<(), String> {
        let mut guard = self.child.lock().map_err(|e| format!("Lock error: {}", e))?;
        if let Some(child) = guard.as_mut() {
            let json_line = serde_json::to_string(command)
                .map_err(|e| format!("Failed to serialize command: {}", e))?;
            let msg = format!("{}\n", json_line);
            child
                .write(msg.as_bytes())
                .map_err(|e| format!("Failed to write to sidecar stdin: {}", e))?;
            Ok(())
        } else {
            Err("Sidecar is not running".to_string())
        }
    }

    /// Kill the sidecar process.
    pub fn kill(&self) -> Result<(), String> {
        let mut guard = self.child.lock().map_err(|e| format!("Lock error: {}", e))?;
        if let Some(child) = guard.take() {
            child
                .kill()
                .map_err(|e| format!("Failed to kill sidecar: {}", e))?;
        }
        Ok(())
    }

    /// Restart the sidecar: kill the current process and spawn a new one.
    pub fn restart(&self, app: &AppHandle) -> Result<(), String> {
        self.kill()?;
        self.spawn(app)
    }
}

/// Parse a JSON line from sidecar stdout and emit the appropriate Tauri event.
fn handle_sidecar_output(app: &AppHandle, line: &str) {
    if line.is_empty() {
        return;
    }

    let parsed: Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("[sidecar] Failed to parse JSON: {} - line: {}", e, line);
            return;
        }
    };

    let event_type = parsed
        .get("event")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown");

    // The sidecar sends all fields at the top level (e.g. {"event": "final_transcript", "text": "..."}).
    // Extract everything except "event" as the payload.
    let payload = if let Some(obj) = parsed.as_object() {
        let mut p = obj.clone();
        p.remove("event");
        Value::Object(p)
    } else {
        Value::Null
    };

    match event_type {
        "recording_started" => {
            let _ = app.emit("recording-started", &payload);
        }
        "recording_stopped" => {
            let _ = app.emit("recording-stopped", &payload);
        }
        "partial_transcript" => {
            let _ = app.emit("partial-transcript", &payload);
        }
        "final_transcript" => {
            let _ = app.emit("final-transcript", &payload);
        }
        "final_delivery" => {
            let _ = app.emit("final-delivery", &payload);
        }
        "auto_stop_triggered" => {
            // Auto-stop: update tray state and hide overlay just like a manual stop
            let tray_state = app.state::<crate::tray::TrayState>();
            tray_state.set(crate::tray::AppState::Processing);
            let _ = crate::tray::update_tray_icon(app, crate::tray::AppState::Processing);
            let _ = app.emit("recording-stopped", &payload);
        }
        "vad_speech_detected" => {
            let _ = app.emit("vad-speech-detected", &payload);
        }
        "transcription_warning" => {
            // Low-confidence ("I couldn't hear you well") warning. The sidecar
            // also piggybacks this onto final_delivery, but forwarding the
            // standalone event lets the frontend's transcription-warning
            // listener fire directly.
            let _ = app.emit("transcription-warning", &payload);
        }
        "active_microphone" => {
            let _ = app.emit("active-microphone", &payload);
        }
        "error" => {
            let _ = app.emit("error", &payload);
        }
        "microphones" | "device_list" => {
            let _ = app.emit("microphones-list", &payload);
        }
        "ready" => {
            eprintln!("[sidecar] Sidecar ready");
            // Send all saved settings to sidecar on startup
            let settings_state = app.state::<std::sync::Mutex<crate::settings::VoiceFlowSettings>>();
            let s = settings_state.lock().unwrap();
            let sidecar_mgr = app.state::<crate::sidecar::SidecarManager>();
            let cmd = serde_json::json!({
                "command": "update_settings",
                "settings": {
                    "microphone": s.microphone_device,
                    "post_processing": {
                        "remove_fillers": s.post_processing.remove_filler_words,
                        "fix_punctuation": s.post_processing.auto_punctuation,
                        "llm_enabled": s.post_processing.llm_enabled,
                        "llm_model_path": s.post_processing.llm_model_path,
                        "llm_context_enabled": s.post_processing.llm_context_enabled,
                        "itn_enabled": s.post_processing.itn_enabled,
                        "custom_vocabulary_path": s.post_processing.custom_vocabulary_path,
                    }
                }
            });
            drop(s);
            let _ = sidecar_mgr.send_command(&cmd);
            eprintln!("[sidecar] Sent saved settings to sidecar");
        }
        "settings_updated" | "shutdown_complete" => {
            // Internal events - no need to forward to frontend
        }
        other => {
            eprintln!("[sidecar] Unknown event type: {}", other);
        }
    }
}
