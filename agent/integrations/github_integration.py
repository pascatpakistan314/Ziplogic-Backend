"""
GitHub Integration for Autonomous PR/Issue Handling
Following Devin's approach with automatic PR reviews and issue resolution
"""
import os
import json
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime
import base64
import hmac
import hashlib

try:
    from github import Github, PullRequest, Issue
    from github.GithubException import GithubException
    GITHUB_AVAILABLE = True
except ImportError:
    print("Warning: PyGithub not installed. GitHub integration will be disabled.")
    print("Install with: pip install PyGithub")
    GITHUB_AVAILABLE = False
    Github = None
    PullRequest = None
    Issue = None
    GithubException = Exception
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
import aiohttp
import yaml

class GitHubPRHandler:
    """
    Handles GitHub PR reviews, issue triaging, and automated fixes
    Similar to Devin's GitHub integration
    """
    
    def __init__(self, github_token: str = None, webhook_secret: str = None):
        if not GITHUB_AVAILABLE:
            raise ImportError("GitHub integration requires PyGithub. Install with: pip install PyGithub")
        
        self.github_token = github_token or os.getenv("GITHUB_TOKEN")
        self.webhook_secret = webhook_secret or os.getenv("GITHUB_WEBHOOK_SECRET")
        
        if not self.github_token:
            raise ValueError("GitHub token required. Set GITHUB_TOKEN environment variable.")
        
        self.github = Github(self.github_token)
        self.llm = ChatAnthropic(
            model="claude-3-5-sonnet-20241022",
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY")
        )
    
    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """Verify GitHub webhook signature for security"""
        if not self.webhook_secret:
            return True  # Skip verification if no secret configured
        
        expected_signature = hmac.new(
            self.webhook_secret.encode(),
            payload,
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(
            f"sha256={expected_signature}",
            signature
        )
    
    async def analyze_pr(self, pr: PullRequest) -> Dict[str, Any]:
        """
        Analyze a pull request for code quality, security, and best practices
        """
        
        # Get PR details
        files = pr.get_files()
        commits = pr.get_commits()
        
        analysis = {
            "pr_number": pr.number,
            "title": pr.title,
            "description": pr.body,
            "files_changed": pr.changed_files,
            "additions": pr.additions,
            "deletions": pr.deletions,
            "checks": []
        }
        
        # Analyze each file
        for file in files:
            if file.status == "removed":
                continue
            
            file_analysis = await self._analyze_file(file)
            analysis["checks"].append(file_analysis)
        
        # Generate overall review
        review = await self._generate_pr_review(analysis)
        analysis["review"] = review
        
        return analysis
    
    async def _analyze_file(self, file) -> Dict[str, Any]:
        """Analyze a single file for issues"""
        
        # Get file content
        try:
            content = base64.b64decode(
                self.github.get_repo(file.repository.full_name)
                .get_contents(file.filename, ref=file.sha)
                .content
            ).decode('utf-8')
        except:
            content = file.patch if file.patch else ""
        
        # Determine file type
        file_ext = os.path.splitext(file.filename)[1]
        language = self._detect_language(file_ext)
        
        analysis_prompt = f"""
        Analyze this code change for potential issues:
        
        File: {file.filename}
        Language: {language}
        Status: {file.status}
        Changes: +{file.additions} -{file.deletions}
        
        Code/Patch:
        ```{language}
        {content[:3000]}  # Limit for token size
        ```
        
        Check for:
        1. Bugs or logical errors
        2. Security vulnerabilities
        3. Performance issues
        4. Code style violations
        5. Missing tests
        6. Documentation needs
        
        Respond with JSON:
        {{
            "issues": [
                {{
                    "line": line_number,
                    "severity": "error|warning|info",
                    "message": "description",
                    "suggestion": "fix suggestion"
                }}
            ],
            "overall_quality": "good|needs_improvement|poor",
            "requires_changes": true|false
        }}
        """
        
        response = self.llm.invoke(analysis_prompt)
        
        try:
            result = json.loads(response.content)
            result["file"] = file.filename
            return result
        except:
            return {
                "file": file.filename,
                "issues": [],
                "overall_quality": "unknown",
                "requires_changes": False
            }
    
    def _detect_language(self, file_ext: str) -> str:
        """Detect programming language from file extension"""
        language_map = {
            '.py': 'python',
            '.js': 'javascript',
            '.ts': 'typescript',
            '.java': 'java',
            '.go': 'go',
            '.rs': 'rust',
            '.cpp': 'cpp',
            '.c': 'c',
            '.rb': 'ruby',
            '.php': 'php',
            '.cs': 'csharp',
            '.swift': 'swift',
            '.kt': 'kotlin',
            '.scala': 'scala',
            '.r': 'r',
            '.sql': 'sql',
            '.sh': 'bash',
            '.yml': 'yaml',
            '.json': 'json',
            '.xml': 'xml',
            '.html': 'html',
            '.css': 'css',
        }
        return language_map.get(file_ext.lower(), 'text')
    
    async def _generate_pr_review(self, analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Generate comprehensive PR review"""
        
        # Count issues by severity
        all_issues = []
        for check in analysis.get("checks", []):
            all_issues.extend(check.get("issues", []))
        
        errors = sum(1 for i in all_issues if i["severity"] == "error")
        warnings = sum(1 for i in all_issues if i["severity"] == "warning")
        info = sum(1 for i in all_issues if i["severity"] == "info")
        
        review_prompt = f"""
        Generate a comprehensive pull request review based on this analysis:
        
        PR Title: {analysis['title']}
        Files Changed: {analysis['files_changed']}
        Lines: +{analysis['additions']} -{analysis['deletions']}
        
        Issues Found:
        - Errors: {errors}
        - Warnings: {warnings}
        - Info: {info}
        
        Detailed Issues:
        {json.dumps(all_issues[:10], indent=2)}  # First 10 issues
        
        Provide a professional review with:
        1. Summary of findings
        2. Critical issues that must be fixed
        3. Suggestions for improvement
        4. Positive feedback on good practices
        5. Overall recommendation (approve/request_changes/comment)
        """
        
        response = self.llm.invoke(review_prompt)
        
        # Determine review decision
        if errors > 0:
            decision = "REQUEST_CHANGES"
        elif warnings > 2:
            decision = "COMMENT"
        else:
            decision = "APPROVE"
        
        return {
            "body": response.content,
            "event": decision,
            "comments": self._format_inline_comments(all_issues)
        }
    
    def _format_inline_comments(self, issues: List[Dict]) -> List[Dict]:
        """Format issues as inline PR comments"""
        comments = []
        
        for issue in issues[:20]:  # Limit to 20 comments
            if "line" in issue:
                comments.append({
                    "path": issue.get("file", ""),
                    "line": issue["line"],
                    "body": f"**{issue['severity'].upper()}**: {issue['message']}\n\n"
                           f"💡 **Suggestion**: {issue.get('suggestion', 'N/A')}"
                })
        
        return comments
    
    async def submit_pr_review(self, repo_name: str, pr_number: int, 
                               review: Dict[str, Any]) -> bool:
        """Submit review to GitHub PR"""
        
        try:
            repo = self.github.get_repo(repo_name)
            pr = repo.get_pull(pr_number)
            
            # Create review
            pr.create_review(
                body=review["body"],
                event=review["event"],
                comments=review.get("comments", [])
            )
            
            # Add labels based on review
            if review["event"] == "REQUEST_CHANGES":
                pr.add_to_labels("needs-work")
            elif review["event"] == "APPROVE":
                pr.add_to_labels("approved")
            
            return True
            
        except GithubException as e:
            print(f"Error submitting review: {e}")
            return False
    
    async def handle_issue(self, issue: Issue) -> Dict[str, Any]:
        """
        Automatically handle GitHub issues
        - Triage and label
        - Generate solution
        - Create PR if possible
        """
        
        analysis = await self._analyze_issue(issue)
        
        # Auto-label the issue
        await self._label_issue(issue, analysis)
        
        # Generate solution if it's a bug
        if analysis.get("type") == "bug":
            solution = await self._generate_bug_fix(issue, analysis)
            
            if solution.get("can_auto_fix"):
                # Create a PR with the fix
                pr_url = await self._create_fix_pr(issue, solution)
                
                # Comment on issue
                issue.create_comment(
                    f"🤖 I've analyzed this issue and created a fix!\n\n"
                    f"**PR**: {pr_url}\n\n"
                    f"**Solution**: {solution['description']}\n\n"
                    f"Please review the proposed changes."
                )
            else:
                # Just comment with analysis
                issue.create_comment(
                    f"🤖 Issue Analysis:\n\n"
                    f"**Type**: {analysis['type']}\n"
                    f"**Severity**: {analysis['severity']}\n"
                    f"**Components**: {', '.join(analysis['components'])}\n\n"
                    f"**Suggested Approach**:\n{solution.get('approach', 'N/A')}"
                )
        
        return analysis
    
    async def _analyze_issue(self, issue: Issue) -> Dict[str, Any]:
        """Analyze an issue to determine type and severity"""
        
        analysis_prompt = f"""
        Analyze this GitHub issue:
        
        Title: {issue.title}
        Body: {issue.body}
        Labels: {[l.name for l in issue.labels]}
        
        Determine:
        1. Issue type (bug, feature, enhancement, documentation, question)
        2. Severity (critical, high, medium, low)
        3. Affected components
        4. Required expertise
        5. Estimated effort
        
        Respond with JSON:
        {{
            "type": "bug|feature|enhancement|documentation|question",
            "severity": "critical|high|medium|low",
            "components": ["list", "of", "components"],
            "expertise": ["required", "skills"],
            "effort": "small|medium|large",
            "can_auto_fix": true|false
        }}
        """
        
        response = self.llm.invoke(analysis_prompt)
        
        try:
            return json.loads(response.content)
        except:
            return {
                "type": "unknown",
                "severity": "medium",
                "components": [],
                "expertise": [],
                "effort": "medium",
                "can_auto_fix": False
            }
    
    async def _label_issue(self, issue: Issue, analysis: Dict[str, Any]):
        """Automatically label issues based on analysis"""
        
        labels = []
        
        # Type labels
        if analysis["type"]:
            labels.append(analysis["type"])
        
        # Severity labels
        if analysis["severity"] in ["critical", "high"]:
            labels.append(f"priority-{analysis['severity']}")
        
        # Effort labels
        if analysis["effort"]:
            labels.append(f"effort-{analysis['effort']}")
        
        # Component labels
        for component in analysis.get("components", [])[:3]:
            labels.append(f"component-{component}")
        
        # Apply labels
        try:
            issue.add_to_labels(*labels)
        except:
            pass
    
    async def _generate_bug_fix(self, issue: Issue, analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a fix for a bug"""
        
        fix_prompt = f"""
        Generate a solution for this bug:
        
        Issue: {issue.title}
        Description: {issue.body}
        Components: {analysis['components']}
        
        Provide:
        1. Root cause analysis
        2. Solution approach
        3. Code changes needed
        4. Test cases
        
        If you can generate the exact fix, provide it.
        """
        
        response = self.llm.invoke(fix_prompt)
        
        return {
            "description": response.content,
            "can_auto_fix": "```" in response.content,  # Has code
            "approach": response.content
        }
    
    async def _create_fix_pr(self, issue: Issue, solution: Dict[str, Any]) -> str:
        """Create a PR with the bug fix"""
        
        repo = issue.repository
        
        # Create branch
        branch_name = f"fix-issue-{issue.number}"
        base_branch = repo.default_branch
        
        try:
            # Get base branch ref
            base_ref = repo.get_git_ref(f"heads/{base_branch}")
            
            # Create new branch
            repo.create_git_ref(
                f"refs/heads/{branch_name}",
                base_ref.object.sha
            )
            
            # Extract code changes from solution
            # This is simplified - in production, parse the solution properly
            
            # Create PR
            pr = repo.create_pull(
                title=f"Fix: {issue.title} (#{issue.number})",
                body=f"Fixes #{issue.number}\n\n## Solution\n{solution['description']}",
                head=branch_name,
                base=base_branch
            )
            
            return pr.html_url
            
        except Exception as e:
            print(f"Error creating PR: {e}")
            return ""

class GitHubActionsWorkflow:
    """
    Creates GitHub Actions workflows for automated agent execution
    """
    
    @staticmethod
    def generate_pr_review_workflow() -> str:
        """Generate GitHub Actions workflow for PR reviews"""
        
        workflow = """name: AI Agent PR Review

on:
  pull_request:
    types: [opened, synchronize, reopened]
  pull_request_review_comment:
    types: [created]

jobs:
  review:
    runs-on: ubuntu-latest
    
    steps:
    - name: Checkout code
      uses: actions/checkout@v3
      with:
        fetch-depth: 0
    
    - name: Setup Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'
    
    - name: Install dependencies
      run: |
        pip install -r requirements.txt
    
    - name: Run AI Agent Review
      env:
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        PR_NUMBER: ${{ github.event.pull_request.number }}
        REPO_NAME: ${{ github.repository }}
      run: |
        python -c "
        from agent.integrations.github_integration import GitHubPRHandler
        import asyncio
        import os
        
        handler = GitHubPRHandler()
        repo = handler.github.get_repo(os.environ['REPO_NAME'])
        pr = repo.get_pull(int(os.environ['PR_NUMBER']))
        
        result = asyncio.run(handler.analyze_pr(pr))
        asyncio.run(handler.submit_pr_review(
            os.environ['REPO_NAME'],
            int(os.environ['PR_NUMBER']),
            result['review']
        ))
        "
    
    - name: Post Review Results
      if: always()
      uses: actions/github-script@v6
      with:
        script: |
          console.log('Review completed and posted to PR')
"""
        return workflow
    
    @staticmethod
    def generate_issue_handler_workflow() -> str:
        """Generate GitHub Actions workflow for issue handling"""
        
        workflow = """name: AI Agent Issue Handler

on:
  issues:
    types: [opened, labeled]
  issue_comment:
    types: [created]

jobs:
  handle:
    runs-on: ubuntu-latest
    if: |
      github.event.issue.pull_request == null &&
      (github.event.action == 'opened' || 
       contains(github.event.label.name, 'ai-agent'))
    
    steps:
    - name: Checkout code
      uses: actions/checkout@v3
    
    - name: Setup Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'
    
    - name: Install dependencies
      run: |
        pip install -r requirements.txt
    
    - name: Analyze and Handle Issue
      env:
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        ISSUE_NUMBER: ${{ github.event.issue.number }}
        REPO_NAME: ${{ github.repository }}
      run: |
        python -c "
        from agent.integrations.github_integration import GitHubPRHandler
        import asyncio
        import os
        
        handler = GitHubPRHandler()
        repo = handler.github.get_repo(os.environ['REPO_NAME'])
        issue = repo.get_issue(int(os.environ['ISSUE_NUMBER']))
        
        asyncio.run(handler.handle_issue(issue))
        "
"""
        return workflow
    
    @staticmethod
    def generate_scheduled_agent_workflow() -> str:
        """Generate workflow for scheduled agent tasks"""
        
        workflow = """name: Scheduled Agent Tasks

on:
  schedule:
    - cron: '0 */6 * * *'  # Every 6 hours
  workflow_dispatch:  # Manual trigger

jobs:
  maintenance:
    runs-on: ubuntu-latest
    
    steps:
    - name: Checkout code
      uses: actions/checkout@v3
    
    - name: Setup Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'
    
    - name: Install dependencies
      run: |
        pip install -r requirements.txt
    
    - name: Run Maintenance Tasks
      env:
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      run: |
        python -c "
        from agent.orchestrator.multi_agent_orchestrator import MultiAgentOrchestrator
        import asyncio
        
        orchestrator = MultiAgentOrchestrator()
        
        # Run maintenance tasks
        tasks = [
            'Update dependencies if outdated',
            'Check for security vulnerabilities',
            'Update documentation if needed',
            'Clean up old branches'
        ]
        
        for task in tasks:
            asyncio.run(orchestrator.orchestrate(task))
        "
"""
        return workflow

# LangChain tools for GitHub integration
@tool
async def review_pull_request(repo_name: str, pr_number: int) -> str:
    """
    Review a GitHub pull request automatically.
    
    Args:
        repo_name: Repository name (owner/repo)
        pr_number: Pull request number
        
    Returns:
        Review results
    """
    handler = GitHubPRHandler()
    
    try:
        repo = handler.github.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        
        # Analyze PR
        analysis = await handler.analyze_pr(pr)
        
        # Submit review
        success = await handler.submit_pr_review(
            repo_name,
            pr_number,
            analysis["review"]
        )
        
        if success:
            return f"""
✅ PR Review Submitted

PR: #{pr_number} - {pr.title}
Decision: {analysis['review']['event']}
Files Analyzed: {analysis['files_changed']}
Issues Found: {len(analysis['review']['comments'])}

Review posted to GitHub successfully.
"""
        else:
            return "❌ Failed to submit review to GitHub"
            
    except Exception as e:
        return f"❌ Error reviewing PR: {str(e)}"

@tool
def create_github_workflow(workflow_type: str = "pr_review", workspace: str = ".") -> str:
    """
    Create a GitHub Actions workflow file.
    
    Args:
        workflow_type: Type of workflow (pr_review, issue_handler, scheduled)
        workspace: Repository root directory
        
    Returns:
        Generated workflow YAML
    """
    from pathlib import Path
    
    generator = GitHubActionsWorkflow()
    
    if workflow_type == "pr_review":
        workflow = generator.generate_pr_review_workflow()
    elif workflow_type == "issue_handler":
        workflow = generator.generate_issue_handler_workflow()
    elif workflow_type == "scheduled":
        workflow = generator.generate_scheduled_agent_workflow()
    else:
        return f"Unknown workflow type: {workflow_type}"
    
    # Save to file
    workspace_path = Path(workspace)
    workflow_dir = workspace_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    
    workflow_file = workflow_dir / f"ai_agent_{workflow_type}.yml"
    workflow_file.write_text(workflow)
    
    return f"""
✅ GitHub Actions Workflow Created

File: {workflow_file}
Type: {workflow_type}

To use:
1. Commit this file to your repository
2. Add required secrets in GitHub:
   - ANTHROPIC_API_KEY
   - GITHUB_TOKEN (usually automatic)
3. The workflow will trigger automatically

Workflow saved successfully!
"""

# Export GitHub tools
github_integration_tools = [review_pull_request, create_github_workflow]
