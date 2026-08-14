//! `grok update` must remain externally managed regardless of local config.

use std::process::Command;

/// Resolve the pager binary like the PTY harness: `PAGER_BINARY` under
/// Bazel (runfiles-relative), else cargo's compile-time constant.
fn pager_binary() -> std::path::PathBuf {
    if let Ok(p) = std::env::var("PAGER_BINARY") {
        return std::path::absolute(&p)
            .unwrap_or_else(|e| panic!("failed to absolutize PAGER_BINARY {p}: {e}"));
    }
    option_env!("CARGO_BIN_EXE_xai-grok-pager")
        .map(std::path::PathBuf::from)
        .expect("PAGER_BINARY is unset and this build is not `cargo test`")
}

fn run_update(config_toml: &str, extra_args: &[&str]) -> std::process::Output {
    let home = tempfile::tempdir().unwrap();
    std::fs::write(home.path().join("config.toml"), config_toml).unwrap();
    Command::new(pager_binary())
        .arg("update")
        .args(extra_args)
        .env_clear()
        .env("HOME", home.path())
        .env("GROK_HOME", home.path())
        .env("PATH", std::env::var("PATH").unwrap_or_default())
        .output()
        .expect("spawn grok update")
}

#[test]
fn config_never_changes_externally_managed_update_outcome() {
    for config in ["[cli]\n", "this is not toml {{{[[["] {
        for args in [
            Vec::<&str>::new(),
            vec!["--check", "--json"],
            vec!["--force-reinstall"],
            vec!["--version", "9.9.9"],
            vec!["--alpha"],
        ] {
            let output = run_update(config, &args);
            assert!(
                !output.status.success(),
                "update unexpectedly succeeded: {args:?}"
            );
            assert!(
                String::from_utf8_lossy(&output.stderr).contains("EXTERNALLY_MANAGED"),
                "missing externally managed error for {args:?}: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }
}
