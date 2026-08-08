//! The resume sandbox with the browser taken out of it.
//!
//! Everything here is a pure function of a string. No `wasm-bindgen`, no
//! `web-sys`, nothing that needs a DOM — which is the point: this module is
//! where the behaviour worth testing lives, and it is testable on the host
//! target with a plain `cargo test` instead of in a headless browser.
//! [`crate::resume_sandbox`] is the thin part that wires it to events.
//!
//! It is a port of `setupResumeSandbox` from the old vanilla-JS portfolio, and
//! it keeps that version's contract: the same required fields, the same
//! violation wording, the same three states, the same sample data. Two things
//! changed on the way across, both because Rust will not let them stay:
//!
//! 1. **Escaping is structural, not a function you must remember to call.**
//!    The JS renderer interpolated `escapeHtml(value)` at every hole and was
//!    correct only for as long as nobody added a hole. Here the renderer is a
//!    `maud` template, so every interpolation escapes because that is the only
//!    thing `maud` can do with a `&str`. There is no unescaped path to forget.
//!
//! 2. **The validator goes where JS's coercion used to.** The old validator
//!    checked that `skills` was an array and stopped; a `skills` of `[1, 2, 3]`
//!    passed, and the renderer printed the numbers. A `projects` entry missing
//!    its `name` passed too, and the renderer printed the word `undefined`
//!    into a résumé. Rust has no `String(undefined)` to fall back on, so every
//!    place the old renderer would have papered over a hole is a named
//!    violation instead.
//!
//! On wording: every check the JS had is here, with its message reproduced
//! verbatim, and there are checks here it did not have. It is *not* a strict
//! superset of its output, and there is exactly one input where it differs.
//! An `experience` entry that is an array — `"experience": [[]]` — made JS
//! report four missing fields, because `"role" in []` is false four times.
//! Here it reports one violation naming the actual problem, that the entry is
//! not an object. Four messages about symptoms are not more informative than
//! one about the cause. (The neighbouring case, an entry that is a string or a
//! number, made JS throw a `TypeError` from `field in item` that the outer
//! `catch` relabelled "Invalid JSON" — a syntax complaint about a file whose
//! syntax was fine.)
//!
//! The consequence is worth stating plainly: if [`validate`] returns nothing,
//! deserialising into [`Resume`] cannot fail. `Blocked` is the only way this
//! module refuses, and `Rendered` is total.

use maud::{DOCTYPE, Markup, PreEscaped, html};
use serde::{Deserialize, Serialize};
use serde_json::Value;

// ------------------------------------------------------------- data model ---

/// A résumé, once it has survived [`validate`].
///
/// Field order is the key order the sample serialises with, and the sample is
/// what fills the editor, so this order is what a visitor reads first.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Resume {
    pub name: String,
    pub headline: String,
    pub contact: Contact,
    pub summary: String,
    pub skills: Vec<String>,
    pub experience: Vec<Position>,
    pub projects: Vec<Project>,
    pub education: Vec<Education>,
}

/// The three contact lines, each optional.
///
/// The JS renderer wrote `contact.location || ""`, so an absent field was
/// always allowed and always empty. `#[serde(default)]` is the same promise,
/// spelled where it can be checked.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Contact {
    #[serde(default)]
    pub location: String,
    #[serde(default)]
    pub email: String,
    #[serde(default)]
    pub github: String,
}

/// One entry under Experience. `role` is the job title; the struct is named
/// `Position` so the field can keep the name the JSON uses.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Position {
    pub role: String,
    pub company: String,
    pub period: String,
    pub bullets: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Project {
    pub name: String,
    pub description: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Education {
    pub school: String,
    pub credential: String,
}

// -------------------------------------------------------------- violations ---

/// The three JSON shapes this schema asks for, and how to say each one in a
/// sentence. The phrasing is load-bearing: it is the JS validator's, verbatim.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Str,
    Array,
    Object,
}

impl Kind {
    /// "a string", "an array", "an object" — article included, because the
    /// article is what changes and a caller should not have to know which.
    pub const fn phrase(self) -> &'static str {
        match self {
            Self::Str => "a string",
            Self::Array => "an array",
            Self::Object => "an object",
        }
    }
}

/// One reason the editor's contents are not a résumé.
///
/// [`validate`] returns every one it finds rather than the first. A person
/// fixing a paste-in wants the whole list; returning one at a time turns one
/// edit into five round trips.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Violation {
    /// The text is not JSON at all. Carries `serde_json`'s message, which
    /// names the line and column — the JS version surfaced the browser's
    /// `SyntaxError.message` here for the same reason.
    Syntax(String),
    /// A required top-level key is absent, or present and `null`.
    Missing(&'static str),
    /// A required key is absent from an item of a collection. `path` is the
    /// item ("`experience[0]`"), `field` the key it lacks.
    MissingField { path: String, field: &'static str },
    /// A value is present but the wrong shape.
    WrongType { path: String, kind: Kind },
}

impl std::fmt::Display for Violation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Syntax(message) => write!(f, "{message}"),
            Self::Missing(field) => write!(f, "Missing required field: {field}"),
            Self::MissingField { path, field } => write!(f, "{path} missing {field}"),
            Self::WrongType { path, kind } => write!(f, "{path} must be {}", kind.phrase()),
        }
    }
}

// -------------------------------------------------------------- validation ---

/// The top-level schema, in the order a reader meets it.
const REQUIRED: [(&str, Kind); 8] = [
    ("name", Kind::Str),
    ("headline", Kind::Str),
    ("contact", Kind::Object),
    ("summary", Kind::Str),
    ("skills", Kind::Array),
    ("experience", Kind::Array),
    ("projects", Kind::Array),
    ("education", Kind::Array),
];

/// Keys every `experience` item must carry.
const POSITION_FIELDS: [&str; 4] = ["role", "company", "period", "bullets"];
/// Of those, the ones that must be strings. `bullets` is checked separately.
const POSITION_STRINGS: [&str; 3] = ["role", "company", "period"];
const PROJECT_FIELDS: [&str; 2] = ["name", "description"];
const EDUCATION_FIELDS: [&str; 2] = ["school", "credential"];
/// Optional, but must be strings when present.
const CONTACT_FIELDS: [&str; 3] = ["location", "email", "github"];

fn is(value: &Value, kind: Kind) -> bool {
    match kind {
        Kind::Str => value.is_string(),
        Kind::Array => value.is_array(),
        Kind::Object => value.is_object(),
    }
}

/// Every way `value` fails the résumé schema, in document order.
///
/// An empty return is a promise: `serde_json::from_value::<Resume>` on the same
/// value will succeed.
pub fn validate(value: &Value) -> Vec<Violation> {
    let mut violations = Vec::new();

    // `Value::get` on a non-object yields `None`, so a bare number or array in
    // the editor reports all eight fields missing — the same answer JS gave for
    // `data?.[field]` on the same input, and a more useful one than "not an
    // object", which says nothing about what was wanted.
    for (field, kind) in REQUIRED {
        match value.get(field) {
            None | Some(Value::Null) => violations.push(Violation::Missing(field)),
            Some(found) if !is(found, kind) => violations.push(Violation::WrongType {
                path: field.to_owned(),
                kind,
            }),
            Some(_) => {}
        }
    }

    if let Some(contact) = value.get("contact").and_then(Value::as_object) {
        for field in CONTACT_FIELDS {
            match contact.get(field) {
                // Absent is "leave the line out". Present-and-null is *not* the
                // same thing, and conflating them was a real bug: `serde`'s
                // `default` fills in a field that is missing, and does nothing
                // for a field that is there holding `null`. A validator that
                // waved `null` through handed `from_value` something it would
                // refuse, and the refusal surfaced under the heading "Invalid
                // JSON" — a syntax complaint about a file whose syntax was
                // fine. Null is a value of the wrong type, and says so.
                None => {}
                Some(found) if !found.is_string() => violations.push(Violation::WrongType {
                    path: format!("contact.{field}"),
                    kind: Kind::Str,
                }),
                Some(_) => {}
            }
        }
    }

    if let Some(skills) = value.get("skills").and_then(Value::as_array) {
        for (index, skill) in skills.iter().enumerate() {
            if !skill.is_string() {
                violations.push(Violation::WrongType {
                    path: format!("skills[{index}]"),
                    kind: Kind::Str,
                });
            }
        }
    }

    if let Some(items) = value.get("experience").and_then(Value::as_array) {
        for (index, item) in items.iter().enumerate() {
            let path = format!("experience[{index}]");
            // JS reached straight for `field in item` here, which throws a
            // TypeError on a string or a number and surfaced as "Invalid JSON"
            // — a syntax complaint about a file whose syntax was fine.
            let Some(object) = item.as_object() else {
                violations.push(Violation::WrongType {
                    path,
                    kind: Kind::Object,
                });
                continue;
            };
            for field in POSITION_FIELDS {
                if !object.contains_key(field) {
                    violations.push(Violation::MissingField {
                        path: path.clone(),
                        field,
                    });
                }
            }
            for field in POSITION_STRINGS {
                if object.get(field).is_some_and(|found| !found.is_string()) {
                    violations.push(Violation::WrongType {
                        path: format!("{path}.{field}"),
                        kind: Kind::Str,
                    });
                }
            }
            match object.get("bullets") {
                None => {}
                Some(Value::Array(bullets)) => {
                    for (bullet_index, bullet) in bullets.iter().enumerate() {
                        if !bullet.is_string() {
                            violations.push(Violation::WrongType {
                                path: format!("{path}.bullets[{bullet_index}]"),
                                kind: Kind::Str,
                            });
                        }
                    }
                }
                Some(_) => violations.push(Violation::WrongType {
                    path: format!("{path}.bullets"),
                    kind: Kind::Array,
                }),
            }
        }
    }

    check_items(value, "projects", &PROJECT_FIELDS, &mut violations);
    check_items(value, "education", &EDUCATION_FIELDS, &mut violations);

    violations
}

/// Objects-of-strings collections: `projects` and `education` have the same
/// shape as each other and neither had any check at all in the JS.
fn check_items(value: &Value, key: &str, fields: &[&'static str], violations: &mut Vec<Violation>) {
    let Some(items) = value.get(key).and_then(Value::as_array) else {
        return;
    };
    for (index, item) in items.iter().enumerate() {
        let path = format!("{key}[{index}]");
        let Some(object) = item.as_object() else {
            violations.push(Violation::WrongType {
                path,
                kind: Kind::Object,
            });
            continue;
        };
        for &field in fields {
            match object.get(field) {
                None | Some(Value::Null) => violations.push(Violation::MissingField {
                    path: path.clone(),
                    field,
                }),
                Some(found) if !found.is_string() => violations.push(Violation::WrongType {
                    path: format!("{path}.{field}"),
                    kind: Kind::Str,
                }),
                Some(_) => {}
            }
        }
    }
}

impl Resume {
    /// Parse, validate, and only then deserialise.
    ///
    /// The order matters. Deserialising first would report one violation —
    /// serde stops at the first field that will not fit — and the whole point
    /// of the sandbox is to hand back the entire list.
    pub fn parse(source: &str) -> Result<Self, Vec<Violation>> {
        let value: Value = serde_json::from_str(source)
            .map_err(|error| vec![Violation::Syntax(error.to_string())])?;
        let violations = validate(&value);
        if !violations.is_empty() {
            return Err(violations);
        }
        // Unreachable if `validate` is complete, and a test pins that. Reported
        // rather than unwrapped anyway: a panic here is a wasm trap, and the
        // cost of being wrong about "unreachable" should not be a dead island.
        serde_json::from_value(value).map_err(|error| vec![Violation::Syntax(error.to_string())])
    }

    /// The sample, pretty-printed exactly as the editor is pre-filled with it.
    pub fn to_pretty_json(&self) -> String {
        // A `Resume` is strings and vectors of strings all the way down; there
        // is no value in it serde can refuse.
        serde_json::to_string_pretty(self).unwrap_or_default()
    }
}

// ------------------------------------------------------------------ sample ---

/// The résumé the editor opens on, carried over from the JS verbatim.
///
/// Real data rather than lorem: the sandbox's claim is that structured input
/// renders to inspectable output, and a demo full of `Foo Bar` demonstrates
/// nothing about whether the schema fits an actual career.
pub fn sample() -> Resume {
    Resume {
        name: "Kennedy Mosoti".to_owned(),
        headline: "Observability Platform Engineer - Infrastructure Automation - AI Tooling \
                   Experiments"
            .to_owned(),
        contact: Contact {
            location: "Dallas-Fort Worth, TX".to_owned(),
            email: "kennedy.rmosoti@gmail.com".to_owned(),
            github: "github.com/kmosoti".to_owned(),
        },
        summary: "I work near Splunk, Logstash, SaltStack, Linux, RCA, remediation, and DR \
                  readiness. I like tools that turn operational fog into inspectable state."
            .to_owned(),
        skills: [
            "Splunk Enterprise",
            "Logstash",
            "Kafka ingestion workflows",
            "SaltStack",
            "Python",
            "Linux",
            "Terraform",
            "AWS",
        ]
        .map(str::to_owned)
        .to_vec(),
        experience: vec![Position {
            role: "Associate Observability Engineer".to_owned(),
            company: "Netbuilder / JPMC LogA Platform Contractor".to_owned(),
            period: "Aug 2024 - Present".to_owned(),
            bullets: [
                "Automated search-filter updates across 3,000+ Splunk roles.",
                "Supported Salt-based Splunk automation across 100+ search heads.",
                "Worked around Kafka, Logstash, Splunk ingestion paths, stale topic cleanup, and \
                 remediation.",
                "Investigated DR readiness gaps around cluster-manager and deployer workflows.",
            ]
            .map(str::to_owned)
            .to_vec(),
        }],
        projects: vec![
            Project {
                name: "resume-builder".to_owned(),
                description: "A structured resume generator where portable source data renders \
                              into inspectable output."
                    .to_owned(),
            },
            Project {
                name: "tea-style".to_owned(),
                description: "A doctrine scaffold for reusable engineering principles, patterns, \
                              notes, and experiments."
                    .to_owned(),
            },
        ],
        education: vec![Education {
            school: "University of Texas at Arlington".to_owned(),
            credential: "B.S. Software Engineering".to_owned(),
        }],
    }
}

/// The sample as the editor's opening text.
pub fn sample_json() -> String {
    sample().to_pretty_json()
}

// ---------------------------------------------------------------- renderer ---

/// The résumé as HTML.
///
/// Deliberately classless. Two audiences read this markup: the preview pane,
/// where `site.css` already styles `h1`/`h2`/`h3`/`p`/`ul`/`li` and any class
/// here would be one the contract's allowlist has to justify; and the
/// downloaded file, which has no stylesheet at all and had better still be
/// legible. Semantic elements are the only thing both of them agree on.
pub fn render(resume: &Resume) -> Markup {
    html! {
        article {
            h1 { (resume.name) }
            p { strong { (resume.headline) } }
            @let contact = contact_line(&resume.contact);
            @if !contact.is_empty() {
                p { (contact) }
            }
            section {
                h2 { "Summary" }
                p { (resume.summary) }
            }
            section {
                h2 { "Skills" }
                ul {
                    @for skill in &resume.skills {
                        li { (skill) }
                    }
                }
            }
            section {
                h2 { "Experience" }
                @for position in &resume.experience {
                    section {
                        h3 { (position.role) " - " (position.company) }
                        p { (position.period) }
                        ul {
                            @for bullet in &position.bullets {
                                li { (bullet) }
                            }
                        }
                    }
                }
            }
            section {
                h2 { "Projects" }
                @for project in &resume.projects {
                    section {
                        h3 { (project.name) }
                        p { (project.description) }
                    }
                }
            }
            section {
                h2 { "Education" }
                @for item in &resume.education {
                    section {
                        h3 { (item.credential) }
                        p { (item.school) }
                    }
                }
            }
        }
    }
}

/// Location, email and GitHub on one line.
///
/// The JS joined all three unconditionally, so a résumé with no email rendered
/// a line reading " -  - ". Empty parts are dropped here; for the sample, which
/// has all three, the output is identical.
fn contact_line(contact: &Contact) -> String {
    [&contact.location, &contact.email, &contact.github]
        .into_iter()
        .filter(|part| !part.is_empty())
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join(" - ")
}

/// The résumé as a file someone can open.
///
/// The JS wrapped the fragment in a bare `<html><body>`, which is a document
/// with no language and no encoding — enough to make a browser guess at both,
/// and enough to mangle any name that is not ASCII. The declaration and the
/// `lang` are the difference between an export and a download.
pub fn document(resume: &Resume) -> Markup {
    html! {
        (DOCTYPE)
        html lang="en" {
            head {
                meta charset="utf-8";
                title { (resume.name) " — resume" }
            }
            body { (render(resume)) }
        }
    }
}

// ------------------------------------------------------------------- state ---

/// What the sandbox is currently showing.
///
/// The three states are the ones the status pill names, and the pill is the
/// only thing on screen that answers "did that edit work?" without reading.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SandboxState {
    /// Before the first evaluation. The pill's opening text, and nothing else.
    NotRendered,
    /// The editor holds a valid résumé. Carries it, so the preview and the
    /// download are rendered from the same parse rather than from two.
    Rendered(Box<Resume>),
    /// The editor holds something that is not one, and why.
    Blocked(Vec<Violation>),
}

impl SandboxState {
    /// Parse the editor's contents and decide what the panel says.
    pub fn evaluate(source: &str) -> Self {
        match Resume::parse(source) {
            Ok(resume) => Self::Rendered(Box::new(resume)),
            Err(violations) => Self::Blocked(violations),
        }
    }

    /// The status pill. Lowercase and terse because it is a machine state, and
    /// it is rendered in `<code>` for the same reason.
    pub const fn pill(&self) -> &'static str {
        match self {
            Self::NotRendered => "not rendered",
            Self::Rendered(_) => "rendered",
            Self::Blocked(_) => "blocked",
        }
    }

    /// The heading above the validation output.
    ///
    /// "Invalid JSON" and "Invalid structure" are different problems with
    /// different fixes, and telling someone their braces are wrong when their
    /// braces are fine is the one unhelpful thing a validator can do.
    pub const fn title(&self) -> &'static str {
        match self {
            Self::NotRendered => "Waiting for input",
            Self::Rendered(_) => "Valid resume data",
            Self::Blocked(violations) => match violations.as_slice() {
                [Violation::Syntax(_)] => "Invalid JSON",
                _ => "Invalid structure",
            },
        }
    }

    /// The body of the validation output, one violation per line.
    pub fn detail(&self) -> String {
        match self {
            Self::NotRendered => String::new(),
            Self::Rendered(_) => {
                "Schema checks passed. Preview rendered from structured JSON.".to_owned()
            }
            Self::Blocked(violations) => violations
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>()
                .join("\n"),
        }
    }

    /// What goes in the preview pane. Blocked states say which half is broken
    /// rather than going blank, because a blank pane reads as "still loading".
    pub fn preview(&self) -> Markup {
        match self {
            Self::NotRendered => html! {},
            Self::Rendered(resume) => render(resume),
            Self::Blocked(violations) => {
                let syntax = matches!(violations.as_slice(), [Violation::Syntax(_)]);
                html! {
                    p class="text-muted" {
                        @if syntax {
                            "Fix JSON syntax before rendering."
                        } @else {
                            "Fix validation errors before rendering."
                        }
                    }
                }
            }
        }
    }

    /// The standalone file the Download HTML button writes, or `None` when
    /// there is nothing worth exporting.
    pub fn document(&self) -> Option<PreEscaped<String>> {
        match self {
            Self::Rendered(resume) => Some(document(resume)),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_value() -> Value {
        serde_json::to_value(sample()).expect("the sample serialises")
    }

    fn messages(violations: &[Violation]) -> Vec<String> {
        violations.iter().map(ToString::to_string).collect()
    }

    /// Every violation raised by removing `field` from the sample.
    fn without(field: &str) -> Vec<String> {
        let mut value = sample_value();
        value
            .as_object_mut()
            .expect("the sample is an object")
            .remove(field)
            .expect("the field was there to remove");
        messages(&validate(&value))
    }

    #[test]
    fn the_sample_is_valid() {
        assert_eq!(validate(&sample_value()), Vec::new());
    }

    #[test]
    fn the_sample_round_trips_through_json() {
        let json = sample_json();
        let parsed = Resume::parse(&json).expect("the sample parses");
        assert_eq!(parsed, sample());
        assert_eq!(parsed.to_pretty_json(), json);
    }

    #[test]
    fn the_sample_carries_the_real_data() {
        let resume = sample();
        assert_eq!(resume.name, "Kennedy Mosoti");
        assert!(
            resume
                .headline
                .starts_with("Observability Platform Engineer")
        );
        assert_eq!(resume.contact.location, "Dallas-Fort Worth, TX");
        assert_eq!(resume.skills.len(), 8);
        assert_eq!(resume.experience.len(), 1);
        assert_eq!(resume.experience[0].bullets.len(), 4);
        assert!(resume.experience[0].company.contains("JPMC"));
        assert_eq!(
            resume
                .projects
                .iter()
                .map(|project| project.name.as_str())
                .collect::<Vec<_>>(),
            ["resume-builder", "tea-style"]
        );
        assert_eq!(
            resume.education[0].school,
            "University of Texas at Arlington"
        );
    }

    #[test]
    fn every_required_field_is_named_when_it_is_removed() {
        for (field, _) in REQUIRED {
            assert_eq!(
                without(field),
                vec![format!("Missing required field: {field}")],
                "removing {field} should report exactly one violation"
            );
        }
    }

    #[test]
    fn a_null_required_field_reads_as_missing() {
        let mut value = sample_value();
        value["contact"] = Value::Null;
        assert_eq!(
            messages(&validate(&value)),
            ["Missing required field: contact"]
        );
    }

    #[test]
    fn every_required_field_is_type_checked() {
        for (field, kind) in REQUIRED {
            let mut value = sample_value();
            // A number is none of the three kinds, so one substitution covers
            // every row of the table.
            value[field] = Value::from(7);
            assert_eq!(
                messages(&validate(&value)),
                vec![format!("{field} must be {}", kind.phrase())],
            );
        }
    }

    #[test]
    fn an_empty_object_reports_all_eight_fields_at_once() {
        let violations = validate(&serde_json::json!({}));
        assert_eq!(violations.len(), REQUIRED.len());
        assert_eq!(messages(&violations)[0], "Missing required field: name");
    }

    #[test]
    fn a_non_object_document_reports_the_whole_schema() {
        // JS answered `undefined` for every lookup on a bare array; so do we,
        // and the list of what was wanted is more useful than "not an object".
        assert_eq!(
            validate(&serde_json::json!([1, 2, 3])).len(),
            REQUIRED.len()
        );
    }

    #[test]
    fn experience_items_must_carry_all_four_fields() {
        for field in POSITION_FIELDS {
            let mut value = sample_value();
            value["experience"][0]
                .as_object_mut()
                .expect("an experience item is an object")
                .remove(field);
            assert_eq!(
                messages(&validate(&value)),
                vec![format!("experience[0] missing {field}")],
            );
        }
    }

    #[test]
    fn experience_bullets_must_be_an_array() {
        let mut value = sample_value();
        value["experience"][0]["bullets"] = Value::from("one, two");
        assert_eq!(
            messages(&validate(&value)),
            ["experience[0].bullets must be an array"]
        );
    }

    #[test]
    fn experience_bullets_must_be_strings() {
        let mut value = sample_value();
        value["experience"][0]["bullets"] = serde_json::json!(["fine", 3]);
        assert_eq!(
            messages(&validate(&value)),
            ["experience[0].bullets[1] must be a string"]
        );
    }

    /// The one place the output deliberately diverges from the JS, pinned so
    /// the divergence stays deliberate. See the module doc.
    #[test]
    fn an_experience_item_that_is_not_an_object_says_so() {
        // A string: the JS threw a TypeError here and reported it as "Invalid
        // JSON", which is a lie about a file whose syntax is fine.
        let mut value = sample_value();
        value["experience"] = serde_json::json!(["Netbuilder"]);
        assert_eq!(
            messages(&validate(&value)),
            ["experience[0] must be an object"]
        );

        // An array: the JS reported four missing fields. One cause beats four
        // symptoms.
        value["experience"] = serde_json::json!([[]]);
        assert_eq!(
            messages(&validate(&value)),
            ["experience[0] must be an object"]
        );
    }

    #[test]
    fn experience_item_index_appears_in_the_message() {
        let mut value = sample_value();
        let first = value["experience"][0].clone();
        value["experience"] = serde_json::json!([first, {"role": "x"}]);
        assert_eq!(
            messages(&validate(&value)),
            [
                "experience[1] missing company",
                "experience[1] missing period",
                "experience[1] missing bullets",
            ]
        );
    }

    #[test]
    fn skills_must_be_strings() {
        let mut value = sample_value();
        value["skills"] = serde_json::json!(["Splunk", 42]);
        assert_eq!(messages(&validate(&value)), ["skills[1] must be a string"]);
    }

    #[test]
    fn optional_contact_lines_are_optional_but_typed() {
        let mut value = sample_value();
        value["contact"] = serde_json::json!({});
        assert_eq!(validate(&value), Vec::new());

        value["contact"] = serde_json::json!({ "email": 12 });
        assert_eq!(
            messages(&validate(&value)),
            ["contact.email must be a string"]
        );
    }

    /// The bug this pins: `#[serde(default)]` fills in a field that is
    /// *missing*, not one that is present holding `null`. A validator that let
    /// `null` through produced zero violations and then a deserialisation
    /// failure, reported as "Invalid JSON" — the one heading that is certainly
    /// wrong for a document whose syntax parsed.
    #[test]
    fn a_null_contact_line_is_a_violation_not_a_default() {
        for field in CONTACT_FIELDS {
            let mut value = sample_value();
            value["contact"][field] = Value::Null;
            assert_eq!(
                messages(&validate(&value)),
                vec![format!("contact.{field} must be a string")],
            );
        }
    }

    /// Every shape a `null` can take in this schema, checked against the
    /// invariant [`Resume::parse`] is built on rather than against a message.
    #[test]
    fn no_null_anywhere_can_pass_validation_and_then_fail_serde() {
        let hostile = [
            serde_json::json!(null),
            serde_json::json!({ "location": null }),
            serde_json::json!({ "email": null }),
            serde_json::json!({ "github": null }),
        ];
        for contact in hostile {
            let mut value = sample_value();
            value["contact"] = contact;
            assert_valid_or_refused(&value);
        }

        let mut value = sample_value();
        value["experience"][0]["role"] = Value::Null;
        assert_valid_or_refused(&value);
        value = sample_value();
        value["experience"][0]["bullets"] = Value::Null;
        assert_valid_or_refused(&value);
        value = sample_value();
        value["experience"][0]["bullets"] = serde_json::json!([null]);
        assert_valid_or_refused(&value);
        value = sample_value();
        value["skills"] = serde_json::json!([null]);
        assert_valid_or_refused(&value);
        value = sample_value();
        value["projects"] = serde_json::json!([{ "name": null, "description": "d" }]);
        assert_valid_or_refused(&value);
        value = sample_value();
        value["education"] = serde_json::json!([null]);
        assert_valid_or_refused(&value);
    }

    /// Either `validate` complains, or `serde` succeeds. There is no third
    /// outcome, and a `Violation::Syntax` out of [`Resume::parse`] on text that
    /// already parsed would be one.
    fn assert_valid_or_refused(value: &Value) {
        let violations = validate(value);
        let source = serde_json::to_string(value).expect("a Value re-serialises");
        match Resume::parse(&source) {
            Ok(_) => assert_eq!(violations, Vec::new(), "serde accepted {source}"),
            Err(reported) => {
                assert!(!violations.is_empty(), "validate waved through {source}");
                assert!(
                    !reported.iter().any(|v| matches!(v, Violation::Syntax(_))),
                    "structural failure reported as a syntax error: {source}",
                );
            }
        }
    }

    #[test]
    fn projects_and_education_items_are_checked() {
        let mut value = sample_value();
        value["projects"] = serde_json::json!([{ "name": "resume-builder" }]);
        value["education"] = serde_json::json!(["UTA"]);
        assert_eq!(
            messages(&validate(&value)),
            [
                "projects[0] missing description",
                "education[0] must be an object",
            ]
        );
    }

    #[test]
    fn violations_accumulate_rather_than_stopping_at_the_first() {
        let value = serde_json::json!({
            "name": 1,
            "headline": "ok",
            "contact": [],
            "summary": "ok",
            "skills": "not a list",
            "experience": [{ "role": "x" }],
            "projects": [],
            "education": [],
        });
        assert_eq!(
            messages(&validate(&value)),
            [
                "name must be a string",
                "contact must be an object",
                "skills must be an array",
                "experience[0] missing company",
                "experience[0] missing period",
                "experience[0] missing bullets",
            ]
        );
    }

    /// The promise [`Resume::parse`] relies on: a clean validation means serde
    /// cannot refuse the same value afterwards.
    #[test]
    fn a_clean_validation_guarantees_deserialisation() {
        let value = serde_json::json!({
            "name": "A", "headline": "B",
            "contact": {},
            "summary": "C", "skills": [],
            "experience": [{ "role": "r", "company": "c", "period": "p", "bullets": [] }],
            "projects": [], "education": [],
            "extra": { "ignored": true },
        });
        assert_eq!(validate(&value), Vec::new());
        serde_json::from_value::<Resume>(value).expect("validation vouched for this");
    }

    // ------------------------------------------------------------ syntax ---

    #[test]
    fn broken_json_is_one_syntax_violation() {
        let state = SandboxState::evaluate("{ oops");
        let SandboxState::Blocked(violations) = &state else {
            panic!("expected Blocked, got {state:?}");
        };
        assert!(matches!(violations.as_slice(), [Violation::Syntax(_)]));
        assert_eq!(state.title(), "Invalid JSON");
        assert_eq!(state.pill(), "blocked");
    }

    #[test]
    fn a_structural_failure_is_not_reported_as_a_syntax_one() {
        let state = SandboxState::evaluate("{}");
        assert_eq!(state.title(), "Invalid structure");
        assert!(state.detail().contains("Missing required field: name"));
        assert!(state.document().is_none());
    }

    // ----------------------------------------------------------- renderer ---

    #[test]
    fn the_sample_renders_every_section() {
        let html = render(&sample()).into_string();
        for expected in [
            "<h1>Kennedy Mosoti</h1>",
            "<h2>Summary</h2>",
            "<h2>Skills</h2>",
            "<h2>Experience</h2>",
            "<h2>Projects</h2>",
            "<h2>Education</h2>",
            "<li>Splunk Enterprise</li>",
            "<h3>Associate Observability Engineer - Netbuilder / JPMC LogA Platform Contractor</h3>",
            "<p>Aug 2024 - Present</p>",
            "<h3>B.S. Software Engineering</h3>",
            "Dallas-Fort Worth, TX - kennedy.rmosoti@gmail.com - github.com/kmosoti",
        ] {
            assert!(html.contains(expected), "render is missing {expected:?}");
        }
    }

    #[test]
    fn hostile_input_is_escaped_everywhere_it_can_appear() {
        let mut resume = sample();
        let hostile = r#"<script>alert("x&y")</script>"#;
        resume.name = hostile.to_owned();
        resume.headline = hostile.to_owned();
        resume.summary = hostile.to_owned();
        resume.contact.email = hostile.to_owned();
        resume.skills = vec![hostile.to_owned()];
        resume.experience[0].role = hostile.to_owned();
        resume.experience[0].company = hostile.to_owned();
        resume.experience[0].period = hostile.to_owned();
        resume.experience[0].bullets = vec![hostile.to_owned()];
        resume.projects[0].name = hostile.to_owned();
        resume.projects[0].description = hostile.to_owned();
        resume.education[0].school = hostile.to_owned();
        resume.education[0].credential = hostile.to_owned();

        let html = render(&resume).into_string();
        // The payload survives as *text* — `alert(` is still in there, and
        // should be. What must not survive is the markup that would run it.
        assert!(!html.contains("<script"), "an unescaped hole survives");
        assert!(!html.contains("</script"));
        assert!(html.contains("&lt;script&gt;"));
        assert!(html.contains("&amp;"));
        assert!(html.contains("&quot;"));
        // Every one of the thirteen holes above escaped, not just the first.
        assert_eq!(html.matches("&lt;script&gt;").count(), 13);
    }

    #[test]
    fn an_attribute_breakout_stays_text() {
        let mut resume = sample();
        resume.name = r#"" onload="alert(1)"#.to_owned();
        let html = render(&resume).into_string();
        assert!(!html.contains(r#"onload="alert"#));
        assert!(html.contains("&quot;"));
    }

    #[test]
    fn an_empty_contact_does_not_render_a_line_of_dashes() {
        assert_eq!(contact_line(&Contact::default()), "");
        assert_eq!(
            contact_line(&Contact {
                email: "a@b.c".to_owned(),
                ..Contact::default()
            }),
            "a@b.c"
        );

        let mut resume = sample();
        resume.contact = Contact::default();
        let html = render(&resume).into_string();
        // No empty paragraph and no paragraph that is only separators. The
        // headline and the period legitimately contain " - ", so the assertion
        // has to be about the contact line's own element, not the whole string.
        assert!(!html.contains("<p></p>"), "an empty contact line survived");
        assert!(
            !html.contains("<p> - "),
            "bare separators leaked into {html}"
        );
    }

    #[test]
    fn the_download_is_a_whole_document() {
        let state = SandboxState::evaluate(&sample_json());
        let document = state.document().expect("a valid resume exports");
        let document = document.into_string();
        assert!(document.starts_with("<!DOCTYPE html>"));
        assert!(document.contains(r#"<html lang="en">"#));
        assert!(document.contains(r#"<meta charset="utf-8">"#));
        assert!(document.contains("<h1>Kennedy Mosoti</h1>"));
    }

    #[test]
    fn a_rendered_state_says_the_checks_passed() {
        let state = SandboxState::evaluate(&sample_json());
        assert_eq!(state.pill(), "rendered");
        assert_eq!(state.title(), "Valid resume data");
        assert!(state.detail().starts_with("Schema checks passed."));
        assert!(state.preview().into_string().contains("<h1>"));
    }

    #[test]
    fn the_opening_state_shows_nothing_and_claims_nothing() {
        let state = SandboxState::NotRendered;
        assert_eq!(state.pill(), "not rendered");
        assert_eq!(state.title(), "Waiting for input");
        assert_eq!(state.detail(), "");
        assert_eq!(state.preview().into_string(), "");
        assert!(state.document().is_none());
    }
}
