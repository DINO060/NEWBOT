# 📚 Guide de Migration PDF Bot - SQLite/JSON vers MongoDB

## 🎯 Vue d'ensemble

Ce guide accompagne la migration de votre bot PDF vers une architecture modulaire avec MongoDB.

### Changements principaux

| Avant | Après |
|-------|-------|
| Fichier monolithique | Modules `batch.py`, `admin.py`, `core.py` |
| SQLite + JSON | MongoDB |
| Sessions en mémoire | Sessions persistantes (optionnel) |
| Force join simple | Multi-channels |

## 🚀 Installation Rapide

```bash
# Lancer l'installation interactive
python install.py

# Démarrer le bot
python main.py
```

## 📁 Structure

```
LINK-BOT/
├── link_bot/
│   ├── batch.py
│   ├── admin.py
│   ├── core.py
│   └── downloaders/
│       └── scribd.py
├── utils/
│   ├── database.py
│   ├── sessions.py
│   └── helpers.py
├── main.py
├── config.py
├── install.py
└── requirements.txt
```

## 🔄 Vérification

```python
import asyncio
from utils.database import db

async def check():
    await db.connect()
    print('Users:', await db.count_users())
    print('Stats:', await db.get_stats())
    await db.disconnect()

asyncio.run(check())
```

## ⚡ Commandes utiles

- `/start` - Menu
- `/batch`, `/process` - Mode séquence
- `/setbanner`, `/setpassword` - Paramètres
- `/addfsub`, `/delfsub`, `/channels` - Force-join (admin)
- `/broadcast`, `/stats`, `/admins` - Admin

## 🆘 Dépannage

- Vérifiez MongoDB est lancé (ou URL Atlas valide)
- Installez Playwright: `python -m playwright install chromium`
- Windows: ouvrez PowerShell en admin pour installations système
