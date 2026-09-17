# BABY Development Guide

## Project Setup

### Prerequisites

- Python 3.10+
- pip or uv
- Git

### Initial Setup

```bash
# Clone the repository
git clone https://github.com/kuij-i/Baby.git
cd Baby

# Create virtual environment
python -m venv venv

# Activate virtual environment
# On macOS/Linux:
source venv/bin/activate

# On Windows:
venv\Scripts\activate

# Install development dependencies
pip install -e .[dev]
```

### Environment Configuration

```bash
# Copy example configuration
cp .env.example .env

# Edit .env with your configuration
# IMPORTANT: Never commit .env
```

## Development Workflow

### 1. Create a Feature Branch

```bash
# Update main
git checkout main
git pull origin main

# Create feature branch
git checkout -b feature/my-feature
```

### 2. Make Changes

```bash
# Edit files, create new modules, etc.
```

### 3. Write Tests

```bash
# Add tests for your changes
# Place in tests/ directory
# Follow naming: test_*.py
```

### 4. Run Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=baby tests/

# Run specific test file
pytest tests/test_contracts.py

# Run specific test
pytest tests/test_contracts.py::TestTaskId::test_task_id_creation

# Run unit tests only
pytest -m unit

# Run with verbose output
pytest -vv
```

### 5. Check Code Quality

```bash
# Validate formatting
black --check baby tests

# Lint
ruff check baby tests

# Type checking
mypy baby

# All foundation checks
black --check baby tests && ruff check baby tests && mypy baby && pytest
```

### 6. Commit and Push

```bash
# Stage changes
git add .

# Commit with clear message
git commit -m "feat: Add new agent capability"

# Push to remote
git push origin feature/my-feature
```

### 7. Create Pull Request

Create PR on GitHub with clear description of changes.

## Project Structure

```
baby/
├── __init__.py
├── core/
│   ├── __init__.py
│   └── contracts.py          # Core domain contracts
├── orchestration/            # Orchestration engine
├── planning/                 # Task planning
├── agents/                   # Agent base class and registry
│   ├── __init__.py
│   └── base.py               # Agent base class
├── tools/                    # Tool abstractions, registry, executor
│   ├── __init__.py
│   └── base.py               # Tool base class
├── permissions/              # Permission and approval management
│   ├── __init__.py
│   └── manager.py            # Permission manager
├── verification/             # Verification boundary
├── memory/                   # Replaceable in-memory memory store
├── audit/                    # Audit logging
│   ├── __init__.py
├── configuration.py          # Configuration management
├── logging.py                # Structured logging
├── errors.py                 # Exception types

tests/
├── __init__.py
├── test_contracts.py         # Contract tests
├── test_permissions.py       # Permission tests
├── test_agents.py            # Agent tests
├── test_tools.py             # Tool and approval tests
├── test_orchestration.py     # Orchestration and verification tests
├── test_memory.py            # Memory store tests
└── test_audit.py             # Audit tests

docs/
├── architecture.md           # Architecture overview
├── security.md               # Security model
└── development.md            # This file

.github/
└── copilot-instructions.md   # Copilot guidance
```

## Code Style

### Python Style

- Follow PEP 8
- Use type hints on all public functions
- Maximum line length: 120 characters
- Use double quotes for strings
- Use meaningful variable names

### Example

```python
from typing import Optional, Dict, Any
from baby.core import Task, AgentSpec, AgentResult

def execute_agent(
    task: Task,
    agent: AgentSpec,
    context: Dict[str, Any],
) -> Optional[AgentResult]:
    """Execute agent for the given task.
    
    Args:
        task: The task to execute
        agent: The agent to execute with
        context: Execution context
    
    Returns:
        Agent result or None if execution failed
    
    Raises:
        PermissionDeniedError: If agent lacks required permissions
    """
    # Implementation
    pass
```

### Docstrings

- Use Google-style docstrings
- Include description, args, returns, and raises
- Keep docstrings concise but complete

## Testing Guidelines

### Test Organization

```python
class TestComponentName:
    """Tests for ComponentName."""
    
    def test_specific_behavior(self) -> None:
        """Test description explaining what's being tested."""
        # Arrange
        input_data = ...
        
        # Act
        result = component.method(input_data)
        
        # Assert
        assert result.success
```

### Test Markers

```python
# Unit test (default)
@pytest.mark.unit
def test_something():
    pass

# Integration test
@pytest.mark.integration
async def test_async_integration():
    pass

# Slow test (optional)
@pytest.mark.slow
def test_expensive_operation():
    pass
```

### Coverage

Aim for >80% code coverage. View report:

```bash
pytest --cov=baby --cov-report=html tests/
open htmlcov/index.html
```

## Adding New Modules

### New Core Domain Contract

```python
# 1. Add to baby/core/contracts.py
class NewContract(BaseModel):
    """Description."""
    field1: str
    field2: int

# 2. Export from baby/core/__init__.py
from baby.core.contracts import NewContract
__all__ = [..., "NewContract"]

# 3. Add tests in tests/test_contracts.py
class TestNewContract:
    def test_creation(self) -> None:
        obj = NewContract(field1="test", field2=42)
        assert obj.field1 == "test"
```

### New Agent Type

```python
# 1. Create baby/agents/my_agent.py
from baby.core import AgentSpec, AgentCapability, Task, AgentResult

class MyAgent:
    """Description of agent."""
    
    def __init__(self) -> None:
        self.spec = AgentSpec(
            id=AgentId(id="my-agent"),
            name="My Agent",
            description="Description",
            role="role",
            capabilities=[...],
        )
    
    async def execute(self, task: Task) -> AgentResult:
        # Implementation
        pass

# 2. Add tests in tests/test_agents.py
class TestMyAgent:
    def test_execution(self) -> None:
        agent = MyAgent()
        task = Task(title="Test", description="Test")
        result = agent.execute(task)
        assert result.success
```

## Common Commands

```bash
# Install package in development mode
pip install -e .[dev]

# Run all tests
pytest

# Run tests with coverage
pytest --cov=baby tests/ --cov-report=term-missing

# Format code
black baby tests

# Lint code
ruff check baby tests

# Type check
mypy baby

# Sort imports
isort baby tests

# Run all checks
black baby tests && ruff check baby tests && mypy baby && isort baby tests && pytest

# Interactive Python with project loaded
python -c "from baby.core import Task; print(Task(title='test', description='test'))"
```

## Documentation

Keep documentation up to date:

- Update README.md when API changes
- Update architecture.md for structural changes
- Update security.md for permission changes
- Add docstrings to all public APIs
- Comment complex logic

## Debugging

### Using Python Debugger

```python
import pdb

pdb.set_trace()  # Break here
```

### Using Logging

```python
from baby.logging import get_logger

logger = get_logger(__name__)
logger.info("Starting task", task_id=task.id)
logger.error("Something failed", exc=exception)
```

### Running Single Test with Output

```bash
pytest -vv -s tests/test_contracts.py::TestTaskId::test_task_id_creation
```

## Current milestone

The repository currently has a hardened foundation: contracts, agent registry,
permission checks, approval storage, tool executor, planner/orchestrator,
verification boundary, and a simple in-memory memory store. Do not implement the
Coding Agent milestone until this foundation remains stable under tests.

## CI/CD

Future: GitHub Actions workflows will:
- Run tests on PR
- Check code quality
- Verify coverage
- Run security checks

## Performance

- Profile code with `cProfile` if performance issues arise
- Use async/await for I/O-bound operations
- Cache expensive operations
- Don't optimize prematurely

## Troubleshooting

### Import Errors

Make sure you've installed in development mode:
```bash
pip install -e .[dev]
```

### Test Failures

1. Run the test with verbose output: `pytest -vv tests/test_name.py`
2. Add `print()` statements for debugging
3. Check if dependencies are updated: `pip install -e .[dev] --upgrade`
4. Check if .env is configured correctly

### Type Checking Errors

Run `mypy baby` to see all type errors, then fix them incrementally.

## Getting Help

- Check existing issues on GitHub
- Review architecture.md and security.md
- Look at similar existing implementations
- Ask in GitHub discussions
