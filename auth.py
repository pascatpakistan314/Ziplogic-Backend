"""Authentication module for SWE Agent API with SQLite database"""

from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import jwt
from passlib.context import CryptContext
from fastapi import HTTPException, status
from pydantic import BaseModel
import os
import uuid
import secrets
from pathlib import Path
from dotenv import load_dotenv

# Load .env file BEFORE reading environment variables
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path, override=True)
else:
    load_dotenv(override=True)

# Import SQLite database
from database import db

# JWT Settings
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-change-this-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440  # 24 hours instead of 30 minutes

# Log the secret key being used (for debugging only - remove in production)
import logging
logger = logging.getLogger("swe_api")
logger.info(f"[AUTH] JWT_SECRET_KEY loaded: {SECRET_KEY[:10]}... (length: {len(SECRET_KEY)})")
logger.info(f"[AUTH] Full JWT_SECRET_KEY hash: {hash(SECRET_KEY)}")

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class UserInDB(BaseModel):
    id: str
    email: str
    name: str
    hashed_password: str
    created_at: str

class User(BaseModel):
    id: str
    email: str
    name: str
    created_at: str

class UserCreate(BaseModel):
    email: str
    password: str
    name: str

class UserLogin(BaseModel):
    email: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

class AuthResponse(BaseModel):
    user: User
    token: str

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Hash a password"""
    return pwd_context.hash(password)

def get_user(email: str) -> Optional[UserInDB]:
    """Get user by email"""
    user_data = db.get_user_by_email(email)
    if user_data:
        return UserInDB(**user_data)
    return None

def authenticate_user(email: str, password: str) -> Optional[UserInDB]:
    """Authenticate user with email and password"""
    user = get_user(email)
    if not user:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Create JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def create_user(user_data: UserCreate) -> UserInDB:
    """Create a new user"""
    # Check if user already exists
    existing_user = db.get_user_by_email(user_data.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    # Create new user
    user_id = str(uuid.uuid4())
    hashed_password = get_password_hash(user_data.password)

    # Save to database
    success = db.create_user(user_id, user_data.email, user_data.name, hashed_password)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user"
        )

    # Get the created user
    user_data_dict = db.get_user_by_email(user_data.email)
    return UserInDB(**user_data_dict)

def login_user(login_data: UserLogin) -> AuthResponse:
    """Login user and return token"""
    user = authenticate_user(login_data.email, login_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.email}, expires_delta=access_token_expires
    )

    user_response = User(
        id=user.id,
        email=user.email,
        name=user.name,
        created_at=user.created_at
    )

    return AuthResponse(user=user_response, token=access_token)

def signup_user(signup_data: UserCreate) -> AuthResponse:
    """Signup new user and return token"""
    new_user = create_user(signup_data)

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": new_user.email}, expires_delta=access_token_expires
    )

    user_response = User(
        id=new_user.id,
        email=new_user.email,
        name=new_user.name,
        created_at=new_user.created_at
    )

    return AuthResponse(user=user_response, token=access_token)

def forgot_password(email: str) -> bool:
    """Generate reset token for password reset"""
    user = get_user(email)
    if not user:
        # Don't reveal if email exists or not for security
        return True

    # Generate reset token
    reset_token = secrets.token_urlsafe(32)
    expires_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()

    # Save reset token to database
    success = db.set_reset_token(email, reset_token, expires_at)

    # In a real application, you would send an email with the reset token here
    # For now, we'll just log it (remove in production)
    if success:
        print(f"Password reset token for {email}: {reset_token}")

    return success

def reset_password(token: str, new_password: str) -> bool:
    """Reset password using reset token"""
    user_data = db.get_user_by_reset_token(token)
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token"
        )

    # Check if token is expired
    expires_at = datetime.fromisoformat(user_data['reset_token_expires'])
    if datetime.utcnow() > expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset token has expired"
        )

    # Update password
    hashed_password = get_password_hash(new_password)
    success = db.update_user_password(user_data['email'], hashed_password)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update password"
        )

    return True

def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify JWT token and return user data"""
    import logging
    logger = logging.getLogger("swe_api")

    try:
        logger.info(f"Verifying token (length: {len(token) if token else 0})")
        logger.info(f"Using SECRET_KEY: {SECRET_KEY[:10]}... (length: {len(SECRET_KEY)})")

        # Decode token with verification
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        logger.info(f"Token decoded successfully. Email: {email}")

        if email is None:
            logger.warning("No email in token payload")
            return None

        user = get_user(email)
        if user is None:
            logger.warning(f"User not found for email: {email}")
            return None

        logger.info(f"User verified: {user.email}")
        return {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "created_at": user.created_at
        }
    except jwt.ExpiredSignatureError as e:
        # Token is expired, return None
        logger.warning(f"Token expired: {e}")
        return None
    except jwt.InvalidSignatureError as e:
        # Signature verification failed - likely due to JWT_SECRET_KEY change
        logger.error(f"JWT signature verification failed: {e}")
        logger.error(f"This usually means the token was created with a different JWT_SECRET_KEY")
        logger.error(f"Current SECRET_KEY: {SECRET_KEY[:10]}... Please ensure your frontend is using a fresh token")
        return None
    except jwt.PyJWTError as e:
        logger.warning(f"JWT error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in verify_token: {e}", exc_info=True)
        return None

def logout_user(token: str) -> bool:
    """Logout user by invalidating token"""
    # In JWT, we can't really "invalidate" tokens without a blacklist
    # For now, we'll just return success and rely on frontend to remove token
    # In production, you might want to implement a token blacklist
    return True

def refresh_token(token: str) -> Optional[str]:
    """Refresh an existing JWT token"""
    try:
        # Decode without verification to get payload even if expired
        unverified_payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_exp": False})
        email: str = unverified_payload.get("sub")
        if email is None:
            return None

        # Check if user still exists
        user = get_user(email)
        if user is None:
            return None

        # Create new token with fresh expiry
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        new_token = create_access_token(
            data={"sub": user.email}, expires_delta=access_token_expires
        )

        return new_token
    except jwt.PyJWTError:
        return None