use crate::{launch::LaunchRequest, portal, store::Result};
use reqwest::Url;
use serde_json::{Value, json};
use std::{
    env,
    io::{BufRead, BufReader, ErrorKind, Write},
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd},
        unix::{net::UnixStream, process::CommandExt},
    },
    path::PathBuf,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

const SIGN_IN: &str = include_str!("../../slate-login.js");

struct Browser(Child);

impl Drop for Browser {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

struct DevTools {
    pipe: BufReader<UnixStream>,
    id: u64,
    session: Option<String>,
}

impl DevTools {
    fn call(&mut self, method: &str, params: Value) -> Result<Value> {
        self.id += 1;
        let mut command = json!({"id": self.id, "method": method, "params": params});
        if let Some(session) = &self.session {
            command["sessionId"] = json!(session);
        }
        serde_json::to_writer(self.pipe.get_mut(), &command)?;
        self.pipe.get_mut().write_all(&[0])?;
        let mut bytes = Vec::new();
        let deadline = Instant::now() + Duration::from_secs(15);
        loop {
            if Instant::now() >= deadline {
                return Err(format!("Chrome timed out while handling {method}.").into());
            }
            match self.pipe.read_until(0, &mut bytes) {
                Ok(0) => return Err("Chrome closed its automation connection.".into()),
                Ok(_) if bytes.last() == Some(&0) => {}
                Ok(_) => return Err("Chrome closed an incomplete automation response.".into()),
                Err(error)
                    if matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) =>
                {
                    continue;
                }
                Err(error) => return Err(error.into()),
            }
            let message: Value = serde_json::from_slice(&bytes[..bytes.len() - 1])?;
            if message["id"].as_u64() != Some(self.id) {
                bytes.clear();
                continue;
            }
            if message.get("error").is_some() {
                return Err(format!("Chrome could not complete {method}.").into());
            }
            return Ok(message["result"].clone());
        }
    }

    fn evaluate(&mut self, expression: &str) -> Result<Value> {
        let result = self.call(
            "Runtime.evaluate",
            json!({
                "expression": expression, "returnByValue": true
            }),
        )?;
        Ok(result["result"]["value"].clone())
    }
}

fn executable() -> Result<PathBuf> {
    if let Some(value) = env::var_os("COLL_TRACK_CHROME") {
        return Ok(value.into());
    }
    let candidates = if cfg!(target_os = "macos") {
        vec![
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    } else {
        vec![
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        ]
    };
    for name in candidates {
        let path = PathBuf::from(name);
        if path.is_absolute() && path.is_file() {
            return Ok(path);
        }
        if !path.is_absolute()
            && let Some(search) = env::var_os("PATH")
            && let Some(path) = env::split_paths(&search)
                .map(|dir| dir.join(name))
                .find(|file| file.is_file())
        {
            return Ok(path);
        }
    }
    Err("Install Chrome or Chromium, or set COLL_TRACK_CHROME to its executable.".into())
}

fn sign_in(devtools: &mut DevTools, request: &LaunchRequest) -> Result<&'static str> {
    let destination = portal::https_url(&request.portal)?;
    let args = serde_json::to_string(&[
        destination.origin().ascii_serialization(),
        request.credentials.email.clone(),
        request.credentials.password.clone(),
    ])?;
    let probe = r#"JSON.stringify({url:location.href, ready:document.readyState !== 'loading', password:!!document.querySelector('input[type=password]'),
        login:[...document.querySelectorAll('a[href]')].find(a => {
            const url = new URL(a.href);
            return url.origin === location.origin && url.pathname.replace(/\/$/, '') === '/account/login'
                && /^(log|sign) in$/i.test(a.innerText.trim()) && a.getClientRects().length;
        })?.href,
        rejected:/invalid email address or password|incorrect password|invalid credentials/i.test(document.body?.innerText ?? '')})"#;
    devtools.call("Page.navigate", json!({"url": destination.as_str()}))?;
    let deadline = Instant::now() + Duration::from_secs(25);
    let mut submitted = false;
    let mut followed_login_link = false;
    while Instant::now() < deadline {
        thread::sleep(Duration::from_millis(250));
        let Ok(state) = devtools.evaluate(probe) else {
            continue;
        };
        let Some(state) = state.as_str() else {
            continue;
        };
        let state: Value = serde_json::from_str(state)?;
        let Some(url) = state["url"].as_str().and_then(|url| Url::parse(url).ok()) else {
            continue;
        };
        if url.as_str() == "about:blank" || state["ready"] != true {
            continue;
        }
        if url.origin() != destination.origin() {
            return Ok("Continue sign-in in the Chrome window.");
        }
        if !submitted
            && !followed_login_link
            && state["password"] == false
            && let Some(login) = state["login"]
                .as_str()
                .and_then(|value| Url::parse(value).ok())
            && login.origin() == destination.origin()
            && login.path().trim_end_matches('/') == "/account/login"
        {
            followed_login_link = true;
            devtools.call("Page.navigate", json!({"url": login.as_str()}))?;
            continue;
        }
        if url.path() == destination.path() && state["password"] == false {
            return Ok(if submitted {
                "Signed in and opened the portal in Chrome."
            } else {
                "Opened the portal in Chrome. Sign in there if prompted."
            });
        }
        if submitted && state["rejected"] == true {
            return Ok(
                "The portal rejected the saved login. Update it in Coll-Track before trying again.",
            );
        }
        if url.path().trim_end_matches('/') != "/account/login" {
            return Ok("Continue sign-in in the Chrome window.");
        }
        if !submitted {
            let expression = format!(
                r#"(([origin,email,password]) => {{
                    if (location.origin !== origin || typeof window.FW?.Postback !== 'function') return 'changed';
                    return ({SIGN_IN})(location.href,email,password);
                }})({args})"#
            );
            match devtools.evaluate(&expression)?.as_str() {
                Some("submitted") => submitted = true,
                Some("verification") => return Ok("Complete verification in the Chrome window."),
                _ => {}
            }
        }
    }
    Ok("Continue sign-in in the Chrome window.")
}

pub fn run(request: LaunchRequest) -> Result<()> {
    let profile = tempfile::Builder::new()
        .prefix("coll-track-chrome-")
        .tempdir()?;
    let (client, server) = UnixStream::pair()?;
    client.set_read_timeout(Some(Duration::from_secs(1)))?;
    client.set_write_timeout(Some(Duration::from_secs(5)))?;
    // Keep the source descriptor above Chrome’s two fixed DevTools descriptors.
    let fd = unsafe { libc::fcntl(server.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 5) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let inherited = unsafe { OwnedFd::from_raw_fd(fd) };
    drop(server);
    let chrome = executable()?;
    let mut command = Command::new(&chrome);
    command
        .arg(format!("--user-data-dir={}", profile.path().display()))
        .args([
            "--remote-debugging-pipe",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    // Chrome reads on fd 3 and writes on fd 4. The socket supports both directions.
    unsafe {
        command.pre_exec(move || {
            if libc::dup2(inherited.as_raw_fd(), 3) < 0 || libc::dup2(inherited.as_raw_fd(), 4) < 0
            {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut browser = Browser(command.spawn()?);
    drop(command);
    let mut devtools = DevTools {
        pipe: BufReader::new(client),
        id: 0,
        session: None,
    };
    let outcome = (|| -> Result<&'static str> {
        let target = devtools.call("Target.createTarget", json!({"url":"about:blank"}))?;
        let attached = devtools.call(
            "Target.attachToTarget",
            json!({
                "targetId": target["targetId"], "flatten": true
            }),
        )?;
        devtools.session = Some(
            attached["sessionId"]
                .as_str()
                .ok_or("Chrome did not attach to the portal tab.")?
                .into(),
        );
        sign_in(&mut devtools, &request)
    })();
    let outcome = match outcome {
        Ok(outcome) => outcome,
        Err(_) => {
            drop(browser);
            Command::new(chrome)
                .arg(&request.portal)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()?;
            println!("Opened Chrome for manual sign-in. Automatic sign-in did not finish.");
            return Ok(());
        }
    };
    println!("{outcome}");
    std::io::stdout().flush()?;
    // Chrome owns the visible session. Keep its private profile until Chrome exits.
    browser.0.wait()?;
    Ok(())
}
