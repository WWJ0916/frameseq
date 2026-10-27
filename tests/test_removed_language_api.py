"""Removed language API routes cannot receive a key or invoke any provider."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from video_platform.api import create_app
from video_platform.store import TaskStore


class RemovedLanguageAPI(unittest.TestCase):
    def test_removed_routes_and_assets_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TaskStore(root/'data/tasks.sqlite3')
            with patch('video_platform.api.ROOT', root), TestClient(create_app(store, {'auth_required':False})) as client:
                for path in ('/assistant-api','/api/assistant/providers','/assets/assistant-api.html','/assets/assistant-api.js'):
                    self.assertEqual(client.get(path).status_code,404,path)
                with patch('httpx.AsyncClient.send') as external:
                    response=client.post('/api/assistant/draft',json={'api_key':'fixture-hidden-key','allow_paid_call':True})
                    self.assertEqual(response.status_code,404)
                    self.assertNotIn('fixture-hidden-key',response.text)
                    external.assert_not_called()
                self.assertEqual(client.get('/video-api').status_code,200)
                self.assertEqual(client.get('/api/video-api/providers').status_code,200)
            self.assertEqual(store.list()['total'],0)
