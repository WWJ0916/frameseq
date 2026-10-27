"""Render the shared sidebar before serving, so navigation never depends on JS injection."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PAGES = ('studio-v3', 'projects', 'storyboard', 'edits', 'video-api', 'index', 'admin')


def build(root=ROOT, check=False):
    fragment = (root / 'web/workspace-sidebar.html').read_text(encoding='utf-8').strip()
    for name in PAGES:
        path = root / 'web' / (name + '.html')
        source = path.read_text(encoding='utf-8')
        updated, count = re.subn(r'<aside\b[^>]*data-workspace-shell[^>]*>.*?</aside>', lambda _: fragment, source, count=1, flags=re.S)
        if count != 1:
            raise ValueError(f'{name}: missing shared sidebar')
        if check:
            if source != updated:
                raise ValueError(f'{name}: sidebar differs from the shared template')
        else:
            path.write_text(updated, encoding='utf-8')


if __name__ == '__main__':
    import sys
    build(check='--check' in sys.argv)
