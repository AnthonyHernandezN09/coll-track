use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{collections::BTreeMap, error::Error, fs, path::Path};

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Snapshot {
    pub school: String,
    pub source: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub imported_at: Option<String>,
    pub checklists: Vec<Section>,
    #[serde(default)]
    pub unreadable: Vec<Value>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Section {
    pub kind: String,
    pub title: String,
    pub items: Vec<Item>,
    #[serde(default)]
    pub date_label: Option<String>,
    #[serde(default)]
    pub skipped_rows: usize,
}

#[derive(Clone, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Item {
    pub item: String,
    pub status: String,
    #[serde(default)]
    pub date_text: Option<String>,
    #[serde(default)]
    pub raw_status: Option<String>,
}

impl Snapshot {
    pub fn read(path: &Path) -> Result<Self, Box<dyn Error>> {
        let text = fs::read_to_string(path)
            .map_err(|error| format!("Could not read {}: {error}", path.display()))?;
        serde_json::from_str(text.trim_start_matches('\u{feff}')).map_err(|error| {
            format!("Invalid checklist JSON in {}: {error}", path.display()).into()
        })
    }

    pub fn require_complete(&self) -> Result<(), Box<dyn Error>> {
        if !self.unreadable.is_empty() || self.checklists.iter().any(|s| s.skipped_rows > 0) {
            return Err("The export contains unreadable checklist sections or rows.".into());
        }
        Ok(())
    }

    pub fn show(&self) {
        println!("{}", self.school);
        for section in &self.checklists {
            println!("\n{}", section.title);
            for item in &section.items {
                println!("  {}: {}", item.item, describe(item));
            }
        }
        if self.checklists.is_empty() {
            println!("No checklist sections.");
        }
        if self.require_complete().is_err() {
            eprintln!("Some checklist rows or sections could not be read.");
        }
    }
}

fn describe(item: &Item) -> String {
    match &item.date_text {
        Some(date) if !date.is_empty() => format!("{} ({date})", item.status),
        _ => item.status.clone(),
    }
}

type Identity = (String, String, String, usize);

fn indexed(snapshot: &Snapshot) -> BTreeMap<Identity, &Item> {
    let mut counts = BTreeMap::new();
    let mut result = BTreeMap::new();
    for section in &snapshot.checklists {
        for item in &section.items {
            let key = (
                section.kind.to_lowercase(),
                section.title.to_lowercase(),
                item.item.to_lowercase(),
            );
            let occurrence = counts.entry(key.clone()).or_insert(0);
            result.insert((key.0, key.1, key.2, *occurrence), item);
            *occurrence += 1;
        }
    }
    result
}

pub fn diff(before: &Snapshot, after: &Snapshot) -> Result<(), Box<dyn Error>> {
    if before.source != after.source {
        return Err("Both exports must come from the same portal page.".into());
    }
    before.require_complete()?;
    after.require_complete()?;
    let old = indexed(before);
    let new = indexed(after);
    let mut changed = false;
    for (key, item) in &new {
        match old.get(key) {
            None => println!("Added {}: {}", item.item, describe(item)),
            Some(previous)
                if (&previous.status, &previous.date_text) != (&item.status, &item.date_text)
                    && item.status != "unknown" =>
            {
                println!(
                    "Changed {}: {} -> {}",
                    item.item,
                    describe(previous),
                    describe(item)
                );
            }
            _ => continue,
        }
        changed = true;
    }
    for (key, item) in &old {
        if !new.contains_key(key) {
            println!("Removed {}: {}", item.item, describe(item));
            changed = true;
        }
    }
    if !changed {
        println!("No checklist changes.");
    }
    Ok(())
}
