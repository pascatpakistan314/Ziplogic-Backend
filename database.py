"""SQLite database implementation for SWE Agent API"""

import sqlite3
import hashlib
import os
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# Database file path
DB_PATH = Path(__file__).parent / "swe_agent.db"

class DatabaseManager:
    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self.init_database()

    def get_connection(self):
        """Get database connection"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # Enable dict-like access
        return conn

    def init_database(self):
        """Initialize database with required tables"""
        conn = self.get_connection()
        try:
            # Users table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    hashed_password TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    reset_token TEXT,
                    reset_token_expires TEXT
                )
            """)

            # Sessions table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            """)

            # Projects table (for future use)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    workspace_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            """)

            # Agent executions table (for audit trail)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_executions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    project_id TEXT,
                    task_description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error_message TEXT,
                    result_data TEXT,
                    FOREIGN KEY (user_id) REFERENCES users (id),
                    FOREIGN KEY (project_id) REFERENCES projects (id)
                )
            """)

            # Conversations table for chat history
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    thread_id TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (project_id) REFERENCES projects (id)
                )
            """)

            # Messages table for chat messages
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    metadata TEXT,
                    FOREIGN KEY (conversation_id) REFERENCES conversations (id)
                )
            """)

            conn.commit()
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing database: {e}")
            raise
        finally:
            conn.close()

    def create_user(self, user_id: str, email: str, name: str, hashed_password: str) -> bool:
        """Create a new user"""
        conn = self.get_connection()
        try:
            now = datetime.utcnow().isoformat()
            conn.execute("""
                INSERT INTO users (id, email, name, hashed_password, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, email, name, hashed_password, now, now))
            conn.commit()
            logger.info(f"User created: {email}")
            return True
        except sqlite3.IntegrityError as e:
            logger.error(f"User creation failed - email already exists: {email}")
            return False
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            return False
        finally:
            conn.close()

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Get user by email"""
        conn = self.get_connection()
        try:
            cursor = conn.execute("""
                SELECT id, email, name, hashed_password, created_at, updated_at, is_active
                FROM users
                WHERE email = ? AND is_active = 1
            """, (email,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting user by email: {e}")
            return None
        finally:
            conn.close()

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user by ID"""
        conn = self.get_connection()
        try:
            cursor = conn.execute("""
                SELECT id, email, name, hashed_password, created_at, updated_at, is_active
                FROM users
                WHERE id = ? AND is_active = 1
            """, (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting user by ID: {e}")
            return None
        finally:
            conn.close()

    def update_user_password(self, email: str, hashed_password: str) -> bool:
        """Update user password"""
        conn = self.get_connection()
        try:
            now = datetime.utcnow().isoformat()
            cursor = conn.execute("""
                UPDATE users
                SET hashed_password = ?, updated_at = ?, reset_token = NULL, reset_token_expires = NULL
                WHERE email = ? AND is_active = 1
            """, (hashed_password, now, email))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating user password: {e}")
            return False
        finally:
            conn.close()

    def set_reset_token(self, email: str, reset_token: str, expires_at: str) -> bool:
        """Set password reset token"""
        conn = self.get_connection()
        try:
            now = datetime.utcnow().isoformat()
            cursor = conn.execute("""
                UPDATE users
                SET reset_token = ?, reset_token_expires = ?, updated_at = ?
                WHERE email = ? AND is_active = 1
            """, (reset_token, expires_at, now, email))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error setting reset token: {e}")
            return False
        finally:
            conn.close()

    def get_user_by_reset_token(self, reset_token: str) -> Optional[Dict[str, Any]]:
        """Get user by reset token"""
        conn = self.get_connection()
        try:
            cursor = conn.execute("""
                SELECT id, email, name, reset_token_expires
                FROM users
                WHERE reset_token = ? AND is_active = 1
            """, (reset_token,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting user by reset token: {e}")
            return None
        finally:
            conn.close()

    def create_session(self, session_id: str, user_id: str, token: str, expires_at: str) -> bool:
        """Create a new session"""
        conn = self.get_connection()
        try:
            now = datetime.utcnow().isoformat()
            conn.execute("""
                INSERT INTO sessions (id, user_id, token, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id, user_id, token, now, expires_at))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error creating session: {e}")
            return False
        finally:
            conn.close()

    def get_session_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Get session by token"""
        conn = self.get_connection()
        try:
            cursor = conn.execute("""
                SELECT s.id, s.user_id, s.token, s.created_at, s.expires_at, s.is_active,
                       u.email, u.name
                FROM sessions s
                JOIN users u ON s.user_id = u.id
                WHERE s.token = ? AND s.is_active = 1 AND u.is_active = 1
            """, (token,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting session by token: {e}")
            return None
        finally:
            conn.close()

    def invalidate_session(self, token: str) -> bool:
        """Invalidate a session"""
        conn = self.get_connection()
        try:
            cursor = conn.execute("""
                UPDATE sessions
                SET is_active = 0
                WHERE token = ?
            """, (token,))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error invalidating session: {e}")
            return False
        finally:
            conn.close()

    def cleanup_expired_sessions(self) -> int:
        """Remove expired sessions"""
        conn = self.get_connection()
        try:
            now = datetime.utcnow().isoformat()
            cursor = conn.execute("""
                UPDATE sessions
                SET is_active = 0
                WHERE expires_at < ? AND is_active = 1
            """, (now,))
            conn.commit()
            return cursor.rowcount
        except Exception as e:
            logger.error(f"Error cleaning up expired sessions: {e}")
            return 0
        finally:
            conn.close()

    # ==================== CHAT HISTORY METHODS ====================

    def create_conversation(self, conversation_id: str, project_id: str, thread_id: str, title: str) -> bool:
        """Create a new conversation"""
        conn = self.get_connection()
        try:
            now = datetime.utcnow().isoformat()
            conn.execute("""
                INSERT INTO conversations (id, project_id, thread_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (conversation_id, project_id, thread_id, title, now, now))
            conn.commit()
            logger.info(f"Conversation created: {conversation_id} for project {project_id}")
            return True
        except Exception as e:
            logger.error(f"Error creating conversation: {e}")
            return False
        finally:
            conn.close()

    def get_conversation_by_thread_id(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Get conversation by thread_id"""
        conn = self.get_connection()
        try:
            cursor = conn.execute("""
                SELECT id, project_id, thread_id, title, created_at, updated_at
                FROM conversations
                WHERE thread_id = ? AND is_active = 1
            """, (thread_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting conversation by thread_id: {e}")
            return None
        finally:
            conn.close()

    def get_conversations_by_project(self, project_id: str) -> list:
        """Get all conversations for a project"""
        conn = self.get_connection()
        try:
            cursor = conn.execute("""
                SELECT c.id, c.project_id, c.thread_id, c.title, c.created_at, c.updated_at,
                       COUNT(m.id) as message_count
                FROM conversations c
                LEFT JOIN messages m ON c.id = m.conversation_id
                WHERE c.project_id = ? AND c.is_active = 1
                GROUP BY c.id
                ORDER BY c.updated_at DESC
            """, (project_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting conversations by project: {e}")
            return []
        finally:
            conn.close()

    def update_conversation_title(self, conversation_id: str, title: str) -> bool:
        """Update conversation title"""
        conn = self.get_connection()
        try:
            now = datetime.utcnow().isoformat()
            cursor = conn.execute("""
                UPDATE conversations
                SET title = ?, updated_at = ?
                WHERE id = ? AND is_active = 1
            """, (title, now, conversation_id))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating conversation title: {e}")
            return False
        finally:
            conn.close()

    def update_conversation_timestamp(self, conversation_id: str) -> bool:
        """Update conversation updated_at timestamp"""
        conn = self.get_connection()
        try:
            now = datetime.utcnow().isoformat()
            cursor = conn.execute("""
                UPDATE conversations
                SET updated_at = ?
                WHERE id = ? AND is_active = 1
            """, (now, conversation_id))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating conversation timestamp: {e}")
            return False
        finally:
            conn.close()

    def delete_conversation(self, conversation_id: str) -> bool:
        """Soft delete a conversation"""
        conn = self.get_connection()
        try:
            now = datetime.utcnow().isoformat()
            cursor = conn.execute("""
                UPDATE conversations
                SET is_active = 0, updated_at = ?
                WHERE id = ?
            """, (now, conversation_id))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error deleting conversation: {e}")
            return False
        finally:
            conn.close()

    def create_message(self, message_id: str, conversation_id: str, role: str, content: str, timestamp: str = None, metadata: str = None) -> bool:
        """Create a new message"""
        conn = self.get_connection()
        try:
            if timestamp is None:
                timestamp = datetime.utcnow().isoformat()
            conn.execute("""
                INSERT INTO messages (id, conversation_id, role, content, timestamp, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (message_id, conversation_id, role, content, timestamp, metadata))
            conn.commit()

            # Update conversation timestamp
            self.update_conversation_timestamp(conversation_id)

            return True
        except Exception as e:
            logger.error(f"Error creating message: {e}")
            return False
        finally:
            conn.close()

    def get_messages_by_conversation(self, conversation_id: str) -> list:
        """Get all messages for a conversation"""
        conn = self.get_connection()
        try:
            cursor = conn.execute("""
                SELECT id, conversation_id, role, content, timestamp, metadata
                FROM messages
                WHERE conversation_id = ?
                ORDER BY timestamp ASC
            """, (conversation_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting messages by conversation: {e}")
            return []
        finally:
            conn.close()

# Global database instance
db = DatabaseManager()