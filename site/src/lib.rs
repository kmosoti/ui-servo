//! ui-servo-site — the product skeleton the control loop steers.
//!
//! Everything the site *is* lives in this library: the document shell, the
//! pages, the fragments, the startup state, and the one route manifest that
//! says which URLs exist. Two binaries consume it, and the split is the point:
//!
//! * `ui-servo-site` (`src/main.rs`) serves it over HTTP, with sensors in dev.
//! * `export` (`src/bin/export.rs`) renders the same page functions to static
//!   files for GitHub Pages, with no socket and no HTTP client in the loop.
//!
//! The second binary is why this is a library at all. An exporter that scraped
//! a running server would be measuring the server; an exporter that re-typed
//! the page list would drift from it. Calling the same `maud` functions through
//! the same [`routes::ROUTES`] manifest leaves nothing to drift.
//!
//! Run the server:
//!
//! ```text
//! UI_SERVO_DEV=1 cargo run          # http://localhost:8080
//! UI_SERVO_PORT=9000 cargo run      # somewhere else
//! ```
//!
//! Build the static site:
//!
//! ```text
//! cargo run --bin export            # site/dist/
//! ```

pub mod error;
pub mod export;
pub mod fragments;
pub mod layout;
pub mod pages;
pub mod routes;
pub mod server;
pub mod span;
pub mod state;
