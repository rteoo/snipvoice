// The manager is a window, not an app: the Snipvoice engine (Python) spawns
// this process, owns its lifetime, and talks to it with JSON-RPC lines on
// stdin/stdout. Nothing else may write to stdout.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, Write};
use std::sync::Mutex;

use tauri::{AppHandle, Emitter, Manager, State};

const BRIDGE_EVENT: &str = "bridge://message";
// One JSON-RPC message per line; a longer line is a protocol fault, not data.
const MAX_LINE_BYTES: usize = 8 * 1024 * 1024;

#[derive(Default)]
struct Inbox {
    ready: bool,
    pending: Vec<String>,
}

struct Bridge {
    inbox: Mutex<Inbox>,
    stdout: Mutex<std::io::Stdout>,
}

/// Called once by the page after its listener is attached. Returns whatever
/// the engine sent before then, so no message is lost during page load.
#[tauri::command]
fn bridge_ready(bridge: State<'_, Bridge>) -> Vec<String> {
    let mut inbox = bridge.inbox.lock().expect("bridge inbox poisoned");
    inbox.ready = true;
    std::mem::take(&mut inbox.pending)
}

#[tauri::command]
fn bridge_send(bridge: State<'_, Bridge>, line: String) -> Result<(), String> {
    if line.contains('\n') || line.contains('\r') {
        return Err("bridge messages must be single-line JSON".into());
    }
    if line.len() > MAX_LINE_BYTES {
        return Err("bridge message too large".into());
    }
    let mut out = bridge.stdout.lock().expect("bridge stdout poisoned");
    out.write_all(line.as_bytes())
        .and_then(|_| out.write_all(b"\n"))
        .and_then(|_| out.flush())
        .map_err(|error| format!("engine pipe closed: {error}"))
}

#[cfg(windows)]
#[link(name = "user32")]
extern "system" {
    fn AllowSetForegroundWindow(process_id: u32) -> i32;
}

/// The engine shows save/open dialogs on its own Tk thread. Windows only
/// lets the foreground process hand focus on, so the window grants it to the
/// engine just before asking; otherwise the dialog opens behind this window.
#[tauri::command]
fn allow_engine_foreground(pid: u32) -> bool {
    #[cfg(windows)]
    {
        // SAFETY: a plain Win32 call with a process id; no pointers cross the boundary.
        pid != 0 && unsafe { AllowSetForegroundWindow(pid) } != 0
    }
    #[cfg(not(windows))]
    {
        let _ = pid;
        true
    }
}

fn deliver(app: &AppHandle, line: String) {
    let bridge = app.state::<Bridge>();
    let mut inbox = bridge.inbox.lock().expect("bridge inbox poisoned");
    if inbox.ready {
        drop(inbox);
        let _ = app.emit(BRIDGE_EVENT, line);
    } else {
        inbox.pending.push(line);
    }
}

fn read_engine(app: AppHandle) {
    let stdin = std::io::stdin();
    let mut reader = stdin.lock();
    let mut line = String::new();
    loop {
        line.clear();
        match reader.read_line(&mut line) {
            // EOF or a broken pipe means the engine is gone; the window has
            // nothing left to show.
            Ok(0) | Err(_) => break,
            Ok(n) if n > MAX_LINE_BYTES => break,
            Ok(_) => {
                let message = line.trim_end_matches(['\r', '\n']);
                if !message.is_empty() {
                    deliver(&app, message.to_owned());
                }
            }
        }
    }
    app.exit(0);
}

fn main() {
    tauri::Builder::default()
        .manage(Bridge {
            inbox: Mutex::new(Inbox::default()),
            stdout: Mutex::new(std::io::stdout()),
        })
        .invoke_handler(tauri::generate_handler![bridge_ready, bridge_send, allow_engine_foreground])
        .setup(|app| {
            let handle = app.handle().clone();
            std::thread::Builder::new()
                .name("engine-reader".into())
                .spawn(move || read_engine(handle))?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to run the Snipvoice manager");
}
