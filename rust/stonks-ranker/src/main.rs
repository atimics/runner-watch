use std::io::{self, Read};

use serde_json::json;
use stonks_integer_ranker::{Request, execute};

fn run() -> Result<serde_json::Value, String> {
    let mut input = String::new();
    io::stdin()
        .read_to_string(&mut input)
        .map_err(|error| format!("could not read request: {error}"))?;
    let request: Request =
        serde_json::from_str(&input).map_err(|error| format!("invalid request: {error}"))?;
    execute(request)
}

fn main() {
    match run() {
        Ok(response) => println!("{response}"),
        Err(error) => {
            println!("{}", json!({"ok": false, "error": error}));
            std::process::exit(1);
        }
    }
}
