"""Retry logic and error handling utilities for SWE Agent"""

import time
import logging
from typing import TypeVar, Callable, Optional, Any
from functools import wraps

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

T = TypeVar('T')


class RetryError(Exception):
    """Custom exception for retry failures"""
    pass


def retry(
    max_attempts: int = 3,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
    on_retry: Optional[Callable] = None
):
    """
    Decorator for retrying functions with exponential backoff
    
    Args:
        max_attempts: Maximum number of retry attempts
        backoff: Backoff multiplier (exponential backoff)
        exceptions: Tuple of exceptions to catch and retry
        on_retry: Optional callback function to call on retry
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception = None
            
            for attempt in range(max_attempts):
                try:
                    # Log the attempt
                    if attempt > 0:
                        logger.info(f"Retry attempt {attempt + 1}/{max_attempts} for {func.__name__}")
                    
                    # Try to execute the function
                    result = func(*args, **kwargs)
                    
                    # Success - return result
                    if attempt > 0:
                        logger.info(f"Success on attempt {attempt + 1} for {func.__name__}")
                    
                    return result
                    
                except exceptions as e:
                    last_exception = e
                    
                    # Log the error
                    logger.warning(
                        f"Attempt {attempt + 1}/{max_attempts} failed for {func.__name__}: {str(e)}"
                    )
                    
                    # Call retry callback if provided
                    if on_retry:
                        on_retry(attempt, e)
                    
                    # Don't sleep on the last attempt
                    if attempt < max_attempts - 1:
                        sleep_time = backoff ** attempt
                        logger.info(f"Waiting {sleep_time:.1f} seconds before retry...")
                        time.sleep(sleep_time)
                    
            # All attempts failed
            logger.error(f"All {max_attempts} attempts failed for {func.__name__}")
            raise RetryError(
                f"Failed after {max_attempts} attempts: {str(last_exception)}"
            ) from last_exception
            
        return wrapper
    return decorator


def retry_on_api_error(func: Callable[..., T]) -> Callable[..., T]:
    """
    Specialized retry decorator for API calls
    
    Retries on common API errors with smart backoff
    """
    api_exceptions = (
        ConnectionError,
        TimeoutError,
        OSError,  # Network errors
    )
    
    @retry(
        max_attempts=3,
        backoff=2.0,
        exceptions=api_exceptions
    )
    @wraps(func)
    def wrapper(*args, **kwargs) -> T:
        return func(*args, **kwargs)
    
    return wrapper


class ErrorHandler:
    """Central error handling for the SWE Agent"""
    
    @staticmethod
    def handle_llm_error(error: Exception, context: dict = None):
        """Handle LLM-related errors"""
        logger.error(f"LLM Error: {str(error)}", extra=context)
        
        # Check for specific error types
        error_message = str(error).lower()
        
        if "rate limit" in error_message:
            logger.info("Rate limit detected, waiting before retry...")
            time.sleep(10)  # Wait 10 seconds for rate limit
            return True  # Indicate retry should happen
            
        elif "timeout" in error_message:
            logger.info("Timeout detected, will retry with longer timeout")
            return True
            
        elif "api key" in error_message or "authentication" in error_message:
            logger.error("Authentication error - check API keys")
            return False  # Don't retry auth errors
            
        else:
            logger.error(f"Unknown LLM error: {error}")
            return True  # Retry unknown errors
    
    @staticmethod
    def handle_file_error(error: Exception, file_path: str):
        """Handle file-related errors"""
        logger.error(f"File Error for {file_path}: {str(error)}")
        
        if isinstance(error, FileNotFoundError):
            logger.info(f"File not found: {file_path}")
            # Could create parent directories here
            from pathlib import Path
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            return True
            
        elif isinstance(error, PermissionError):
            logger.error(f"Permission denied for file: {file_path}")
            return False
            
        elif isinstance(error, UnicodeDecodeError):
            logger.error(f"Encoding error for file: {file_path}")
            return False
            
        else:
            return False
    
    @staticmethod
    def wrap_safe(func: Callable, default_return: Any = None):
        """Wrap a function to catch and log all errors"""
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in {func.__name__}: {str(e)}", exc_info=True)
                return default_return
        return wrapper


# Enhanced agent invocation with retry logic
@retry_on_api_error
def invoke_agent_with_retry(agent, input_data: dict, config: dict = None):
    """
    Invoke an agent with automatic retry logic
    
    Args:
        agent: The agent to invoke
        input_data: Input data for the agent
        config: Optional configuration
    
    Returns:
        Agent response
    """
    logger.info(f"Invoking agent with task: {input_data.get('messages', [{}])[0].get('content', '')[:100]}...")
    
    try:
        result = agent.invoke(input_data, config=config)
        logger.info("Agent invocation successful")
        return result
        
    except Exception as e:
        logger.error(f"Agent invocation failed: {str(e)}")
        
        # Check if we should retry
        if ErrorHandler.handle_llm_error(e):
            raise  # Re-raise to trigger retry
        else:
            # Don't retry, return error
            return {"error": str(e), "success": False}


# File operation with retry
@retry(max_attempts=3, backoff=1.5)
def safe_file_write(file_path: str, content: str, encoding: str = "utf-8"):
    """
    Safely write content to a file with retry logic
    
    Args:
        file_path: Path to the file
        content: Content to write
        encoding: File encoding
    """
    from pathlib import Path
    
    try:
        # Ensure parent directory exists
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Write the file
        with open(file_path, 'w', encoding=encoding) as f:
            f.write(content)
            f.flush()
            
        logger.debug(f"Successfully wrote to {file_path}")
        
    except Exception as e:
        logger.error(f"Failed to write to {file_path}: {str(e)}")
        if not ErrorHandler.handle_file_error(e, file_path):
            raise


# Cache implementation for reducing API calls
class SimpleCache:
    """Simple in-memory cache for reducing API calls"""
    
    def __init__(self, ttl: int = 3600):
        """
        Initialize cache
        
        Args:
            ttl: Time to live in seconds
        """
        self.cache = {}
        self.timestamps = {}
        self.ttl = ttl
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired"""
        if key in self.cache:
            if time.time() - self.timestamps[key] < self.ttl:
                logger.debug(f"Cache hit for key: {key}")
                return self.cache[key]
            else:
                # Expired
                del self.cache[key]
                del self.timestamps[key]
                logger.debug(f"Cache expired for key: {key}")
        
        logger.debug(f"Cache miss for key: {key}")
        return None
    
    def set(self, key: str, value: Any):
        """Set value in cache"""
        self.cache[key] = value
        self.timestamps[key] = time.time()
        logger.debug(f"Cached value for key: {key}")
    
    def clear(self):
        """Clear all cache"""
        self.cache.clear()
        self.timestamps.clear()
        logger.debug("Cache cleared")


# Global cache instance
research_cache = SimpleCache(ttl=1800)  # 30 minutes TTL


def cached_research(func: Callable[..., T]) -> Callable[..., T]:
    """Decorator to cache research results"""
    @wraps(func)
    def wrapper(*args, **kwargs) -> T:
        # Create cache key from function name and arguments
        cache_key = f"{func.__name__}:{str(args)}:{str(kwargs)}"
        
        # Check cache
        cached_result = research_cache.get(cache_key)
        if cached_result is not None:
            return cached_result
        
        # Execute function
        result = func(*args, **kwargs)
        
        # Cache result
        research_cache.set(cache_key, result)
        
        return result
    
    return wrapper


# Export main utilities
__all__ = [
    'retry',
    'retry_on_api_error',
    'ErrorHandler',
    'invoke_agent_with_retry',
    'safe_file_write',
    'SimpleCache',
    'research_cache',
    'cached_research',
    'logger'
]


if __name__ == "__main__":
    # Test the retry logic
    @retry(max_attempts=3, backoff=2.0)
    def test_function():
        """Test function that fails twice then succeeds"""
        if not hasattr(test_function, 'attempt'):
            test_function.attempt = 0
        
        test_function.attempt += 1
        
        if test_function.attempt < 3:
            raise Exception(f"Test failure {test_function.attempt}")
        
        return "Success!"
    
    # Test the function
    try:
        result = test_function()
        print(f"Result: {result}")
    except RetryError as e:
        print(f"Failed: {e}")
