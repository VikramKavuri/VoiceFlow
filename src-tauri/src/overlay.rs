use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};

/// Create the recording overlay window.
/// The overlay is frameless, always-on-top, transparent, and click-through.
pub fn create_overlay(app: &AppHandle) -> Result<(), String> {
    // Don't create if it already exists
    if app.get_webview_window("overlay").is_some() {
        return Ok(());
    }

    WebviewWindowBuilder::new(app, "overlay", WebviewUrl::App("/overlay".into()))
        .title("VoiceFlow Overlay")
        .inner_size(560.0, 640.0)
        .decorations(false)
        .always_on_top(true)
        .transparent(true)
        .skip_taskbar(true)
        .resizable(false)
        .visible(false)
        .build()
        .map_err(|e| format!("Failed to create overlay window: {}", e))?;

    Ok(())
}

/// Show the overlay window positioned near the current cursor location.
pub fn show_overlay(app: &AppHandle) -> Result<(), String> {
    // Create the overlay if it doesn't exist
    create_overlay(app)?;

    if let Some(window) = app.get_webview_window("overlay") {
        // Position near the cursor. We use a fixed offset from cursor position.
        // The frontend can reposition if it has access to cursor coordinates.
        let _ = window.center();
        window
            .show()
            .map_err(|e| format!("Failed to show overlay: {}", e))?;
        // NOTE: Do NOT call set_focus() here. The overlay must not steal
        // focus from the user's active text box, otherwise live text
        // injection (Ctrl+V) would paste into the overlay instead of the
        // target application.
    }

    Ok(())
}
