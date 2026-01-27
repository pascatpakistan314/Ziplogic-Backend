# Setup Authentication for SWE Agent Backend

## Install Required Dependencies

Run this command in your backend directory:

```bash
pip install PyJWT==2.8.0 passlib[bcrypt]==1.7.4 python-multipart==0.0.6
```

Or install from requirements file:

```bash
pip install -r requirements_auth.txt
```

## Environment Variables

Add these to your `.env` file:

```env
# JWT Secret Key (change this in production!)
JWT_SECRET_KEY=your-secret-key-change-this-in-production

# Existing variables
ANTHROPIC_API_KEY=sk-ant-...
SWE_API_TOKEN=your-api-token
```

## Test the Backend

1. Start your FastAPI server:
```bash
python simple_api.py
```

2. Visit http://localhost:8000 to see the API documentation

3. Try the new auth endpoints:
   - POST `/auth/login` - Login with email/password
   - POST `/auth/signup` - Create new account

## Frontend Integration

Your ZipLogic frontend is now configured to:
- Login/Signup at http://localhost:8000/auth/login and /auth/signup
- Execute SWE Agent tasks via the chat interface
- Authenticate requests with JWT tokens

## User Storage

Users are stored in `users.json` file (for demo - use real database in production).

Example user creation via API:
```json
{
  "email": "test@example.com",
  "password": "password123",
  "name": "Test User"
}
```