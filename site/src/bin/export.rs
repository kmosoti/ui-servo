//! `export` — render the site to static files for GitHub Pages.
//!
//! ```text
//! cargo run --bin export              # site/dist/
//! cargo run --bin export -- /tmp/out  # somewhere else
//! ```
//!
//! The work is in `ui_servo_site::export`; this is the process boundary. It
//! exits non-zero on a broken internal link, a corrupted résumé, or a page that
//! will not render, because the alternative is a deploy that succeeds and a
//! site that is wrong.

use std::path::PathBuf;
use std::process::ExitCode;
use ui_servo_site::export;
use ui_servo_site::state::AppState;

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("error: {error}");
            let mut source = std::error::Error::source(&*error);
            while let Some(cause) = source {
                eprintln!("  caused by: {cause}");
                source = cause.source();
            }
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let dist = destination()?;
    // Never `from_env`: the export must not depend on whether the shell that
    // ran it had `UI_SERVO_DEV=1` set. A published page carrying the probe
    // would beacon from a visitor's browser to a host that is not listening.
    let state = AppState::for_export()?;
    let report = export::export(&state, &dist)?;

    println!("exported to {}", report.dist.display());
    for file in report.files()? {
        println!("  {file}");
    }
    println!(
        "{} document(s), {} asset(s), {} internal reference(s) resolved",
        report.pages.len(),
        report.assets,
        report.links
    );
    Ok(())
}

/// `argv[1]`, else `UI_SERVO_DIST`, else `site/dist`.
///
/// An *empty* argument or variable is rejected rather than defaulted. Both are
/// easy to produce by accident — `-- "$OUT"` with `OUT` unset, an exported but
/// empty `UI_SERVO_DIST` — and an empty path is the working directory, which is
/// the one destination the export must never be handed: it deletes its
/// destination first. `export::export` refuses this too; catching it here is
/// what makes the message name the mistake rather than its consequence.
fn destination() -> Result<PathBuf, String> {
    let chosen = match std::env::args_os().nth(1) {
        Some(argument) => Some(argument),
        None => std::env::var_os("UI_SERVO_DIST"),
    };
    match chosen {
        Some(dir) if dir.is_empty() => Err(
            "the export destination is empty; pass a path, or leave it unset for site/dist"
                .to_owned(),
        ),
        Some(dir) => Ok(PathBuf::from(dir)),
        None => Ok(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("dist")),
    }
}
