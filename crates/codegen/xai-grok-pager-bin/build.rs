use std::process::Command;

fn git_output(args: &[&str]) -> String {
    let output = Command::new("git")
        .args(args)
        .output()
        .unwrap_or_else(|error| panic!("git {} failed to start: {error}", args.join(" ")));
    assert!(
        output.status.success(),
        "git {} failed: {}",
        args.join(" "),
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout)
        .expect("git output must be UTF-8")
        .trim()
        .to_owned()
}

fn require_hex_revision(label: &str, revision: &str) {
    assert!(
        revision.len() == 40 && revision.bytes().all(|byte| byte.is_ascii_hexdigit()),
        "{label} must be a 40-character hexadecimal revision"
    );
}

fn watch_git_head() {
    let head_path = git_output(&["rev-parse", "--git-path", "HEAD"]);
    assert!(!head_path.is_empty(), "git HEAD path must not be empty");
    println!("cargo:rerun-if-changed={head_path}");

    let symbolic = Command::new("git")
        .args(["symbolic-ref", "-q", "HEAD"])
        .output()
        .expect("git symbolic-ref failed to start");
    if symbolic.status.success() {
        let reference = String::from_utf8(symbolic.stdout)
            .expect("git symbolic ref must be UTF-8")
            .trim()
            .to_owned();
        assert!(!reference.is_empty(), "git symbolic ref must not be empty");
        let reference_path = git_output(&["rev-parse", "--git-path", &reference]);
        assert!(
            !reference_path.is_empty(),
            "git reference path must not be empty"
        );
        println!("cargo:rerun-if-changed={reference_path}");
    }
}

fn main() {
    println!("cargo:rerun-if-changed=../../../SOURCE_REV");
    println!("cargo:rerun-if-env-changed=GROK_VERSION");
    println!("cargo:rerun-if-env-changed=CHIP_FORK_REVISION");
    println!("cargo:rerun-if-env-changed=CHIP_UPSTREAM_REVISION");

    let source_rev = std::fs::read_to_string("../../../SOURCE_REV")
        .expect("SOURCE_REV must be present when building the Chip fork");
    let source_rev = source_rev.trim();
    require_hex_revision("SOURCE_REV", source_rev);
    println!("cargo:rustc-env=CHIP_SOURCE_REV={source_rev}");

    let upstream_rev = std::env::var("CHIP_UPSTREAM_REVISION")
        .expect("CHIP_UPSTREAM_REVISION must pin the public upstream commit");
    require_hex_revision("upstream revision", &upstream_rev);
    println!("cargo:rustc-env=CHIP_UPSTREAM_REV={upstream_rev}");

    let fork_rev = match std::env::var("CHIP_FORK_REVISION") {
        Ok(revision) => revision,
        Err(_) => {
            watch_git_head();
            git_output(&["rev-parse", "HEAD"])
        }
    };
    require_hex_revision("fork revision", &fork_rev);
    println!("cargo:rustc-env=CHIP_FORK_REV={fork_rev}");

    let commit = &fork_rev[..7];
    let version = std::env::var("GROK_VERSION")
        .or_else(|_| std::env::var("CARGO_PKG_VERSION"))
        .unwrap_or_else(|_| "0.0.0".to_string());

    println!(
        "cargo:rustc-env=VERSION_WITH_COMMIT={} ({})",
        version, commit
    );
}
