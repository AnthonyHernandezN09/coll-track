use crate::{
    portal::Credentials,
    store::{Portal, Result, Store},
};

#[cfg(any(target_os = "macos", target_os = "linux", windows))]
use keyring::{Entry, Error};

#[cfg(any(target_os = "macos", target_os = "linux", windows))]
fn entry(store: &Store, id: &str, portal: &Portal) -> Result<Entry> {
    let directory = store.directory().canonicalize()?;
    let origin = crate::portal::https_url(&portal.url)?
        .origin()
        .ascii_serialization();
    let account = serde_json::to_string(&(directory, id, origin))?;
    Ok(Entry::new("Coll-Track CLI", &account)?)
}

#[cfg(any(target_os = "macos", target_os = "linux", windows))]
pub fn load(store: &Store, id: &str, portal: &Portal) -> Result<Credentials> {
    let password = match entry(store, id, portal)?.get_password() {
        Ok(password) => password,
        Err(Error::NoEntry) => return Err("No saved login. Enter a login for this check.".into()),
        Err(error) => return Err(error.into()),
    };
    Ok(serde_json::from_str(&password)?)
}

#[cfg(any(target_os = "macos", target_os = "linux", windows))]
pub fn save(store: &Store, id: &str, portal: &Portal, credentials: &Credentials) -> Result<()> {
    Ok(entry(store, id, portal)?.set_password(&serde_json::to_string(credentials)?)?)
}

#[cfg(any(target_os = "macos", target_os = "linux", windows))]
pub fn forget(store: &Store, id: &str, portal: &Portal) -> Result<()> {
    match entry(store, id, portal)?.delete_credential() {
        Ok(()) | Err(Error::NoEntry) => Ok(()),
        Err(error) => Err(error.into()),
    }
}

#[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
pub fn load(_: &Store, _: &str, _: &Portal) -> Result<Credentials> {
    Err("Saved logins are unavailable on this operating system.".into())
}

#[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
pub fn save(_: &Store, _: &str, _: &Portal, _: &Credentials) -> Result<()> {
    Err("Saved logins are unavailable on this operating system.".into())
}

#[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
pub fn forget(_: &Store, _: &str, _: &Portal) -> Result<()> {
    Err("Saved logins are unavailable on this operating system.".into())
}
