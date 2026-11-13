use crate::{
    portal::{self, Credentials},
    store::{Portal, Result},
};
use serde::{Deserialize, Serialize};
use std::{
    fmt::Write as _,
    io::{self, BufRead, BufReader, Write},
    process::{Command, Stdio},
    time::{Duration, Instant},
};
use tiny_http::{Header, Method, Response, Server};

const PATH: &str = "/coll-track/quick-launch";
const LIFETIME: Duration = Duration::from_secs(120);
const LANDING_PAGE: &str = r#"<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Coll-Track Quick Launch</title>
<p id="status" role="status">Opening a separate Chrome window…</p>
<script>
const status = document.querySelector('#status');
const token = location.hash.slice(1);
history.replaceState(null, '', location.pathname);
if (!/^[a-f0-9]{64}$/.test(token)) {
  status.textContent = 'This Quick Launch link is invalid or was already used.';
} else {
  fetch(location.pathname, { method: 'POST', headers: { Authorization: 'Bearer ' + token } })
    .then(async response => { status.textContent = await response.text() || 'Create a new Quick Launch link.'; })
    .catch(() => { status.textContent = 'This Quick Launch link expired. Create a new one in the terminal.'; });
}
</script>
</html>"#;

#[derive(Serialize, Deserialize)]
pub struct LaunchRequest {
    pub portal: String,
    pub credentials: Credentials,
}

fn start_browser(portal: &str, credentials: &Credentials) -> Result<String> {
    let mut worker = Command::new(std::env::current_exe()?)
        .arg("browser-session")
        .env_remove("COLL_TRACK_EMAIL")
        .env_remove("COLL_TRACK_PASSWORD")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let mut input = worker
        .stdin
        .take()
        .ok_or("Could not send the browser request.")?;
    serde_json::to_writer(
        &mut input,
        &serde_json::json!({
            "portal": portal, "credentials": credentials,
        }),
    )?;
    drop(input);
    let mut message = String::new();
    BufReader::new(
        worker
            .stdout
            .take()
            .ok_or("Could not read the browser result.")?,
    )
    .read_line(&mut message)?;
    std::thread::spawn(move || {
        let _ = worker.wait();
    });
    if message.is_empty() {
        return Err("Chrome could not open the portal.".into());
    }
    Ok(message)
}

pub fn browser_session() -> Result<()> {
    #[cfg(unix)]
    {
        let request = serde_json::from_reader(io::stdin().lock())?;
        if let Err(error) = crate::launch_browser::run(request) {
            println!("Quick Launch could not open Chrome. {error}");
        }
        Ok(())
    }
    #[cfg(not(unix))]
    Err("Browser sign-in links are currently available on macOS and Linux.".into())
}

pub fn serve(portal: &Portal, credentials: &Credentials) -> Result<()> {
    if !cfg!(unix) {
        return Err("Browser sign-in links are currently available on macOS and Linux.".into());
    }
    let destination = portal::https_url(&portal.url)?;
    if destination.port().is_some() {
        return Err("Quick Launch requires a portal on the standard HTTPS port.".into());
    }

    let mut random = [0u8; 32];
    getrandom::fill(&mut random).map_err(|error| format!("Could not generate a link: {error}"))?;
    let mut token = String::with_capacity(64);
    for byte in random {
        write!(token, "{byte:02x}")?;
    }
    let server = Server::http("127.0.0.1:0").map_err(|error| error.to_string())?;
    let address = server.server_addr().to_string();
    let origin = format!("http://{address}");
    let authorization = format!("Bearer {token}");
    println!("{origin}{PATH}#{token}");
    eprintln!(
        "Open the link in your browser. Keep this terminal open until it is used. The link expires in two minutes."
    );
    io::stdout().flush()?;

    let deadline = Instant::now() + LIFETIME;
    while let Some(remaining) = deadline.checked_duration_since(Instant::now()) {
        let Some(request) = server.recv_timeout(remaining)? else {
            break;
        };
        let header = |name| {
            request
                .headers()
                .iter()
                .find(|header| header.field.equiv(name))
                .map(|header| header.value.as_str())
        };
        if request.url() != PATH || header("Host") != Some(address.as_str()) {
            let _ = request.respond(Response::empty(404));
            continue;
        }
        if request.method() == &Method::Get {
            let _ = request.respond(Response::from_string(LANDING_PAGE)
                .with_header(Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap())
                .with_header(Header::from_bytes("Cache-Control", "no-store").unwrap())
                .with_header(Header::from_bytes("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'").unwrap()));
            continue;
        }
        if request.method() != &Method::Post
            || header("Origin") != Some(origin.as_str())
            || header("Authorization") != Some(authorization.as_str())
        {
            let _ = request.respond(Response::empty(403));
            continue;
        }
        let message = match start_browser(destination.as_str(), credentials) {
            Ok(message) => message,
            Err(error) => {
                let text = format!("Quick Launch could not open Chrome. {error}");
                request.respond(Response::from_string(text).with_status_code(502))?;
                return Err(error);
            }
        };
        request.respond(
            Response::from_string(message.clone())
                .with_header(Header::from_bytes("Cache-Control", "no-store").unwrap()),
        )?;
        print!("{message}");
        return Ok(());
    }
    Err("Quick Launch link expired.".into())
}
