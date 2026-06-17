"""
Circuit Breaker — Fail-fast pattern for external API calls.

Features:
  - Three states: CLOSED (normal), OPEN (failing), HALF-OPEN (testing)
  - Automatic recovery after timeout
  - Prevents cascade failures when services are down
  - Thread-safe state transitions

Usage:
    from shared import CircuitBreaker
    
    breaker = CircuitBreaker(failure_threshold=5, timeout=60)
    
    @breaker
    def fetch_weather():
        return requests.get("https://api.weather.com").json()
    
    try:
        data = fetch_weather()
    except CircuitBreakerOpen:
        # Use cached data or default
        pass
"""

import functools
import threading
import time
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

T = TypeVar('T')


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject fast
    HALF_OPEN = "half_open"  # Testing if recovered


class CircuitBreakerOpen(Exception):
    """Raised when circuit is open."""
    pass


class CircuitBreaker:
    """Thread-safe circuit breaker for external service calls."""
    
    def __init__(
        self,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        timeout: float = 60.0,
        half_open_max_calls: int = 3,
    ):
        """
        Initialize circuit breaker.
        
        Args:
            failure_threshold: Failures before opening circuit
            success_threshold: Successes in half-open to close
            timeout: Seconds before attempting recovery
            half_open_max_calls: Max calls in half-open state
        """
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout = timeout
        self.half_open_max_calls = half_open_max_calls
        
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._half_open_calls = 0
        
        self._lock = threading.RLock()
    
    @property
    def state(self) -> CircuitState:
        """Current circuit state."""
        with self._lock:
            return self._state
    
    @property
    def is_open(self) -> bool:
        """Check if circuit is open (failing)."""
        with self._lock:
            return self._state == CircuitState.OPEN
    
    def _can_attempt(self) -> bool:
        """Check if call should be allowed through."""
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            
            if self._state == CircuitState.OPEN:
                # Check if timeout elapsed
                if self._last_failure_time:
                    elapsed = time.time() - self._last_failure_time
                    if elapsed >= self.timeout:
                        self._state = CircuitState.HALF_OPEN
                        self._half_open_calls = 0
                        return True
                return False
            
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls < self.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False
            
            return True
    
    def _record_success(self) -> None:
        """Record successful call."""
        with self._lock:
            self._failure_count = 0
            
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._success_count = 0
                    self._half_open_calls = 0
    
    def _record_failure(self) -> None:
        """Record failed call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            self._success_count = 0
            
            if self._state == CircuitState.HALF_OPEN:
                # Back to open
                self._state = CircuitState.OPEN
                self._half_open_calls = 0
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
    
    def call(self, func: Callable[..., T], *args, **kwargs) -> T:
        """
        Execute function through circuit breaker.
        
        Args:
            func: Function to call
            *args: Function arguments
            **kwargs: Function keyword arguments
        
        Returns:
            Function result
        
        Raises:
            CircuitBreakerOpen: If circuit is open
            Exception: Original exception from func
        """
        if not self._can_attempt():
            raise CircuitBreakerOpen(f"Circuit is {self._state.value}")
        
        try:
            result = func(*args, **kwargs)
            self._record_success()
            return result
        except Exception as e:
            self._record_failure()
            raise
    
    def __call__(self, func: Callable[..., T]) -> Callable[..., T]:
        """Decorator support."""
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return self.call(func, *args, **kwargs)
        
        # Store reference for external access
        wrapper._circuit_breaker = self  # type: ignore[attr-defined]
        return wrapper
    
    def manual_reset(self) -> None:
        """Manually reset circuit to closed state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._half_open_calls = 0
            self._last_failure_time = None
    
    def get_stats(self) -> dict:
        """Get current circuit statistics."""
        with self._lock:
            return {
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "last_failure": self._last_failure_time,
                "timeout_remaining": max(
                    0,
                    self.timeout - (time.time() - (self._last_failure_time or 0))
                ) if self._state == CircuitState.OPEN else 0,
            }


# Pre-configured breakers for common services
WEATHER_BREAKER = CircuitBreaker(
    failure_threshold=3,
    timeout=300,  # 5 minutes
)

TELEGRAM_BREAKER = CircuitBreaker(
    failure_threshold=5,
    timeout=60,  # 1 minute
)

LORA_BREAKER = CircuitBreaker(
    failure_threshold=3,
    timeout=180,  # 3 minutes
)
