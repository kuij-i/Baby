# PHASE 7 Implementation Summary

**Status:** ✅ COMPLETE — verified by automated tests, Ruff, Black, and Mypy

## Scope & Implementation

Phase 7 establishes the repository-bounded Coding Agent capability and coding toolset for Baby.

### 1. Repository Boundary Enforcement (`baby/tools/coding/path_resolver.py`)

- **Strict Boundary Guarantee:** All filesystem interactions resolve against the configured `coding_repo_root` (`CODING_REPO_ROOT`).
- **Fail-Closed Protection:** If `coding_repo_root` is not configured or does not exist, operations immediately raise `ConfigurationError` without falling back to `cwd`, user home, or parent directories.
- **Traversal Defense:** Rejects `../` traversal, normalized escaping, and outside absolute paths by raising `PathTraversalError`.
- **Symlink Escape Defense:** Canonicalizes paths using `Path.resolve()` and enforces `target.is_relative_to(repo_root)`.
- **.git Protection:** Forbids access or modification to `.git` directory and its internal structures by raising `RepositoryBoundaryError`.

### 2. Repository-Bounded Coding Tools (`baby/tools/coding/`)

- **`ListFilesTool` (`list_files`):**
  - Permission: `LOCAL_READ` (`ALLOW`).
  - Lists directories within repository root with deterministic ordering.
  - Automatically filters out `.git` internals.
  - Limits recursion depth and total entries via configuration bounds.
- **`ReadFileTool` (`read_file`):**
  - Permission: `LOCAL_READ` (`ALLOW`).
  - Reads text files within repository boundary.
  - Enforces maximum file size limit (`coding_max_file_size_bytes`).
  - Validates UTF-8 encoding; rejects directories.
- **`WriteFileTool` (`write_file`):**
  - Permission: `LOCAL_WRITE` (`APPROVAL_REQUIRED` by default).
  - Creates or replaces files within repository boundary with parent directory creation.
  - Enforces fail-closed approval: unapproved write attempts raise `ApprovalRequiredError`, record `APPROVAL_REQUESTED` in audit log, and never execute.
  - Zero subprocess, zero shell invocation, zero automatic test execution.

### 3. Specialist Coding Agent (`baby/agents/coding/`)

- **`CodingAgent` (`baby/agents/coding/agent.py`):**
  - Subclasses `Agent` with `role="coding"`.
  - Capabilities: `read_code` (`LOCAL_READ`), `write_code` (`LOCAL_WRITE`).
  - Permissions: `LOCAL_READ` (allow), `LOCAL_WRITE` (approval required).
  - Integrates with `ModelProvider` abstraction (`complete_with_tools`).
  - Dispatches all actions through `ToolExecutor` to guarantee permission checking and audit event emission.

### 4. Configuration (`baby/configuration.py`)

- `coding_repo_root: Optional[str] = None`
- `coding_max_file_size_bytes: int = 1_048_576` (1 MB)
- `coding_max_list_entries: int = 1000`
- `coding_max_list_depth: int = 20`

### 5. Tests & Verification

- **Tests Before:** 127 passed.
- **Tests After:** 168 passed, 1 skipped (symlink privileges on Windows), 0 failed.
- Total coverage: 91% across `baby/`.
- AST static analysis verifies zero `subprocess`, `os.system`, or shell primitives in coding modules.

## Deliberate Constraints & Deferred Items

- Zero generic `run_command` or shell tools.
- No automatic execution of code written to repository.
- No Git CLI / git command execution via subprocess.
- No persistent multi-turn approval state machine yet (planned for future milestone).
- No Anthropic provider yet (OpenAI-compatible abstraction maintained).
