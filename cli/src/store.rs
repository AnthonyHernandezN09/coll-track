use crate::snapshot::Snapshot;
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    error::Error,
    fs::{self, File, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
};

pub type Result<T> = std::result::Result<T, Box<dyn Error>>;

#[derive(Clone, Deserialize, Serialize)]
pub struct Portal {
    pub name: String,
    pub url: String,
    pub table: String,
    pub section: String,
    pub kind: String,
    #[serde(default)]
    pub saved_login: bool,
    pub previous: Option<Snapshot>,
    pub current: Option<Snapshot>,
}

#[derive(Default, Deserialize, Serialize)]
pub struct State {
    pub portals: BTreeMap<String, Portal>,
}

pub struct Store {
    directory: PathBuf,
    _lock: File,
}

impl Store {
    pub fn open(directory: PathBuf) -> Result<Self> {
        fs::create_dir_all(&directory)?;
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true).truncate(false);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let lock = options.open(directory.join("state.lock"))?;
        lock.lock()?;
        Ok(Self {
            directory,
            _lock: lock,
        })
    }

    pub fn directory(&self) -> &Path {
        &self.directory
    }

    pub fn load(&self) -> Result<State> {
        match fs::read(self.directory.join("state.json")) {
            Ok(data) => Ok(serde_json::from_slice(&data)?),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(State::default()),
            Err(error) => Err(error.into()),
        }
    }

    pub fn save(&self, state: &State) -> Result<()> {
        write_json(&self.directory.join("state.json"), state, true)
    }
}

pub fn write_json(path: &Path, value: &impl Serialize, replace: bool) -> Result<()> {
    let directory = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let mut temp = tempfile::NamedTempFile::new_in(directory)?;
    serde_json::to_writer_pretty(&mut temp, value)?;
    writeln!(temp)?;
    temp.as_file().sync_all()?;
    if replace {
        temp.persist(path)?;
    } else {
        temp.persist_noclobber(path)?;
    }
    Ok(())
}
