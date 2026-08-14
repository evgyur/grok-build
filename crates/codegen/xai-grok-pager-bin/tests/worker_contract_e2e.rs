use std::process::Command;

use serde_json::Value;
use xai_grok_test_support::{MockInferenceServer, TestSandbox, grok_binary};

fn request(
    receipt_root: &std::path::Path,
    body: Value,
) -> (std::path::PathBuf, std::path::PathBuf) {
    let request = receipt_root.join("request.json");
    let receipt = receipt_root.join("receipt.json");
    std::fs::write(&request, serde_json::to_vec(&body).unwrap()).unwrap();
    (request, receipt)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "requires a pre-built binary"]
async fn built_binary_worker_success_writes_correlated_receipt_and_manifest() {
    let server = MockInferenceServer::start().await.unwrap();
    server.set_response("WORKER-MOCK-RESPONSE");
    let mut sandbox = TestSandbox::builder().mock_url(server.url()).build();
    sandbox.set_env("GROK_SANDBOX", "off");
    std::fs::write(sandbox.workspace().join("artifact.txt"), b"artifact bytes").unwrap();
    let (request, receipt) = request(
        sandbox.root(),
        serde_json::json!({
            "contract_version": 1,
            "request_id": "worker-e2e-1",
            "prompt": "reply from the mock server",
            "cwd": sandbox.workspace(),
            "sandbox": {"required": false, "profile": "off"},
            "deliverables": ["artifact.txt"],
            "canary": "WORKER-CANARY-1"
        }),
    );
    let mut command = Command::new(grok_binary());
    sandbox.apply_to_std_command(&mut command);
    let output = command
        .args(["worker", "--request"])
        .arg(&request)
        .arg("--receipt")
        .arg(&receipt)
        .current_dir(sandbox.workspace())
        .output()
        .unwrap();

    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8_lossy(&output.stdout).contains("WORKER-MOCK-RESPONSE"));
    let receipt: Value = serde_json::from_slice(&std::fs::read(receipt).unwrap()).unwrap();
    assert_eq!(receipt["contract_version"], 1);
    assert_eq!(receipt["request_id"], "worker-e2e-1");
    assert_eq!(receipt["canary"], "WORKER-CANARY-1");
    assert_eq!(receipt["status"], "success");
    assert_eq!(receipt["deliverables"][0]["path"], "artifact.txt");
    assert_eq!(receipt["deliverables"][0]["bytes"], 14);
    let request_paths: Vec<_> = server
        .requests()
        .into_iter()
        .map(|entry| entry.path)
        .collect();
    assert_eq!(
        request_paths,
        ["/v1/models", "/v1/responses", "/v1/chat/completions"],
        "{}",
        server.request_log_summary()
    );
}

#[test]
#[ignore = "requires a pre-built binary"]
fn built_binary_worker_rejects_invalid_request_and_writes_bounded_receipt() {
    let sandbox = TestSandbox::new();
    let (request, receipt) = request(
        sandbox.root(),
        serde_json::json!({
            "contract_version": 2,
            "request_id": "worker-e2e-invalid",
            "prompt": "go"
        }),
    );
    let mut command = Command::new(grok_binary());
    sandbox.apply_to_std_command(&mut command);
    let output = command
        .args(["worker", "--request"])
        .arg(&request)
        .arg("--receipt")
        .arg(&receipt)
        .current_dir(sandbox.workspace())
        .output()
        .unwrap();

    assert!(!output.status.success());
    let bytes = std::fs::read(receipt).unwrap();
    assert!(bytes.len() <= 1024 * 1024);
    let receipt: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(receipt["status"], "failure");
    assert_eq!(receipt["error"]["code"], "REQUEST_INVALID");
}

#[test]
#[ignore = "requires a pre-built binary"]
fn built_binary_version_and_updater_policy_are_machine_readable() {
    let binary = grok_binary();
    let version = Command::new(&binary)
        .args(["version", "--json"])
        .output()
        .unwrap();
    assert!(version.status.success());
    let value: Value = serde_json::from_slice(&version.stdout).unwrap();
    assert_eq!(value["distribution"], "chip");
    assert_eq!(value["worker_contracts"], serde_json::json!([1]));
    assert_eq!(value["auto_update"], "externally-managed");
    for key in ["fork_commit", "upstream_commit", "upstream_source_rev"] {
        assert_eq!(value[key].as_str().unwrap().len(), 40, "{key}");
    }

    let update = Command::new(binary).arg("update").output().unwrap();
    assert!(!update.status.success());
    assert!(String::from_utf8_lossy(&update.stderr).contains("EXTERNALLY_MANAGED"));
}
