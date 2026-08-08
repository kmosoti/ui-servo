//! `ui-servo-site` — serve the site over HTTP.
//!
//! Everything this binary does lives in the library (`src/lib.rs`); what is
//! left here is the process boundary: run the server, and turn a failure into
//! an exit code and a message somebody can act on. The split exists so that
//! `src/bin/export.rs` can render the very same pages to static files without
//! either binary owning the definition of what a page is.
//!
//! ```text
//! UI_SERVO_DEV=1 cargo run          # http://localhost:8080
//! UI_SERVO_PORT=9000 cargo run      # somewhere else
//! ```

use std::process::ExitCode;
use ui_servo_site::server;

#[tokio::main]
async fn main() -> ExitCode {
    match server::run().await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            // Not `Result` from `main`: that prints the Debug form, which
            // swallows the remediation text these errors exist to carry
            // ("re-emit tokens.css with …"). Print Display, plus the source
            // chain, because a startup failure here is someone's next command.
            eprintln!("error: {error}");
            let mut source = std::error::Error::source(&error);
            while let Some(cause) = source {
                eprintln!("  caused by: {cause}");
                source = cause.source();
            }
            ExitCode::FAILURE
        }
    }
}
