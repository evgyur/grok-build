use std::collections::HashSet;
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::time::Instant;

use anyhow::Result;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use xai_grok_pager::headless::{HeadlessOptions, HeadlessPrompt, OutputFormat};

pub const UPSTREAM_REVISION: &str = env!("CHIP_UPSTREAM_REV");
const CONTRACT_VERSION: u32 = 1;
const MAX_REQUEST_BYTES: u64 = 1024 * 1024;
const MAX_REQUEST_ID_BYTES: usize = 128;
const MAX_PROMPT_BYTES: usize = 512 * 1024;
const MAX_MODEL_BYTES: usize = 256;
const MAX_PATH_BYTES: usize = 4096;
const MAX_CANARY_BYTES: usize = 256;
const MAX_ERROR_BYTES: usize = 4096;
const MAX_DELIVERABLES: usize = 128;
const MAX_DELIVERABLE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_DELIVERABLE_TOTAL_BYTES: u64 = 256 * 1024 * 1024;
const MAX_RECEIPT_BYTES: usize = 1024 * 1024;

pub fn version_json() -> serde_json::Value {
    serde_json::json!({
        "distribution": "chip",
        "version": env!("CARGO_PKG_VERSION"),
        "currentVersion": env!("VERSION_WITH_COMMIT"),
        "fork_commit": env!("CHIP_FORK_REV"),
        "upstream_commit": UPSTREAM_REVISION,
        "upstream_source_rev": env!("CHIP_SOURCE_REV"),
        "source_revision_provenance": "SOURCE_REV",
        "worker_contracts": [CONTRACT_VERSION],
        "auto_update": "externally-managed"
    })
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkerRequest {
    contract_version: u32,
    request_id: String,
    prompt: String,
    #[serde(default)]
    cwd: Option<PathBuf>,
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    max_turns: Option<u32>,
    #[serde(default)]
    verbatim: bool,
    #[serde(default)]
    sandbox: SandboxRequest,
    #[serde(default)]
    deliverables: Vec<String>,
    #[serde(default)]
    canary: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct SandboxRequest {
    #[serde(default)]
    required: bool,
    #[serde(default)]
    profile: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
struct ContractError {
    code: &'static str,
    message: String,
}

impl ContractError {
    fn new(code: &'static str, message: impl AsRef<str>) -> Self {
        Self {
            code,
            message: truncate_utf8(message.as_ref(), MAX_ERROR_BYTES),
        }
    }
}

impl std::fmt::Display for ContractError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for ContractError {}

#[derive(Debug, Serialize)]
struct Receipt {
    contract_version: u32,
    request_id: String,
    status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    canary: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<ContractError>,
    deliverables: Vec<Deliverable>,
    runtime: RuntimeIdentity,
    duration_ms: u64,
}

#[derive(Debug, Serialize)]
struct RuntimeIdentity {
    fork_revision: &'static str,
    upstream_revision: &'static str,
    source_revision: &'static str,
    sandbox_enforced: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    sandbox_profile: Option<String>,
}

#[derive(Debug, Serialize)]
struct Deliverable {
    path: String,
    bytes: u64,
    sha256: String,
}

impl Receipt {
    fn success(request_id: String, canary: Option<String>, deliverables: Vec<Deliverable>) -> Self {
        Self::new(request_id, canary, "success", None, deliverables, 0)
    }

    fn failure(
        request_id: String,
        canary: Option<String>,
        error: ContractError,
        duration_ms: u64,
    ) -> Self {
        Self::new(
            request_id,
            canary,
            "failure",
            Some(error),
            Vec::new(),
            duration_ms,
        )
    }

    fn new(
        request_id: String,
        canary: Option<String>,
        status: &'static str,
        error: Option<ContractError>,
        deliverables: Vec<Deliverable>,
        duration_ms: u64,
    ) -> Self {
        Self {
            contract_version: CONTRACT_VERSION,
            request_id,
            status,
            canary,
            error,
            deliverables,
            runtime: RuntimeIdentity {
                fork_revision: env!("CHIP_FORK_REV"),
                upstream_revision: UPSTREAM_REVISION,
                source_revision: env!("CHIP_SOURCE_REV"),
                sandbox_enforced: xai_grok_sandbox::is_active()
                    || cfg!(target_os = "linux") && xai_grok_sandbox::is_inside_bwrap(),
                sandbox_profile: xai_grok_sandbox::profile_name()
                    .or_else(xai_grok_sandbox::configured_profile_name)
                    .map(|profile| truncate_utf8(profile, MAX_MODEL_BYTES)),
            },
            duration_ms,
        }
    }
}

pub async fn run(request_path: Option<&Path>, receipt_path: &Path) -> Result<()> {
    let started = Instant::now();
    let request = match load_request(request_path) {
        Ok(request) => request,
        Err(error) => {
            let receipt =
                Receipt::failure("unknown".into(), None, error.clone(), elapsed_ms(started));
            write_receipt_atomic(receipt_path, &receipt)?;
            return Err(error.into());
        }
    };

    let request_id = request.request_id.clone();
    let canary = request.canary.clone();
    let result = run_request(request).await;
    match result {
        Ok(deliverables) => {
            let mut receipt = Receipt::success(request_id, canary, deliverables);
            receipt.duration_ms = elapsed_ms(started);
            write_receipt_atomic(receipt_path, &receipt)?;
            Ok(())
        }
        Err(error) => {
            let receipt = Receipt::failure(request_id, canary, error.clone(), elapsed_ms(started));
            write_receipt_atomic(receipt_path, &receipt)?;
            Err(error.into())
        }
    }
}

async fn run_request(
    request: WorkerRequest,
) -> std::result::Result<Vec<Deliverable>, ContractError> {
    validate_sandbox_request(&request.sandbox)?;
    let cwd = request.cwd.unwrap_or_else(|| PathBuf::from("."));
    let cwd = dunce::canonicalize(&cwd).map_err(|error| {
        ContractError::new(
            "CONFIG",
            format!("could not resolve worker cwd '{}': {error}", cwd.display()),
        )
    })?;
    if !cwd.is_dir() {
        return Err(ContractError::new(
            "CONFIG",
            "worker cwd is not a directory",
        ));
    }

    if request.sandbox.profile.is_some() || request.sandbox.required {
        xai_grok_shell::config::apply_sandbox(None, request.sandbox.profile.as_deref(), Some(&cwd))
            .map_err(|error| ContractError::new("SANDBOX_REQUIRED", error.to_string()))?;
        verify_sandbox_evidence(&request.sandbox)?;
    }

    let runtime_result = xai_grok_pager::headless::run_single_turn(
        HeadlessPrompt::Text(request.prompt),
        request.verbatim,
        HeadlessOptions {
            session_id: None,
            resume: None,
            resume_title_pinned: false,
            cwd: Some(cwd.clone()),
            yolo: true,
            trust: false,
            output_format: OutputFormat::Json,
            include_partial_messages: false,
            json_schema: None,
            model: request.model,
            rules: None,
            system_prompt_override: None,
            continue_last_session: false,
            fork_session: false,
            worktree: None,
            restore_code: false,
            agent: None,
            agents_json: None,
            cli_tools: None,
            cli_disallowed_tools: None,
            disable_web_search: true,
            allow_rules: Vec::new(),
            deny_rules: Vec::new(),
            max_turns: request.max_turns,
            permission_mode_flag: Some("bypassPermissions".into()),
            reasoning_effort: None,
            wait_for_background: true,
            background_wait_timeout: std::time::Duration::from_secs(30),
        },
    )
    .await;
    runtime_result.map_err(|error| classify_runtime_error(&format!("{error:#}")))?;
    build_manifest(&cwd, &request.deliverables)
}

fn load_request(request_path: Option<&Path>) -> std::result::Result<WorkerRequest, ContractError> {
    let bytes = match request_path {
        Some(path) => {
            let metadata = std::fs::symlink_metadata(path).map_err(|error| {
                ContractError::new(
                    "REQUEST_INVALID",
                    format!("could not inspect request: {error}"),
                )
            })?;
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return Err(ContractError::new(
                    "REQUEST_INVALID",
                    "request must be a regular file",
                ));
            }
            if metadata.len() > MAX_REQUEST_BYTES {
                return Err(ContractError::new(
                    "REQUEST_INVALID",
                    "request is too large",
                ));
            }
            let file = File::open(path).map_err(|error| {
                ContractError::new(
                    "REQUEST_INVALID",
                    format!("could not open request: {error}"),
                )
            })?;
            read_request_bytes(file)?
        }
        None => read_request_bytes(std::io::stdin().lock())?,
    };
    let value = serde_json::from_slice(&bytes).map_err(|error| {
        ContractError::new(
            "REQUEST_INVALID",
            format!("request is not valid JSON: {error}"),
        )
    })?;
    parse_request_value(value)
}

fn read_request_bytes(input: impl Read) -> std::result::Result<Vec<u8>, ContractError> {
    let mut bytes = Vec::new();
    input
        .take(MAX_REQUEST_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| {
            ContractError::new(
                "REQUEST_INVALID",
                format!("could not read request: {error}"),
            )
        })?;
    if bytes.len() as u64 > MAX_REQUEST_BYTES {
        return Err(ContractError::new(
            "REQUEST_INVALID",
            "request is too large",
        ));
    }
    Ok(bytes)
}

fn parse_request_value(
    value: serde_json::Value,
) -> std::result::Result<WorkerRequest, ContractError> {
    let request: WorkerRequest = serde_json::from_value(value).map_err(|error| {
        ContractError::new(
            "REQUEST_INVALID",
            format!("request schema is invalid: {error}"),
        )
    })?;
    if request.contract_version != CONTRACT_VERSION {
        return Err(ContractError::new(
            "REQUEST_INVALID",
            format!(
                "unsupported contract_version {}; expected 1",
                request.contract_version
            ),
        ));
    }
    require_bounded_nonempty("request_id", &request.request_id, MAX_REQUEST_ID_BYTES)?;
    require_bounded_nonempty("prompt", &request.prompt, MAX_PROMPT_BYTES)?;
    if let Some(model) = request.model.as_deref() {
        require_bounded_nonempty("model", model, MAX_MODEL_BYTES)?;
    }
    if request
        .max_turns
        .is_some_and(|turns| turns == 0 || turns > 1000)
    {
        return Err(ContractError::new(
            "REQUEST_INVALID",
            "max_turns must be between 1 and 1000",
        ));
    }
    if let Some(canary) = request.canary.as_deref() {
        require_bounded_nonempty("canary", canary, MAX_CANARY_BYTES)?;
    }
    if request.deliverables.len() > MAX_DELIVERABLES {
        return Err(ContractError::new(
            "REQUEST_INVALID",
            "too many deliverables",
        ));
    }
    for path in &request.deliverables {
        require_bounded_nonempty("deliverable path", path, MAX_PATH_BYTES)?;
    }
    if let Some(cwd) = request.cwd.as_ref()
        && cwd.to_string_lossy().len() > MAX_PATH_BYTES
    {
        return Err(ContractError::new(
            "REQUEST_INVALID",
            "cwd path is too long",
        ));
    }
    validate_sandbox_request(&request.sandbox)?;
    Ok(request)
}

fn require_bounded_nonempty(
    field: &str,
    value: &str,
    max_bytes: usize,
) -> std::result::Result<(), ContractError> {
    if value.trim().is_empty() || value.len() > max_bytes {
        return Err(ContractError::new(
            "REQUEST_INVALID",
            format!("{field} must contain 1..={max_bytes} bytes"),
        ));
    }
    Ok(())
}

fn validate_sandbox_request(sandbox: &SandboxRequest) -> std::result::Result<(), ContractError> {
    if let Some(profile) = sandbox.profile.as_deref() {
        let parsed = profile
            .parse::<xai_grok_sandbox::ProfileName>()
            .map_err(|error| {
                ContractError::new(
                    "REQUEST_INVALID",
                    format!("invalid sandbox profile: {error}"),
                )
            })?;
        if sandbox.required && parsed == xai_grok_sandbox::ProfileName::Off {
            return Err(ContractError::new(
                "SANDBOX_REQUIRED",
                "sandbox.required cannot use the off profile",
            ));
        }
    } else if sandbox.required {
        return Err(ContractError::new(
            "SANDBOX_REQUIRED",
            "sandbox.required needs an explicit enforcing profile",
        ));
    }
    Ok(())
}

fn verify_sandbox_evidence(sandbox: &SandboxRequest) -> std::result::Result<(), ContractError> {
    #[cfg(target_os = "linux")]
    let verified_external_enforcement = xai_grok_sandbox::is_inside_bwrap();
    #[cfg(not(target_os = "linux"))]
    let verified_external_enforcement = false;
    sandbox_evidence_satisfies(
        sandbox.required,
        xai_grok_sandbox::configured_profile_name(),
        xai_grok_sandbox::is_active(),
        verified_external_enforcement,
    )
}

fn sandbox_evidence_satisfies(
    required: bool,
    configured_profile: Option<&str>,
    active: bool,
    verified_external_enforcement: bool,
) -> std::result::Result<(), ContractError> {
    if !required {
        return Ok(());
    }
    if configured_profile.is_none_or(|profile| profile == "off") {
        return Err(ContractError::new(
            "SANDBOX_REQUIRED",
            "no enforcing sandbox profile was configured",
        ));
    }
    if !active && !verified_external_enforcement {
        return Err(ContractError::new(
            "SANDBOX_REQUIRED",
            "sandbox enforcement could not be verified",
        ));
    }
    Ok(())
}

fn classify_runtime_error(message: &str) -> ContractError {
    let code = if message.contains("max turns reached") {
        "MAX_TURNS"
    } else if message.starts_with("Failed to load config")
        || message.starts_with("Failed to create agent config")
        || message.contains("permission-mode")
    {
        "CONFIG"
    } else if message.starts_with("Couldn't set model")
        || message.contains("reasoning-effort")
        || message.contains("model catalog")
    {
        "MODEL"
    } else if message.starts_with("Couldn't start session")
        || message.starts_with("Couldn't initialize")
        || message.starts_with("Couldn't create session")
        || message.to_ascii_lowercase().contains("authentication")
    {
        "STARTUP"
    } else {
        "RUNTIME"
    };
    ContractError::new(code, message)
}

fn build_manifest(
    root: &Path,
    paths: &[String],
) -> std::result::Result<Vec<Deliverable>, ContractError> {
    let root = dunce::canonicalize(root).map_err(|error| {
        ContractError::new(
            "ARTIFACT_INVALID",
            format!("could not resolve artifact root: {error}"),
        )
    })?;
    let mut total = 0_u64;
    let mut seen = HashSet::new();
    let mut manifest = Vec::with_capacity(paths.len());
    for relative in paths {
        let relative_path = Path::new(relative);
        if relative.is_empty()
            || relative_path.is_absolute()
            || relative_path
                .components()
                .any(|component| !matches!(component, Component::Normal(_)))
        {
            return Err(ContractError::new(
                "ARTIFACT_INVALID",
                format!("deliverable path is not a normalized relative path: {relative}"),
            ));
        }
        if !seen.insert(relative.clone()) {
            return Err(ContractError::new(
                "ARTIFACT_INVALID",
                format!("duplicate deliverable path: {relative}"),
            ));
        }

        let mut candidate = root.clone();
        for component in relative_path.components() {
            candidate.push(component.as_os_str());
            let metadata = std::fs::symlink_metadata(&candidate).map_err(|error| {
                ContractError::new(
                    "ARTIFACT_INVALID",
                    format!("could not inspect deliverable '{relative}': {error}"),
                )
            })?;
            if metadata.file_type().is_symlink() {
                return Err(ContractError::new(
                    "ARTIFACT_INVALID",
                    format!("deliverable path contains a symlink: {relative}"),
                ));
            }
        }
        let canonical = dunce::canonicalize(&candidate).map_err(|error| {
            ContractError::new(
                "ARTIFACT_INVALID",
                format!("could not resolve deliverable '{relative}': {error}"),
            )
        })?;
        if !canonical.starts_with(&root) {
            return Err(ContractError::new(
                "ARTIFACT_INVALID",
                format!("deliverable escapes the worker cwd: {relative}"),
            ));
        }
        let metadata = std::fs::metadata(&canonical).map_err(|error| {
            ContractError::new(
                "ARTIFACT_INVALID",
                format!("could not stat '{relative}': {error}"),
            )
        })?;
        if !metadata.is_file() {
            return Err(ContractError::new(
                "ARTIFACT_INVALID",
                format!("deliverable is not a regular file: {relative}"),
            ));
        }
        if metadata.len() > MAX_DELIVERABLE_BYTES {
            return Err(ContractError::new(
                "ARTIFACT_INVALID",
                format!("deliverable exceeds the per-file limit: {relative}"),
            ));
        }
        total = total.checked_add(metadata.len()).ok_or_else(|| {
            ContractError::new("ARTIFACT_INVALID", "deliverable byte count overflow")
        })?;
        if total > MAX_DELIVERABLE_TOTAL_BYTES {
            return Err(ContractError::new(
                "ARTIFACT_INVALID",
                "deliverables exceed the aggregate byte limit",
            ));
        }

        let mut file = File::open(&canonical).map_err(|error| {
            ContractError::new(
                "ARTIFACT_INVALID",
                format!("could not open '{relative}': {error}"),
            )
        })?;
        let mut hasher = Sha256::new();
        let copied = std::io::copy(
            &mut std::io::Read::by_ref(&mut file).take(MAX_DELIVERABLE_BYTES + 1),
            &mut hasher,
        )
        .map_err(|error| {
            ContractError::new(
                "ARTIFACT_INVALID",
                format!("could not hash '{relative}': {error}"),
            )
        })?;
        if copied != metadata.len() || copied > MAX_DELIVERABLE_BYTES {
            return Err(ContractError::new(
                "ARTIFACT_INVALID",
                format!("deliverable changed while hashing: {relative}"),
            ));
        }
        manifest.push(Deliverable {
            path: relative.clone(),
            bytes: copied,
            sha256: format!("{:x}", hasher.finalize()),
        });
    }
    Ok(manifest)
}

fn write_receipt_atomic(path: &Path, receipt: &Receipt) -> Result<()> {
    let mut bytes = serde_json::to_vec(receipt)?;
    bytes.push(b'\n');
    if bytes.len() > MAX_RECEIPT_BYTES {
        anyhow::bail!("receipt exceeds the {MAX_RECEIPT_BYTES}-byte limit");
    }
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let file_name = path
        .file_name()
        .ok_or_else(|| anyhow::anyhow!("receipt path has no file name"))?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        file_name.to_string_lossy(),
        uuid::Uuid::new_v4()
    ));
    let write_result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        file.write_all(&bytes)?;
        file.flush()?;
        file.sync_all()?;
        std::fs::rename(&temporary, path)?;
        if let Ok(directory) = File::open(parent) {
            let _ = directory.sync_all();
        }
        Ok(())
    })();
    if write_result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    write_result
}

fn truncate_utf8(value: &str, max_bytes: usize) -> String {
    if value.len() <= max_bytes {
        return value.to_owned();
    }
    let mut end = max_bytes;
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    value[..end].to_owned()
}

fn elapsed_ms(started: Instant) -> u64 {
    started.elapsed().as_millis().min(u128::from(u64::MAX)) as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_json_pins_chip_provenance_and_runtime_policy() {
        let value = version_json();
        assert_eq!(value["distribution"], "chip");
        assert_eq!(value["fork_commit"], env!("CHIP_FORK_REV"));
        assert_eq!(value["upstream_commit"], UPSTREAM_REVISION);
        assert_eq!(value["upstream_source_rev"], env!("CHIP_SOURCE_REV"));
        assert_eq!(value["worker_contracts"], serde_json::json!([1]));
        assert_eq!(value["auto_update"], "externally-managed");
    }

    #[test]
    fn request_validation_rejects_unknown_contract_and_unbounded_fields() {
        let bad_version = serde_json::json!({
            "contract_version": 2,
            "request_id": "request-1",
            "prompt": "go"
        });
        assert_eq!(
            parse_request_value(bad_version).unwrap_err().code,
            "REQUEST_INVALID"
        );

        let long_id = "x".repeat(MAX_REQUEST_ID_BYTES + 1);
        let bad_id = serde_json::json!({
            "contract_version": 1,
            "request_id": long_id,
            "prompt": "go"
        });
        assert_eq!(
            parse_request_value(bad_id).unwrap_err().code,
            "REQUEST_INVALID"
        );
    }

    #[test]
    fn runtime_errors_have_stable_categories_and_bounded_messages() {
        for (message, code) in [
            ("Failed to load config: malformed", "CONFIG"),
            ("Couldn't start session: unavailable", "STARTUP"),
            ("Couldn't initialize: closed", "STARTUP"),
            ("Couldn't set model 'missing': unknown", "MODEL"),
            ("max turns reached", "MAX_TURNS"),
        ] {
            assert_eq!(classify_runtime_error(message).code, code);
        }
        let classified = classify_runtime_error(&"z".repeat(MAX_ERROR_BYTES * 2));
        assert!(classified.message.len() <= MAX_ERROR_BYTES);
    }

    #[test]
    fn manifest_hashes_regular_normalized_relative_files() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir(root.path().join("out")).unwrap();
        std::fs::write(root.path().join("out/result.txt"), b"hello").unwrap();
        let manifest = build_manifest(root.path(), &["out/result.txt".into()]).unwrap();
        assert_eq!(manifest[0].path, "out/result.txt");
        assert_eq!(manifest[0].bytes, 5);
        assert_eq!(
            manifest[0].sha256,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
    }

    #[test]
    fn manifest_rejects_traversal_symlinks_and_oversize_files() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(
            root.path().join("large"),
            vec![0; MAX_DELIVERABLE_BYTES as usize + 1],
        )
        .unwrap();
        assert_eq!(
            build_manifest(root.path(), &["../escape".into()])
                .unwrap_err()
                .code,
            "ARTIFACT_INVALID"
        );
        assert_eq!(
            build_manifest(root.path(), &["large".into()])
                .unwrap_err()
                .code,
            "ARTIFACT_INVALID"
        );

        #[cfg(unix)]
        {
            std::os::unix::fs::symlink("large", root.path().join("link")).unwrap();
            assert_eq!(
                build_manifest(root.path(), &["link".into()])
                    .unwrap_err()
                    .code,
                "ARTIFACT_INVALID"
            );
        }
    }

    #[test]
    fn receipt_write_is_atomic_bounded_and_preserves_canary() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("receipt.json");
        std::fs::write(&path, b"old").unwrap();
        let receipt = Receipt::success("request-1".into(), Some("CANARY-42".into()), vec![]);
        write_receipt_atomic(&path, &receipt).unwrap();
        let bytes = std::fs::read(&path).unwrap();
        assert!(bytes.len() <= MAX_RECEIPT_BYTES);
        let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(value["canary"], "CANARY-42");
        assert_eq!(value["status"], "success");
        assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 1);
    }

    #[test]
    fn required_sandbox_rejects_missing_or_unapplied_evidence() {
        let missing = SandboxRequest {
            required: true,
            profile: None,
        };
        assert_eq!(
            validate_sandbox_request(&missing).unwrap_err().code,
            "SANDBOX_REQUIRED"
        );
        assert!(sandbox_evidence_satisfies(true, Some("workspace"), false, false).is_err());
        assert!(sandbox_evidence_satisfies(true, Some("workspace"), true, false).is_ok());
        assert!(sandbox_evidence_satisfies(true, Some("workspace"), false, true).is_ok());
    }
}
