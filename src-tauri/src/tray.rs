use serde_json::Value;
use std::sync::Mutex;
use std::time::SystemTime;
use tauri::{
    image::Image,
    menu::{Menu, MenuItem},
    tray::TrayIconEvent,
    AppHandle, Emitter, Manager,
};

use crate::overlay;
use crate::settings;
use crate::sidecar::SidecarManager;

/// Represents the current application state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AppState {
    Idle,
    Recording,
    Processing,
    Error,
}

/// Thread-safe wrapper around AppState for Tauri state management.
pub struct TrayState {
    state: Mutex<AppState>,
    session: Mutex<Option<String>>,
}

impl TrayState {
    pub fn new() -> Self {
        Self {
            state: Mutex::new(AppState::Idle),
            session: Mutex::new(None),
        }
    }

    pub fn get(&self) -> AppState {
        *self.state.lock().unwrap()
    }

    pub fn set(&self, new_state: AppState) {
        let mut s = self.state.lock().unwrap();
        *s = new_state;
    }

    pub fn set_session_id(&self, id: Option<String>) {
        let mut g = self.session.lock().unwrap();
        *g = id;
    }

    pub fn take_session_id(&self) -> Option<String> {
        let mut g = self.session.lock().unwrap();
        g.take()
    }

    pub fn get_session_id(&self) -> Option<String> {
        let g = self.session.lock().unwrap();
        g.clone()
    }
}

/// Build the system tray menu.
pub fn build_tray_menu(app: &AppHandle) -> Result<Menu<tauri::Wry>, String> {
    let settings_item = MenuItem::with_id(app, "settings", "Settings", true, None::<&str>)
        .map_err(|e| format!("Failed to create Settings menu item: {}", e))?;

    let pause_item = MenuItem::with_id(app, "pause_resume", "Pause", true, None::<&str>)
        .map_err(|e| format!("Failed to create Pause menu item: {}", e))?;

    let about_item = MenuItem::with_id(app, "about", "About VoiceFlow", true, None::<&str>)
        .map_err(|e| format!("Failed to create About menu item: {}", e))?;

    let quit_item = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)
        .map_err(|e| format!("Failed to create Quit menu item: {}", e))?;

    let menu = Menu::with_items(app, &[&settings_item, &pause_item, &about_item, &quit_item])
        .map_err(|e| format!("Failed to create tray menu: {}", e))?;

    Ok(menu)
}

/// Handle tray icon events (left-click and menu item clicks).
pub fn handle_tray_event(app: &AppHandle, event: TrayIconEvent) {
    match event {
        TrayIconEvent::Click {
            button: tauri::tray::MouseButton::Left,
            button_state: tauri::tray::MouseButtonState::Up,
            ..
        } => {
            // Left-click toggles recording
            if let Err(e) = toggle_recording(app) {
                eprintln!("[tray] Failed to toggle recording: {}", e);
            }
        }
        _ => {}
    }
}

/// Handle menu item clicks from the system tray context menu.
pub fn handle_menu_event(app: &AppHandle, event: tauri::menu::MenuEvent) {
    match event.id().as_ref() {
        "settings" => {
            // Show the settings window
            if let Some(window) = app.get_webview_window("settings") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }
        "pause_resume" => {
            if let Err(e) = toggle_recording(app) {
                eprintln!("[tray] Failed to toggle from menu: {}", e);
            }
        }
        "about" => {
            // Show a simple about dialog by emitting an event
            if let Some(window) = app.get_webview_window("settings") {
                let _ = window.show();
                let _ = window.set_focus();
                let _ = window.emit("show-about", ());
            }
        }
        "quit" => {
            // Kill sidecar before quitting
            let sidecar = app.state::<SidecarManager>();
            let _ = sidecar.kill();
            app.exit(0);
        }
        _ => {}
    }
}

fn send_command_with_restart(app: &AppHandle, command: &Value) -> Result<(), String> {
    let sidecar = app.state::<SidecarManager>();
    if let Err(err) = sidecar.send_command(command) {
        eprintln!("[tray] send_command failed: {}. Attempting restart.", err);
        sidecar
            .restart(app)
            .map_err(|restart_err| format!("Failed to restart sidecar: {}", restart_err))?;
        sidecar
            .send_command(command)
            .map_err(|retry_err| format!("Failed after restart: {}", retry_err))?;
    }
    Ok(())
}

fn start_recording(app: &AppHandle) -> Result<String, String> {
    let tray_state = app.state::<TrayState>();
    let session_id = format!("sess-{}", SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH).unwrap()
        .as_millis());

    let cmd = serde_json::json!({ "command": "start_recording", "session_id": session_id });
    send_command_with_restart(app, &cmd)?;

    tray_state.set(AppState::Recording);
    tray_state.set_session_id(Some(session_id.clone()));
    update_tray_icon(app, AppState::Recording)?;

    let settings_state = app.state::<std::sync::Mutex<settings::VoiceFlowSettings>>();
    if let Ok(s) = settings_state.lock() {
        if s.show_overlay {
            let _ = overlay::show_overlay(app);
        }
    }

    let _ = app.emit("recording-started", serde_json::json!({
        "timestamp": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64,
        "session_id": tray_state.get_session_id()
    }));

    Ok("recording".to_string())
}

fn stop_recording(app: &AppHandle) -> Result<String, String> {
    let tray_state = app.state::<TrayState>();
    let sid = tray_state.take_session_id();
    let cmd = serde_json::json!({ "command": "stop_recording", "session_id": sid });
    send_command_with_restart(app, &cmd)?;

    tray_state.set(AppState::Processing);
    update_tray_icon(app, AppState::Processing)?;
    let _ = app.emit("recording-stopped", serde_json::json!({ "duration_ms": 0, "session_id": sid }));

    Ok("processing".to_string())
}

/// Toggle recording state and send the appropriate command to the sidecar.
pub fn toggle_recording(app: &AppHandle) -> Result<String, String> {
    let tray_state = app.state::<TrayState>();
    match tray_state.get() {
        AppState::Idle | AppState::Error => start_recording(app),
        AppState::Recording => stop_recording(app),
        AppState::Processing => Ok("processing".to_string()),
    }
}

/// Push-to-talk key-down: start recording only if currently idle.
/// Idempotent so OS key-repeat on a held hotkey does not restart the session.
pub fn begin_recording_if_idle(app: &AppHandle) -> Result<String, String> {
    let tray_state = app.state::<TrayState>();
    match tray_state.get() {
        AppState::Idle | AppState::Error => start_recording(app),
        _ => Ok("noop".to_string()),
    }
}

/// Push-to-talk key-up: stop recording only if currently recording.
pub fn end_recording_if_active(app: &AppHandle) -> Result<String, String> {
    let tray_state = app.state::<TrayState>();
    match tray_state.get() {
        AppState::Recording => stop_recording(app),
        _ => Ok("noop".to_string()),
    }
}

// ── Unit Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ── AppState equality ────────────────────────────────────────────────────

    #[test]
    fn app_state_idle_eq() {
        assert_eq!(AppState::Idle, AppState::Idle);
    }

    #[test]
    fn app_state_recording_ne_idle() {
        assert_ne!(AppState::Recording, AppState::Idle);
    }

    #[test]
    fn app_state_processing_ne_recording() {
        assert_ne!(AppState::Processing, AppState::Recording);
    }

    #[test]
    fn app_state_error_ne_idle() {
        assert_ne!(AppState::Error, AppState::Idle);
    }

    #[test]
    fn app_state_copy_is_equal() {
        let s = AppState::Recording;
        let t = s;
        assert_eq!(s, t);
    }

    // ── TrayState::new ───────────────────────────────────────────────────────

    #[test]
    fn tray_state_new_starts_idle() {
        let ts = TrayState::new();
        assert_eq!(ts.get(), AppState::Idle);
    }

    #[test]
    fn tray_state_new_session_id_is_none() {
        let ts = TrayState::new();
        assert!(ts.get_session_id().is_none());
    }

    // ── TrayState::set / get ─────────────────────────────────────────────────

    #[test]
    fn tray_state_set_recording() {
        let ts = TrayState::new();
        ts.set(AppState::Recording);
        assert_eq!(ts.get(), AppState::Recording);
    }

    #[test]
    fn tray_state_set_processing() {
        let ts = TrayState::new();
        ts.set(AppState::Processing);
        assert_eq!(ts.get(), AppState::Processing);
    }

    #[test]
    fn tray_state_set_error() {
        let ts = TrayState::new();
        ts.set(AppState::Error);
        assert_eq!(ts.get(), AppState::Error);
    }

    #[test]
    fn tray_state_set_back_to_idle() {
        let ts = TrayState::new();
        ts.set(AppState::Recording);
        ts.set(AppState::Idle);
        assert_eq!(ts.get(), AppState::Idle);
    }

    #[test]
    fn tray_state_multiple_transitions() {
        let ts = TrayState::new();
        ts.set(AppState::Recording);
        ts.set(AppState::Processing);
        ts.set(AppState::Idle);
        assert_eq!(ts.get(), AppState::Idle);
    }

    // ── TrayState::set_session_id / get_session_id / take_session_id ─────────

    #[test]
    fn tray_state_set_and_get_session_id() {
        let ts = TrayState::new();
        ts.set_session_id(Some("sess-001".to_string()));
        assert_eq!(ts.get_session_id(), Some("sess-001".to_string()));
    }

    #[test]
    fn tray_state_take_session_id_returns_and_clears() {
        let ts = TrayState::new();
        ts.set_session_id(Some("sess-123".to_string()));
        let taken = ts.take_session_id();
        assert_eq!(taken, Some("sess-123".to_string()));
        // After take, it should be None
        assert!(ts.get_session_id().is_none());
    }

    #[test]
    fn tray_state_take_session_id_when_none() {
        let ts = TrayState::new();
        let taken = ts.take_session_id();
        assert!(taken.is_none());
    }

    #[test]
    fn tray_state_get_session_id_does_not_consume() {
        let ts = TrayState::new();
        ts.set_session_id(Some("sess-abc".to_string()));
        let _ = ts.get_session_id();
        // Still there after get
        assert_eq!(ts.get_session_id(), Some("sess-abc".to_string()));
    }

    #[test]
    fn tray_state_set_session_id_none_clears() {
        let ts = TrayState::new();
        ts.set_session_id(Some("sess-xyz".to_string()));
        ts.set_session_id(None);
        assert!(ts.get_session_id().is_none());
    }

    // ── Thread safety (basic): concurrent state reads ────────────────────────

    #[test]
    fn tray_state_concurrent_reads_do_not_panic() {
        use std::sync::Arc;
        use std::thread;

        let ts = Arc::new(TrayState::new());
        ts.set(AppState::Recording);

        let handles: Vec<_> = (0..8)
            .map(|_| {
                let ts_clone = Arc::clone(&ts);
                thread::spawn(move || {
                    let _ = ts_clone.get();
                })
            })
            .collect();

        for h in handles {
            h.join().expect("thread panicked");
        }
    }

    #[test]
    fn tray_state_concurrent_writes_do_not_panic() {
        use std::sync::Arc;
        use std::thread;

        let ts = Arc::new(TrayState::new());

        let handles: Vec<_> = (0..4)
            .map(|i| {
                let ts_clone = Arc::clone(&ts);
                thread::spawn(move || {
                    let state = if i % 2 == 0 { AppState::Idle } else { AppState::Recording };
                    ts_clone.set(state);
                })
            })
            .collect();

        for h in handles {
            h.join().expect("thread panicked");
        }
        // Final state is one of the valid states
        let _ = ts.get();
    }
}

/// Update the tray icon to reflect the current application state.
pub fn update_tray_icon(app: &AppHandle, state: AppState) -> Result<(), String> {
    let tray = app
        .tray_by_id("main")
        .ok_or_else(|| "Tray icon not found".to_string())?;

    let icon_path = match state {
        AppState::Idle => "icons/tray-idle.png",
        AppState::Recording => "icons/tray-recording.png",
        AppState::Processing => "icons/tray-processing.png",
        AppState::Error => "icons/tray-error.png",
    };

    let tooltip = match state {
        AppState::Idle => "VoiceFlow - Ready",
        AppState::Recording => "VoiceFlow - Recording...",
        AppState::Processing => "VoiceFlow - Processing...",
        AppState::Error => "VoiceFlow - Error",
    };

    // Load icon from the app's resource directory
    if let Ok(resource_path) = app.path().resource_dir() {
        let full_path = resource_path.join(icon_path);
        if full_path.exists() {
            if let Ok(icon) = Image::from_path(&full_path) {
                let _ = tray.set_icon(Some(icon));
            }
        }
    }

    let _ = tray.set_tooltip(Some(tooltip));

    Ok(())
}
