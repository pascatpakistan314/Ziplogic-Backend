"""WebSocket handler for real-time communication between frontend and backend"""

import asyncio
import json
import uuid
from typing import Dict, Any, Optional, Set
from datetime import datetime
import logging

from fastapi import WebSocket, WebSocketDisconnect
from auth import verify_token

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.user_sessions: Dict[str, Set[str]] = {}  # user_id -> set of session_ids
        self.session_users: Dict[str, str] = {}      # session_id -> user_id

    async def connect(self, websocket: WebSocket, session_id: str, user_id: str):
        """Connect a WebSocket client"""
        await websocket.accept()
        self.active_connections[session_id] = websocket
        self.session_users[session_id] = user_id

        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = set()
        self.user_sessions[user_id].add(session_id)

        logger.info(f"WebSocket connected: session={session_id}, user={user_id}")

    def disconnect(self, session_id: str):
        """Disconnect a WebSocket client"""
        if session_id in self.active_connections:
            del self.active_connections[session_id]

        user_id = self.session_users.get(session_id)
        if user_id and user_id in self.user_sessions:
            self.user_sessions[user_id].discard(session_id)
            if not self.user_sessions[user_id]:
                del self.user_sessions[user_id]

        if session_id in self.session_users:
            del self.session_users[session_id]

        logger.info(f"WebSocket disconnected: session={session_id}")

    async def send_personal_message(self, message: dict, session_id: str):
        """Send message to a specific session"""
        websocket = self.active_connections.get(session_id)
        if websocket:
            try:
                await websocket.send_text(json.dumps(message))
            except Exception as e:
                logger.error(f"Error sending message to session {session_id}: {e}")
                self.disconnect(session_id)

    async def send_to_user(self, message: dict, user_id: str):
        """Send message to all sessions of a user"""
        sessions = self.user_sessions.get(user_id, set())
        for session_id in sessions.copy():
            await self.send_personal_message(message, session_id)

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients"""
        for session_id in list(self.active_connections.keys()):
            await self.send_personal_message(message, session_id)

# Global connection manager
manager = ConnectionManager()

class WebSocketHandler:
    def __init__(self, websocket: WebSocket, session_id: str, user_id: str):
        self.websocket = websocket
        self.session_id = session_id
        self.user_id = user_id
        self.seq_counter = 0

    def next_seq(self) -> int:
        """Get next sequence number"""
        self.seq_counter += 1
        return self.seq_counter

    async def send_message(self, message_type: str, payload: Any):
        """Send a message with proper envelope"""
        envelope = {
            "type": message_type,
            "sid": self.session_id,
            "seq": self.next_seq(),
            "ts": int(datetime.utcnow().timestamp() * 1000),
            "payload": payload
        }
        await manager.send_personal_message(envelope, self.session_id)

    async def send_action(self, action: Dict[str, Any]):
        """Send an agent action"""
        await self.send_message("action", action)

    async def send_status(self, message: str):
        """Send a status message"""
        await self.send_action({
            "action": "status.note",
            "message": message
        })

    async def send_files(self, files: Dict[str, str]):
        """Send multiple files"""
        file_list = [{"path": path, "contents": content} for path, content in files.items()]
        await self.send_action({
            "action": "fs.write_many",
            "files": file_list
        })

    async def sync_workspace_files(self, workspace_dir: str = "./workspace_repo"):
        """Send all workspace files to the client"""
        try:
            import os
            workspace_files = {}

            # Collect all files recursively
            for root, dirs, files in os.walk(workspace_dir):
                # Skip .git and other hidden directories
                dirs[:] = [d for d in dirs if not d.startswith('.')]

                for file in files:
                    if file.startswith('.'):
                        continue

                    file_path = os.path.join(root, file)
                    # Get relative path from workspace root
                    rel_path = os.path.relpath(file_path, workspace_dir)

                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                            workspace_files[rel_path] = content
                    except (UnicodeDecodeError, IOError):
                        # Skip binary files or unreadable files
                        continue

            if workspace_files:
                await self.send_files(workspace_files)
                await self.send_status(f"Synchronized {len(workspace_files)} files from workspace")

                # Check if package.json exists and trigger preview setup
                if 'package.json' in workspace_files:
                    await self.trigger_preview_setup()

        except Exception as e:
            await self.send_status(f"Error syncing workspace files: {str(e)}")

    async def trigger_preview_setup(self):
        """Trigger automatic preview setup after files are synced"""
        try:
            # Install dependencies
            await self.send_status("📦 Installing dependencies...")
            await self.send_shell_command("npm install", cwd="/", label="install-deps")

            # Wait a moment for install to complete
            import asyncio
            await asyncio.sleep(2)

            # Start dev server
            await self.send_status("🚀 Starting development server...")
            await self.send_server_start("npm run dev", cwd="/", port=5173, label="dev-server")

        except Exception as e:
            await self.send_status(f"Error setting up preview: {str(e)}")

    async def send_shell_command(self, command: str, cwd: Optional[str] = None, label: Optional[str] = None):
        """Send shell command action"""
        action = {
            "action": "shell.run",
            "command": command
        }
        if cwd:
            action["cwd"] = cwd
        if label:
            action["label"] = label

        await self.send_action(action)

    async def send_server_start(self, command: str, cwd: Optional[str] = None, port: Optional[int] = None, label: str = "dev-server"):
        """Send server start action"""
        action = {
            "action": "server.start",
            "command": command,
            "label": label
        }
        if cwd:
            action["cwd"] = cwd
        if port:
            action["expectPort"] = port

        await self.send_action(action)

    async def send_server_stop(self, label: str = "dev-server"):
        """Send server stop action"""
        await self.send_action({
            "action": "server.stop",
            "label": label
        })

    async def handle_control_message(self, payload: Dict[str, Any]):
        """Handle control messages from client"""
        op = payload.get("op")

        if op == "resume":
            last_seq = payload.get("lastSeq", 0)
            await self.send_message("control", {
                "op": "hello",
                "serverSeq": self.seq_counter,
                "lastSeq": last_seq
            })

        elif op == "ping":
            await self.send_message("control", {"op": "pong"})

        elif op == "resend_file":
            file_id = payload.get("fileId")
            # Handle file resend request
            logger.info(f"File resend requested: {file_id}")

    async def handle_ack(self, payload: Dict[str, Any]):
        """Handle acknowledgment from client"""
        ack_seq = payload.get("ackSeq")
        file_id = payload.get("fileId")
        chunk_index = payload.get("chunkIndex")

        logger.debug(f"Received ACK: seq={ack_seq}, file={file_id}, chunk={chunk_index}")

    async def handle_telemetry(self, payload: Dict[str, Any]):
        """Handle telemetry data from client"""
        logger.info(f"Telemetry received from {self.user_id}: {payload}")

    async def handle_message(self, message: dict):
        """Handle incoming WebSocket message"""
        message_type = message.get("type")
        payload = message.get("payload", {})

        if message_type == "control":
            await self.handle_control_message(payload)
        elif message_type == "ack":
            await self.handle_ack(payload)
        elif message_type == "telemetry":
            await self.handle_telemetry(payload)
        else:
            logger.warning(f"Unknown message type: {message_type}")

async def websocket_endpoint(websocket: WebSocket, token: str):
    """WebSocket endpoint handler"""
    # Verify authentication
    user_data = verify_token(token)
    if not user_data:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    session_id = str(uuid.uuid4())
    user_id = user_data["id"]

    # Connect to manager
    await manager.connect(websocket, session_id, user_id)
    handler = WebSocketHandler(websocket, session_id, user_id)

    try:
        # Send initial hello
        await handler.send_message("control", {
            "op": "hello",
            "sessionId": session_id,
            "userId": user_id
        })

        # Sync existing workspace files on connection
        await handler.sync_workspace_files()

        # Listen for messages
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                await handler.handle_message(message)
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON received from {session_id}")
            except Exception as e:
                logger.error(f"Error handling message from {session_id}: {e}")

    except WebSocketDisconnect:
        logger.info(f"Client {session_id} disconnected")
    except Exception as e:
        logger.error(f"WebSocket error for {session_id}: {e}")
    finally:
        manager.disconnect(session_id)

def get_connection_manager() -> ConnectionManager:
    """Get the global connection manager"""
    return manager