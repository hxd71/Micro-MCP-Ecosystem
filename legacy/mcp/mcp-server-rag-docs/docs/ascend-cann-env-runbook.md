# Ascend CANN Environment Runbook

## Scenario

Inference service fails during startup with CANN, ACL, OPP, or device initialization errors.

## Checks

- `ASCEND_HOME_PATH` should point to the installed Ascend toolkit directory.
- `ASCEND_OPP_PATH` should point to the operator package path.
- Runtime library paths should include Ascend toolkit libraries.
- In containers, `/dev/davinci*`, driver libraries, and related runtime mounts must be visible.
- `npu-smi info` should work under the same user that starts the inference service.

## Symptoms

- `acl init failed`
- `aclrtSetDevice failed`
- `OPP path not found`
- `npu-smi not found`
- process can start on CPU path but fails when binding Ascend device

## Safe Actions

- Re-load the CANN environment script in the current shell.
- Compare environment variables between interactive shell and service manager.
- Re-run diagnostics before restarting the service.

## Approval Required

- Restarting the serving process.
- Changing system-level environment files.
- Modifying container runtime device mounts.
