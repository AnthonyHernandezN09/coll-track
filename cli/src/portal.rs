use crate::{
    snapshot::{Item, Section, Snapshot},
    store::{Portal, Result},
};
use reqwest::{
    Url,
    blocking::{Client, Response},
    redirect::Policy,
};
use scraper::{ElementRef, Html, Selector};
use std::{io::Read, time::Duration};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};

const MAX_PAGE: u64 = 4 * 1024 * 1024;

#[derive(serde::Serialize, serde::Deserialize)]
pub struct Credentials {
    pub email: String,
    pub password: String,
}

pub fn https_url(value: &str) -> Result<Url> {
    let url = Url::parse(value)?;
    if url.scheme() != "https"
        || url.host().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return Err("Enter an HTTPS portal URL without credentials or a fragment.".into());
    }
    Ok(url)
}

pub fn source(url: &Url) -> String {
    let mut url = url.clone();
    url.set_query(None);
    url.into()
}

pub fn selector(value: &str) -> Result<Selector> {
    Selector::parse(value).map_err(|_| "Enter a valid CSS table selector.".into())
}

fn page(response: Response) -> Result<(Url, Html)> {
    let response = response
        .error_for_status()
        .map_err(reqwest::Error::without_url)?;
    let url = response.url().clone();
    let mut bytes = Vec::new();
    response.take(MAX_PAGE + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_PAGE {
        return Err("The portal response exceeds 4 MiB.".into());
    }
    Ok((url, Html::parse_document(&String::from_utf8_lossy(&bytes))))
}

fn get(client: &Client, url: &Url) -> Result<(Url, Html)> {
    page(
        client
            .get(url.clone())
            .send()
            .map_err(reqwest::Error::without_url)?,
    )
}

fn login(client: &Client, url: &Url, document: &Html, credentials: &Credentials) -> Result<()> {
    let email = selector(
        "input[name=email]:is([type=email], [type=text], :not([type])):not([disabled]):not([readonly])",
    )?;
    let password = selector("input[name=password][type=password]:not([disabled]):not([readonly])")?;
    let forms = selector("form[data-fw-form='1']")?;
    let hidden = selector("input[type=hidden][name]:not([disabled])")?;
    let mut candidates = document
        .select(&forms)
        .filter(|form| form.select(&email).count() == 1 && form.select(&password).count() == 1);
    let form = candidates
        .next()
        .ok_or("This sign-in requires an unsupported form or a browser.")?;
    if candidates.next().is_some()
        || !form
            .attr("method")
            .is_some_and(|method| method.eq_ignore_ascii_case("post"))
        || form
            .attr("target")
            .is_some_and(|target| !target.is_empty() && target != "_self")
        || form
            .attr("enctype")
            .is_some_and(|kind| kind != "application/x-www-form-urlencoded")
    {
        return Err("This sign-in requires an unsupported form or a browser.".into());
    }
    let action = url.join(form.attr("action").unwrap_or(""))?;
    if action.origin() != url.origin()
        || !action.username().is_empty()
        || action.password().is_some()
    {
        return Err("The sign-in form points outside the portal.".into());
    }
    let mut fields: Vec<_> = form
        .select(&hidden)
        .filter_map(|field| {
            let name = field.attr("name")?;
            (!matches!(name, "email" | "password"))
                .then(|| (name, field.attr("value").unwrap_or("")))
        })
        .collect();
    fields.extend([
        ("email", credentials.email.as_str()),
        ("password", credentials.password.as_str()),
    ]);
    client
        .post(action)
        .header(reqwest::header::ORIGIN, url.origin().ascii_serialization())
        .form(&fields)
        .send()
        .and_then(Response::error_for_status)
        .map_err(reqwest::Error::without_url)?;
    Ok(())
}

fn cell_text(cell: ElementRef<'_>) -> String {
    cell.text()
        .collect::<Vec<_>>()
        .join(" ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn column(label: &str) -> Option<&'static str> {
    match label.trim_end_matches(':').to_ascii_lowercase().as_str() {
        "status" | "item status" | "requirement status" | "document status"
        | "completion status" => Some("status"),
        "item" | "items" | "details" | "requirement" | "requirements" | "material"
        | "materials" | "document" | "documents" | "form" | "forms" | "checklist item"
        | "document name" | "material name" => Some("item"),
        "date" | "dates" | "status date" | "date received" | "date complete" | "date completed"
        | "received date" | "completed date" | "due date" | "deadline" => Some("date"),
        _ => None,
    }
}

fn status(label: &str) -> &str {
    match label.trim_end_matches(':').to_ascii_lowercase().as_str() {
        "received" => "received",
        "complete" | "completed" => "complete",
        "awaiting" | "awaiting materials" | "pending" => "pending",
        "missing" | "not received" => "missing",
        "waived" => "waived",
        "not required" => "not_required",
        _ => "unknown",
    }
}

fn cells(row: ElementRef<'_>) -> impl Iterator<Item = ElementRef<'_>> {
    row.child_elements()
        .filter(|cell| matches!(cell.value().name(), "th" | "td"))
}

fn hidden(cell: ElementRef<'_>) -> bool {
    cell.attr("hidden").is_some() || cell.attr("aria-hidden") == Some("true")
}

fn span(cell: ElementRef<'_>, name: &str) -> Option<usize> {
    cell.attr(name).unwrap_or("1").parse().ok()
}

fn read_table(document: &Html, table_selector: &Selector) -> Result<Vec<Item>> {
    let mut matches = document.select(table_selector);
    let table = matches
        .next()
        .ok_or("The checklist table was absent from the HTML response.")?;
    if matches.next().is_some() || table.value().name() != "table" {
        return Err("The selector must match exactly one HTML table.".into());
    }
    if table.select(&selector("table")?).next().is_some() {
        return Err("Nested checklist tables require a browser.".into());
    }
    let row_selector = selector("tr")?;
    let mut rows = table.select(&row_selector).filter(|row| {
        row.parent()
            .and_then(ElementRef::wrap)
            .is_none_or(|parent| parent.value().name() != "tfoot")
    });
    let header = rows.next().ok_or("The checklist table has no header.")?;
    let mut roles = Vec::new();
    let mut status_width = 1;
    for cell in cells(header) {
        let role = column(&cell_text(cell));
        let width = span(cell, "colspan");
        if span(cell, "rowspan") != Some(1)
            || hidden(cell)
            || (width != Some(1) && !(role == Some("status") && width == Some(2)))
        {
            return Err("This checklist uses an unsupported header layout.".into());
        }
        roles.push(role);
        if width == Some(2) {
            status_width = 2;
            roles.push(None);
        }
    }
    let positions = |name| {
        roles
            .iter()
            .enumerate()
            .filter_map(|(index, role)| (*role == Some(name)).then_some(index))
            .collect::<Vec<_>>()
    };
    let (items, statuses, dates) = (positions("item"), positions("status"), positions("date"));
    if items.len() != 1 || statuses.len() != 1 || dates.len() > 1 {
        return Err(
            "The table needs one item column, one status column, and at most one date column."
                .into(),
        );
    }
    let mut checklist = Vec::new();
    for row in rows {
        let row: Vec<_> = cells(row).collect();
        let status_columns = statuses[0]..statuses[0] + status_width;
        if row.len() != roles.len()
            || row.iter().enumerate().any(|(index, cell)| {
                span(*cell, "colspan") != Some(1)
                    || span(*cell, "rowspan") != Some(1)
                    || (hidden(*cell) && !(status_width == 2 && status_columns.contains(&index)))
            })
        {
            return Err(
                "The checklist contains an incomplete or merged row. The snapshot was kept.".into(),
            );
        }
        let values: Vec<_> = row.iter().map(|cell| cell_text(*cell)).collect();
        if values[items[0]].is_empty() {
            return Err(
                "The checklist contains a row without an item name. The snapshot was kept.".into(),
            );
        }
        let mut known = values[status_columns]
            .iter()
            .map(|value| status(value))
            .filter(|value| *value != "unknown");
        let item_status = known.next().unwrap_or("unknown");
        if known.any(|other| other != item_status) {
            return Err(
                "The checklist contains conflicting statuses. The snapshot was kept.".into(),
            );
        }
        if item_status == "unknown" {
            return Err(
                "The checklist contains an unsupported status. The snapshot was kept.".into(),
            );
        }
        checklist.push(Item {
            item: values[items[0]].clone(),
            status: item_status.into(),
            raw_status: None,
            date_text: dates
                .first()
                .map(|&i| values[i].clone())
                .filter(|text| !text.is_empty()),
        });
    }
    if checklist.is_empty() {
        return Err("The checklist returned no rows. The snapshot was kept.".into());
    }
    Ok(checklist)
}

fn slate_checklist(
    client: &Client,
    url: &Url,
    document: &Html,
    table: &Selector,
) -> Result<Vec<Item>> {
    let scripts = selector("script")?;
    if !document.select(&scripts).any(|script| {
        script
            .text()
            .any(|text| text.contains(r##"FW.Lazy.Fetch("?cmd=" + tab, $("#content_body"))"##))
    }) {
        return Err("The checklist table was absent from the HTML response. Check the table selector or open the portal in your browser.".into());
    }

    let tabs = selector("ul.subtabs li > a[data-tab]")?;
    let mut available = document
        .select(&tabs)
        .filter_map(|tab| tab.attr("data-tab"));
    let requested = url
        .query_pairs()
        .find_map(|(key, value)| (key == "tab").then_some(value));
    let tab = match requested.as_deref() {
        Some(tab) => available.find(|candidate| *candidate == tab),
        None => available.next(),
    }
    .ok_or("The requested Slate tab was absent from the page.")?;
    if tab.is_empty()
        || !tab
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err("The Slate tab name is unsupported.".into());
    }
    let mut endpoint = url.clone();
    endpoint.set_query(None);
    endpoint.query_pairs_mut().append_pair("cmd", tab);
    let response = client
        .get(endpoint.clone())
        .header("X-Requested-With", "XMLHttpRequest")
        .send()
        .map_err(reqwest::Error::without_url)?;
    let (final_url, content) = page(response)?;
    if final_url != endpoint
        || content
            .select(&selector("input[type=password]")?)
            .next()
            .is_some()
    {
        return Err("The portal did not return the requested checklist tab.".into());
    }
    read_table(&content, table)
}

pub fn check(portal: &Portal, credentials: Option<&Credentials>) -> Result<Snapshot> {
    let url = https_url(&portal.url)?;
    let origin = url.origin();
    let client = Client::builder()
        .cookie_store(true)
        .timeout(Duration::from_secs(25))
        .redirect(Policy::custom(move |attempt| {
            if attempt.previous().len() >= 8
                || attempt.url().origin() != origin
                || !attempt.url().username().is_empty()
                || attempt.url().password().is_some()
            {
                attempt.error("The portal redirected outside its allowed sign-in flow.")
            } else {
                attempt.follow()
            }
        }))
        .build()?;
    let password = selector("input[type=password]")?;
    let challenge = selector("iframe[src*='captcha'], [data-sitekey]")?;
    let (mut final_url, mut document) = get(&client, &url)?;
    if document.select(&challenge).next().is_some() {
        return Err("The portal requires verification in a browser.".into());
    }
    if document.select(&password).next().is_some() {
        let credentials = credentials
            .ok_or("The portal requires sign-in. Choose a login or run check with --login.")?;
        login(&client, &final_url, &document, credentials)?;
        (final_url, document) = get(&client, &url)?;
    }
    if document.select(&password).next().is_some() || document.select(&challenge).next().is_some() {
        return Err(
            "Sign-in did not complete. Check the credentials or sign in using a browser.".into(),
        );
    }
    if final_url.path().trim_end_matches('/') != url.path().trim_end_matches('/')
        || (url.query().is_some() && final_url.query() != url.query())
    {
        return Err("The portal returned a different page. The snapshot was kept.".into());
    }
    let table = selector(&portal.table)?;
    let items = if document.select(&table).next().is_some() {
        read_table(&document, &table)?
    } else {
        slate_checklist(&client, &final_url, &document, &table)?
    };
    Ok(Snapshot {
        school: portal.name.clone(),
        source: source(&url),
        imported_at: Some(OffsetDateTime::now_utc().format(&Rfc3339)?),
        checklists: vec![Section {
            kind: portal.kind.clone(),
            title: portal.section.clone(),
            items,
            date_label: None,
            skipped_rows: 0,
        }],
        unreadable: Vec::new(),
    })
}
