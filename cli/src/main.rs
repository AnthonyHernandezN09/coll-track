mod launch;
#[cfg(unix)]
mod launch_browser;
mod logins;
mod portal;
mod shared;
mod snapshot;
mod store;
mod tui;

use clap::{Parser, Subcommand};
use portal::Credentials;
use snapshot::{Snapshot, diff};
use std::{env, path::PathBuf, process::ExitCode};
use store::{Portal, Result, State, Store, write_json};

#[derive(Parser)]
#[command(
    version,
    about = "Track and compare application checklists from a terminal."
)]
struct Cli {
    #[arg(
        long,
        global = true,
        value_name = "DIRECTORY",
        help = "Directory for portal settings and snapshots"
    )]
    data_dir: Option<PathBuf>,
    /// Use the legacy local store for offline data
    #[arg(long, alias = "standalone", global = true)]
    offline: bool,
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Open the interactive terminal menu
    Menu,
    #[command(hide = true)]
    BrowserSession,
    /// Register an HTTPS portal and its checklist table
    Add {
        id: String,
        url: String,
        #[arg(long)]
        name: Option<String>,
        #[arg(long, default_value = "table")]
        table: String,
        #[arg(long, default_value = "Checklist")]
        section: String,
        #[arg(long, default_value = "admissions")]
        kind: String,
    },
    /// List my schools
    #[command(visible_alias = "my-schools")]
    List,
    /// Remove a school and its saved data
    #[command(visible_alias = "remove-school")]
    Remove { id: String },
    /// Check now and save the latest checklist
    #[command(visible_alias = "check-now")]
    Check {
        id: String,
        #[arg(
            long,
            help = "Use COLL_TRACK_EMAIL and COLL_TRACK_PASSWORD for this check"
        )]
        login: bool,
        #[arg(long, conflicts_with = "login")]
        saved_login: bool,
        #[arg(
            long,
            requires = "login",
            help = "Save the supplied login in your system credential store"
        )]
        save_login: bool,
        #[arg(skip)]
        credentials: Option<Credentials>,
    },
    /// Set a school’s shared automatic check interval (minutes)
    Track {
        id: String,
        #[arg(required_unless_present = "pause", value_parser = clap::value_parser!(u32).range(0..=10_080))]
        minutes: Option<u32>,
        #[arg(long)]
        pause: bool,
    },
    /// Save a school-specific login to the shared vault
    SaveLogin { id: String },
    /// Mark this school’s shared updates as read
    MarkRead { id: String },
    /// Save or replace the default login and assign it to the listed schools
    SaveDefaultLogin {
        #[arg(required = true)]
        schools: Vec<String>,
    },
    /// Assign the saved default login to more schools
    UseDefaultLogin {
        #[arg(required = true)]
        schools: Vec<String>,
    },
    /// Update a checklist from its portal or a JSON export
    Update {
        id: String,
        #[arg(long, value_name = "FILE")]
        from_json: Option<PathBuf>,
    },
    /// Copy an offline school registration into the local service
    Migrate { id: String },
    /// Quick launch with a saved login
    #[command(visible_alias = "quick-launch")]
    Link { id: String },
    /// Delete a portal’s saved login
    ForgetLogin { id: String },
    /// Import checklist JSON
    #[command(visible_alias = "import-checklist")]
    Import { id: String, file: PathBuf },
    /// View checklist
    #[command(visible_alias = "view-checklist")]
    Status { id: String },
    /// View updates for a school
    #[command(visible_alias = "view-updates")]
    Changes { id: String },
    /// Export checklist JSON
    #[command(visible_alias = "export-checklist")]
    Export { id: String, file: PathBuf },
    /// Display a downloaded checklist JSON file
    Show { file: PathBuf },
    /// Compare two downloaded checklist JSON files
    Diff { previous: PathBuf, current: PathBuf },
}

fn data_dir() -> Result<PathBuf> {
    if let Some(dir) = env::var_os("COLL_TRACK_DATA_DIR").filter(|dir| !dir.is_empty()) {
        return Ok(dir.into());
    }
    #[cfg(windows)]
    {
        env::var_os("LOCALAPPDATA")
            .map(|base| PathBuf::from(base).join("CollTrack"))
            .ok_or_else(|| "Set --data-dir to choose a data directory.".into())
    }
    #[cfg(not(windows))]
    {
        if let Some(base) = env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .filter(|p| p.is_absolute())
        {
            return Ok(base.join("coll-track"));
        }
        let home = env::home_dir().ok_or("Set --data-dir to choose a data directory.")?;
        Ok(home.join(".local/share/coll-track"))
    }
}

fn current(portal: &Portal) -> Result<&Snapshot> {
    portal
        .current
        .as_ref()
        .ok_or_else(|| "No snapshot yet. Run check or import first.".into())
}

fn registered(state: &State, id: &str) -> Result<Portal> {
    state
        .portals
        .get(id)
        .cloned()
        .ok_or_else(|| format!("Unknown portal: {id}").into())
}

fn save_snapshot(
    store: &Store,
    state: &mut State,
    id: &str,
    mut portal: Portal,
    snapshot: Snapshot,
) -> Result<()> {
    if let Some(previous) = &portal.current
        && let (Some(old), Some(new)) = (&previous.imported_at, &snapshot.imported_at)
        && let (Ok(old), Ok(new)) = (
            time::OffsetDateTime::parse(old, &time::format_description::well_known::Rfc3339),
            time::OffsetDateTime::parse(new, &time::format_description::well_known::Rfc3339),
        )
        && new < old
    {
        return Err("The snapshot is older than the current snapshot.".into());
    }
    let baseline = portal.current.is_none();
    portal.previous = portal.current.take();
    portal.current = Some(snapshot);
    state.portals.insert(id.into(), portal);
    store.save(state)?;
    let portal = &state.portals[id];
    if baseline {
        println!("Saved the first snapshot for {}.", portal.name);
    } else if let Some(previous) = &portal.previous {
        diff(previous, current(portal)?)?;
    }
    Ok(())
}

fn open_store(directory: Option<PathBuf>) -> Result<Store> {
    Store::open(match directory {
        Some(dir) => dir,
        None => data_dir()?,
    })
}

fn run(command: Command, directory: Option<PathBuf>, shared: bool) -> Result<()> {
    match &command {
        Command::Menu => return tui::run(directory, shared),
        Command::BrowserSession => return launch::browser_session(),
        Command::Show { file } => {
            Snapshot::read(file)?.show();
            return Ok(());
        }
        Command::Diff { previous, current } => {
            return diff(&Snapshot::read(previous)?, &Snapshot::read(current)?);
        }
        _ => {}
    }
    if shared {
        return shared::Backend::new()?.run(command);
    }
    let store = open_store(directory)?;
    let mut state = store.load()?;
    match command {
        Command::Add {
            id,
            url,
            name,
            table,
            section,
            kind,
        } => {
            if id.is_empty()
                || !id
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
            {
                return Err(
                    "Use letters, numbers, dashes, or underscores for the portal ID.".into(),
                );
            }
            let url = portal::https_url(&url)?.to_string();
            portal::selector(&table)?;
            if state.portals.contains_key(&id) {
                return Err("That portal ID already exists.".into());
            }
            let portal = Portal {
                name: name.unwrap_or_else(|| id.clone()),
                url,
                table,
                section,
                kind,
                saved_login: false,
                previous: None,
                current: None,
            };
            state.portals.insert(id.clone(), portal);
            store.save(&state)?;
            println!("Added {id}.");
        }
        Command::List => {
            for (id, portal) in &state.portals {
                println!("{id}\t{}\t{}", portal.name, portal.url);
            }
            if state.portals.is_empty() {
                println!("No portals registered.");
            }
        }
        Command::Remove { id } => {
            let portal = registered(&state, &id)?;
            if portal.saved_login {
                logins::forget(&store, &id, &portal)?;
            }
            state.portals.remove(&id);
            store.save(&state)?;
            println!("Removed {id}.");
        }
        Command::Link { id } => {
            let portal = registered(&state, &id)?;
            let credentials = logins::load(&store, &id, &portal)?;
            drop(store);
            return launch::serve(&portal, &credentials);
        }
        Command::ForgetLogin { id } => {
            let mut portal = registered(&state, &id)?;
            logins::forget(&store, &id, &portal)?;
            portal.saved_login = false;
            state.portals.insert(id, portal);
            store.save(&state)?;
            println!("Saved login removed.");
        }
        Command::Check {
            id,
            login,
            saved_login,
            save_login,
            credentials,
        } => {
            let mut portal = registered(&state, &id)?;
            let credentials = if credentials.is_some() {
                credentials
            } else if saved_login {
                Some(logins::load(&store, &id, &portal)?)
            } else if login {
                Some(Credentials {
                    email: env::var("COLL_TRACK_EMAIL")
                        .map_err(|_| "Set COLL_TRACK_EMAIL for this check.")?,
                    password: env::var("COLL_TRACK_PASSWORD")
                        .map_err(|_| "Set COLL_TRACK_PASSWORD for this check.")?,
                })
            } else {
                None
            };
            let snapshot = portal::check(&portal, credentials.as_ref())
                .map_err(|error| format!("{error}\nPortal address\n{}", portal.url))?;
            if let Some(current) = &portal.current {
                let section = &snapshot.checklists[0];
                if !matches!(current.checklists.as_slice(), [previous]
                    if previous.kind.eq_ignore_ascii_case(&section.kind)
                        && previous.title.to_lowercase() == section.title.to_lowercase())
                {
                    return Err("The live table does not cover the stored checklist sections. Register a separate portal ID for live checks.".into());
                }
            }
            if save_login && let Some(credentials) = &credentials {
                logins::save(&store, &id, &portal, credentials)?;
                portal.saved_login = true;
                state.portals.insert(id.clone(), portal.clone());
                store.save(&state)?;
                println!("Login saved.");
            }
            save_snapshot(&store, &mut state, &id, portal, snapshot)?;
        }
        Command::Import { id, file } => {
            let portal = registered(&state, &id)?;
            let snapshot = Snapshot::read(&file)?;
            snapshot.require_complete()?;
            if snapshot.source != portal::source(&portal::https_url(&portal.url)?) {
                return Err("The export belongs to a different portal page.".into());
            }
            save_snapshot(&store, &mut state, &id, portal, snapshot)?;
        }
        Command::Status { id } => current(&registered(&state, &id)?)?.show(),
        Command::Changes { id } => {
            let portal = registered(&state, &id)?;
            let previous = portal
                .previous
                .as_ref()
                .ok_or("Two snapshots are needed to compare changes.")?;
            diff(previous, current(&portal)?)?;
        }
        Command::Export { id, file } => {
            write_json(&file, current(&registered(&state, &id)?)?, false)?
        }
        Command::Track { .. }
        | Command::SaveLogin { .. }
        | Command::SaveDefaultLogin { .. }
        | Command::UseDefaultLogin { .. }
        | Command::Update { .. }
        | Command::MarkRead { .. }
        | Command::Migrate { .. } => {
            return Err("This command requires the local Coll-Track service.".into());
        }
        Command::Menu | Command::BrowserSession | Command::Show { .. } | Command::Diff { .. } => {
            unreachable!()
        }
    }
    Ok(())
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let shared = !cli.offline && cli.data_dir.is_none();
    match run(cli.command.unwrap_or(Command::Menu), cli.data_dir, shared) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("Error: {error}");
            ExitCode::FAILURE
        }
    }
}
