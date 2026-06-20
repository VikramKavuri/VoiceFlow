// Prevents an additional console window on Windows in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod hotkey;
mod models;
mod overlay;
mod settings;
mod sidecar;
mod tray;

use sidecar::SidecarManager;
use tauri::{Listener, Manager};

// ── Tauri Commands ──────────────────────────────────────────────────────────

/// Get the current application state (idle, recording, processing, error).
/// Used by overlay/frontend windows to sync state on mount.
#[tauri::command]
fn get_app_state(app: tauri::AppHandle) -> String {
    let tray_state = app.state::<tray::TrayState>();
    match tray_state.get() {
        tray::AppState::Idle => "idle".to_string(),
        tray::AppState::Recording => "recording".to_string(),
        tray::AppState::Processing => "processing".to_string(),
        tray::AppState::Error => "error".to_string(),
    }
}

/// Toggle recording on/off. Called from the frontend or hotkey.
#[tauri::command]
fn toggle_recording(app: tauri::AppHandle) -> Result<String, String> {
    tray::toggle_recording(&app)
}

/// Get the current application settings.
#[tauri::command]
fn get_settings(
    settings_state: tauri::State<'_, std::sync::Mutex<settings::VoiceFlowSettings>>,
) -> Result<settings::VoiceFlowSettings, String> {
    let s = settings_state
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?;
    Ok(s.clone())
}

/// Update application settings. Persists to disk and applies changes.
///
/// The parameter is named `settings` to match the frontend invoke key
/// (Tauri v2 applies camelCase conversion on parameter names).
#[tauri::command]
fn update_settings(
    app: tauri::AppHandle,
    settings: crate::settings::VoiceFlowSettings,
) -> Result<(), String> {
    // Save to disk
    crate::settings::save(&settings)?;

    // Check if hotkey changed and update accordingly
    let settings_state = app.state::<std::sync::Mutex<crate::settings::VoiceFlowSettings>>();
    let mut current = settings_state
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?;

    let hotkey_changed = current.hotkey != settings.hotkey;

    // Update in-memory state
    *current = settings.clone();

    // Drop the lock before any further work (hotkey re-registration,
    // sidecar command) to avoid holding it longer than necessary.
    drop(current);

    // Re-register hotkey if it changed
    if hotkey_changed {
        hotkey::update_hotkey(&app, &settings.hotkey)?;
    }

    // Send updated settings to sidecar, including post-processing toggles.
    // Field names are mapped to the sidecar's PostProcessConfig names.
    let sidecar_mgr = app.state::<SidecarManager>();
    let settings_cmd = serde_json::json!({
        "command": "update_settings",
        "settings": {
            "microphone": settings.microphone_device,
            "post_processing": {
                "remove_fillers": settings.post_processing.remove_filler_words,
                "fix_punctuation": settings.post_processing.auto_punctuation,
                "llm_enabled": settings.post_processing.llm_enabled,
                "llm_model_path": settings.post_processing.llm_model_path,
                "llm_context_enabled": settings.post_processing.llm_context_enabled,
                "itn_enabled": settings.post_processing.itn_enabled,
                "custom_vocabulary_path": settings.post_processing.custom_vocabulary_path,
            }
        }
    });
    sidecar_mgr.send_command(&settings_cmd)?;

    Ok(())
}

#[tauri::command]
fn set_microphone(app: tauri::AppHandle, microphone: String) -> Result<(), String> {
    let settings_state = app.state::<std::sync::Mutex<crate::settings::VoiceFlowSettings>>();
    let mut current = settings_state
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?;

    current.microphone_device = microphone.clone();
    let updated = current.clone();
    drop(current);

    crate::settings::save(&updated)?;

    let sidecar_mgr = app.state::<SidecarManager>();
    let settings_cmd = serde_json::json!({
        "command": "update_settings",
        "settings": {
            "microphone": microphone
        }
    });
    sidecar_mgr.send_command(&settings_cmd)?;

    Ok(())
}

/// Ask the sidecar for the list of available microphones.
#[tauri::command]
fn list_microphones(app: tauri::AppHandle) -> Result<(), String> {
    let sidecar_mgr = app.state::<SidecarManager>();
    let cmd = serde_json::json!({ "command": "list_microphones" });
    sidecar_mgr.send_command(&cmd)?;
    // The sidecar will respond with a "microphones" event via stdout,
    // which gets emitted as "microphones-list" to the frontend.
    Ok(())
}

// ── Application Setup ───────────────────────────────────────────────────────

fn main() {
    tauri::Builder::default()
        // ── Plugins ─────────────────────────────────────────────────────
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec![]),
        ))
        // ── Commands ────────────────────────────────────────────────────
        .invoke_handler(tauri::generate_handler![
            get_app_state,
            toggle_recording,
            get_settings,
            update_settings,
            set_microphone,
            list_microphones,
            models::needs_setup,
            models::start_model_setup,
        ])
        // ── Setup ───────────────────────────────────────────────────────
        .setup(|app| {
            // Load settings
            let loaded_settings = settings::load().unwrap_or_else(|e| {
                eprintln!("[setup] Failed to load settings, using defaults: {}", e);
                settings::defaults()
            });
            let hotkey_str = loaded_settings.hotkey.clone();

            // Manage state
            app.manage(std::sync::Mutex::new(loaded_settings));
            app.manage(tray::TrayState::new());

            // Create and manage sidecar
            let sidecar_mgr = SidecarManager::new();
            app.manage(sidecar_mgr);

            // Build system tray
            let tray_menu =
                tray::build_tray_menu(&app.handle()).expect("Failed to build tray menu");

            let app_handle_tray = app.handle().clone();
            let app_handle_menu = app.handle().clone();

            tauri::tray::TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().cloned().unwrap())
                .tooltip("VoiceFlow Transcriptor")
                .menu(&tray_menu)
                .on_tray_icon_event(move |_tray, event| {
                    tray::handle_tray_event(&app_handle_tray, event);
                })
                .on_menu_event(move |_app, event| {
                    tray::handle_menu_event(&app_handle_menu, event);
                })
                .build(app)
                .expect("Failed to create tray icon");

            // Enable launch-on-login so Ctrl+Shift+Space is always available.
            use tauri_plugin_autostart::ManagerExt;
            let autostart = app.autolaunch();
            if let Ok(false) = autostart.is_enabled() {
                let _ = autostart.enable();
            }

            if models::needs_setup_internal() {
                // First run: don't start the pipeline yet. The frontend setup
                // screen calls `start_model_setup`; the app is restarted/loaded
                // normally once models exist. We still register the hotkey lazily
                // after setup via the frontend reloading the window.
                eprintln!("[setup] models missing — entering first-run setup");
                // Show the settings window so the user sees the setup screen.
                if let Some(w) = app.get_webview_window("settings") {
                    let _ = w.show();
                }
            } else {
                let sidecar_state = app.state::<SidecarManager>();
                if let Err(e) = sidecar_state.spawn(&app.handle()) {
                    eprintln!("[setup] Failed to spawn sidecar: {}", e);
                }
                if let Err(e) = hotkey::register_hotkey(&app.handle(), &hotkey_str) {
                    eprintln!("[setup] Failed to register hotkey: {}", e);
                }
            }

            // Create overlay window (hidden by default)
            if let Err(e) = overlay::create_overlay(&app.handle()) {
                eprintln!("[setup] Failed to create overlay: {}", e);
            }

            // Listen for the final delivery event to reset tray state only after
            // the final clipboard copy and paste attempt have completed.
            let app_handle_events = app.handle().clone();
            app.listen("final-delivery", move |_event| {
                let tray_state = app_handle_events.state::<tray::TrayState>();
                tray_state.set(tray::AppState::Idle);
                let _ = tray::update_tray_icon(&app_handle_events, tray::AppState::Idle);
            });

            let app_handle_err = app.handle().clone();
            app.listen("error", move |_event| {
                let tray_state = app_handle_err.state::<tray::TrayState>();
                tray_state.set(tray::AppState::Error);
                let _ = tray::update_tray_icon(&app_handle_err, tray::AppState::Error);
            });

            // After first-run download completes, spawn the sidecar + register hotkey.
            let app_handle_setup = app.handle().clone();
            app.listen("setup-complete", move |_event| {
                let mgr = app_handle_setup.state::<SidecarManager>();
                if let Err(e) = mgr.spawn(&app_handle_setup) {
                    eprintln!("[setup-complete] spawn failed: {}", e);
                }
                // Read the hotkey into an owned String in a tight scope so the
                // State + MutexGuard borrows are fully released before we call
                // register_hotkey (which borrows app_handle_setup again).
                let hk: Option<String> = {
                    let settings_state = app_handle_setup
                        .state::<std::sync::Mutex<crate::settings::VoiceFlowSettings>>();
                    let hk = settings_state.lock().ok().map(|g| g.hotkey.clone());
                    hk
                };
                if let Some(hk) = hk {
                    let _ = hotkey::register_hotkey(&app_handle_setup, &hk);
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Error while running VoiceFlow Transcriptor");
}
