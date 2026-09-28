# Contributing to Local Knowledge Hub

Thank you for your interest in contributing to Local Knowledge Hub! We welcome issues, suggestions, and pull requests to help improve developer productivity with local context for AI coding agents.

## Code of Conduct

Please be respectful, constructive, and collaborative in all discussions, issues, and pull requests.

## How to Contribute

### Reporting Issues

- Check existing issues before opening a new one to avoid duplicates.
- Provide clear steps to reproduce, including your operating system (macOS or Windows), Python version, and relevant log outputs.

### Development and Testing

1. Clone the repository and set up a Python 3.11+ environment:
   ```bash
   git clone https://github.com/aoright/local-knowledge-hub.git
   cd local-knowledge-hub
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\Activate.ps1
   pip install -r app/requirements.txt
   ```

2. Run the test suite:
   ```bash
   python -m unittest discover -s app/tests -v
   ```

3. Ensure all tests pass and checksum manifests remain consistent before submitting changes.

### Submitting Pull Requests

1. Fork the repository and create a descriptive branch:
   ```bash
   git checkout -b fix/issue-description
   ```
2. Commit your changes with clear, standard commit messages.
3. Open a Pull Request against the `main` branch with a summary of changes, motivation, and verification steps.

## Licensing

By contributing to Local Knowledge Hub, you agree that your contributions will be licensed under the project's MIT License.
