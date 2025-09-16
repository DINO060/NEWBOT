"""
MongoDB database management for PDF Bot
"""
import motor.motor_asyncio
from typing import Optional, Dict, List, Any
import logging
from datetime import datetime, timedelta
import os

logger = logging.getLogger(__name__)

class MongoDB:
    def __init__(self, url: Optional[str] = None):
        self.url = url or os.getenv('MONGODB_URL', 'mongodb://localhost:27017')
        self.client = None
        self.db = None
        
    async def connect(self):
        """Connect to MongoDB"""
        try:
            self.client = motor.motor_asyncio.AsyncIOMotorClient(self.url)
            self.db = self.client.pdfbot_database
            
            # Create indexes
            await self._create_indexes()
            logger.info("✅ MongoDB connected successfully")
            return True
        except Exception as e:
            logger.error(f"❌ MongoDB connection failed: {e}")
            return False
    
    async def _create_indexes(self):
        """Create necessary indexes"""
        # Users collection
        await self.db.users.create_index('user_id', unique=True)
        
        # Settings collection  
        await self.db.user_settings.create_index('user_id', unique=True)
        
        # Batch collection
        await self.db.batch_files.create_index('user_id')
        await self.db.batch_files.create_index('created_at')
        
    async def disconnect(self):
        """Disconnect from MongoDB"""
        if self.client:
            self.client.close()
            logger.info("MongoDB disconnected")
    
    # ========== User Management ==========
    
    async def track_user(self, user_id: int) -> bool:
        """Track a user in database"""
        try:
            await self.db.users.update_one(
                {'user_id': user_id},
                {
                    '$set': {
                        'user_id': user_id,
                        'last_seen': datetime.now()
                    },
                    '$setOnInsert': {
                        'created_at': datetime.now(),
                        'total_files': 0
                    },
                    '$inc': {'visit_count': 1}
                },
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Error tracking user {user_id}: {e}")
            return False
    
    async def get_user(self, user_id: int) -> Optional[Dict]:
        """Get user data"""
        return await self.db.users.find_one({'user_id': user_id})
    
    async def count_users(self) -> int:
        """Count total users"""
        return await self.db.users.count_documents({})
    
    async def get_all_users(self) -> List[int]:
        """Get all user IDs"""
        cursor = self.db.users.find({}, {'user_id': 1})
        users = []
        async for doc in cursor:
            users.append(doc['user_id'])
        return users
    
    # ========== User Settings ==========
    
    async def get_user_settings(self, user_id: int) -> Dict:
        """Get user settings"""
        doc = await self.db.user_settings.find_one({'user_id': user_id})
        return doc or {
            'user_id': user_id,
            'username': None,
            'banner_path': None,
            'lock_password': None,
            'text_position': 'end',
            'delete_delay': 300
        }
    
    async def update_user_settings(self, user_id: int, **settings) -> bool:
        """Update user settings"""
        try:
            await self.db.user_settings.update_one(
                {'user_id': user_id},
                {
                    '$set': {**settings, 'updated_at': datetime.now()},
                    '$setOnInsert': {'created_at': datetime.now()}
                },
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Error updating settings for {user_id}: {e}")
            return False
    
    # ========== Batch Management ==========
    
    async def add_batch_file(self, user_id: int, file_info: Dict) -> bool:
        """Add file to batch"""
        try:
            file_info['user_id'] = user_id
            file_info['created_at'] = datetime.now()
            await self.db.batch_files.insert_one(file_info)
            return True
        except Exception as e:
            logger.error(f"Error adding batch file: {e}")
            return False
    
    async def get_batch_files(self, user_id: int) -> List[Dict]:
        """Get all batch files for user"""
        cursor = self.db.batch_files.find(
            {'user_id': user_id}
        ).sort('created_at', 1)
        
        files = []
        async for doc in cursor:
            doc.pop('_id', None)
            files.append(doc)
        return files
    
    async def clear_batch(self, user_id: int) -> bool:
        """Clear user's batch"""
        try:
            await self.db.batch_files.delete_many({'user_id': user_id})
            return True
        except Exception as e:
            logger.error(f"Error clearing batch: {e}")
            return False
    
    # ========== Force Join Channels ==========
    
    async def get_forced_channels(self) -> List[str]:
        """Get list of forced subscription channels"""
        doc = await self.db.config.find_one({'_id': 'force_join'})
        if doc:
            return doc.get('channels', [])
        return []
    
    async def set_forced_channels(self, channels: List[str]) -> bool:
        """Set forced subscription channels"""
        try:
            # Clean channel names
            clean_channels = []
            for ch in channels:
                c = str(ch).strip().lstrip('@').lstrip('#')
                if c and c not in clean_channels:
                    clean_channels.append(c)
            
            await self.db.config.update_one(
                {'_id': 'force_join'},
                {'$set': {'channels': clean_channels, 'updated_at': datetime.now()}},
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Error setting forced channels: {e}")
            return False
    
    async def add_forced_channels(self, channels: List[str]) -> List[str]:
        """Add channels to forced subscription list"""
        current = await self.get_forced_channels()
        for ch in channels:
            c = str(ch).strip().lstrip('@').lstrip('#')
            if c and c not in current:
                current.append(c)
        await self.set_forced_channels(current)
        return current
    
    async def remove_forced_channels(self, channels: List[str]) -> List[str]:
        """Remove channels from forced subscription list"""
        current = await self.get_forced_channels()
        for ch in channels:
            c = str(ch).strip().lstrip('@').lstrip('#')
            if c in current:
                current.remove(c)
        await self.set_forced_channels(current)
        return current
    
    # ========== Statistics ==========
    
    async def get_stats(self) -> Dict:
        """Get global statistics"""
        doc = await self.db.stats.find_one({'_id': 'global'})
        return doc or {'files': 0, 'storage_bytes': 0}
    
    async def bump_stats(self, file_size: int = 0) -> bool:
        """Update statistics"""
        try:
            await self.db.stats.update_one(
                {'_id': 'global'},
                {
                    '$inc': {
                        'files': 1,
                        'storage_bytes': file_size
                    },
                    '$set': {'last_updated': datetime.now()}
                },
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Error updating stats: {e}")
            return False
    
    # ========== Sessions Management ==========
    
    async def save_session(self, user_id: int, session_data: Dict) -> bool:
        """Save user session to database (optional persistence)"""
        try:
            await self.db.sessions.update_one(
                {'user_id': user_id},
                {
                    '$set': {
                        'data': session_data,
                        'updated_at': datetime.now()
                    }
                },
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Error saving session: {e}")
            return False
    
    async def get_session(self, user_id: int) -> Optional[Dict]:
        """Get user session from database"""
        doc = await self.db.sessions.find_one({'user_id': user_id})
        return doc.get('data') if doc else None
    
    async def clear_old_sessions(self, hours: int = 24) -> int:
        """Clear old sessions"""
        cutoff = datetime.now() - timedelta(hours=hours)
        result = await self.db.sessions.delete_many({
            'updated_at': {'$lt': cutoff}
        })
        return result.deleted_count


# Global instance
db = MongoDB()
