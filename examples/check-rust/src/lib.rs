//! The smallest crate that gives `check.rust.yml` something real to do:
//! `cargo fmt --check`, `cargo clippy -D warnings` and `cargo test` all run
//! against it from `.github/workflows/canary.check-rust.yml`.

/// Adds two numbers.
pub fn add(a: u64, b: u64) -> u64 {
    a + b
}

#[cfg(test)]
mod tests {
    use super::add;

    #[test]
    fn adds() {
        assert_eq!(add(2, 3), 6);
    }
}
