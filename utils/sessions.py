"""
Session management for PDF Bot
Handles user sessions with optional persistence
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Global sessions storage
sessions: Dict[int, Dict[str, Any]] = {}

# Session configuration
SESSION_TIMEOUT = 3600  # 1 hour
CLEANUP_INTERVAL = 600  # 10 minutes

def ensure_session_dict(user_id: int) -> Dict[str, Any]:
    """Ensure a session exists for the user and return it"""
    if user_id not in sessions:
        sessions[user_id] = {
            'created_at': datetime.now(),
            'last_activity': datetime.now()
        }
    else:
        # Update last activity
        sessions[user_id]['last_activity'] = datetime.now()
    
    return sessions[user_id]

def get_session(user_id: int) -> Optional[Dict[str, Any]]:
    """Get user session if exists"""
    return sessions.get(user_id)

def clear_session(user_id: int):
    """Clear user session"""
    if user_id in sessions:
        del sessions[user_id]
        logger.debug(f"Session cleared for user {user_id}")

def set_session_value(user_id: int, key: str, value: Any):
    """Set a value in user session"""
    session = ensure_session_dict(user_id)
    session[key] = value
    session['last_activity'] = datetime.now()

def get_session_value(user_id: int, key: str, default: Any = None) -> Any:
    """Get a value from user session"""
    session = sessions.get(user_id, {})
    return session.get(key, default)

def pop_session_value(user_id: int, key: str, default: Any = None) -> Any:
    """Pop a value from user session"""
    session = sessions.get(user_id)
    if session:
        session['last_activity'] = datetime.now()
        return session.pop(key, default)
    return default

async def cleanup_old_sessions():
    """Background task to clean up old sessions"""
    while True:
        try:
            now = datetime.now()
            timeout = timedelta(seconds=SESSION_TIMEOUT)
            
            # Find sessions to remove
            to_remove = []
            for user_id, session in sessions.items():
                last_activity = session.get('last_activity', session.get('created_at', now))
                if now - last_activity > timeout:
                    # Don't remove if processing is active
                    if not session.get('processing'):
                        to_remove.append(user_id)
            
            # Remove old sessions
            for user_id in to_remove:
                clear_session(user_id)
                logger.debug(f"Cleaned old session for user {user_id}")
            
            if to_remove:
                logger.info(f"Cleaned {len(to_remove)} old sessions")
            
        except Exception as e:
            logger.error(f"Error in cleanup_old_sessions: {e}")
        
        await asyncio.sleep(CLEANUP_INTERVAL)

async def save_sessions_to_db():
    """Save important session data to database (optional)"""
    from utils.database import db
    
    try:
        for user_id, session in sessions.items():
            # Only save sessions with important data
            if session.get('username') or session.get('banner_path'):
                await db.save_session(user_id, {
                    'username': session.get('username'),
                    'banner_path': session.get('banner_path'),
                    'text_position': session.get('text_position', 'end'),
                    'delete_delay': session.get('delete_delay', 300)
                })
        
        logger.debug("Sessions saved to database")
    except Exception as e:
        logger.error(f"Error saving sessions: {e}")

async def load_sessions_from_db():
    """Load sessions from database on startup (optional)"""
    from utils.database import db
    
    try:
        # This is optional - only load recent sessions
        # You can skip this if you prefer fresh sessions on restart
        pass
    except Exception as e:
        logger.error(f"Error loading sessions: {e}")

# Processing flag management
def set_processing_flag(user_id: int, chat_id: Optional[int] = None, source: str = "") -> None:
    """Set processing flag for a user"""
    session = ensure_session_dict(user_id)
    session['processing'] = True
    session['processing_started'] = datetime.now()
    
    if source:
        session['processing_source'] = source
    if chat_id is not None:
        session['processing_chat_id'] = chat_id
    
    logger.info(f"[processing] SET user={user_id} source={source}")
    
    # Start watchdog
    asyncio.create_task(_processing_watchdog(user_id))

def clear_processing_flag(user_id: int, source: str = "", reason: str = "") -> None:
    """Clear processing flag for a user"""
    session = ensure_session_dict(user_id)
    
    started = session.get('processing_started', datetime.now())
    elapsed = (datetime.now() - started).total_seconds()
    
    session['processing'] = False
    
    logger.info(
        f"[processing] CLEAR user={user_id} source={source} elapsed={elapsed:.2f}s reason={reason}"
    )

async def _processing_watchdog(user_id: int):
    """Auto-clear processing flag after timeout"""
    TIMEOUT = 180  # 3 minutes
    
    try:
        await asyncio.sleep(TIMEOUT)
    except asyncio.CancelledError:
        return
    
    session = sessions.get(user_id)
    if session and session.get('processing'):
        started = session.get('processing_started', datetime.now())
        elapsed = (datetime.now() - started).total_seconds()
        
        if elapsed >= TIMEOUT:
            logger.warning(f"[processing] WATCHDOG CLEAR user={user_id} elapsed={elapsed:.2f}s")
            session['processing'] = False
            
            # Notify user if possible
            from pyrogram import Client
            chat_id = session.get('processing_chat_id')
            if chat_id:
                try:
                    # This needs the app instance
                    # In practice, you'd pass the client or use a global
                    pass
                except Exception:
                    pass

# State management helpers
def is_user_processing(user_id: int) -> bool:
    """Check if user has active processing"""
    session = sessions.get(user_id, {})
    return session.get('processing', False)

def is_batch_mode(user_id: int) -> bool:
    """Check if user is in batch mode"""
    session = sessions.get(user_id, {})
    return session.get('batch_mode', False)

def get_user_state(user_id: int) -> str:
    """Get current user state"""
    session = sessions.get(user_id, {})
    
    if session.get('processing'):
        return 'processing'
    elif session.get('batch_mode'):
        return 'batch'
    elif session.get('awaiting_username'):
        return 'awaiting_username'
    elif session.get('awaiting_password'):
        return 'awaiting_password'
    elif session.get('awaiting_pages'):
        return 'awaiting_pages'
    else:
        return 'idle'

def reset_user_state(user_id: int):
    """Reset user state to idle"""
    session = ensure_session_dict(user_id)
    
    # Keep important data
    keep_keys = ['username', 'banner_path', 'text_position', 'delete_delay', 
                 'created_at', 'last_activity']
    
    # Remove all other keys
    keys_to_remove = [k for k in session.keys() if k not in keep_keys]
    for key in keys_to_remove:
        session.pop(key, None)
    
    logger.debug(f"State reset for user {user_id}")
