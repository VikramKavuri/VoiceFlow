use tauri::{AppHandle, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

use crate::tray;

/// Read the configured recording mode ("toggle" or "push_to_talk").
/// Falls back to "toggle" if settings can't be read.
fn is_push_to_talk(app: &AppHandle) -> bool {
    let state = app.state::<std::sync::Mutex<crate::settings::VoiceFlowSettings>>();
    state
        .lock()
        .map(|s| s.recording_mode == "push_to_talk")
        .unwrap_or(false)
}

/// Parse a hotkey string (e.g. "Ctrl+Shift+Space") into a Shortcut.
fn parse_shortcut(hotkey: &str) -> Result<Shortcut, String> {
    hotkey
        .parse::<Shortcut>()
        .map_err(|e| format!("Failed to parse hotkey '{}': {}", hotkey, e))
}

/// Register the global hotkey from settings. When pressed, it toggles recording.
pub fn register_hotkey(app: &AppHandle, hotkey_str: &str) -> Result<(), String> {
    let shortcut = parse_shortcut(hotkey_str)?;
    let global_shortcut = app.global_shortcut();

    // Unregister any existing shortcuts first
    let _ = global_shortcut.unregister_all();

    let app_handle = app.clone();
    global_shortcut
        .on_shortcut(shortcut, move |_app, _shortcut, event| {
            // In toggle mode, a key-down flips recording on/off and key-up is
            // ignored. In push-to-talk mode, key-down starts recording and
            // key-up stops it (hold to talk).
            let push_to_talk = is_push_to_talk(&app_handle);
            match event.state {
                ShortcutState::Pressed => {
                    let result = if push_to_talk {
                        tray::begin_recording_if_idle(&app_handle)
                    } else {
                        tray::toggle_recording(&app_handle)
                    };
                    if let Err(e) = result {
                        eprintln!("[hotkey] press handler failed: {}", e);
                    }
                }
                ShortcutState::Released => {
                    if push_to_talk {
                        if let Err(e) = tray::end_recording_if_active(&app_handle) {
                            eprintln!("[hotkey] release handler failed: {}", e);
                        }
                    }
                }
            }
        })
        .map_err(|e| format!("Failed to register hotkey: {}", e))?;

    println!("[hotkey] Registered global hotkey: {}", hotkey_str);
    Ok(())
}

/// Unregister all global shortcuts and re-register with a new hotkey.
pub fn update_hotkey(app: &AppHandle, new_hotkey: &str) -> Result<(), String> {
    let global_shortcut = app.global_shortcut();
    global_shortcut
        .unregister_all()
        .map_err(|e| format!("Failed to unregister shortcuts: {}", e))?;
    register_hotkey(app, new_hotkey)
}
