use crate::{Command, Portal, Result, Snapshot, portal, store::write_json};
use reqwest::{blocking::Client, redirect::Policy};
use serde::{Deserialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::{
    io::{self, IsTerminal},
    path::Path,
    thread,
    time::{Duration, Instant},
};

const BASE: &str = "http://127.0.0.1:46387";

#[derive(Deserialize)]
pub(crate) struct School {
    pub id: String,
    pub host: String,
    pub portal: Option<String>,
    #[serde(flatten)]
    pub snapshot: Snapshot,
}

impl School {
    fn address(&self) -> &str {
        self.portal.as_deref().unwrap_or(&self.snapshot.source)
    }
}

pub(crate) struct Backend(Client);

impl Backend {
    pub fn new() -> Result<Self> {
        Ok(Self(
            Client::builder()
                .no_proxy()
                .redirect(Policy::none())
                .timeout(Duration::from_secs(5))
                .build()?,
        ))
    }

    fn request<T: DeserializeOwned>(&self, path: &str, body: Option<Value>) -> Result<T> {
        let url = format!("{BASE}{path}");
        let response = match body {
            Some(data) => self.0.put(url).header("Content-Type", "application/json").body(data.to_string()),
            None => self.0.get(url),
        }.header("X-Coll-Track", "cli").send().map_err(|_| "Could not reach the Coll-Track backend. Start the Coll-Track service and try again.")?;
        let status = response.status();
        if !status.is_success() {
            let details = serde_json::from_str::<Value>(&response.text().unwrap_or_default())
                .unwrap_or_default();
            return Err(details["error"]
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| format!("Coll-Track backend returned {status}."))
                .into());
        }
        Ok(serde_json::from_str(&response.text()?)?)
    }

    pub fn schools(&self) -> Result<Vec<School>> {
        self.request("/cli/portals", None)
    }

    pub fn set_portal_address(&self, school: &School, address: &str) -> Result<()> {
        let _: Value = self.request(
            "/schools",
            Some(json!({
                "host": school.host, "name": school.snapshot.school, "portal": address
            })),
        )?;
        println!("Portal address saved.");
        Ok(())
    }

    fn school(&self, id: &str) -> Result<School> {
        let mut schools = self.schools()?;
        if let Some(index) = schools
            .iter()
            .position(|school| school.host.eq_ignore_ascii_case(id))
        {
            return Ok(schools.swap_remove(index));
        }
        if let Some(index) = schools
            .iter()
            .position(|school| school.id.eq_ignore_ascii_case(id))
        {
            return Ok(schools.swap_remove(index));
        }
        let mut matches = schools.into_iter().filter(|s| {
            s.snapshot.school.eq_ignore_ascii_case(id)
                || s.host
                    .split('.')
                    .any(|label| label.eq_ignore_ascii_case(id))
        });
        let school = matches
            .next()
            .ok_or_else(|| format!("Unknown school: {id}. Run coll-track list."))?;
        if matches.next().is_some() {
            return Err("More than one school matches. Use its full portal hostname.".into());
        }
        Ok(school)
    }

    fn selected_profile(&self, school: &School) -> Result<portal::Credentials> {
        self.request(
            "/cli/launch-profile",
            Some(json!({"url": school.address()})),
        )
    }

    fn check(&self, school: &School, offer_recovery: bool) -> Result<()> {
        let queued: Value = self.request(
            "/trackers/check",
            Some(json!({"url": school.address(), "school": school.snapshot.school})),
        )?;
        let start = Instant::now();
        eprintln!(
            "Checking {} through the local service…",
            school.snapshot.school
        );
        while start.elapsed() < Duration::from_secs(60) {
            let trackers: Vec<Value> = self.request("/trackers", None)?;
            if let Some(tracker) = trackers.iter().find(|t| t["source"] == queued["source"])
                && tracker["checked"].as_i64() >= queued["requested"].as_i64()
                && tracker["requested"].is_null()
            {
                let result = tracker["result"].as_str();
                let message = match result {
                    Some("checked") => {
                        println!("Portal checked. Results saved.");
                        return Ok(());
                    }
                    Some("missing_login" | "sign_in") => "This portal needs a saved login.",
                    Some("choose_login") => {
                        "This portal has multiple saved logins and needs one selected."
                    }
                    Some("login_failed") => "The portal rejected the saved login.",
                    Some("login_timeout") => "The portal did not finish signing in.",
                    Some("status_page_missing") => {
                        "The application landing page did not lead to a readable status page."
                    }
                    Some("verification") => {
                        "This portal requires verification or uses an unsupported sign-in."
                    }
                    Some("unreadable") => {
                        "The checklist could not be read. Your previous checklist is still saved."
                    }
                    _ => "The portal check failed. Your previous checklist is still saved.",
                };
                let login_needed = matches!(
                    result,
                    Some(
                        "missing_login"
                            | "sign_in"
                            | "choose_login"
                            | "login_failed"
                            | "login_timeout"
                    )
                );
                if offer_recovery && io::stdin().is_terminal() && io::stdout().is_terminal() {
                    if let Some(command) =
                        crate::tui::recover_update(message, &school.host, login_needed)?
                    {
                        let retry = matches!(
                            command,
                            Command::SaveLogin { .. }
                                | Command::UseDefaultLogin { .. }
                                | Command::SaveDefaultLogin { .. }
                        );
                        self.run(command)?;
                        if retry {
                            return self.check(school, false);
                        }
                        return Ok(());
                    }
                    return Err(crate::tui::Cancelled.into());
                }
                let next_step = if login_needed {
                    "Use save-login or use-default-login to set a login."
                } else if result == Some("status_page_missing") {
                    "Check the portal address in School settings."
                } else {
                    "Try again or use update --from-json to import a checklist."
                };
                return Err(format!("{message} {next_step}").into());
            }
            thread::sleep(Duration::from_millis(500));
        }
        Err("The check is still running in the local service. Use view-checklist later to see its result.".into())
    }

    fn import(&self, school: &School, file: &Path) -> Result<()> {
        let mut snapshot = Snapshot::read(file)?;
        snapshot.require_complete()?;
        if portal::https_url(&snapshot.source)?.host_str() != Some(&school.host) {
            return Err("The export belongs to a different portal host.".into());
        }
        if snapshot.imported_at.is_none() {
            return Err(
                "The export has no import date. Export the checklist again and retry.".into(),
            );
        }
        snapshot.school = school.snapshot.school.clone();
        let _: Value = self.request("/snapshots", Some(serde_json::to_value(snapshot)?))?;
        println!("Checklist imported.");
        Ok(())
    }

    fn default_login(&self, ids: &[String], save: bool) -> Result<()> {
        let hosts = ids
            .iter()
            .map(|id| self.school(id).map(|school| school.host))
            .collect::<Result<Vec<_>>>()?;
        let mut body = json!({"hosts": hosts});
        if save {
            eprintln!(
                "Saving the default login updates it for every school already assigned to it."
            );
            let credentials = crate::tui::login_input()?;
            body["email"] = credentials.email.into();
            body["password"] = credentials.password.into();
        }
        let _: Value = self.request("/cli/default-login", Some(body))?;
        println!("Default login assigned to the selected schools.");
        Ok(())
    }

    pub fn run(&self, command: Command) -> Result<()> {
        match command {
            Command::Add { id, url, name, table, .. } => {
                if table != "table" { return Err("Shared mode detects checklist columns automatically. Use --offline for a custom selector.".into()); }
                let _: Value = self.request("/schools", Some(json!({"alias": id, "name": name.as_deref().unwrap_or(&id), "portal": url})))?;
                println!("Added {id}.");
            }
            Command::List => {
                let schools = self.schools()?;
                if schools.is_empty() { println!("No schools saved. Add one here or in the dashboard."); }
                for s in schools { println!("{}\t{}\t{}", s.id, s.snapshot.school, s.address()); }
            }
            Command::Check { id, login: false, credentials: None, .. } => self.check(&self.school(&id)?, true)?,
            Command::Check { .. } => return Err("Checks use a saved portal login. Use coll-track save-login to update it.".into()),
            Command::Status { id } => self.snapshot(&id)?.show(),
            Command::Export { id, file } => write_json(&file, &self.snapshot(&id)?, false)?,
            Command::Import { id, file } | Command::Update { id, from_json: Some(file) } =>
                self.import(&self.school(&id)?, &file)?,
            Command::Update { id, from_json: None } => self.check(&self.school(&id)?, true)?,
            Command::SaveDefaultLogin { schools } => self.default_login(&schools, true)?,
            Command::UseDefaultLogin { schools } => self.default_login(&schools, false)?,
            Command::Changes { id } => {
                self.show_updates(&self.school(&id)?)?;
            }
            Command::Track { id, minutes, pause } => {
                let minutes = minutes.unwrap_or_default();
                if !pause && minutes == 0 {
                    return Err("Enter a check interval from 1 to 10,080 minutes.".into());
                }
                let school = self.school(&id)?;
                let _: Value = self.request("/cli/tracking", Some(json!({
                    "url": school.address(), "school": school.snapshot.school,
                    "interval": minutes, "enabled": !pause
                })))?;
                if pause { println!("Automatic checks paused."); }
                else { println!("Automatic checks set to every {minutes} minutes."); }
            }
            Command::MarkRead { id } => {
                let school = self.school(&id)?;
                let result: Value = self.request("/cli/mark-read", Some(json!({"host": school.host})))?;
                println!("Marked {} updates as read.", result["count"]);
            }
            Command::SaveLogin { id } => {
                let school = self.school(&id)?;
                let credentials = crate::tui::login_input()?;
                let _: Value = self.request("/cli/login", Some(json!({
                    "host": school.host, "url": school.address(), "school": school.snapshot.school,
                    "email": credentials.email, "password": credentials.password
                })))?;
                println!("Saved a school-specific login. Run check to verify it.");
            }
            Command::Migrate { id } => {
                let store = crate::open_store(None)?;
                let portal = crate::registered(&store.load()?, &id)?;
                let _: Value = self.request("/schools", Some(json!({
                    "alias": id, "name": portal.name, "portal": portal.url
                })))?;
                println!("School added to the local service. Offline logins and history remain in the offline store.");
            }
            Command::Link { id } => {
                let school = self.school(&id)?;
                let credentials = self.selected_profile(&school)?;
                let target = Portal { name: school.snapshot.school.clone(), url: school.address().into(),
                    table: "table".into(), section: "Checklist".into(), kind: "admissions".into(),
                    saved_login: true, previous: None, current: None };
                crate::launch::serve(&target, &credentials)?;
            }
            Command::Remove { id } => {
                let school = self.school(&id)?;
                let _: Value = self.request("/schools/remove", Some(json!({"host": school.host})))?;
                println!("Removed {}.", school.snapshot.school);
            }
            Command::ForgetLogin { .. } =>
                return Err("Shared schools and login profiles are managed in the dashboard and extension. Use --offline for your separate terminal data.".into()),
            _ => unreachable!(),
        }
        Ok(())
    }

    pub fn show_updates(&self, school: &School) -> Result<usize> {
        let updates: Vec<Value> = self.request("/updates", None)?;
        let mut found = false;
        let mut unread = 0;
        for update in updates.iter().filter(|update| {
            update["source"]
                .as_str()
                .and_then(|source| portal::https_url(source).ok())
                .is_some_and(|url| url.host_str() == Some(&school.host))
        }) {
            found = true;
            let prefix = if update["read"] == false {
                unread += 1;
                "Unread · "
            } else {
                ""
            };
            println!(
                "{prefix}{}  {}",
                update["at"].as_str().unwrap_or_default(),
                update["item"].as_str().unwrap_or_default()
            );

            let changes = update["changes"].as_array().into_iter().flatten();
            let single =
                (update.get("before").is_some() || update.get("after").is_some()).then_some(update);
            for change in changes.chain(single) {
                let item = change["item"].as_str().unwrap_or_default();
                let before = describe_update_item(&change["before"]);
                let after = describe_update_item(&change["after"]);
                println!("  {item}: {before} → {after}");
            }
            for (key, prefix) in [("added", "+"), ("removed", "-")] {
                for line in update["page"][key]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                {
                    println!("  {prefix} {line}");
                }
            }
        }
        if !found {
            println!("No changes recorded for this school.");
        }
        Ok(unread)
    }

    fn snapshot(&self, id: &str) -> Result<Snapshot> {
        let snapshot = self.school(id)?.snapshot;
        if snapshot.imported_at.is_none() {
            return Err(
                "No checklist saved yet. Check the portal or import a checklist first.".into(),
            );
        }
        Ok(snapshot)
    }
}

fn describe_update_item(item: &Value) -> String {
    let status = item["status"].as_str().unwrap_or("—");
    match item["dateText"].as_str().filter(|date| !date.is_empty()) {
        Some(date) => format!("{status} ({date})"),
        None => status.into(),
    }
}
