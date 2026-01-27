"""Simple API server for SWE Agent with proper authentication and async-safe agent calls"""

from fastapi import FastAPI, Depends, HTTPException, status, Request, WebSocket, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, APIKeyHeader
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, Set, List
from starlette.concurrency import run_in_threadpool
from contextlib import asynccontextmanager

# Import authentication
from auth import (
    UserCreate, UserLogin, AuthResponse, ForgotPasswordRequest, ResetPasswordRequest,
    login_user, signup_user, forgot_password, reset_password, verify_token, logout_user, refresh_token
)

# Import database
from database import db

# Import file management tools
from agent.tools.write import write_tools_map, get_files_structure

# Import WebSocket handler
from websocket_handler import websocket_endpoint, get_connection_manager

import os
import asyncio
from redis import asyncio as aioredis
from pathlib import Path
from dotenv import load_dotenv
import logging

# ===================== ENV & LOGGING =====================
# Load .env from the script directory FIRST, then from CWD as fallback.
_own_env = Path(__file__).with_name(".env")
if _own_env.exists():
    load_dotenv(dotenv_path=_own_env, override=False)
load_dotenv(override=False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("swe_api")

def _clean_token(s: str) -> str:
    # Trim whitespace and surrounding quotes
    return s.strip().strip('"').strip("'")

def _split_tokens(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    parts = [raw]
    for sep in (",", ";", " ", "\n", "\t"):
        parts = [p for chunk in parts for p in chunk.split(sep)]
    return [_clean_token(p) for p in parts if _clean_token(p)]

def get_allowed_tokens() -> Set[str]:
    # Supports either a single token or multiple
    single = _split_tokens(os.getenv("SWE_API_TOKEN", ""))
    multi = _split_tokens(os.getenv("SWE_API_TOKENS", ""))
    return set(single + multi)

ALLOW_QUERY_TOKEN = os.getenv("ALLOW_QUERY_TOKEN", "false").lower() == "true"
AUTH_DEBUG = os.getenv("SWE_AUTH_DEBUG", "false").lower() == "true"
WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", "./workspace_repo")

# Anthropic key for the underlying agent
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    logger.error("ANTHROPIC_API_KEY not found in environment variables! Add it to your .env.")
else:
    logger.info("Anthropic API key loaded")
    os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY  # ensure downstream libs see it

# Import the ORCHESTRATED agent AFTER env is ready
try:
    # Try to use the orchestrated agent with all features
    from agent.orchestrated_agent import orchestrated_swe_agent_compatible as swe_agent
    logger.info("Using ORCHESTRATED agent with multi-agent support, GitHub integration, and multi-language features")
except ImportError as e:
    # Fallback to basic agent if orchestrated fails
    logger.warning(f"Could not load orchestrated agent: {e}")
    logger.info("Falling back to basic agent")
    from agent.graph import swe_agent

# ===================== LIFESPAN CONTEXT MANAGER =====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    toks = list(get_allowed_tokens())
    if toks:
        masks = [f"...{t[-4:]}" if len(t) >= 4 else "..." for t in toks]
        logger.info(f"Auth tokens configured: {len(toks)} ({', '.join(masks)})")
    else:
        logger.warning("No SWE_API_TOKEN/SWE_API_TOKENS set. Auth will reject all requests.")

    # Ensure workspace directory exists
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    logger.info(f"Workspace directory ready: {WORKSPACE_DIR}")

    yield

    # Shutdown logic (if needed in future)
    logger.info("Application shutting down...")

# ===================== FASTAPI APP =====================
app = FastAPI(
    title="SWE Agent API",
    description="AI-powered Software Engineering Agent",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===================== AUTH =====================
bearer_scheme = HTTPBearer(auto_error=False)
x_api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)

async def require_auth(
    request: Request,
    bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    x_api_key: Optional[str] = Depends(x_api_key_scheme),
) -> None:
    allowed = get_allowed_tokens()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server misconfigured: set SWE_API_TOKEN or SWE_API_TOKENS in the environment.",
        )

    provided: Optional[str] = None
    # Prefer Authorization: Bearer <token>
    if bearer and (bearer.scheme or "").lower() == "bearer":
        provided = bearer.credentials
    # Or X-API-Key: <token>
    if not provided and x_api_key:
        provided = x_api_key
    # Optional: allow ?token= for quick local testing (set ALLOW_QUERY_TOKEN=true in .env)
    if not provided and ALLOW_QUERY_TOKEN:
        provided = request.query_params.get("token")

    if not provided or _clean_token(provided) not in allowed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token.",
        )

# ===================== MODELS =====================
class AgentRequest(BaseModel):
    task: str = Field(..., description="Task description")
    context: Optional[str] = Field(None, description="Additional context")

class AgentResponse(BaseModel):
    success: bool
    implementation_plan: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

class FileCreateRequest(BaseModel):
    path: str = Field(..., description="File path relative to workspace")
    content: str = Field(..., description="File content")

class FileUpdateRequest(BaseModel):
    path: str = Field(..., description="File path relative to workspace")
    content: str = Field(..., description="New file content")

class FileResponse(BaseModel):
    success: bool
    message: str
    path: Optional[str] = None

class FileContentResponse(BaseModel):
    success: bool
    content: Optional[str] = None
    error: Optional[str] = None

class FilesStructureResponse(BaseModel):
    success: bool
    structure: Optional[str] = None
    error: Optional[str] = None

# ===================== AGENT CALL (ASYNC-SAFE) =====================
async def run_agent(payload: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    """
    Thin wrapper around swe_agent.invoke/ainvoke that forwards extra kwargs
    (e.g., config=...) to support LangGraph runtime options like checkpointing.
    """
    ainvoke = getattr(swe_agent, "ainvoke", None)
    if callable(ainvoke):
        return await ainvoke(payload, **kwargs)
    # Threadpool fallback for sync .invoke
    import functools
    return await run_in_threadpool(functools.partial(swe_agent.invoke, payload, **kwargs))

# ===================== WEBSOCKET FILE SYNCHRONIZATION =====================
async def sync_workspace_files_to_frontend(user_id: str):
    """Send all workspace files to frontend via WebSocket"""
    try:
        connection_manager = get_connection_manager()

        # Get all files from workspace_repo
        workspace_files = {}

        # Collect all files recursively
        for root, dirs, files in os.walk(WORKSPACE_DIR):
            # Skip .git and other hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.')]

            for file in files:
                if file.startswith('.'):
                    continue

                file_path = os.path.join(root, file)
                # Get relative path from workspace root
                rel_path = os.path.relpath(file_path, WORKSPACE_DIR)

                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        workspace_files[rel_path] = content
                except (UnicodeDecodeError, IOError):
                    # Skip binary files or unreadable files
                    continue

        if workspace_files:
            # Send files via WebSocket using fs.write_many action
            message = {
                "type": "action",
                "payload": {
                    "action": "fs.write_many",
                    "files": [{"path": path, "contents": content} for path, content in workspace_files.items()]
                }
            }

            await connection_manager.send_to_user(message, user_id)
            logger.info(f"Sent {len(workspace_files)} files to user {user_id} via WebSocket")

    except Exception as e:
        logger.error(f"Error syncing workspace files to frontend: {e}")

def detect_preview_mode(workspace_files: Dict[str, str]) -> str:
    """Detect if project should use static or dynamic preview"""
    # Check for package.json - indicates dynamic project
    if 'package.json' in workspace_files:
        try:
            import json
            pkg = json.loads(workspace_files['package.json'])

            # Check both dependencies and devDependencies
            all_deps = set()
            if 'dependencies' in pkg:
                all_deps.update(pkg['dependencies'].keys())
            if 'devDependencies' in pkg:
                all_deps.update(pkg['devDependencies'].keys())

            # Expanded framework list
            frameworks = [
                'react', 'vue', 'angular', 'svelte',
                'next', 'nuxt', 'gatsby', 'astro',
                'vite', '@vitejs/plugin-react', '@vitejs/plugin-vue',
                'webpack', 'parcel', 'rollup', '@angular/core',
                'preact', 'solid-js', 'qwik'
            ]

            if any(dep in all_deps for dep in frameworks):
                matched = all_deps & set(frameworks)
                logger.info(f"Detected dynamic preview mode (frameworks: {matched})")
                return 'dynamic'

        except json.JSONDecodeError as e:
            logger.error(f"Invalid package.json: {e}")
            return 'static'  # Fallback to static on error
        except Exception as e:
            logger.error(f"Error parsing package.json: {e}")
            return 'static'

    # Check for framework files (more comprehensive)
    file_list = workspace_files.keys()
    framework_extensions = ('.tsx', '.jsx', '.vue', '.svelte', '.astro')
    if any(f.endswith(framework_extensions) for f in file_list):
        logger.info(f"Detected dynamic preview mode (framework files found)")
        return 'dynamic'

    # Check for build configs
    build_configs = [
        'vite.config.js', 'vite.config.ts',
        'webpack.config.js', 'webpack.config.ts',
        'next.config.js', 'nuxt.config.js',
        'astro.config.mjs', 'gatsby-config.js',
        'rollup.config.js', 'parcel.config.js'
    ]
    if any(config in file_list for config in build_configs):
        logger.info(f"Detected dynamic preview mode (build config found)")
        return 'dynamic'

    # Otherwise use static mode
    logger.info(f"Detected static preview mode")
    return 'static'

async def sync_project_files_to_frontend(user_id: str, project_workspace: str):
    """Send project-specific files to frontend via WebSocket"""
    try:
        connection_manager = get_connection_manager()

        # Get all files from project workspace
        workspace_files = {}
        skipped_files = []
        error_files = []

        if os.path.exists(project_workspace):
            # Collect all files recursively from project directory
            for root, dirs, files in os.walk(project_workspace):
                # Skip .git and other hidden directories
                dirs[:] = [d for d in dirs if not d.startswith('.')]

                for file in files:
                    if file.startswith('.'):
                        continue

                    file_path = os.path.join(root, file)
                    # Get relative path from project workspace root
                    rel_path = os.path.relpath(file_path, project_workspace)

                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                            workspace_files[rel_path] = content
                    except UnicodeDecodeError:
                        skipped_files.append(rel_path)
                        logger.warning(f"Skipped binary file: {rel_path}")
                    except IOError as e:
                        error_files.append((rel_path, str(e)))
                        logger.error(f"Error reading {rel_path}: {e}")

        if workspace_files:
            # Detect preview mode
            preview_mode = detect_preview_mode(workspace_files)

            # Send files via WebSocket using fs.write_many action
            message = {
                "type": "action",
                "payload": {
                    "action": "fs.write_many",
                    "files": [{"path": path, "contents": content} for path, content in workspace_files.items()],
                    "previewMode": preview_mode
                }
            }

            await connection_manager.send_to_user(message, user_id)
            logger.info(f"Sent {len(workspace_files)} project files to user {user_id} via WebSocket (mode: {preview_mode})")

            # Send status notifications for skipped/error files
            if skipped_files:
                await connection_manager.send_to_user({
                    "type": "action",
                    "payload": {
                        "action": "status.note",
                        "message": f"⚠️ Skipped {len(skipped_files)} binary files (images, etc.)"
                    }
                }, user_id)
                logger.info(f"Skipped {len(skipped_files)} binary files")

            if error_files:
                await connection_manager.send_to_user({
                    "type": "action",
                    "payload": {
                        "action": "status.note",
                        "message": f"❌ Failed to read {len(error_files)} files - check backend logs"
                    }
                }, user_id)
                logger.error(f"Failed to read {len(error_files)} files: {error_files}")
        else:
            logger.warning(f"No files found in project workspace: {project_workspace}")

    except Exception as e:
        logger.error(f"Error syncing project files to frontend: {e}")

async def trigger_preview_setup(user_id: str, project_workspace: str):
    """Trigger automatic preview setup after files are synced (only for dynamic previews)"""
    try:
        connection_manager = get_connection_manager()

        # Check if package.json exists
        package_json_path = os.path.join(project_workspace, "package.json")
        has_package_json = os.path.exists(package_json_path)

        if not has_package_json:
            logger.info(f"No package.json found in {project_workspace}, using static preview (no build needed)")
            return

        # Read files to detect mode
        workspace_files = {}
        for root, dirs, files in os.walk(project_workspace):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for file in files:
                if not file.startswith('.'):
                    file_path = os.path.join(root, file)
                    rel_path = os.path.relpath(file_path, project_workspace)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            workspace_files[rel_path] = f.read()
                    except:
                        pass

        preview_mode = detect_preview_mode(workspace_files)

        if preview_mode == 'static':
            logger.info(f"Static preview detected for {project_workspace}, skipping build setup")
            return

        # Send status note
        await connection_manager.send_to_user({
            "type": "action",
            "payload": {
                "action": "status.note",
                "message": "📦 Installing dependencies..."
            }
        }, user_id)

        # Install dependencies
        await connection_manager.send_to_user({
            "type": "action",
            "payload": {
                "action": "shell.run",
                "command": "npm install",
                "cwd": "/",
                "timeoutSec": 120,
                "label": "install-deps"
            }
        }, user_id)

        # Give it a moment to complete
        await asyncio.sleep(2)

        # Send status note for server start
        await connection_manager.send_to_user({
            "type": "action",
            "payload": {
                "action": "status.note",
                "message": "🚀 Starting development server..."
            }
        }, user_id)

        # Start dev server
        await connection_manager.send_to_user({
            "type": "action",
            "payload": {
                "action": "server.start",
                "command": "npm run dev",
                "expectPort": 5173,
                "label": "dev-server",
                "cwd": "/"
            }
        }, user_id)

        logger.info(f"Triggered preview setup for user {user_id}")

    except Exception as e:
        logger.error(f"Error triggering preview setup: {e}")

# ===================== AUTHENTICATION ROUTES =====================
@app.post("/auth/login", response_model=AuthResponse)
async def login(login_data: UserLogin):
    """Login endpoint for ZipLogic frontend"""
    try:
        return login_user(login_data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Login failed: {str(e)}"
        )

@app.post("/auth/signup", response_model=AuthResponse)
async def signup(signup_data: UserCreate):
    """Signup endpoint for ZipLogic frontend"""
    try:
        return signup_user(signup_data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Signup failed: {str(e)}"
        )

@app.post("/auth/logout")
async def logout():
    """Logout endpoint for ZipLogic frontend"""
    try:
        return {"success": True, "message": "Logged out successfully"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Logout failed: {str(e)}"
        )

@app.post("/auth/forgot-password")
async def forgot_password_endpoint(request: ForgotPasswordRequest):
    """Forgot password endpoint for ZipLogic frontend"""
    try:
        success = forgot_password(request.email)
        return {"success": success, "message": "If email exists, reset instructions have been sent"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Forgot password failed: {str(e)}"
        )

@app.post("/auth/reset-password")
async def reset_password_endpoint(request: ResetPasswordRequest):
    """Reset password endpoint for ZipLogic frontend"""
    try:
        success = reset_password(request.token, request.new_password)
        return {"success": success, "message": "Password reset successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Reset password failed: {str(e)}"
        )

@app.get("/auth/verify")
async def verify_auth(request: Request, bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    """Verify authentication token"""
    if not bearer or bearer.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header"
        )

    user_data = verify_token(bearer.credentials)
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )

    return {"user": user_data, "valid": True}

@app.post("/auth/refresh")
async def refresh_auth_token(bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    """Refresh authentication token"""
    if not bearer or bearer.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header"
        )

    new_token = refresh_token(bearer.credentials)
    if not new_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )

    return {"token": new_token}

# ===================== SECURITY: PATH VALIDATION =====================
def secure_path_join(base_dir: str, user_path: str) -> str:
    """
    Safely join paths preventing traversal attacks

    Args:
        base_dir: Base directory (trusted)
        user_path: User-provided path (untrusted)

    Returns:
        Absolute safe path within base_dir

    Raises:
        HTTPException: If path traversal detected
    """
    # Normalize base directory
    base = os.path.abspath(base_dir)

    # Remove leading slashes/backslashes from user path
    user_path = user_path.lstrip('/').lstrip('\\')

    # Join and normalize
    full = os.path.normpath(os.path.join(base, user_path))

    # Resolve any remaining ../ or symlinks
    full = os.path.realpath(full)

    # Ensure it's still within base directory
    # On Windows, need to check with os.sep
    if not full.startswith(base + os.sep) and full != base:
        logger.warning(f"Path traversal attempt blocked: {user_path}")
        raise HTTPException(
            status_code=400,
            detail="Invalid path: Directory traversal attempt detected"
        )

    return full

# ===================== PROJECT MANAGEMENT STORAGE =====================
from datetime import datetime
project_chats: Dict[str, List[Dict]] = {}
project_structures: Dict[str, Any] = {}

# ===================== PROJECT ENDPOINTS =====================

@app.get("/api/projects")
async def get_projects(bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    """Get all projects for the authenticated user"""
    try:
        # Verify JWT token
        if not bearer or bearer.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header"
            )

        user_data = verify_token(bearer.credentials)
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )

        # For now, return mock projects - you can extend this to use a real database
        projects = []

        # Scan workspace for existing projects
        if os.path.exists(WORKSPACE_DIR):
            for item in os.listdir(WORKSPACE_DIR):
                project_path = os.path.join(WORKSPACE_DIR, item)
                if os.path.isdir(project_path) and item.startswith('project_'):
                    project_id = item.replace('project_', '')
                    # Get creation time
                    created_at = datetime.fromtimestamp(os.path.getctime(project_path)).isoformat()
                    updated_at = datetime.fromtimestamp(os.path.getmtime(project_path)).isoformat()

                    projects.append({
                        "id": project_id,
                        "name": f"Project {project_id}",
                        "description": "AI-generated project",
                        "created_at": created_at,
                        "updated_at": updated_at
                    })

        return {"projects": projects}

    except Exception as e:
        logger.error(f"Error getting projects: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving projects: {str(e)}"
        )

@app.post("/api/projects")
async def create_project(request: Dict, bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    """Create a new project"""
    try:
        # Verify JWT token
        if not bearer or bearer.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header"
            )

        user_data = verify_token(bearer.credentials)
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )

        # Generate unique project ID
        import uuid
        project_id = str(uuid.uuid4())[:8]

        # Create project workspace directory
        project_workspace = os.path.join(WORKSPACE_DIR, f"project_{project_id}")
        os.makedirs(project_workspace, exist_ok=True)

        # Initialize empty chat history
        project_chats[project_id] = [{
            "id": "msg_0",
            "role": "assistant",
            "content": "Welcome! I'm ready to help you build your project. What would you like to create?",
            "timestamp": datetime.now().isoformat()
        }]

        logger.info(f"Created new project {project_id} for user {user_data.get('email', 'unknown')}")

        return {"id": project_id}

    except Exception as e:
        logger.error(f"Error creating project: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating project: {str(e)}"
        )

@app.get("/api/projects/{project_id}")
async def get_project(project_id: str, bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    """Get project details"""
    try:
        # Verify JWT token
        if not bearer or bearer.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header"
            )

        user_data = verify_token(bearer.credentials)
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )

        project_workspace = os.path.join(WORKSPACE_DIR, f"project_{project_id}")

        if not os.path.exists(project_workspace):
            raise HTTPException(status_code=404, detail="Project not found")

        # Get project metadata
        created_at = datetime.fromtimestamp(os.path.getctime(project_workspace)).isoformat()
        updated_at = datetime.fromtimestamp(os.path.getmtime(project_workspace)).isoformat()

        return {
            "id": project_id,
            "name": f"Project {project_id}",
            "description": "AI-generated project",
            "created_at": created_at,
            "updated_at": updated_at
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting project: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving project: {str(e)}"
        )

@app.get("/api/projects/{project_id}/structure")
async def get_project_structure(project_id: str):
    """Get the project file structure"""
    try:
        # Create project workspace directory
        project_workspace = os.path.join(WORKSPACE_DIR, f"project_{project_id}")
        
        # Create if doesn't exist
        if not os.path.exists(project_workspace):
            os.makedirs(project_workspace, exist_ok=True)
        
        # Build tree structure
        def build_tree(path, base=""):
            tree = []
            try:
                for item in sorted(os.listdir(path)):
                    if item.startswith('.'):
                        continue
                    item_path = os.path.join(path, item)
                    # Use forward slashes for web compatibility (frontend expects Unix-style paths)
                    rel_path = f"{base}/{item}" if base else item

                    if os.path.isdir(item_path):
                        tree.append({
                            "name": item,
                            "type": "directory",
                            "path": rel_path,
                            "children": build_tree(item_path, rel_path)
                        })
                    else:
                        tree.append({
                            "name": item,
                            "type": "file",
                            "path": rel_path,
                            "size": os.path.getsize(item_path)
                        })
            except:
                pass
            return tree
        
        tree = build_tree(project_workspace)
        
        return {
            "tree": tree,
            "files_count": sum(1 for _ in Path(project_workspace).rglob('*') if _.is_file())
        }
    except Exception as e:
        logger.error(f"Error getting project structure: {e}")
        return {"tree": [], "files_count": 0}

@app.get("/api/projects/{project_id}/chat")
async def get_project_chat(project_id: str):
    """Get chat history for a project"""
    messages = project_chats.get(project_id, [])
    
    # Add welcome message if empty
    if not messages:
        messages = [{
            "id": "msg_0",
            "role": "assistant",
            "content": "Welcome! I'm ready to help you build your project.",
            "timestamp": datetime.now().isoformat()
        }]
        project_chats[project_id] = messages
    
    return {
        "messages": messages,
        "project_id": project_id
    }

@app.post("/api/projects/{project_id}/chat")
async def send_chat_message(
    project_id: str,
    bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    request: Dict[str, Any] = Body(...)
):

    import uuid
    import json

    try:
        # Verify JWT token
        if not bearer or bearer.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header"
            )

        user_data = verify_token(bearer.credentials)
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )

        # Get or create conversation
        thread_id = request.get("thread_id")
        conversation_id = request.get("conversation_id")
        message_content = request.get("message", "")

        # If no thread_id provided, create a new conversation
        if not thread_id:
            thread_id = f"thread_{uuid.uuid4().hex[:16]}"
            conversation_id = f"conv_{uuid.uuid4().hex[:16]}"

            # Auto-generate title from first message
            title = message_content[:50] + "..." if len(message_content) > 50 else message_content

            # Create conversation in database
            db.create_conversation(
                conversation_id=conversation_id,
                project_id=project_id,
                thread_id=thread_id,
                title=title
            )
            logger.info(f"Created new conversation: {conversation_id} with thread_id: {thread_id}")
        else:
            # Get existing conversation by thread_id
            conversation = db.get_conversation_by_thread_id(thread_id)
            if not conversation:
                raise HTTPException(status_code=404, detail="Conversation not found")
            conversation_id = conversation['id']
            logger.info(f"Resuming conversation: {conversation_id} with thread_id: {thread_id}")

        # Save user message to database
        user_message_id = f"msg_{uuid.uuid4().hex[:16]}"
        db.create_message(
            message_id=user_message_id,
            conversation_id=conversation_id,
            role="user",
            content=message_content
        )

        # Execute task with SWE agent using thread_id for checkpointing
        project_workspace = os.path.join(WORKSPACE_DIR, f"project_{project_id}")
        os.makedirs(project_workspace, exist_ok=True)

        # Set workspace path
        from agent.tools.write import set_workspace_path
        from agent.utils.paths import set_workspace_path as set_utils_workspace_path

        set_workspace_path(project_workspace)
        set_utils_workspace_path(project_workspace)

        # CRITICAL: Set workspace_path in state for architect graph
        initial_state = {
            "task_description": message_content,
            "workspace_path": project_workspace,
            "workspace_dir": project_workspace,
            "implementation_research_scratchpad": [],
        }

        # Execute agent with checkpointing enabled via thread_id
        logger.info(f"Executing task for conversation {conversation_id} with thread_id {thread_id}")

        # This config enables resuming from checkpoints
        config = {
            "configurable": {
                "thread_id": thread_id  # This enables LangGraph checkpointing
            }
        }

        result = await run_agent(initial_state, config=config)

        # Generate response based on agent result
        if isinstance(result, dict) and result.get("implementation_plan"):
            content = f" Task completed successfully!\\n\\nI've generated the project files for you. Check the file explorer to see the created files."
        else:
            content = f" I've processed your request: '{message_content}'\\n\\nThe files should now be available in your workspace."

        # Save assistant response to database
        assistant_message_id = f"msg_{uuid.uuid4().hex[:16]}"
        db.create_message(
            message_id=assistant_message_id,
            conversation_id=conversation_id,
            role="assistant",
            content=content
        )

        # Get full conversation history for response
        messages = db.get_messages_by_conversation(conversation_id)

        # Convert to API format
        formatted_messages = [{
            "id": msg["id"],
            "role": msg["role"],
            "content": msg["content"],
            "timestamp": msg["timestamp"]
        } for msg in messages]

        # Send files via WebSocket to frontend
        await sync_project_files_to_frontend(user_data["id"], project_workspace)
        await trigger_preview_setup(user_data["id"], project_workspace)

        return {
            "success": True,
            "message": formatted_messages[-1],  # Return latest message
            "thread_id": thread_id,
            "conversation_id": conversation_id,
            "messages": formatted_messages  # Return full history
        }

    except Exception as e:
        logger.exception(f"Error in chat execution for project {project_id}")

        # Save error message to database if conversation exists
        if 'conversation_id' in locals() and conversation_id:
            error_message_id = f"msg_{uuid.uuid4().hex[:16]}"
            error_content = f" Sorry, I encountered an error: {str(e)}"
            db.create_message(
                message_id=error_message_id,
                conversation_id=conversation_id,
                role="assistant",
                content=error_content
            )

        return {
            "success": False,
            "error": str(e),
            "thread_id": thread_id if 'thread_id' in locals() else None,
            "conversation_id": conversation_id if 'conversation_id' in locals() else None
        }

# Get all conversations for a project
@app.get("/api/projects/{project_id}/conversations")
async def get_conversations(
    project_id: str,
    bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)
):
    """Get all conversations for a project"""
    try:
        if not bearer or bearer.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header"
            )

        user_data = verify_token(bearer.credentials)
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )

        conversations = db.get_conversations_by_project(project_id)

        # Add last message for each conversation
        for conv in conversations:
            messages = db.get_messages_by_conversation(conv['id'])
            if messages:
                conv['last_message'] = {
                    "role": messages[-1]["role"],
                    "content": messages[-1]["content"],
                    "timestamp": messages[-1]["timestamp"]
                }

        return {
            "conversations": conversations,
            "project_id": project_id
        }

    except Exception as e:
        logger.error(f"Error getting conversations: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving conversations: {str(e)}"
        )

# Get messages for a specific conversation
@app.get("/api/projects/{project_id}/conversations/{conversation_id}")
async def get_conversation_messages(
    project_id: str,
    conversation_id: str,
    bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)
):
    """Get all messages for a conversation"""
    try:
        if not bearer or bearer.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header"
            )

        user_data = verify_token(bearer.credentials)
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )

        messages = db.get_messages_by_conversation(conversation_id)

        # Convert to API format
        formatted_messages = [{
            "id": msg["id"],
            "role": msg["role"],
            "content": msg["content"],
            "timestamp": msg["timestamp"]
        } for msg in messages]

        return {
            "messages": formatted_messages,
            "conversation_id": conversation_id,
            "project_id": project_id
        }

    except Exception as e:
        logger.error(f"Error getting conversation messages: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving messages: {str(e)}"
        )

# Delete a conversation
@app.delete("/api/projects/{project_id}/conversations/{conversation_id}")
async def delete_conversation_endpoint(
    project_id: str,
    conversation_id: str,
    bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)
):
    """Delete a conversation"""
    try:
        if not bearer or bearer.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header"
            )

        user_data = verify_token(bearer.credentials)
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )

        success = db.delete_conversation(conversation_id)

        if success:
            return {"success": True, "message": "Conversation deleted"}
        else:
            raise HTTPException(status_code=404, detail="Conversation not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting conversation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting conversation: {str(e)}"
        )

# Rename a conversation
@app.patch("/api/projects/{project_id}/conversations/{conversation_id}")
async def rename_conversation_endpoint(
    project_id: str,
    conversation_id: str,
    request: Dict[str, Any] = Body(...),
    bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)
):
    """Rename a conversation"""
    try:
        if not bearer or bearer.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header"
            )

        user_data = verify_token(bearer.credentials)
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )

        title = request.get("title")
        if not title:
            raise HTTPException(status_code=400, detail="Title is required")

        success = db.update_conversation_title(conversation_id, title)

        if success:
            return {"success": True, "message": "Conversation renamed"}
        else:
            raise HTTPException(status_code=404, detail="Conversation not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error renaming conversation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error renaming conversation: {str(e)}"
        )

@app.get("/api/projects/{project_id}/files")
async def get_project_file_by_param(project_id: str, path: str = Query(...)):
    """Get file content using query parameter (frontend compatible)"""
    try:
        project_workspace = os.path.join(WORKSPACE_DIR, f"project_{project_id}")
        full_path = secure_path_join(project_workspace, path)  # Secure path validation

        if os.path.exists(full_path) and os.path.isfile(full_path):
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return {"content": content}
        else:
            return {"success": False, "error": "File not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/projects/{project_id}/files/{file_path:path}")
async def get_project_file(project_id: str, file_path: str):
    """Get file content"""
    try:
        project_workspace = os.path.join(WORKSPACE_DIR, f"project_{project_id}")
        full_path = secure_path_join(project_workspace, file_path)  # Secure path validation
        
        if os.path.exists(full_path) and os.path.isfile(full_path):
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return {"content": content, "path": file_path, "success": True}
        else:
            return {"success": False, "error": "File not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.put("/api/projects/{project_id}/files")
async def update_project_file_by_param(project_id: str, request: Dict, path: str = Query(...)):
    """Update file content using query parameter (frontend compatible)"""
    try:
        project_workspace = os.path.join(WORKSPACE_DIR, f"project_{project_id}")
        full_path = secure_path_join(project_workspace, path)  # Secure path validation

        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        # Write the content
        content = request.get("content", "")
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)

        return {"success": True, "message": f"File {path} updated successfully"}

    except Exception as e:
        return {"success": False, "error": str(e)}



# ===================== FILE MANAGEMENT ROUTES =====================
@app.get("/files/structure", response_model=FilesStructureResponse)
async def get_files_structure():
    """Get the file structure of the workspace repository"""
    try:
        structure = get_files_structure(WORKSPACE_DIR)
        return FilesStructureResponse(success=True, structure=structure)
    except Exception as e:
        return FilesStructureResponse(success=False, error=str(e))

@app.get("/files/content")
async def get_file_content(path: str):
    """Get the content of a specific file"""
    try:
        full_path = os.path.join(WORKSPACE_DIR, path.lstrip('/'))

        # Security check - ensure path is within workspace
        full_path = os.path.abspath(full_path)
        workspace_abs = os.path.abspath(WORKSPACE_DIR)
        if not full_path.startswith(workspace_abs):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Path outside workspace not allowed"
            )

        if not os.path.exists(full_path):
            return FileContentResponse(success=False, error="File not found")

        if os.path.isdir(full_path):
            return FileContentResponse(success=False, error="Path is a directory, not a file")

        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()

        return FileContentResponse(success=True, content=content)
    except UnicodeDecodeError:
        return FileContentResponse(success=False, error="File is not a text file")
    except Exception as e:
        return FileContentResponse(success=False, error=str(e))

@app.post("/files/create", response_model=FileResponse)
async def create_file(request: FileCreateRequest):
    """Create a new file in the workspace"""
    try:
        full_path = os.path.join(WORKSPACE_DIR, request.path.lstrip('/'))

        # Security check - ensure path is within workspace
        full_path = os.path.abspath(full_path)
        workspace_abs = os.path.abspath(WORKSPACE_DIR)
        if not full_path.startswith(workspace_abs):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Path outside workspace not allowed"
            )

        # Use the create_file tool
        create_file_tool = write_tools_map["create_file"]
        result = create_file_tool.invoke({"path": full_path, "content": request.content})

        if "Successfully created" in result:
            return FileResponse(success=True, message=result, path=request.path)
        else:
            return FileResponse(success=False, message=result)
    except Exception as e:
        return FileResponse(success=False, message=str(e))

@app.put("/files/update", response_model=FileResponse)
async def update_file(request: FileUpdateRequest):
    """Update an existing file in the workspace"""
    try:
        full_path = os.path.join(WORKSPACE_DIR, request.path.lstrip('/'))

        # Security check - ensure path is within workspace
        full_path = os.path.abspath(full_path)
        workspace_abs = os.path.abspath(WORKSPACE_DIR)
        if not full_path.startswith(workspace_abs):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Path outside workspace not allowed"
            )

        # Use the write_to_file tool
        write_to_file_tool = write_tools_map["write_to_file"]
        result = write_to_file_tool.invoke({"path": full_path, "content": request.content})

        if "Successfully" in result:
            return FileResponse(success=True, message=result, path=request.path)
        else:
            return FileResponse(success=False, message=result)
    except Exception as e:
        return FileResponse(success=False, message=str(e))

@app.delete("/files/delete")
async def delete_file(path: str):
    """Delete a file from the workspace"""
    try:
        full_path = os.path.join(WORKSPACE_DIR, path.lstrip('/'))

        # Security check - ensure path is within workspace
        full_path = os.path.abspath(full_path)
        workspace_abs = os.path.abspath(WORKSPACE_DIR)
        if not full_path.startswith(workspace_abs):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Path outside workspace not allowed"
            )

        if not os.path.exists(full_path):
            return FileResponse(success=False, message="File not found")

        if os.path.isdir(full_path):
            os.rmdir(full_path)
            message = f"Successfully deleted directory {path}"
        else:
            os.remove(full_path)
            message = f"Successfully deleted file {path}"

        return FileResponse(success=True, message=message, path=path)
    except Exception as e:
        return FileResponse(success=False, message=str(e))

# ===================== ROUTES =====================
@app.get("/")
async def root():
    return {
        "name": "SWE Agent API",
        "version": "1.0.0",
        "endpoints": {
            "POST /execute": "Execute agent task",
            "GET /health": "Health check",
            "POST /auth/login": "User authentication",
            "POST /auth/signup": "User registration",
            "POST /auth/refresh": "Refresh authentication token",
            "GET /files/structure": "Get workspace file structure",
            "GET /files/content": "Get file content",
            "POST /files/create": "Create new file",
            "PUT /files/update": "Update existing file",
            "DELETE /files/delete": "Delete file"
        },
        "anthropic_key_loaded": bool(ANTHROPIC_API_KEY),
        "auth_required": True,
        "auth_configured": bool(get_allowed_tokens()),
        "auth_header": "Authorization: Bearer <JWT_TOKEN> for authenticated endpoints",
        "api_token_header": "Authorization: Bearer <SWE_API_TOKEN> or X-API-Key: <SWE_API_TOKEN> for agent endpoints",
        "workspace_dir": WORKSPACE_DIR,
    }

@app.get("/debug/auth")
async def debug_auth():
    if not AUTH_DEBUG:
        # Hide this unless explicitly enabled
        raise HTTPException(status_code=404, detail="Not found.")
    toks = list(get_allowed_tokens())
    masks = [f"...{t[-4:]}" if len(t) >= 4 else "..." for t in toks]
    return {
        "configured_tokens": masks,
        "allow_query_token": ALLOW_QUERY_TOKEN,
        "note": "Values are masked. Set SWE_AUTH_DEBUG=false to disable this endpoint.",
    }

@app.post("/execute", response_model=AgentResponse)
async def execute_agent(request: AgentRequest, bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    # Check JWT authentication
    if not bearer or bearer.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header"
        )
    
    # Verify the JWT token
    user_data = verify_token(bearer.credentials)
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )
    
    if not os.getenv("ANTHROPIC_API_KEY"):
        return AgentResponse(success=False, error="ANTHROPIC_API_KEY not configured in .env")

    try:
        logger.info(f"Executing task for user {user_data.get('email', 'unknown')}: {request.task[:100]}...")

        # Determine workspace path (could be project-specific or default)
        workspace_path = os.path.join(WORKSPACE_DIR, f"project_{user_data.get('id', 'default')}")
        os.makedirs(workspace_path, exist_ok=True)
        
        # Set workspace path for file tools AND path utilities
        from agent.tools.write import set_workspace_path
        from agent.utils.paths import set_workspace_path as set_utils_workspace_path

        set_workspace_path(workspace_path)  # For write tools
        set_utils_workspace_path(workspace_path)  # For path utilities (architect graph!)

        initial_state = {
            "task_description": request.task,
            "workspace_path": workspace_path,  # CRITICAL for architect graph
            "workspace_dir": workspace_path,  # Also add for compatibility
            "implementation_research_scratchpad": [],
        }

        # (Optional) keep messages if other parts of your graph use them:
        if request.context:
            initial_state["messages"] = [
                {"role": "user", "content": request.task},
                {"role": "user", "content": f"Context: {request.context}"},
            ]

        result = await run_agent(initial_state)

        # Extract and convert implementation_plan to dict
        impl = result.get("implementation_plan") if isinstance(result, dict) else None
        if impl:
            # If it's a Pydantic model, convert to dict
            if hasattr(impl, 'model_dump'):
                impl = impl.model_dump()
            elif hasattr(impl, 'dict'):
                impl = impl.dict()
        
        # Send files via WebSocket to frontend after successful execution
        await sync_workspace_files_to_frontend(user_data["id"])

        logger.info("Task completed successfully")
        return AgentResponse(success=True, implementation_plan=impl)

    except Exception as e:
        logger.exception("Error executing agent")
        return AgentResponse(success=False, error=str(e))

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "anthropic_key_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
        "langsmith_configured": bool(os.getenv("LANGSMITH_API_KEY")),
        "auth_configured": bool(get_allowed_tokens()),
        "workspace_dir_exists": os.path.isdir(WORKSPACE_DIR),
    }

# ===================== WEBSOCKET ENDPOINTS =====================
@app.websocket("/ws")
async def websocket_handler(websocket: WebSocket, token: str = Query(None)):
    """WebSocket endpoint for real-time communication"""
    # Token can come from query params
    if not token:
        # Try to get from Authorization header if not in query
        auth_header = websocket.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    
    if not token:
        await websocket.close(code=4001, reason="No authentication token provided")
        return
        
    await websocket_endpoint(websocket, token)

# ===================== MAIN =====================
if __name__ == "__main__":
    import uvicorn
    workspace_dir = os.getenv("WORKSPACE_DIR", "./workspace_repo")
    os.makedirs(workspace_dir, exist_ok=True)

    if not ANTHROPIC_API_KEY:
        print("\n" + "=" * 60)
        print("ERROR: ANTHROPIC_API_KEY not found!")
        print("=" * 60)
        print("Add to C:\\ZIP_SWE\\swe-agent\\.env and restart:")
        print("  ANTHROPIC_API_KEY=sk-ant-...")
        print("  SWE_API_TOKEN=<your-token>  (or SWE_API_TOKENS=a,b,c)")
        print("=" * 60 + "\n")
    else:
        print("\n" + "=" * 60)
        print("SWE Agent API Server")
        print("=" * 60)
        print("[OK] Anthropic API key loaded")
        print(f"[OK] Workspace directory: {workspace_dir}")
        print("[OK] Starting server on http://0.0.0.0:8000")
        print("=" * 60 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=8000)