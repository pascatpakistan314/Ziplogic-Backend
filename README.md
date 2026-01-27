# SWE Agent v2.0 with LangGraph - Enhanced Edition

<img src="./static/cover.png" width="400" alt="Cover">

![Stable](https://img.shields.io/badge/status-stable-green) ![Python](https://img.shields.io/badge/python-3.12+-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![Claude](https://img.shields.io/badge/Claude-Opus%204-purple)

A comprehensive AI-powered software engineering platform that automates the entire development lifecycle - from planning and implementation to testing, review, and deployment. Built with LangGraph for orchestrated multi-agent workflows and powered by Claude Opus 4.

## 🚀 What's New in v2.0

- **🧪 Automated Testing Agent**: Generates and executes comprehensive test suites
- **👁️ Code Review Agent**: Performs security, quality, and best practice analysis  
- **🔒 Security Scanner**: OWASP compliance and vulnerability detection
- **⚡ Performance Analyzer**: Profiling, complexity analysis, and optimization
- **📚 Documentation Generator**: Auto-generates README, API docs, and docstrings
- **🔄 Git Integration**: Automated branching, commits, and version control
- **🐛 Debugging Assistant**: Error analysis and fix suggestions
- **🌐 REST API Server**: Full-featured API with WebSocket support
- **💻 CLI Interface**: Rich command-line tools for all operations
- **📊 Real-time Monitoring**: WebSocket-based progress tracking

## 🏗️ Architecture Overview

The system uses a sophisticated multi-agent architecture where specialized agents handle different aspects of software development:

### Core Agents

1. **Architect Agent** - Research & Planning
   - Analyzes requirements
   - Researches codebase
   - Creates implementation plans

2. **Developer Agent** - Implementation
   - Executes plans step-by-step
   - Modifies code with precision
   - Creates new files

3. **Tester Agent** - Quality Assurance
   - Generates comprehensive test cases
   - Executes test suites
   - Analyzes code coverage

4. **Reviewer Agent** - Code Review
   - Checks code quality
   - Scans for security issues
   - Validates best practices

### Workflow Modes

- **Full Workflow**: Complete development cycle (Plan → Develop → Test → Review)
- **Development Only**: Quick implementation without testing
- **Test & Review**: Quality checks on existing code
- **Custom Pipeline**: Mix and match agents as needed

## 📦 Installation

```bash
# Clone the repository
git clone https://github.com/your-org/swe-agent.git
cd swe-agent

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Or install with pip
pip install -e .
```

## 🎯 Quick Start

### CLI Usage

```bash
# Execute a development task
swe-agent execute "Create a REST API for user management"

# Run with specific mode
swe-agent execute "Add authentication" --mode dev

# Test existing code
swe-agent test ./tests --coverage

# Review code quality
swe-agent review src/main.py --security --complexity

# Start API server
swe-agent serve --port 8000
```

### Python Usage

```python
from agent.graph_v2 import enhanced_swe_agent

# Execute a task
result = enhanced_swe_agent.invoke({
    "implementation_research_scratchpad": [
        {"role": "user", "content": "Implement user authentication with JWT"}
    ],
    "workflow_mode": "full"
})

print(f"Files modified: {result['files_modified']}")
print(f"Tests passed: {result['test_validation_passed']}")
print(f"Review approved: {result['code_review_approved']}")
```

### API Usage

```bash
# Start the server
python api_server.py

# Execute a task via API
curl -X POST "http://localhost:8000/agent/execute" \
  -H "Content-Type: application/json" \
  -d '{"task_description": "Create user authentication"}'
```

## 🛠️ Features

### Development Capabilities
- ✨ Intelligent code generation with Claude Opus 4
- 🔍 Semantic code analysis and understanding
- 📝 Atomic task decomposition for safe changes
- 🔄 Incremental development approach
- 🎯 Pattern recognition and reuse

### Testing Features
- 🧪 Automated unit test generation
- 📊 Code coverage analysis
- 🔧 Test fixture and mock creation
- ✅ Test quality validation
- 🏃 Multi-framework support (pytest, jest, mocha)

### Code Quality Tools
- 🔍 Static analysis and linting
- 📈 Complexity metrics
- 🐛 Code smell detection
- 📋 Best practices validation
- 🎨 Automatic code formatting

### Security Features
- 🔒 Vulnerability scanning
- 🔑 Secret detection
- 🛡️ OWASP compliance checking
- 📦 Dependency security analysis
- 🔐 Security header generation

### Performance Tools
- ⚡ Code profiling
- 📊 Time complexity analysis
- 💾 Memory leak detection
- 🗄️ Database query optimization
- 💼 Caching strategy suggestions

### Documentation
- 📚 README generation
- 📖 API documentation
- 💬 Docstring generation
- 📊 Documentation coverage analysis
- 📝 Changelog generation

### Git Integration
- 🌿 Automatic branch creation
- 💾 Smart commit messages
- 🔄 Merge conflict handling
- 📜 Blame analysis
- 📦 Stash management

## 📡 API Endpoints

| Endpoint | Method | Description |
|----------|---------|-------------|
| `/agent/execute` | POST | Execute full agent workflow |
| `/agent/status/{job_id}` | GET | Get job status |
| `/agent/test` | POST | Run tests only |
| `/agent/review` | POST | Run code review |
| `/agent/analyze` | POST | Analyze codebase |
| `/agent/optimize` | POST | Optimize code |
| `/agent/debug` | POST | Debug errors |
| `/ws/{job_id}` | WS | WebSocket for real-time updates |

## 🔧 Configuration

Create a `.env` file:

```env
ANTHROPIC_API_KEY=your_claude_api_key
LANGCHAIN_API_KEY=your_langchain_key
WORKSPACE_DIR=./workspace_repo
LOG_LEVEL=INFO
```

## 📚 Tool Categories

### Testing Tools
- `generate_unit_test` - Create unit tests
- `run_tests` - Execute test suites
- `analyze_test_coverage` - Coverage analysis
- `generate_test_fixtures` - Create test data
- `validate_test_quality` - Check test quality

### Quality Tools
- `run_linter` - Static analysis
- `check_type_hints` - Type checking
- `analyze_code_complexity` - Complexity metrics
- `detect_code_smells` - Find anti-patterns
- `check_best_practices` - Validate practices

### Security Tools
- `scan_vulnerabilities` - Security scanning
- `check_dependencies_security` - Dependency audit
- `detect_secrets` - Find hardcoded secrets
- `check_owasp_compliance` - OWASP validation
- `generate_security_headers` - Security configs

### Performance Tools
- `profile_code` - Performance profiling
- `analyze_time_complexity` - Algorithm analysis
- `detect_memory_leaks` - Memory analysis
- `optimize_database_queries` - Query optimization
- `suggest_caching_strategies` - Cache recommendations

### Documentation Tools
- `generate_docstring` - Create docstrings
- `generate_readme` - README generation
- `generate_api_docs` - API documentation
- `check_documentation_coverage` - Doc coverage
- `generate_changelog` - Changelog creation

### Git Tools
- `git_status` - Repository status
- `create_branch` - Branch management
- `commit_changes` - Smart commits
- `generate_commit_message` - Message generation
- `handle_merge_conflict` - Conflict resolution

### Debugging Tools
- `analyze_error_message` - Error analysis
- `add_error_handling` - Add try-catch
- `add_logging` - Insert logging
- `analyze_stack_trace` - Stack analysis
- `create_debug_wrapper` - Debug wrapping

## 🤝 Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## 📄 License

This project is licensed under the MIT License - see [LICENSE](LICENSE) for details.

## 🙏 Acknowledgments

- Built with [LangGraph](https://github.com/langchain-ai/langgraph) and [LangChain](https://github.com/langchain-ai/langchain)
- Powered by [Claude Opus 4](https://anthropic.com) from Anthropic
- Inspired by [SWE-bench](https://www.swebench.com/) and modern AI-assisted development

## 📞 Support

- 📧 Email: support@swe-agent.ai
- 💬 Discord: [Join our community](https://discord.gg/swe-agent)
- 🐛 Issues: [GitHub Issues](https://github.com/your-org/swe-agent/issues)
- 📖 Docs: [Full Documentation](https://docs.swe-agent.ai)

## 🚀 Roadmap

- [ ] Multi-language support expansion
- [ ] Cloud deployment options
- [ ] Visual Studio Code extension
- [ ] Team collaboration features
- [ ] Custom model fine-tuning
- [ ] Kubernetes deployment tools
- [ ] GraphQL API support
- [ ] Mobile app development agents

---

**Built with ❤️ by the AI Engineering Community**
