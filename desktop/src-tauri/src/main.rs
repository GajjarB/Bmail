// Desktop shell for Bmail.
//
// The shell owns a window and a child process, and nothing else. All the
// application logic lives in the Python core, which this starts as a local
// daemon and then points a webview at. Keeping the Rust this thin is
// deliberate: the core stays importable, scriptable and testable on its own,
// and the shell is a client of it rather than a container for it.
//
// The daemon binds a random loopback port and prints one JSON line with its URL
// and session token. Nothing is ever exposed beyond 127.0.0.1.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

/// The child daemon, kept so it can be stopped when the window closes.
struct Daemon(Mutex<Option<Child>>);

/// How long to wait for the daemon to announce itself before giving up.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(30);

/// Candidate ways to run the core, in order of preference: a bundled sidecar,
/// then `bmail` on PATH, then the module through whichever Python is available.
fn candidates() -> Vec<(String, Vec<String>)> {
    let mut found: Vec<(String, Vec<String>)> = Vec::new();

    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let sidecar = dir.join(if cfg!(windows) { "bmail.exe" } else { "bmail" });
            if sidecar.is_file() {
                found.push((sidecar.to_string_lossy().into_owned(), Vec::new()));
            }
        }
    }
    if let Ok(custom) = std::env::var("BMAIL_BIN") {
        if !custom.is_empty() {
            found.push((custom, Vec::new()));
        }
    }
    found.push(("bmail".into(), Vec::new()));
    for python in ["python3", "python"] {
        found.push((python.into(), vec!["-m".into(), "bmail".into()]));
    }
    found
}

/// Start the daemon and read back the URL it is serving on.
fn start_daemon() -> Result<(String, Child), String> {
    let mut problems: Vec<String> = Vec::new();

    for (program, prefix) in candidates() {
        let mut command = Command::new(&program);
        // The engine is a console program, so that it can print its URL. On
        // Windows that would open a black terminal window beside the app.
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        command
            .args(&prefix)
            // --exit-with-parent is the backstop: the cleanup below handles a
            // normal window close, but only the daemon watching this process can
            // survive the shell being killed outright.
            .args(["serve", "--json", "--port", "0", "--exit-with-parent"])
            // Which process to outlive no longer: this one. The engine cannot
            // work it out itself when it runs under the frozen-app unpacker.
            .env("BMAIL_PARENT_PID", std::process::id().to_string())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(err) => {
                problems.push(format!("{program}: {err}"));
                continue;
            }
        };

        let stdout = match child.stdout.take() {
            Some(stdout) => stdout,
            None => {
                let _ = child.kill();
                problems.push(format!("{program}: no stdout"));
                continue;
            }
        };

        // The daemon prints exactly one JSON line, then serves for ever, so read
        // lines until that line arrives rather than waiting for the process.
        let deadline = Instant::now() + STARTUP_TIMEOUT;
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        let mut url: Option<String> = None;

        while Instant::now() < deadline {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) => break, // the process exited without announcing itself
                Ok(_) => {
                    if let Ok(value) = serde_json::from_str::<serde_json::Value>(line.trim()) {
                        if let Some(found) = value.get("url").and_then(|u| u.as_str()) {
                            url = Some(found.to_string());
                            break;
                        }
                    }
                }
                Err(err) => {
                    problems.push(format!("{program}: {err}"));
                    break;
                }
            }
        }

        match url {
            Some(url) => return Ok((url, child)),
            None => {
                let _ = child.kill();
                let _ = child.wait();
                problems.push(format!("{program}: did not report a URL"));
            }
        }
    }

    Err(format!(
        "could not start the Bmail engine. Tried: {}. Install it with `pip install bmail`, \
         or set BMAIL_BIN to its path.",
        problems.join("; ")
    ))
}

/// Updates, when this build carries an update key. They download quietly in
/// the background and install when the window closes, so nobody's work is
/// interrupted by a restart they did not ask for. Every download is checked
/// against the public key built into the app before it is installed.
#[cfg(feature = "updater")]
mod update {
    use std::sync::Mutex;

    use tauri::{AppHandle, Manager};
    use tauri_plugin_updater::{Update, UpdaterExt};

    #[derive(Default)]
    pub struct Pending(pub Mutex<Option<(Update, Vec<u8>)>>);

    pub fn fetch_in_background(handle: AppHandle) {
        tauri::async_runtime::spawn(async move {
            let updater = match handle.updater() {
                Ok(updater) => updater,
                Err(err) => return eprintln!("bmail-desktop: updater unavailable: {err}"),
            };
            match updater.check().await {
                Ok(Some(update)) => match update.download(|_, _| {}, || {}).await {
                    Ok(bytes) => {
                        eprintln!(
                            "bmail-desktop: {} downloaded; installs on quit",
                            update.version
                        );
                        *handle.state::<Pending>().0.lock().unwrap() = Some((update, bytes));
                    }
                    Err(err) => eprintln!("bmail-desktop: update download failed: {err}"),
                },
                Ok(None) => {}
                // Offline, or GitHub unreachable: try again next launch.
                Err(err) => eprintln!("bmail-desktop: update check failed: {err}"),
            }
        });
    }

    pub fn install_pending(handle: &AppHandle) {
        let pending = handle.state::<Pending>().0.lock().unwrap().take();
        if let Some((update, bytes)) = pending {
            if let Err(err) = update.install(bytes) {
                eprintln!("bmail-desktop: update install failed: {err}");
            }
        }
    }
}

fn main() {
    let builder = tauri::Builder::default().manage(Daemon(Mutex::new(None)));
    #[cfg(feature = "updater")]
    let builder = builder
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(update::Pending::default());

    builder
        .setup(|app| {
            let handle = app.handle().clone();
            #[cfg(feature = "updater")]
            update::fetch_in_background(handle.clone());
            let (url, child) = match start_daemon() {
                Ok(started) => started,
                Err(message) => {
                    // Fall back to the bundled page, which explains what to do,
                    // rather than opening an empty window with no explanation.
                    eprintln!("bmail-desktop: {message}");
                    WebviewWindowBuilder::new(&handle, "main", WebviewUrl::default())
                        .title("Bmail")
                        .inner_size(720.0, 420.0)
                        .build()?;
                    return Ok(());
                }
            };

            *app.state::<Daemon>().0.lock().unwrap() = Some(child);

            let parsed = url
                .parse()
                .map_err(|err| format!("the engine reported an unusable URL: {err}"))?;
            WebviewWindowBuilder::new(&handle, "main", WebviewUrl::External(parsed))
                .title("Bmail")
                .inner_size(1280.0, 860.0)
                .min_inner_size(880.0, 560.0)
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to start the Bmail desktop shell")
        .run(|handle, event| match event {
            tauri::RunEvent::ExitRequested { .. } => {
                // The daemon holds the mail store open; never leave it running
                // after the window it belongs to has gone.
                if let Some(mut child) = handle.state::<Daemon>().0.lock().unwrap().take() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
            #[cfg(feature = "updater")]
            tauri::RunEvent::Exit => update::install_pending(handle),
            _ => {}
        });
}
