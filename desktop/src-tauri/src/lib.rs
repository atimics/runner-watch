use std::fs;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;
use tauri::{AppHandle, Manager, RunEvent, State};
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use url::Url;
use uuid::Uuid;

const SCANNER_STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const EXTERNAL_HOSTS: &[&str] = &[
    "openrouter.ai",
    "rati.chat",
    "runners.rati.chat",
    "sports.rati.chat",
    "sec.gov",
    "www.sec.gov",
    "legal.yahoo.com",
    "apewisdom.io",
    "www.gdeltproject.org",
    "bsky.social",
    "massive.com",
    "www.nasdaqtrader.com",
    "www.nasdaq.com",
    "fintel.io",
    "the-odds-api.com",
    "disneytermsofuse.com",
    "github.com",
];

#[derive(Clone, Copy, Default, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum ScannerStatus {
    #[default]
    Starting,
    Ready,
    Failed,
    Stopped,
}

#[derive(Clone, Default)]
struct ScannerSnapshot {
    status: ScannerStatus,
    url: String,
    token: String,
    error: String,
}

#[derive(Default)]
struct ScannerRuntime {
    snapshot: ScannerSnapshot,
    child: Option<CommandChild>,
}

struct DesktopState {
    scanner: Mutex<ScannerRuntime>,
    quitting: AtomicBool,
}

impl DesktopState {
    fn new() -> Self {
        Self {
            scanner: Mutex::new(ScannerRuntime::default()),
            quitting: AtomicBool::new(false),
        }
    }

    fn snapshot(&self) -> ScannerSnapshot {
        self.scanner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .snapshot
            .clone()
    }

    fn update_snapshot(&self, update: impl FnOnce(&mut ScannerSnapshot)) {
        let mut runtime = self
            .scanner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        update(&mut runtime.snapshot);
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimePayload {
    app_version: String,
    node_url: String,
    node_token: String,
    platform: &'static str,
    scanner_error: String,
    scanner_state: ScannerStatus,
}

#[tauri::command]
fn desktop_runtime(app: AppHandle, state: State<'_, DesktopState>) -> RuntimePayload {
    let scanner = state.snapshot();
    RuntimePayload {
        app_version: app.package_info().version.to_string(),
        node_url: scanner.url,
        node_token: scanner.token,
        platform: platform_name(),
        scanner_error: scanner.error,
        scanner_state: scanner.status,
    }
}

#[tauri::command]
fn open_external(app: AppHandle, url: String) -> Result<bool, String> {
    let safe = safe_external_url(&url)
        .ok_or_else(|| "This external address is not allowed".to_string())?;
    app.opener()
        .open_url(safe.as_str(), None::<&str>)
        .map_err(|error| format!("Could not open this address: {error}"))?;
    Ok(true)
}

fn platform_name() -> &'static str {
    match std::env::consts::OS {
        "macos" => "darwin",
        value => value,
    }
}

fn is_https_host(url: &Url, allowed_hosts: &[&str]) -> bool {
    url.scheme() == "https"
        && url.port_or_known_default() == Some(443)
        && url.username().is_empty()
        && url.password().is_none()
        && url
            .host_str()
            .is_some_and(|host| allowed_hosts.contains(&host))
}

fn safe_external_url(value: &str) -> Option<Url> {
    let url = Url::parse(value).ok()?;
    is_https_host(&url, EXTERNAL_HOSTS).then_some(url)
}

fn initialize_scanner(app: AppHandle) {
    let configured_url = std::env::var("RATI_NODE_URL")
        .ok()
        .filter(|value| !value.trim().is_empty());
    if let Some(url) = configured_url {
        let token = std::env::var("RATI_NODE_TOKEN").unwrap_or_default();
        app.state::<DesktopState>().update_snapshot(|snapshot| {
            snapshot.status = ScannerStatus::Ready;
            snapshot.url = url;
            snapshot.token = token;
            snapshot.error.clear();
        });
        return;
    }

    if cfg!(debug_assertions) {
        let token = std::env::var("RATI_NODE_TOKEN").unwrap_or_default();
        app.state::<DesktopState>().update_snapshot(|snapshot| {
            snapshot.status = ScannerStatus::Ready;
            snapshot.url = "http://127.0.0.1:8787".to_string();
            snapshot.token = token;
            snapshot.error.clear();
        });
        return;
    }

    tauri::async_runtime::spawn(start_bundled_scanner(app));
}

async fn start_bundled_scanner(app: AppHandle) {
    let data_dir = match app.path().app_data_dir() {
        Ok(path) => path,
        Err(error) => {
            fail_scanner(&app, format!("Could not find the app data folder: {error}"));
            return;
        }
    };
    if let Err(error) = fs::create_dir_all(&data_dir) {
        fail_scanner(
            &app,
            format!("Could not create the app data folder: {error}"),
        );
        return;
    }
    let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
    let command = match app.shell().sidecar("rati-scanner") {
        Ok(command) => command,
        Err(error) => {
            fail_scanner(&app, format!("The bundled scanner was not found: {error}"));
            return;
        }
    };
    let spawn = command
        .env("DATABASE_PATH", data_dir.join("rati-scanner.db"))
        .env("RATI_NODE_TOKEN", &token)
        .env("RATI_NODE_HOST", "127.0.0.1")
        .env("RATI_NODE_MODE", "local")
        .env("RATI_NODE_PORT", "0")
        .spawn();
    let (mut events, child) = match spawn {
        Ok(value) => value,
        Err(error) => {
            fail_scanner(&app, format!("The local scanner could not start: {error}"));
            return;
        }
    };
    {
        let state = app.state::<DesktopState>();
        let mut runtime = state
            .scanner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        runtime.snapshot.token = token;
        runtime.child = Some(child);
    }

    let deadline = tokio::time::Instant::now() + SCANNER_STARTUP_TIMEOUT;
    let ready = loop {
        let event = match tokio::time::timeout_at(deadline, events.recv()).await {
            Ok(Some(event)) => event,
            Ok(None) => break false,
            Err(_) => {
                fail_scanner(
                    &app,
                    "The local scanner did not start within 60 seconds.".to_string(),
                );
                stop_scanner(&app);
                return;
            }
        };
        match event {
            CommandEvent::Stdout(bytes) => {
                if let Some(url) = ready_url(&bytes) {
                    app.state::<DesktopState>().update_snapshot(|snapshot| {
                        snapshot.status = ScannerStatus::Ready;
                        snapshot.url = url;
                        snapshot.error.clear();
                    });
                    break true;
                }
            }
            CommandEvent::Stderr(bytes) => remember_scanner_error(&app, &bytes),
            CommandEvent::Terminated(_) => break false,
            _ => {}
        }
    };

    if !ready {
        let detail = app.state::<DesktopState>().snapshot().error;
        let message = if detail.is_empty() {
            "The local scanner stopped during startup.".to_string()
        } else {
            detail
        };
        fail_scanner(&app, message);
        return;
    }

    while let Some(event) = events.recv().await {
        match event {
            CommandEvent::Stderr(bytes) => remember_scanner_error(&app, &bytes),
            CommandEvent::Terminated(_) => {
                let state = app.state::<DesktopState>();
                let mut runtime = state
                    .scanner
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                runtime.child = None;
                if !state.quitting.load(Ordering::Relaxed) {
                    runtime.snapshot.status = ScannerStatus::Stopped;
                    runtime.snapshot.url.clear();
                    runtime.snapshot.error = "The local scanner stopped unexpectedly.".to_string();
                }
                return;
            }
            _ => {}
        }
    }
}

fn ready_url(bytes: &[u8]) -> Option<String> {
    let text = String::from_utf8_lossy(bytes);
    for line in text.lines() {
        let Ok(event) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if event.get("event").and_then(Value::as_str) == Some("ready") {
            let url = event.get("url").and_then(Value::as_str)?;
            let parsed = Url::parse(url).ok()?;
            let loopback = parsed.scheme() == "http"
                && parsed.host_str() == Some("127.0.0.1")
                && parsed.port().is_some();
            if loopback {
                return Some(parsed.to_string().trim_end_matches('/').to_string());
            }
        }
    }
    None
}

fn remember_scanner_error(app: &AppHandle, bytes: &[u8]) {
    let detail = String::from_utf8_lossy(bytes).trim().to_string();
    if !detail.is_empty() {
        app.state::<DesktopState>().update_snapshot(|snapshot| {
            snapshot.error = detail.chars().take(2_000).collect();
        });
    }
}

fn fail_scanner(app: &AppHandle, message: String) {
    app.state::<DesktopState>().update_snapshot(|snapshot| {
        snapshot.status = ScannerStatus::Failed;
        snapshot.url.clear();
        snapshot.error = message;
    });
}

fn stop_scanner(app: &AppHandle) {
    let state = app.state::<DesktopState>();
    let child = state
        .scanner
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .child
        .take();
    if let Some(child) = child {
        let _ = child.kill();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(DesktopState::new())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![desktop_runtime, open_external])
        .setup(|app| {
            initialize_scanner(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building RATi Swarm")
        .run(|app, event| {
            if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
                app.state::<DesktopState>()
                    .quitting
                    .store(true, Ordering::Relaxed);
                stop_scanner(app);
            }
        });
}

#[cfg(test)]
mod tests {
    use super::safe_external_url;

    #[test]
    fn external_urls_require_an_allowlisted_https_host() {
        assert!(safe_external_url("https://github.com/atimics/runner-watch").is_some());
        assert!(safe_external_url("http://github.com/atimics/runner-watch").is_none());
        assert!(safe_external_url("https://example.com").is_none());
        assert!(safe_external_url("https://github.com.evil.test").is_none());
    }
}
