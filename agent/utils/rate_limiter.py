"""Rate limiting utilities to prevent API overload errors"""

import time
from functools import wraps
import logging

logger = logging.getLogger(__name__)

class RateLimiter:
    """Simple rate limiter to prevent API overload"""
    
    def __init__(self, max_calls=5, period=60):
        """
        Initialize rate limiter.
        
        Args:
            max_calls: Maximum number of calls allowed in the period
            period: Time period in seconds
        """
        self.max_calls = max_calls
        self.period = period
        self.calls = []
    
    def __call__(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            now = time.time()
            # Remove old calls outside the period
            self.calls = [c for c in self.calls if now - c < self.period]
            
            if len(self.calls) >= self.max_calls:
                wait_time = self.period - (now - self.calls[0])
                logger.warning(f"Rate limit reached. Waiting {wait_time:.1f} seconds...")
                time.sleep(wait_time)
                self.calls = []
            
            self.calls.append(now)
            return func(*args, **kwargs)
        return wrapper

# Create default rate limiters for different API operations
api_rate_limiter = RateLimiter(max_calls=3, period=60)  # 3 calls per minute
research_rate_limiter = RateLimiter(max_calls=5, period=60)  # 5 calls per minute
