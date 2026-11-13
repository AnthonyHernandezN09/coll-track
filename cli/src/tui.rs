use crate::{Command, portal::Credentials, store::Result};
use crossterm::{
    cursor::MoveTo,
    event::{self, Event, KeyCode, KeyEventKind, KeyModifiers},
    execute,
    terminal::{self, Clear, ClearType},
};
use std::{
    error::Error,
    fmt,
    io::{self, IsTerminal, Write},
    path::PathBuf,
};

const ACTIONS: [&str; 9] = [
    "List portals",
    "Add a portal",
    "Check a portal",
    "View latest checklist",
    "View changes",
    "Import a checklist",
    "Export a checklist",
    "Remove a portal",
    "View or compare export files",
];

#[derive(Debug)]
pub(crate) struct Cancelled;

impl fmt::Display for Cancelled {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        output.write_str("Cancelled.")
    }
}

impl Error for Cancelled {}

fn end_of_input(error: &(dyn Error + 'static)) -> bool {
    error
        .downcast_ref::<io::Error>()
        .is_some_and(|error| error.kind() == io::ErrorKind::UnexpectedEof)
}

fn finish_action(result: Result<()>, prompt: &str) -> Result<()> {
    if let Err(error) = result {
        if error.is::<Cancelled>() {
            return Ok(());
        }
        if end_of_input(error.as_ref()) {
            return Err(error);
        }
        eprintln!("{error}");
    }
    line(prompt)?;
    Ok(())
}

struct RawMode;

impl Drop for RawMode {
    fn drop(&mut self) {
        let _ = terminal::disable_raw_mode();
    }
}

fn select(title: &str, actions: &[&str], exit: &str) -> io::Result<Option<usize>> {
    terminal::enable_raw_mode()?;
    let _raw_mode = RawMode;
    let mut output = io::stdout().lock();
    let mut selected = 0;
    loop {
        execute!(output, MoveTo(0, 0), Clear(ClearType::All))?;
        let start = selected / 9 * 9;
        write!(output, "{title}\r\n\r\n")?;
        for (index, action) in actions[start..].iter().take(9).enumerate() {
            let marker = if start + index == selected { ">" } else { " " };
            write!(output, "{marker} {}. {action}\r\n", index + 1)?;
        }
        if actions.len() > 9 {
            write!(
                output,
                "\r\n{} of {} options. Scroll with Up/Down.\r\n",
                selected + 1,
                actions.len()
            )?;
        }
        write!(output, "  0. {exit}\r\n")?;
        write!(
            output,
            "\r\nUp/Down and Enter, or press a number. Esc: {exit}.\r\n"
        )?;
        output.flush()?;
        match event::read()? {
            Event::Key(key) if key.kind != KeyEventKind::Release => match key.code {
                KeyCode::Up => selected = (selected + actions.len() - 1) % actions.len(),
                KeyCode::Down => selected = (selected + 1) % actions.len(),
                KeyCode::Enter => return Ok(Some(selected)),
                KeyCode::Esc | KeyCode::Char('0') => return Ok(None),
                KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    return Ok(None);
                }
                KeyCode::Char(value @ '1'..='9')
                    if !key.modifiers.contains(KeyModifiers::CONTROL) =>
                {
                    let index = start + value as usize - '1' as usize;
                    if index < actions.len() {
                        return Ok(Some(index));
                    }
                }
                _ => {}
            },
            _ => {}
        }
    }
}

fn line(label: &str) -> io::Result<String> {
    print!("{label}: ");
    io::stdout().flush()?;
    let mut value = String::new();
    if io::stdin().read_line(&mut value)? == 0 {
        return Err(io::ErrorKind::UnexpectedEof.into());
    }
    Ok(value.trim().into())
}

pub(crate) fn login_input() -> Result<Credentials> {
    let email = required("Email (blank to cancel)")?;
    let password = password_input()?;
    Ok(Credentials { email, password })
}

fn password_input() -> Result<String> {
    print!("Password (visible, blank to cancel): ");
    io::stdout().flush()?;
    let mut password = String::new();
    if io::stdin().read_line(&mut password)? == 0 {
        return Err(io::Error::from(io::ErrorKind::UnexpectedEof).into());
    }
    if password.ends_with('\n') {
        password.pop();
        if password.ends_with('\r') {
            password.pop();
        }
    }
    if password.is_empty() {
        return Err(Cancelled.into());
    }
    Ok(password)
}

fn required(label: &str) -> Result<String> {
    let value = line(label)?;
    if value.is_empty() {
        return Err(Cancelled.into());
    }
    Ok(value)
}

fn default(label: &str, value: &str) -> io::Result<String> {
    let input = line(&format!("{label} [{value}]"))?;
    Ok(if input.is_empty() {
        value.into()
    } else {
        input
    })
}

fn confirm(label: &str) -> io::Result<bool> {
    Ok(matches!(
        line(label)?.to_ascii_lowercase().as_str(),
        "y" | "yes"
    ))
}

fn command(selected: usize, directory: Option<PathBuf>) -> Result<Option<Command>> {
    let portal_id = || required("Portal ID (blank to cancel)");
    let file = || required("Checklist JSON path (blank to cancel)").map(PathBuf::from);
    Ok(Some(match selected {
        0 => Command::List,
        1 => Command::Add {
            id: portal_id()?,
            url: required("HTTPS status page URL (blank to cancel)")?,
            name: Some(default("Display name", "")?).filter(|name| !name.is_empty()),
            table: default("Checklist table selector", "table")?,
            section: default("Checklist title", "Checklist")?,
            kind: default("Checklist kind", "admissions")?,
        },
        2 => return check_command(directory),
        3 => Command::Status { id: portal_id()? },
        4 => Command::Changes { id: portal_id()? },
        5 => Command::Import {
            id: portal_id()?,
            file: file()?,
        },
        6 => Command::Export {
            id: portal_id()?,
            file: file()?,
        },
        7 => {
            let id = portal_id()?;
            if !confirm(&format!("Remove {id} and its stored snapshots? [y/N]"))? {
                return Ok(None);
            }
            Command::Remove { id }
        }
        8 => match line("1. View an export   2. Compare two exports")?.as_str() {
            "1" => Command::Show { file: file()? },
            "2" => Command::Diff {
                previous: required("Older export path (blank to cancel)")?.into(),
                current: required("Newer export path (blank to cancel)")?.into(),
            },
            _ => return Ok(None),
        },
        _ => unreachable!(),
    }))
}

fn check_command(directory: Option<PathBuf>) -> Result<Option<Command>> {
    let state = crate::open_store(directory)?.load()?;
    if state.portals.is_empty() {
        return Err("No saved schools. Add a portal first.".into());
    }
    let portals: Vec<_> = state.portals.iter().collect();
    let names: Vec<_> = portals
        .iter()
        .map(|(id, portal)| {
            let login = if portal.saved_login {
                " | login saved"
            } else {
                ""
            };
            format!("{} ({id}){login}", portal.name)
        })
        .collect();
    let labels: Vec<_> = names.iter().map(String::as_str).collect();
    let Some(selected) = select("Choose a school to check", &labels, "Back")? else {
        return Ok(None);
    };
    let (id, portal) = portals[selected];
    execute!(io::stdout(), MoveTo(0, 0), Clear(ClearType::All))?;
    println!("Check {}\n", portal.name);
    if portal.saved_login {
        println!(
            "Enter. Use saved login   1. Enter another login   2. Check without signing in   3. Forget saved login"
        );
    } else {
        println!("1. Enter a login   2. Check without signing in");
    }
    if portal.saved_login {
        println!("4. Create a Quick Launch sign-in link");
    }
    let choice = line("Choose an option")?;
    let mut credentials = None;
    let mut save_login = false;
    match choice.as_str() {
        "1" => {
            let login = login_input()?;
            save_login = confirm("Save this login for future checks? [y/N]")?;
            credentials = Some(login);
        }
        "2" => {}
        "3" if portal.saved_login => {
            return Ok(Some(Command::ForgetLogin { id: id.clone() }));
        }
        "4" if portal.saved_login => return Ok(Some(Command::Link { id: id.clone() })),
        "" if portal.saved_login => {}
        _ => return Ok(None),
    }
    Ok(Some(Command::Check {
        id: id.clone(),
        login: false,
        saved_login: choice.is_empty(),
        save_login,
        credentials,
    }))
}

pub(crate) fn recover_update(
    message: &str,
    id: &str,
    login_needed: bool,
) -> Result<Option<Command>> {
    let mut choices = vec!["Import a checklist JSON file"];
    if login_needed {
        choices.extend([
            "Assign the default login and check again",
            "Save a login for this school and check again",
            "Save a default login and check again",
        ]);
    }
    Ok(match select(message, &choices, "Cancel")? {
        Some(0) => Some(Command::Import {
            id: id.into(),
            file: checklist_file()?,
        }),
        Some(1) => Some(Command::UseDefaultLogin {
            schools: vec![id.into()],
        }),
        Some(2) => Some(Command::SaveLogin { id: id.into() }),
        Some(3) => Some(Command::SaveDefaultLogin {
            schools: vec![id.into()],
        }),
        _ => None,
    })
}

fn checklist_file() -> Result<PathBuf> {
    Ok(required("Checklist JSON path (blank to cancel)")?.into())
}

fn default_login_command(schools: &[crate::shared::School]) -> Result<Option<Command>> {
    let Some(action) = select(
        "Default login",
        &[
            "Save or replace the default login",
            "Assign the saved default login",
        ],
        "Back",
    )?
    else {
        return Ok(None);
    };
    execute!(io::stdout(), MoveTo(0, 0), Clear(ClearType::All))?;
    for (index, school) in schools.iter().enumerate() {
        println!("{}. {}", index + 1, school.snapshot.school);
    }
    let input = required("School numbers separated by commas (blank to cancel)")?;
    let mut selected = Vec::new();
    for entry in input.split(',') {
        let index = entry
            .trim()
            .parse::<usize>()
            .ok()
            .and_then(|n| n.checked_sub(1))
            .filter(|&index| index < schools.len())
            .ok_or("Enter the school numbers shown in the list.")?;
        let host = &schools[index].host;
        if !selected.contains(host) {
            selected.push(host.clone());
        }
    }
    Ok(Some(if action == 0 {
        Command::SaveDefaultLogin { schools: selected }
    } else {
        Command::UseDefaultLogin { schools: selected }
    }))
}

const SHARED_ACTIONS: [&str; 3] = ["My schools", "Add a school", "Default login"];

fn add_school() -> Result<(String, Command)> {
    let url = required("HTTPS application status page (blank to cancel)")?;
    let host = crate::portal::https_url(&url)?
        .host_str()
        .ok_or("Missing portal hostname.")?
        .to_owned();
    Ok((
        host.clone(),
        Command::Add {
            id: default("Short name", &host.replace('.', "-"))?,
            url,
            name: Some(required("School name (blank to cancel)")?),
            table: "table".into(),
            section: "Checklist".into(),
            kind: "admissions".into(),
        },
    ))
}

fn school_settings(
    backend: &crate::shared::Backend,
    school: &crate::shared::School,
) -> Result<bool> {
    let title = format!("School settings · {}", school.snapshot.school);
    while let Some(action) = select(
        &title,
        &[
            "Save a separate login",
            "Use the default login",
            "Save and use a new default login",
            "Set check interval",
            "Pause automatic checks",
            "Edit portal address",
            "Remove school",
        ],
        "Back",
    )? {
        let id = school.host.clone();
        if action == 5 {
            let address = line("HTTPS address of the signed-in Status Page (blank to cancel)")?;
            if !address.is_empty() {
                if let Err(error) = backend.set_portal_address(school, &address) {
                    eprintln!("{error}");
                }
                line("Press Enter to return to settings")?;
            }
            continue;
        }
        if action == 6 {
            if confirm(&format!(
                "Remove {} and its saved checklists, settings, and updates? [y/N]",
                school.snapshot.school
            ))? {
                backend.run(Command::Remove { id })?;
                line("Press Enter to return to your schools")?;
                return Ok(true);
            }
            continue;
        }
        let command = match action {
            0 => Command::SaveLogin { id },
            1 => Command::UseDefaultLogin { schools: vec![id] },
            2 => Command::SaveDefaultLogin { schools: vec![id] },
            3 => {
                let value = line("Check every how many minutes? (blank to cancel)")?;
                if value.is_empty() {
                    continue;
                }
                match value.parse::<u32>() {
                    Ok(minutes) if (1..=10_080).contains(&minutes) => Command::Track {
                        id,
                        minutes: Some(minutes),
                        pause: false,
                    },
                    _ => {
                        println!("Enter a whole number from 1 to 10,080.");
                        line("Press Enter to return to settings")?;
                        continue;
                    }
                }
            }
            4 => Command::Track {
                id,
                minutes: None,
                pause: true,
            },
            _ => unreachable!(),
        };
        finish_action(backend.run(command), "Press Enter to return to settings")?;
    }
    Ok(false)
}

fn school_workspace(
    backend: &crate::shared::Backend,
    school: &crate::shared::School,
) -> Result<()> {
    let actions = [
        "Check now",
        "Import checklist JSON",
        "View checklist",
        "Export checklist JSON",
        "View updates",
        "Quick launch",
        "School settings",
    ];
    while let Some(action) = select(&school.snapshot.school, &actions, "My schools")? {
        let id = school.host.clone();
        if action == 6 {
            if school_settings(backend, school)? {
                break;
            }
            continue;
        }
        let result = match action {
            0 => backend.run(Command::Update {
                id,
                from_json: None,
            }),
            1 => checklist_file().and_then(|file| backend.run(Command::Import { id, file })),
            2 => backend.run(Command::Status { id }),
            3 => required("Save checklist JSON to (blank to cancel)").and_then(|file| {
                backend.run(Command::Export {
                    id,
                    file: file.into(),
                })
            }),
            4 => backend.show_updates(school).and_then(|unread| {
                if unread > 0 && confirm("Mark this school's updates as read? [y/N]")? {
                    backend.run(Command::MarkRead { id })?;
                }
                Ok(())
            }),
            5 => backend.run(Command::Link { id }),
            _ => unreachable!(),
        };
        finish_action(result, "Press Enter to return to this school")?;
    }
    Ok(())
}

fn choose_school(
    backend: &crate::shared::Backend,
    mut schools: Vec<crate::shared::School>,
) -> Result<()> {
    loop {
        if schools.is_empty() {
            println!("No schools saved yet.");
            if confirm("Add a school now? [y/N]")? {
                add_and_open(backend)?;
                schools = backend.schools()?;
                if !schools.is_empty() {
                    continue;
                }
            }
            return Ok(());
        }
        let labels: Vec<_> = schools
            .iter()
            .map(|school| school.snapshot.school.as_str())
            .collect();
        let Some(index) = select("My schools", &labels, "Main menu")? else {
            return Ok(());
        };
        school_workspace(backend, &schools[index])?;
        schools = backend.schools()?;
    }
}

fn add_and_open(backend: &crate::shared::Backend) -> Result<()> {
    let (host, command) = add_school()?;
    backend.run(command)?;
    if let Some(school) = backend
        .schools()?
        .into_iter()
        .find(|school| school.host == host)
    {
        school_workspace(backend, &school)?;
    }
    Ok(())
}

fn run_shared() -> Result<()> {
    let backend = crate::shared::Backend::new()?;
    while let Some(action) = select("Coll-Track · Local service", &SHARED_ACTIONS, "Quit")? {
        let result = match action {
            0 => backend
                .schools()
                .and_then(|schools| choose_school(&backend, schools)),
            1 => add_and_open(&backend),
            2 => backend.schools().and_then(|schools| {
                if schools.is_empty() {
                    return Err("Add a school before assigning the default login.".into());
                }
                if let Some(command) = default_login_command(&schools)? {
                    backend.run(command)?;
                    line("Press Enter to return to the main menu")?;
                }
                Ok(())
            }),
            _ => unreachable!(),
        };
        if let Err(error) = result
            && let Err(error) = finish_action(Err(error), "Press Enter to return to the main menu")
        {
            if end_of_input(error.as_ref()) {
                break;
            }
            return Err(error);
        }
    }
    Ok(())
}

pub fn run(directory: Option<PathBuf>, shared: bool) -> Result<()> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        return Err(
            "The menu needs an interactive terminal. Run coll-track --help for commands.".into(),
        );
    }

    if shared {
        return run_shared();
    }
    while let Some(selected) = select("Coll-Track · Offline", &ACTIONS, "Quit")? {
        execute!(io::stdout(), MoveTo(0, 0), Clear(ClearType::All))?;
        println!("{}\n", ACTIONS[selected]);
        let result = match command(selected, directory.clone()) {
            Ok(Some(command)) => crate::run(command, directory.clone(), shared),
            Ok(None) => continue,
            Err(error) => Err(error),
        };
        if let Err(error) = finish_action(result, "Press Enter to return to the menu") {
            if end_of_input(error.as_ref()) {
                break;
            }
            return Err(error);
        }
    }
    Ok(())
}
